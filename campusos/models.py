# models.py
"""
Academic platform data model.

Domain overview
----------------
Users (students, lecturers, admins) belong to courses. Lecturers upload
Documents (notes, question banks, research, etc.) scoped by visibility.
Lecturers build QuestionBanks -> Questions, assemble them into Exams that
students Attempt and Answer. Students can also submit research papers
(ResearchSubmission) that get scored against a Rubric, producing one or more
RubricEvaluation records over time.

AI features (highlighting, marking, research guidance) are metered:
- AIServiceConfig (singleton) holds admin-configurable cost/limit settings.
- UserAICredit stores each user's token balance (auto-created per user).
- UserAIUsage logs every token-consuming AI call, with a trace back to what
  was processed.
- PaymentTransaction records purchases of AI tokens and is the source of
  truth that feeds UserAICredit via signal.
- AIPurchasePackage provides predefined token bundles for the frontend.

CHANGELOG (fixes applied):
- UserAICredit.refund_tokens() added: a proper refund that ONLY touches
  remaining_tokens. The AI views previously reused add_tokens() for refunds,
  which also increments total_purchased_tokens — permanently corrupting
  each user's lifetime purchase statistics on every failed AI call.
- Exam.total_marks / ExamQuerySet.with_total_marks() now use explicit F()
  expressions inside Coalesce() instead of bare strings, which is the
  canonical form and avoids FieldError on stricter Django versions.
- Document.file: max_length=500 added — the default 100 chars was too short
  for Cloudinary-style paths and crashed uploads with DataError on Postgres.
- ResearchSubmission: added approved_by / approved_at so lecturer approvals
  are audit-trailed (who approved, when), and the roster view can filter on
  an "approved" state without touching lecturer_comments heuristics.
- ExamAttempt: added last_activity_at, timed_out, abandoned_at so a strict
  exam session can be enforced server-side. Every save-answer / heartbeat
  bumps last_activity_at; the deadline is computed as
  min(start_time + duration, exam.end_time). A student who never returns
  gets timed_out=True when the deadline passes and Celery Beat sweeps
  stale IN_PROGRESS attempts; a student who closes the tab can call
  /abandon/ which stamps abandoned_at. Either way the attempt is finalized
  and the lecturer sees it flagged in analytics.
- Document: added content_pages (JSON list of {page, text}) so the correction
  report can cite exact page numbers ("Page 2, Paragraph 1") instead of
  referring to flat character offsets in content_text.
- CorrectionReport: new model. One row per generated branded correction PDF.
  Attached to a ResearchSubmission and (optionally) the specific
  RubricEvaluation it was generated from, so historical PDFs survive later
  re-evaluations. Status tracks PENDING → GENERATING → READY/FAILED so the
  frontend can poll.
- StudentProfile / LecturerProfile: added `avatar` (ImageField, nullable,
  blank). Stored on Cloudinary via MediaCloudinaryStorage. Existing rows and
  users created without a picture are unaffected (NULL). The UserSerializer
  exposes avatar_url so the frontend can render it everywhere the user is
  shown (nav, submissions roster, exam analytics, etc.).
- Removed duplicate cloudinary_storage import that appeared twice in the
  module. No behaviour change, just cleanup.
  
"""


import logging
from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField, SearchVector
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models, transaction
from django.db.models import Sum, F
from django.db.models.functions import Coalesce
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone
from cloudinary_storage.storage import RawMediaCloudinaryStorage, MediaCloudinaryStorage
logger = logging.getLogger(__name__)


# =============================================================================
# Users & profiles
# =============================================================================

class User(AbstractUser):
    """
    Custom user model, extending Django's AbstractUser with a `role` field.
    """
    class Role(models.TextChoices):
        STUDENT = 'STUDENT', 'Student'
        LECTURER = 'LECTURER', 'Lecturer'
        ADMIN = 'ADMIN', 'Administrator'

    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.STUDENT,
        db_index=True,
        help_text="Which portal/permission set this account uses.",
    )
    email = models.EmailField(
        unique=True,
        help_text="Used for login/notifications; must be unique across all accounts.",
    )

    def __str__(self):
        return f"{self.username} ({self.role})"


@receiver(post_save, sender=User)
def create_ai_credit_for_new_user(sender, instance, created, **kwargs):
    """Auto-create a UserAICredit row for every new user."""
    if created:
        UserAICredit.objects.get_or_create(user=instance)


class StudentProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='student_profile',
        help_text="The underlying auth account. Deleting the user deletes this profile.",
    )
    student_id = models.CharField(max_length=20, unique=True, help_text="Institution-issued student number.")
    department = models.CharField(max_length=100, blank=True, db_index=True)
    enrollment_year = models.PositiveIntegerField(
        null=True, blank=True,
        validators=[MinValueValidator(1900), MaxValueValidator(2100)],
        help_text="Calendar year the student enrolled, e.g. 2024.",
    )
    avatar = models.ImageField(
        upload_to="avatars/students/%Y/%m/",
        storage=MediaCloudinaryStorage(),
        max_length=500,
        null=True, blank=True,
        help_text="Profile picture. Nullable — students without one show initials.",
    )

    def __str__(self):
        return self.user.get_full_name() or self.user.username


class LecturerProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='lecturer_profile',
    )
    staff_id = models.CharField(max_length=20, unique=True, help_text="Institution-issued staff number.")
    department = models.CharField(max_length=100, blank=True, db_index=True)
    designation = models.CharField(max_length=100, blank=True, help_text='e.g. "Senior Lecturer", "Professor".')
    avatar = models.ImageField(
        upload_to="avatars/lecturers/%Y/%m/",
        storage=MediaCloudinaryStorage(),
        max_length=500,
        null=True, blank=True,
        help_text="Profile picture. Nullable — lecturers without one show initials.",
    )

    def __str__(self):
        return self.user.get_full_name() or self.user.username


# =============================================================================
# Courses & enrollment
# =============================================================================

class Course(models.Model):
    code = models.CharField(max_length=20, unique=True, help_text='e.g. "CS301".')
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    lecturer = models.ForeignKey(
        LecturerProfile,
        on_delete=models.SET_NULL,
        null=True,
        related_name='courses',
        help_text="Primary instructor. Nullable so course history survives lecturer deletion.",
    )
    department = models.CharField(max_length=100, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['code']
        indexes = [
            models.Index(fields=['department', 'code']),
        ]

    def __str__(self):
        return f"{self.code} - {self.title}"


class Enrollment(models.Model):
    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name='enrollments')
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='enrollments')
    semester = models.CharField(max_length=20, blank=True, db_index=True, help_text='e.g. "2024-Fall".')
    enrolled_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('student', 'course', 'semester')
        ordering = ['course__code']
        indexes = [
            models.Index(fields=['course', 'semester']),
        ]

    def __str__(self):
        return f"{self.student} enrolled in {self.course}"


# =============================================================================
# Documents
# =============================================================================

class DocumentQuerySet(models.QuerySet):
    def visible_to(self, user):
        if not user or not user.is_authenticated:
            return self.none()
        if user.is_superuser or getattr(user, 'role', None) == User.Role.ADMIN:
            return self
        course_ids = (
            user.student_profile.enrollments.values_list('course_id', flat=True)
            if hasattr(user, 'student_profile') else []
        )
        return self.filter(
            models.Q(visibility=Document.Visibility.PUBLIC)
            | models.Q(uploaded_by=user)
            | models.Q(visibility=Document.Visibility.COURSE, course_id__in=course_ids)
        )

    def with_uploader(self):
        return self.select_related('uploaded_by', 'course')


class Document(models.Model):
    class Category(models.TextChoices):
        NOTE = 'NOTE', 'Note'
        QUESTION_BANK = 'QUESTION_BANK', 'Question Bank'
        SOLUTION_BANK = 'SOLUTION_BANK', 'Solution Bank'
        RESEARCH = 'RESEARCH', 'Research'
        PROPOSAL = 'PROPOSAL', 'Proposal'
        OTHER = 'OTHER', 'Other'

    class Visibility(models.TextChoices):
        PRIVATE = 'PRIVATE', 'Private (only uploader)'
        COURSE = 'COURSE', 'Course (enrolled students)'
        PUBLIC = 'PUBLIC', 'Public (all authenticated users)'

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    file = models.FileField(
        upload_to="documents/%Y/%m/%d/",
        storage=RawMediaCloudinaryStorage(),
        max_length=500,
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='uploaded_documents',
    )
    course = models.ForeignKey(
        Course,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='documents',
    )
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.OTHER)
    visibility = models.CharField(
        max_length=20,
        choices=Visibility.choices,
        default=Visibility.COURSE,
        db_index=True,
    )
    content_text = models.TextField(blank=True)
    content_pages = models.JSONField(
        null=True, blank=True,
        help_text="List of {page: int, text: str} extracted per-PDF-page. "
                  "Used by the correction report so findings can cite exact "
                  "page numbers. Null for non-PDF uploads or pre-existing rows.",
    )
    search_vector = SearchVectorField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = DocumentQuerySet.as_manager()

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['course', 'category']),
            models.Index(fields=['uploaded_by']),
            models.Index(fields=['course', 'visibility', 'category']),
            GinIndex(fields=['search_vector'], name='doc_search_vector_gin'),
        ]

    def __str__(self):
        return self.title

    def build_search_vector(self):
        return (
            SearchVector('title', weight='A')
            + SearchVector('description', weight='B')
            + SearchVector('content_text', weight='C')
        )


@receiver(post_save, sender=Document)
def sync_document_search_vector(sender, instance, created, update_fields, **kwargs):
    if update_fields and set(update_fields) == {'search_vector'}:
        return
    Document.objects.filter(pk=instance.pk).update(search_vector=instance.build_search_vector())


class Highlight(models.Model):
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name='highlights')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='highlights')
    start_offset = models.PositiveIntegerField()
    end_offset = models.PositiveIntegerField()
    text = models.TextField()
    note = models.TextField(blank=True)
    color = models.CharField(max_length=20, default='yellow')
    is_ai_generated = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['start_offset']
        indexes = [
            models.Index(fields=['document', 'start_offset']),
        ]

    def clean(self):
        super().clean()
        if self.end_offset <= self.start_offset:
            raise ValidationError({'end_offset': 'end_offset must be greater than start_offset.'})

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Highlight in {self.document.title}"


# =============================================================================
# Question banks, questions, exams
# =============================================================================

class QuestionBank(models.Model):
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='question_banks')
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='question_banks')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['course', '-created_at']),
        ]

    def __str__(self):
        return self.title


class Question(models.Model):
    class QuestionType(models.TextChoices):
        MCQ = 'MCQ', 'Multiple Choice'
        STRUCTURED = 'STRUCTURED', 'Structured (Essay/Long Answer)'

    bank = models.ForeignKey(QuestionBank, on_delete=models.CASCADE, related_name='questions')
    question_type = models.CharField(max_length=20, choices=QuestionType.choices, db_index=True)
    text = models.TextField()

    options = models.JSONField(
        null=True, blank=True,
        help_text='List of choice strings. Required for MCQ, must be null otherwise.',
    )
    correct_option = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="0-based index into `options` identifying the correct choice. Required for MCQ.",
    )

    marks = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
    explanation = models.TextField(blank=True)
    model_answer = models.TextField(blank=True)
    rubric = models.JSONField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['id']
        indexes = [
            models.Index(fields=['bank', 'question_type']),
        ]

    def clean(self):
        super().clean()
        if self.question_type == self.QuestionType.MCQ:
            if self.options is None or self.correct_option is None:
                raise ValidationError('MCQ questions require both options and correct_option.')
            if not isinstance(self.options, list) or len(self.options) < 2:
                raise ValidationError({'options': 'Options must be a list with at least two choices.'})
            if self.correct_option < 0 or self.correct_option >= len(self.options):
                raise ValidationError({'correct_option': 'correct_option index is out of range.'})
        elif self.question_type == self.QuestionType.STRUCTURED:
            if self.options is not None or self.correct_option is not None:
                raise ValidationError('Structured questions must not have options or correct_option.')

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.get_question_type_display()}: {self.text[:50]}"


class ExamQuerySet(models.QuerySet):
    def with_total_marks(self):
        return self.annotate(
            total_marks_annotated=Sum(
                Coalesce(F('exam_questions__marks'), F('exam_questions__question__marks'))
            )
        )

    def upcoming(self):
        return self.filter(is_published=True, start_time__gte=timezone.now())


class Exam(models.Model):
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='exams')
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    instructions = models.TextField(blank=True)
    duration_minutes = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    start_time = models.DateTimeField(null=True, blank=True, db_index=True)
    end_time = models.DateTimeField(null=True, blank=True)
    questions = models.ManyToManyField(
        Question,
        through='ExamQuestion',
        related_name='exams',
    )
    is_published = models.BooleanField(default=False, db_index=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='created_exams')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ExamQuerySet.as_manager()

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['course', 'is_published']),
            models.Index(fields=['is_published', 'start_time']),
        ]

    def clean(self):
        super().clean()
        if self.start_time and self.end_time and self.end_time <= self.start_time:
            raise ValidationError({'end_time': 'end_time must be after start_time.'})

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)

    def __str__(self):
        return self.title

    @property
    def total_marks(self):
        result = self.exam_questions.aggregate(
            total=Sum(Coalesce(F('marks'), F('question__marks')))
        )
        return result['total'] or 0


class ExamQuestion(models.Model):
    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name='exam_questions')
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name='exam_links')
    order = models.PositiveIntegerField()
    marks = models.PositiveIntegerField(
        null=True, blank=True,
        validators=[MinValueValidator(1)],
        help_text="Override Question.marks for this exam specifically.",
    )

    class Meta:
        ordering = ['order']
        unique_together = ('exam', 'question')
        indexes = [
            models.Index(fields=['exam', 'order']),
        ]

    def __str__(self):
        return f"{self.exam.title} - Q{self.order}"


# =============================================================================
# Attempts & answers
# =============================================================================

class ExamAttempt(models.Model):
    class Status(models.TextChoices):
        IN_PROGRESS = 'IN_PROGRESS', 'In Progress'
        SUBMITTED = 'SUBMITTED', 'Submitted'
        GRADED = 'GRADED', 'Graded'

    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name='attempts')
    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name='exam_attempts')
    attempt_number = models.PositiveSmallIntegerField(default=1)
    start_time = models.DateTimeField(default=timezone.now)
    end_time = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.IN_PROGRESS, db_index=True)
    score = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(0)],
    )
    feedback = models.TextField(blank=True)

    last_activity_at = models.DateTimeField(
        default=timezone.now,
        db_index=True,
        help_text="Updated on every save-answer / heartbeat. "
                  "Stale value ⇒ the student left without submitting.",
    )
    timed_out = models.BooleanField(
        default=False,
        db_index=True,
        help_text="True when the attempt was auto-finalized because the "
                  "server-side deadline passed (min(start_time + duration, "
                  "exam.end_time)) without a submit.",
    )
    abandoned_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Set when the student explicitly exited without submitting.",
    )

    class Meta:
        ordering = ['-start_time']
        unique_together = ('exam', 'student', 'attempt_number')
        indexes = [
            models.Index(fields=['student', 'status']),
            models.Index(fields=['exam', 'status']),
            models.Index(fields=['status', 'last_activity_at']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['exam', 'student'],
                condition=models.Q(status='IN_PROGRESS'),
                name='one_in_progress_attempt_per_student',
            ),
        ]

    def __str__(self):
        return f"{self.student} - {self.exam.title} (#{self.attempt_number}, {self.status})"

    @property
    def deadline(self):
        """
        The earliest moment the attempt is considered over:
        min(start_time + duration_minutes, exam.end_time).
        """
        from datetime import timedelta
        cutoff = self.start_time + timedelta(minutes=self.exam.duration_minutes)
        if self.exam.end_time and self.exam.end_time < cutoff:
            return self.exam.end_time
        return cutoff

    @property
    def seconds_remaining(self):
        if self.status != self.Status.IN_PROGRESS:
            return 0
        delta = (self.deadline - timezone.now()).total_seconds()
        return max(0, int(delta))

    @property
    def is_flagged(self):
        """True when this attempt was not cleanly submitted by the student."""
        return self.timed_out or self.abandoned_at is not None


class Answer(models.Model):
    attempt = models.ForeignKey(ExamAttempt, on_delete=models.CASCADE, related_name='answers')
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name='answers')
    selected_option = models.PositiveIntegerField(null=True, blank=True)
    answer_text = models.TextField(blank=True)
    score = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(0)],
    )
    feedback = models.TextField(blank=True)
    ai_evaluation = models.JSONField(null=True, blank=True)
    is_correct = models.BooleanField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['question_id']
        unique_together = ('attempt', 'question')
        indexes = [
            models.Index(fields=['question', 'is_correct']),
        ]

    def __str__(self):
        return f"Answer for Q{self.question_id} by {self.attempt.student}"


# =============================================================================
# Rubrics & research submissions
# =============================================================================

class Rubric(models.Model):
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='rubrics')
    course = models.ForeignKey(
        Course, on_delete=models.SET_NULL, null=True, blank=True, related_name='rubrics',
    )
    criteria = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.title


class ResearchSubmission(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending Evaluation'
        EVALUATED = 'EVALUATED', 'Evaluated'
        REVIEWED = 'REVIEWED', 'Reviewed by Lecturer'

    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name='research_submissions')
    course = models.ForeignKey(Course, on_delete=models.SET_NULL, null=True, blank=True, related_name='research_submissions')
    document = models.OneToOneField(
        Document, on_delete=models.CASCADE, related_name='research_submission',
    )
    rubric = models.ForeignKey(Rubric, on_delete=models.SET_NULL, null=True, blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True)
    evaluation_status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True)
    ai_feedback = models.JSONField(null=True, blank=True)
    overall_score = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    lecturer_comments = models.TextField(blank=True)

    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='approved_submissions',
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-submitted_at']
        indexes = [
            models.Index(fields=['student', 'evaluation_status']),
            models.Index(fields=['course', 'evaluation_status']),
        ]

    def __str__(self):
        return f"{self.student} - {self.document.title}"


class RubricEvaluation(models.Model):
    submission = models.ForeignKey(ResearchSubmission, on_delete=models.CASCADE, related_name='evaluations')
    rubric = models.ForeignKey(Rubric, on_delete=models.PROTECT)
    scores = models.JSONField()
    feedback = models.JSONField()
    overall_score = models.DecimalField(
        max_digits=5, decimal_places=2,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    evaluated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-evaluated_at']
        indexes = [
            models.Index(fields=['submission', '-evaluated_at']),
        ]

    def __str__(self):
        return f"Evaluation for {self.submission}"


@receiver(post_save, sender=RubricEvaluation)
def sync_latest_evaluation_to_submission(sender, instance, created, **kwargs):
    if not created:
        return
    ResearchSubmission.objects.filter(pk=instance.submission_id).update(
        ai_feedback=instance.feedback,
        overall_score=instance.overall_score,
        evaluation_status=ResearchSubmission.Status.EVALUATED,
    )


class CorrectionReport(models.Model):
    """
    A branded PDF correction report generated from a research submission's
    latest RubricEvaluation. One row per generated file so historical PDFs
    survive later re-evaluations.
    """
    class Status(models.TextChoices):
        PENDING    = "PENDING",    "Pending"
        GENERATING = "GENERATING", "Generating"
        READY      = "READY",      "Ready"
        FAILED     = "FAILED",     "Failed"

    submission = models.ForeignKey(
        ResearchSubmission,
        on_delete=models.CASCADE,
        related_name="correction_reports",
    )
    evaluation = models.ForeignKey(
        RubricEvaluation,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="correction_reports",
        help_text="The evaluation this report was generated from. Nullable "
                  "so the report survives if the evaluation is later deleted.",
    )
    file = models.FileField(
        upload_to="corrections/%Y/%m/%d/",
        storage=RawMediaCloudinaryStorage(),
        max_length=500,
        null=True, blank=True,
    )
    status = models.CharField(
        max_length=20, choices=Status.choices,
        default=Status.PENDING, db_index=True,
    )
    generated_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-generated_at", "-id"]
        indexes = [
            models.Index(fields=["submission", "-generated_at"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return f"CorrectionReport#{self.id} for submission {self.submission_id} ({self.status})"


# =============================================================================
# AI usage & billing
# =============================================================================

class AIServiceConfig(models.Model):
    ai_highlighting_enabled = models.BooleanField(default=True)
    ai_marking_enabled = models.BooleanField(default=True)
    ai_research_guidance_enabled = models.BooleanField(default=True)

    token_cost_cents = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
    )
    free_tokens_per_month = models.PositiveIntegerField(default=1000)
    allow_pay_as_you_go = models.BooleanField(default=True)
    max_purchasable_tokens = models.PositiveIntegerField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "AI Service Configuration"
        verbose_name_plural = "AI Service Configuration"

    def __str__(self):
        return "AI Service Configuration"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class UserAICredit(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='ai_credit',
    )
    remaining_tokens = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0)],
    )
    total_purchased_tokens = models.PositiveIntegerField(default=0)
    last_reset_at = models.DateTimeField(default=timezone.now)

    class Meta:
        pass

    def clean(self):
        super().clean()
        if self.remaining_tokens < 0:
            raise ValidationError({'remaining_tokens': 'Cannot be negative.'})

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Credit for {self.user}"

    def try_consume(self, amount):
        if amount <= 0:
            raise ValueError("amount must be positive")
        updated = UserAICredit.objects.filter(
            pk=self.pk, remaining_tokens__gte=amount
        ).update(remaining_tokens=F('remaining_tokens') - amount)
        if updated:
            self.refresh_from_db(fields=['remaining_tokens'])
        return bool(updated)

    def add_tokens(self, amount):
        if amount <= 0:
            raise ValueError("amount must be positive")
        UserAICredit.objects.filter(pk=self.pk).update(
            remaining_tokens=F('remaining_tokens') + amount,
            total_purchased_tokens=F('total_purchased_tokens') + amount,
        )
        self.refresh_from_db(fields=['remaining_tokens', 'total_purchased_tokens'])

    def refund_tokens(self, amount):
        if amount <= 0:
            raise ValueError("amount must be positive")
        UserAICredit.objects.filter(pk=self.pk).update(
            remaining_tokens=F('remaining_tokens') + amount,
        )
        self.refresh_from_db(fields=['remaining_tokens'])

    @classmethod
    def reset_monthly_allowances(cls):
        """Celery-beat task entry point: top up under-quota users to the free allowance."""
        config = AIServiceConfig.load()
        allowance = config.free_tokens_per_month
        if not allowance:
            return 0
        updated = 0
        for credit in cls.objects.filter(remaining_tokens__lt=allowance):
            delta = allowance - credit.remaining_tokens
            credit.refund_tokens(delta)
            credit.last_reset_at = timezone.now()
            credit.save(update_fields=['last_reset_at'])
            updated += 1
        return updated


class UserAIUsage(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='ai_usages',
    )
    feature = models.CharField(max_length=50, db_index=True)
    tokens_used = models.PositiveIntegerField()
    cost_cents = models.PositiveIntegerField()
    related_document = models.ForeignKey(
        Document, on_delete=models.SET_NULL, null=True, blank=True, related_name='ai_usages',
    )
    related_answer = models.ForeignKey(
        Answer, on_delete=models.SET_NULL, null=True, blank=True, related_name='ai_usages',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', '-created_at']),
            models.Index(fields=['feature', '-created_at']),
        ]

    def __str__(self):
        return f"{self.user} used {self.tokens_used} tokens on {self.feature}"


class PaymentTransaction(models.Model):
    """
    A record of a student's purchase of AI tokens.

    Lifecycle:
      PENDING  → created by PurchaseTokensView when the order is submitted
                 to Pesapal. `transaction_id` == `pesapal_merchant_reference`.
      SUCCESS  → set by mark_successful() when the IPN confirms payment.
                 Crediting tokens to UserAICredit happens in the same
                 atomic block, exactly once.
      FAILED   → set by mark_failed() when Pesapal reports failure or the
                 order submission itself errored.
      REFUNDED → reserved for admin-initiated refunds (manual for now).

    The `transaction_id` unique constraint is the ultimate guard against
    double-crediting even under a race condition. `mark_successful()` also
    uses `.exclude(status=SUCCESS)` inside a transaction, so re-firing an
    IPN is completely safe.
    """
    class Status(models.TextChoices):
        PENDING  = 'PENDING',  'Pending'
        SUCCESS  = 'SUCCESS',  'Success'
        FAILED   = 'FAILED',   'Failed'
        REFUNDED = 'REFUNDED', 'Refunded'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='payments',
    )
    amount_cents = models.PositiveIntegerField(
        help_text="Amount charged, in minor units (cents). e.g. 5000 = UGX 50.00.",
    )
    tokens_purchased = models.PositiveIntegerField()
    status = models.CharField(
        max_length=20, choices=Status.choices,
        default=Status.PENDING, db_index=True,
    )
    transaction_id = models.CharField(
        max_length=255, blank=True, null=True, unique=True,
        help_text="Public-facing reference. For Pesapal orders this is the "
                  "same value sent as `merchant_reference`.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    # ─── Pesapal integration fields ────────────────────────────────
    pesapal_tracking_id = models.CharField(
        max_length=255, blank=True, null=True, db_index=True,
        help_text="Pesapal's order_tracking_id. Required to query payment "
                  "status via GetTransactionStatus.",
    )
    pesapal_merchant_reference = models.CharField(
        max_length=255, blank=True, null=True, db_index=True,
        help_text="Reference we sent to Pesapal as the order `id`. Usually "
                  "identical to transaction_id. Kept separate so the field "
                  "survives if transaction_id is ever reformatted.",
    )
    redirect_url = models.URLField(
        max_length=500, blank=True, null=True,
        help_text="Pesapal-hosted payment page the student was redirected to.",
    )
    raw_pesapal_response = models.JSONField(
        null=True, blank=True,
        help_text="Last full response from Pesapal (SubmitOrder or status "
                  "query). Useful for debugging and audit.",
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', '-created_at']),
            models.Index(fields=['status']),
            models.Index(fields=['pesapal_tracking_id']),
        ]

    def __str__(self):
        return f"Payment {self.id} by {self.user} ({self.status})"

    # ─── State transitions ─────────────────────────────────────────

    def mark_successful(self):
        """
        Atomically transition to SUCCESS and credit tokens exactly once.

        Safe against double-firing: the .exclude(status=SUCCESS) filter
        means a second call is a no-op. Token credit happens inside the
        same transaction, so either both writes land or neither does.
        """
        with transaction.atomic():
            updated = (
                PaymentTransaction.objects
                .filter(pk=self.pk)
                .exclude(status=PaymentTransaction.Status.SUCCESS)
                .update(
                    status=PaymentTransaction.Status.SUCCESS,
                    completed_at=timezone.now(),
                )
            )
            if not updated:
                return False
            credit, _ = UserAICredit.objects.get_or_create(user_id=self.user_id)
            credit.add_tokens(self.tokens_purchased)

        self.refresh_from_db(fields=['status', 'completed_at'])
        return True

    def mark_failed(self, reason: str = ""):
        """
        Transition to FAILED. Same idempotency guard as mark_successful():
        a SUCCESS row is never downgraded by a late/duplicate IPN.
        """
        with transaction.atomic():
            updated = (
                PaymentTransaction.objects
                .filter(pk=self.pk)
                .exclude(status__in=[
                    PaymentTransaction.Status.SUCCESS,
                    PaymentTransaction.Status.REFUNDED,
                ])
                .update(status=PaymentTransaction.Status.FAILED)
            )
        if updated:
            self.refresh_from_db(fields=['status'])
            if reason:
                logger.info(
                    "PaymentTransaction %s marked FAILED: %s", self.pk, reason,
                )
        return bool(updated)

    @property
    def is_pesapal_order(self) -> bool:
        """True if this payment was submitted to Pesapal (vs simulated)."""
        return bool(self.pesapal_tracking_id or self.pesapal_merchant_reference)

class AIPurchasePackage(models.Model):
    name = models.CharField(max_length=100)
    tokens = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    price_cents = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['price_cents']

    def __str__(self):
        return f"{self.name} ({self.tokens} tokens)"