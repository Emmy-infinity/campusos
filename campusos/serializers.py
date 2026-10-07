# serializers.py
"""
Django REST Framework serializers for the academic platform.

Conventions:
- read_only_fields = system-managed fields.
- Foreign keys are exposed twice: a nested read-only representation for
  display, and a write-only `<name>_id` PrimaryKeyRelatedField for writes.
- Fields attributed to "the current user" are NEVER writable from client
  input; they are always set from request.user.

CHANGELOG (fixes applied):
- CourseSerializer.lecturer_id: added required=False. Previously the field
  was required=True even though allow_null=True, which caused
  "lecturer_id: This field is required" on POST /api/courses/ from a
  lecturer — the validation ran before perform_create() could auto-assign
  the lecturer. Now the field is optional; if omitted, the view assigns it.
- RubricSerializer.course_id: now REQUIRED with a validator that ensures
  lecturers can only attach rubrics to courses they own. Prevents
  course-less rubrics, which bypassed the enrolment check and created
  orphan submissions invisible to every lecturer.
- ResearchSubmissionCreateSerializer.rubric_id: now REQUIRED, and the
  rubric must be tied to a course. The submission's `course` field is
  derived from the rubric (not the document) so it is always non-null.
  This closes the "orphan submission" class of bugs permanently.
- DocumentSerializer: now exposes `content_pages` (read-only). Required by
  the correction-report frontend which needs page-anchored citations.
- ExamSerializer.get_total_marks: handles the case where the annotation
  returns None (exam with zero questions) by falling back to the property.
- ExamAttemptForLecturerSerializer: now uses ExamForLecturerPreviewSerializer
  instead of ExamSerializer. Previously the nested `exam_questions[*].question`
  was sent as a raw PK, which broke the /exams/attempt/{id}/ preview page
  for lecturers (`undefined mark(s)` and no question text). The new
  serializer nests the full question object (with answer key) so the page
  renders identically to the student view.
- (All prior changelog entries retained.)
"""
from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework_simplejwt.tokens import RefreshToken

from .models import (
    User, StudentProfile, LecturerProfile, Course, Enrollment,
    Document, Highlight, QuestionBank, Question, Exam, ExamQuestion,
    ExamAttempt, Answer, Rubric, ResearchSubmission, RubricEvaluation,
    AIServiceConfig, UserAICredit, UserAIUsage, PaymentTransaction,
    CorrectionReport, AIPurchasePackage,
)

User = get_user_model()


# =============================================================================
# User & profile serializers
# =============================================================================

class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'role', 'first_name', 'last_name']
        read_only_fields = ['id', 'role']


class StudentProfileSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)
    user_id = serializers.PrimaryKeyRelatedField(
        source='user', queryset=User.objects.all(), write_only=True
    )

    class Meta:
        model = StudentProfile
        fields = ['id', 'user', 'user_id', 'student_id', 'department', 'enrollment_year']
        read_only_fields = ['id']


class LecturerProfileSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)
    user_id = serializers.PrimaryKeyRelatedField(
        source='user', queryset=User.objects.all(), write_only=True
    )

    class Meta:
        model = LecturerProfile
        fields = ['id', 'user', 'user_id', 'staff_id', 'department', 'designation']
        read_only_fields = ['id']


# =============================================================================
# Course & enrollment serializers
# =============================================================================

class CourseSerializer(serializers.ModelSerializer):
    lecturer = LecturerProfileSerializer(read_only=True)
    lecturer_id = serializers.PrimaryKeyRelatedField(
        source='lecturer',
        queryset=LecturerProfile.objects.all(),
        write_only=True,
        allow_null=True,
        required=False,
    )

    class Meta:
        model = Course
        fields = ['id', 'code', 'title', 'description', 'lecturer', 'lecturer_id',
                  'department', 'created_at']
        read_only_fields = ['id', 'created_at']


class EnrollmentSerializer(serializers.ModelSerializer):
    student = StudentProfileSerializer(read_only=True)
    course = CourseSerializer(read_only=True)
    student_id = serializers.PrimaryKeyRelatedField(
        source='student', queryset=StudentProfile.objects.all(), write_only=True
    )
    course_id = serializers.PrimaryKeyRelatedField(
        source='course', queryset=Course.objects.all(), write_only=True
    )

    class Meta:
        model = Enrollment
        fields = ['id', 'student', 'course', 'student_id', 'course_id',
                  'semester', 'enrolled_at']
        read_only_fields = ['id', 'enrolled_at']


# =============================================================================
# Registration serializer
# =============================================================================

class RegisterSerializer(serializers.Serializer):
    """
    Public registration serializer.

    Creates a User plus the matching profile (StudentProfile or LecturerProfile)
    atomically. Role must be STUDENT or LECTURER — ADMIN accounts are created
    only via createsuperuser or the create_superuser.py script.

    Returns JWT tokens so the caller is immediately logged in.
    """
    username = serializers.CharField(max_length=150)
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=8)
    password_confirm = serializers.CharField(write_only=True, min_length=8)
    first_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    last_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    role = serializers.ChoiceField(choices=['STUDENT', 'LECTURER'])

    student_id = serializers.CharField(max_length=20, required=False, allow_blank=True)
    department = serializers.CharField(max_length=100, required=False, allow_blank=True)
    enrollment_year = serializers.IntegerField(required=False, allow_null=True)

    staff_id = serializers.CharField(max_length=20, required=False, allow_blank=True)
    designation = serializers.CharField(max_length=100, required=False, allow_blank=True)

    def validate_username(self, value):
        if User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError("This username is already taken.")
        return value

    def validate_email(self, value):
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("This email is already registered.")
        return value

    def validate(self, attrs):
        if attrs['password'] != attrs['password_confirm']:
            raise serializers.ValidationError({"password_confirm": "Passwords do not match."})

        role = attrs.get('role')

        if role == 'STUDENT':
            if not attrs.get('student_id'):
                raise serializers.ValidationError({"student_id": "student_id is required for students."})
            if StudentProfile.objects.filter(student_id=attrs['student_id']).exists():
                raise serializers.ValidationError({"student_id": "This student ID is already registered."})

        if role == 'LECTURER':
            if not attrs.get('staff_id'):
                raise serializers.ValidationError({"staff_id": "staff_id is required for lecturers."})
            if LecturerProfile.objects.filter(staff_id=attrs['staff_id']).exists():
                raise serializers.ValidationError({"staff_id": "This staff ID is already registered."})

        return attrs

    @transaction.atomic
    def create(self, validated_data):
        student_id = validated_data.pop('student_id', None)
        staff_id = validated_data.pop('staff_id', None)
        enrollment_year = validated_data.pop('enrollment_year', None)
        department = validated_data.pop('department', '')
        designation = validated_data.pop('designation', '')

        role = validated_data.pop('role')
        password = validated_data.pop('password')
        validated_data.pop('password_confirm')

        user = User(**validated_data)
        user.role = role
        user.set_password(password)
        user.save()

        if role == 'STUDENT':
            StudentProfile.objects.create(
                user=user,
                student_id=student_id,
                department=department or '',
                enrollment_year=enrollment_year,
            )
        elif role == 'LECTURER':
            LecturerProfile.objects.create(
                user=user,
                staff_id=staff_id,
                department=department or '',
                designation=designation or '',
            )

        return user

    def to_representation(self, instance):
        refresh = RefreshToken.for_user(instance)
        return {
            "user": UserSerializer(instance).data,
            "refresh": str(refresh),
            "access": str(refresh.access_token),
        }


# =============================================================================
# Document & Highlight serializers
# =============================================================================

class DocumentSerializer(serializers.ModelSerializer):
    uploaded_by = UserSerializer(read_only=True)
    course = CourseSerializer(read_only=True)
    course_id = serializers.PrimaryKeyRelatedField(
        source='course',
        queryset=Course.objects.all(),
        write_only=True,
        allow_null=True,
        required=False,
    )

    class Meta:
        model = Document
        fields = [
            'id', 'title', 'description', 'file', 'uploaded_by',
            'course', 'course_id', 'category', 'visibility',
            'content_text', 'content_pages',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'uploaded_by', 'content_text', 'content_pages',
            'created_at', 'updated_at',
        ]


class HighlightSerializer(serializers.ModelSerializer):
    document = serializers.PrimaryKeyRelatedField(queryset=Document.objects.all())
    user = UserSerializer(read_only=True)

    class Meta:
        model = Highlight
        fields = ['id', 'document', 'user', 'start_offset', 'end_offset', 'text',
                  'note', 'color', 'is_ai_generated', 'created_at']
        read_only_fields = ['id', 'user', 'is_ai_generated', 'created_at']

    def validate_document(self, document):
        request = self.context.get('request')
        if request is not None:
            if not Document.objects.visible_to(request.user).filter(pk=document.pk).exists():
                raise serializers.ValidationError("You do not have access to this document.")
        return document

    def create(self, validated_data):
        request = self.context.get('request')
        if request is None or not request.user or not request.user.is_authenticated:
            raise serializers.ValidationError("Authentication is required to create a highlight.")
        validated_data['user'] = request.user
        return Highlight.objects.create(**validated_data)


# =============================================================================
# Question, Question bank, Exam serializers
# =============================================================================

class QuestionSerializer(serializers.ModelSerializer):
    bank = serializers.PrimaryKeyRelatedField(queryset=QuestionBank.objects.all())

    class Meta:
        model = Question
        fields = [
            'id', 'bank', 'question_type', 'text', 'options', 'correct_option',
            'marks', 'explanation', 'model_answer', 'rubric',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def validate(self, attrs):
        question_type = attrs.get('question_type', getattr(self.instance, 'question_type', None))
        options = attrs.get('options', getattr(self.instance, 'options', None))
        correct_option = attrs.get('correct_option', getattr(self.instance, 'correct_option', None))

        if question_type == Question.QuestionType.MCQ:
            if not options or correct_option is None:
                raise serializers.ValidationError("MCQ questions require options and correct_option.")
            if not isinstance(options, list) or len(options) < 2:
                raise serializers.ValidationError("MCQ options must be a list with at least two choices.")
            if correct_option < 0 or correct_option >= len(options):
                raise serializers.ValidationError("correct_option index out of range.")
        elif question_type == Question.QuestionType.STRUCTURED:
            if options is not None or correct_option is not None:
                raise serializers.ValidationError("Structured questions must not have options or correct_option.")
        return attrs


class QuestionBankSerializer(serializers.ModelSerializer):
    course = CourseSerializer(read_only=True)
    course_id = serializers.PrimaryKeyRelatedField(
        source='course', queryset=Course.objects.all(), write_only=True
    )
    created_by = UserSerializer(read_only=True)
    questions = serializers.SerializerMethodField()

    class Meta:
        model = QuestionBank
        fields = ['id', 'course', 'course_id', 'title', 'description',
                  'created_by', 'questions', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_by', 'created_at', 'updated_at']

    def get_questions(self, obj):
        qs = obj.questions.order_by('id')
        return QuestionSerializer(qs, many=True).data


class ExamQuestionSerializer(serializers.ModelSerializer):
    """Lecturer-facing exam-question link. `question` is a raw PK for lookup."""
    class Meta:
        model = ExamQuestion
        fields = ['id', 'exam', 'question', 'order', 'marks']
        read_only_fields = ['id']


class ExamSerializer(serializers.ModelSerializer):
    total_marks = serializers.SerializerMethodField()
    created_by = UserSerializer(read_only=True)
    course = CourseSerializer(read_only=True)
    course_id = serializers.PrimaryKeyRelatedField(
        source='course', queryset=Course.objects.all(), write_only=True
    )
    exam_questions = ExamQuestionSerializer(many=True, read_only=True)

    class Meta:
        model = Exam
        fields = [
            'id', 'course', 'course_id', 'title', 'description', 'instructions',
            'duration_minutes', 'start_time', 'end_time', 'is_published',
            'created_by', 'created_at', 'updated_at', 'total_marks',
            'exam_questions',
        ]
        read_only_fields = ['id', 'created_by', 'created_at', 'updated_at']

    def get_total_marks(self, obj):
        # Prefer the annotated value when it's a real number; fall back to the
        # property for exams with zero questions (annotation yields None).
        annotated = getattr(obj, 'total_marks_annotated', None)
        if annotated is not None:
            return annotated
        return obj.total_marks


# =============================================================================
# Attempt & Answer serializers
# =============================================================================

class ExamAttemptSerializer(serializers.ModelSerializer):
    exam = ExamSerializer(read_only=True)
    exam_id = serializers.PrimaryKeyRelatedField(
        source='exam', queryset=Exam.objects.all(), write_only=True
    )
    student = StudentProfileSerializer(read_only=True)

    class Meta:
        model = ExamAttempt
        fields = [
            'id', 'exam', 'exam_id', 'student', 'attempt_number', 'start_time',
            'end_time', 'submitted_at', 'status', 'score', 'feedback',
        ]
        read_only_fields = ['id', 'student', 'start_time', 'end_time',
                            'submitted_at', 'score', 'feedback']


class AnswerSerializer(serializers.ModelSerializer):
    """Lecturer/admin view. Includes the full question with answer key."""
    question = QuestionSerializer(read_only=True)
    question_id = serializers.PrimaryKeyRelatedField(
        source='question', queryset=Question.objects.all(), write_only=True
    )

    class Meta:
        model = Answer
        fields = [
            'id', 'attempt', 'question', 'question_id', 'selected_option',
            'answer_text', 'score', 'feedback', 'ai_evaluation', 'is_correct',
            'created_at',
        ]
        read_only_fields = ['id', 'created_at', 'score', 'feedback',
                            'ai_evaluation', 'is_correct']


# =============================================================================
# Rubric & Research serializers
# =============================================================================

class RubricSerializer(serializers.ModelSerializer):
    """
    A rubric is always tied to a course. This avoids the class of bugs where
    a course-less rubric bypasses the enrolment check and produces an orphan
    submission that no lecturer can find.

    Lecturers may only attach rubrics to courses they teach; admins may
    attach to any course.
    """
    created_by = UserSerializer(read_only=True)
    course = CourseSerializer(read_only=True)
    course_id = serializers.PrimaryKeyRelatedField(
        source='course',
        queryset=Course.objects.all(),
        write_only=True,
        required=True,        # ← was allow_null=True, now required
        allow_null=False,
    )

    class Meta:
        model = Rubric
        fields = ['id', 'title', 'description', 'created_by', 'course',
                  'course_id', 'criteria', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_by', 'created_at', 'updated_at']

    def validate_course_id(self, course):
        """
        Admins may attach rubrics to any course.
        Lecturers may only attach rubrics to courses they own.
        """
        request = self.context.get('request')
        if request is None:
            return course
        user = request.user

        if user.is_superuser or getattr(user, 'role', None) == User.Role.ADMIN:
            return course

        profile = getattr(user, 'lecturer_profile', None)
        if profile is None or course.lecturer_id != profile.id:
            raise serializers.ValidationError(
                "You can only create rubrics for courses you teach."
            )
        return course


class ResearchSubmissionSerializer(serializers.ModelSerializer):
    student = StudentProfileSerializer(read_only=True)
    course = CourseSerializer(read_only=True)
    document = DocumentSerializer(read_only=True)
    rubric = RubricSerializer(read_only=True)
    approved_by = UserSerializer(read_only=True)

    class Meta:
        model = ResearchSubmission
        fields = [
            'id', 'student', 'course', 'document', 'rubric',
            'submitted_at', 'evaluation_status', 'ai_feedback',
            'overall_score', 'lecturer_comments',
            'approved_by', 'approved_at',
        ]
        read_only_fields = fields


class RubricEvaluationSerializer(serializers.ModelSerializer):
    submission = ResearchSubmissionSerializer(read_only=True)
    rubric = RubricSerializer(read_only=True)
    submission_id = serializers.PrimaryKeyRelatedField(
        source='submission', queryset=ResearchSubmission.objects.all(), write_only=True
    )
    rubric_id = serializers.PrimaryKeyRelatedField(
        source='rubric', queryset=Rubric.objects.all(), write_only=True
    )

    class Meta:
        model = RubricEvaluation
        fields = ['id', 'submission', 'submission_id', 'rubric', 'rubric_id',
                  'scores', 'feedback', 'overall_score', 'evaluated_at']
        read_only_fields = ['id', 'evaluated_at']


# =============================================================================
# Research submission — create / roster / approve
# =============================================================================

class ResearchSubmissionCreateSerializer(serializers.ModelSerializer):
    """
    A research submission is always attached to a rubric, and that rubric is
    always tied to a course. The submission's `course` field is derived from
    the rubric (never from the document) so it is guaranteed to be non-null.

    This permanently eliminates the "orphan submission" class of bugs:
    every submission is findable by the rubric's author, and every
    submission rolls up into course analytics.
    """
    document_id = serializers.PrimaryKeyRelatedField(
        source='document', queryset=Document.objects.all()
    )
    rubric_id = serializers.PrimaryKeyRelatedField(
        source='rubric',
        queryset=Rubric.objects.all(),
        required=True,          # ← was required=False
        allow_null=False,
    )

    class Meta:
        model = ResearchSubmission
        fields = ['id', 'document_id', 'rubric_id', 'submitted_at',
                  'evaluation_status']
        read_only_fields = ['id', 'submitted_at', 'evaluation_status']

    def validate_document_id(self, document):
        request = self.context['request']
        user = request.user

        if document.category not in (
            Document.Category.RESEARCH,
            Document.Category.PROPOSAL,
        ):
            raise serializers.ValidationError(
                "Only RESEARCH or PROPOSAL documents can be submitted."
            )
        if document.uploaded_by_id != user.id and not user.is_superuser:
            raise serializers.ValidationError(
                "You can only submit your own documents."
            )
        if ResearchSubmission.objects.filter(document=document).exists():
            raise serializers.ValidationError(
                "This document has already been submitted."
            )
        return document

    def validate_rubric_id(self, rubric):
        if rubric is None:
            raise serializers.ValidationError(
                "A rubric is required. Choose one from the list."
            )
        if rubric.course_id is None:
            raise serializers.ValidationError(
                "This rubric isn't tied to a course. Ask your lecturer to "
                "attach it to one before you can submit against it."
            )

        request = self.context['request']
        user = request.user
        profile = getattr(user, 'student_profile', None)
        if profile is None:
            raise serializers.ValidationError(
                "Only students can submit research."
            )
        if not Enrollment.objects.filter(
            student=profile, course_id=rubric.course_id
        ).exists():
            raise serializers.ValidationError(
                "You are not enrolled in the course this rubric belongs to."
            )
        return rubric

    def create(self, validated_data):
        user = self.context['request'].user
        profile = getattr(user, 'student_profile', None)
        if profile is None:
            raise serializers.ValidationError(
                "Only students with a student profile can submit research."
            )

        rubric = validated_data['rubric']      # already validated: non-null, has course
        return ResearchSubmission.objects.create(
            student=profile,
            course=rubric.course,              # derived from rubric, guaranteed non-null
            **validated_data,
        )


class SubmissionRosterSerializer(serializers.ModelSerializer):
    student_name = serializers.SerializerMethodField()
    student_id = serializers.CharField(source='student.student_id', read_only=True)
    document_title = serializers.CharField(source='document.title', read_only=True)
    course_code = serializers.CharField(
        source='course.code', read_only=True, allow_null=True
    )
    status_color = serializers.SerializerMethodField()
    approved_by_name = serializers.SerializerMethodField()

    class Meta:
        model = ResearchSubmission
        fields = [
            'id', 'student_name', 'student_id', 'document_title',
            'course_code', 'evaluation_status', 'status_color',
            'overall_score', 'submitted_at',
            'approved_by_name', 'approved_at',
        ]

    def get_student_name(self, obj):
        u = obj.student.user
        return (u.get_full_name() or u.username).strip()

    def get_status_color(self, obj):
        s = obj.evaluation_status
        if s == ResearchSubmission.Status.REVIEWED:
            return 'green'
        if s == ResearchSubmission.Status.EVALUATED:
            return 'yellow'
        return 'red'

    def get_approved_by_name(self, obj):
        if not obj.approved_by_id:
            return None
        u = obj.approved_by
        return (u.get_full_name() or u.username).strip()


class ResearchSubmissionApproveSerializer(serializers.Serializer):
    lecturer_comments = serializers.CharField(
        required=False, allow_blank=True, max_length=5000
    )

    def validate(self, attrs):
        submission = self.context['submission']
        if submission.evaluation_status != ResearchSubmission.Status.EVALUATED:
            raise serializers.ValidationError(
                "Only EVALUATED submissions can be approved. "
                f"Current status: {submission.evaluation_status}."
            )
        return attrs


# =============================================================================
# Exam attempts & answers — student-facing write serializers
# =============================================================================

class AnswerUpsertSerializer(serializers.ModelSerializer):
    """
    Save (create or update) a single answer inside an IN_PROGRESS attempt.
    The attempt is provided by the view via serializer.context['attempt'].
    """
    question_id = serializers.PrimaryKeyRelatedField(
        source='question', queryset=Question.objects.all()
    )

    class Meta:
        model = Answer
        fields = ['question_id', 'selected_option', 'answer_text']

    def validate(self, attrs):
        attempt = self.context.get('attempt')
        if attempt is None:
            raise serializers.ValidationError("Attempt context is required.")
        if attempt.status != ExamAttempt.Status.IN_PROGRESS:
            raise serializers.ValidationError(
                "Cannot modify answers on a submitted attempt."
            )

        question = attrs['question']
        if not ExamQuestion.objects.filter(exam=attempt.exam, question=question).exists():
            raise serializers.ValidationError(
                "This question is not part of the exam."
            )

        if question.question_type == Question.QuestionType.MCQ:
            if attrs.get('answer_text'):
                raise serializers.ValidationError(
                    {"answer_text": "MCQ questions only accept selected_option."}
                )
            sel = attrs.get('selected_option')
            options = question.options or []
            if sel is None or sel < 0 or sel >= len(options):
                raise serializers.ValidationError(
                    {"selected_option": "Required and must be a valid option index."}
                )
        else:  # STRUCTURED
            if attrs.get('selected_option') is not None:
                raise serializers.ValidationError(
                    {"selected_option": "Structured questions only accept answer_text."}
                )
        return attrs


# ── Student-facing serializers (hide the answer key) ─────────────────

class QuestionForStudentSerializer(serializers.ModelSerializer):
    """
    Question view for a student actively taking an exam.
    Excludes correct_option, model_answer, explanation, and rubric.
    """
    class Meta:
        model = Question
        fields = ['id', 'question_type', 'text', 'options', 'marks']


class ExamQuestionForStudentSerializer(serializers.ModelSerializer):
    """
    Exam-question link for students: nests the full (safe) question plus
    the effective marks (override or question default) for display.
    """
    question = QuestionForStudentSerializer(read_only=True)
    effective_marks = serializers.SerializerMethodField()

    class Meta:
        model = ExamQuestion
        fields = ['id', 'order', 'marks', 'effective_marks', 'question']

    def get_effective_marks(self, obj):
        return obj.marks if obj.marks is not None else obj.question.marks


class ExamForStudentSerializer(serializers.ModelSerializer):
    """
    Exam metadata plus stripped questions for the student taking it.
    No is_published/created_by leakage; students only see what they need.
    """
    course = CourseSerializer(read_only=True)
    exam_questions = ExamQuestionForStudentSerializer(many=True, read_only=True)
    total_marks = serializers.SerializerMethodField()

    class Meta:
        model = Exam
        fields = [
            'id', 'title', 'description', 'instructions',
            'duration_minutes', 'start_time', 'end_time',
            'course', 'total_marks', 'exam_questions',
        ]

    def get_total_marks(self, obj):
        annotated = getattr(obj, 'total_marks_annotated', None)
        if annotated is not None:
            return annotated
        return obj.total_marks


# ── Lecturer preview serializers (same shape, includes answer key) ───

class ExamQuestionForLecturerSerializer(serializers.ModelSerializer):
    """
    Lecturer preview: nests the FULL question (with correct_option,
    model_answer, explanation) so a lecturer opening /exams/attempt/{id}/
    sees the same layout as a student, plus the answer key for verification.
    """
    question = QuestionSerializer(read_only=True)
    effective_marks = serializers.SerializerMethodField()

    class Meta:
        model = ExamQuestion
        fields = ['id', 'order', 'marks', 'effective_marks', 'question']

    def get_effective_marks(self, obj):
        return obj.marks if obj.marks is not None else obj.question.marks


class ExamForLecturerPreviewSerializer(serializers.ModelSerializer):
    """
    Same top-level shape as ExamForStudentSerializer, but with the nested
    questions carrying the full answer key. Used when a lecturer previews
    an attempt at /exams/attempt/{id}/.
    """
    course = CourseSerializer(read_only=True)
    exam_questions = ExamQuestionForLecturerSerializer(many=True, read_only=True)
    total_marks = serializers.SerializerMethodField()

    class Meta:
        model = Exam
        fields = [
            'id', 'title', 'description', 'instructions',
            'duration_minutes', 'start_time', 'end_time',
            'course', 'total_marks', 'exam_questions',
        ]

    def get_total_marks(self, obj):
        annotated = getattr(obj, 'total_marks_annotated', None)
        if annotated is not None:
            return annotated
        return obj.total_marks


# ── Attempt detail serializers ────────────────────────────────────────

class AnswerDetailSerializer(serializers.ModelSerializer):
    """
    Student-facing answer view. Uses the stripped question serializer
    so the answer key (correct_option) never reaches the client during
    or after the exam.
    """
    question = QuestionForStudentSerializer(read_only=True)

    class Meta:
        model = Answer
        fields = [
            'id', 'question', 'selected_option', 'answer_text',
            'score', 'feedback', 'ai_evaluation', 'is_correct', 'created_at',
        ]
        read_only_fields = fields


class AnswerForStudentSerializer(serializers.ModelSerializer):
    """
    Alias kept for clarity — same shape as AnswerDetailSerializer.
    """
    question = QuestionForStudentSerializer(read_only=True)

    class Meta:
        model = Answer
        fields = [
            'id', 'question', 'selected_option', 'answer_text',
            'score', 'feedback', 'ai_evaluation', 'is_correct', 'created_at',
        ]
        read_only_fields = fields


class ExamAttemptDetailSerializer(serializers.ModelSerializer):
    """
    Student-facing attempt view: exam questions are stripped of the
    answer key. Use this for GET /api/attempts/{id}/ for students.
    """
    exam = ExamForStudentSerializer(read_only=True)
    student = StudentProfileSerializer(read_only=True)
    answers = AnswerDetailSerializer(many=True, read_only=True)

    class Meta:
        model = ExamAttempt
        fields = [
            'id', 'exam', 'student', 'attempt_number', 'start_time',
            'end_time', 'submitted_at', 'status', 'score', 'feedback',
            'answers',
        ]
        read_only_fields = fields


class ExamAttemptForLecturerSerializer(serializers.ModelSerializer):
    """
    Lecturer/admin view of an attempt. Uses ExamForLecturerPreviewSerializer
    so the questions render the same way in the UI as they do for students
    (nested objects with text/marks/question_type), plus the answer key
    for verification. Previously this used ExamSerializer, which nests
    questions as bare PKs and broke the /exams/attempt/{id}/ preview.
    """
    exam = ExamForLecturerPreviewSerializer(read_only=True)
    student = StudentProfileSerializer(read_only=True)
    answers = AnswerSerializer(many=True, read_only=True)

    class Meta:
        model = ExamAttempt
        fields = [
            'id', 'exam', 'student', 'attempt_number', 'start_time',
            'end_time', 'submitted_at', 'status', 'score', 'feedback',
            'answers',
        ]
        read_only_fields = fields


class ExamAttemptStartSerializer(serializers.Serializer):
    """Body for POST /api/attempts/start/."""
    exam_id = serializers.IntegerField()


# =============================================================================
# AI & Billing serializers
# =============================================================================

class AIServiceConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = AIServiceConfig
        fields = [
            'ai_highlighting_enabled', 'ai_marking_enabled',
            'ai_research_guidance_enabled', 'token_cost_cents',
            'free_tokens_per_month', 'allow_pay_as_you_go',
            'max_purchasable_tokens', 'updated_at',
        ]
        read_only_fields = ['updated_at']


class UserAICreditSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)

    class Meta:
        model = UserAICredit
        fields = ['id', 'user', 'remaining_tokens', 'total_purchased_tokens',
                  'last_reset_at']
        read_only_fields = ['id', 'user', 'remaining_tokens',
                            'total_purchased_tokens', 'last_reset_at']


class UserAIUsageSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)
    related_document = DocumentSerializer(read_only=True)
    related_answer = AnswerSerializer(read_only=True)

    class Meta:
        model = UserAIUsage
        fields = ['id', 'user', 'feature', 'tokens_used', 'cost_cents',
                  'related_document', 'related_answer', 'created_at']
        read_only_fields = ['id', 'created_at']


class PaymentTransactionSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)

    class Meta:
        model = PaymentTransaction
        fields = ['id', 'user', 'amount_cents', 'tokens_purchased', 'status',
                  'transaction_id', 'created_at', 'completed_at']
        read_only_fields = ['id', 'user', 'status', 'created_at', 'completed_at']
class PaymentTransactionStatusSerializer(serializers.ModelSerializer):
    """
    Minimal shape the frontend polls after returning from Pesapal.
    Exposes only what the /tokens page needs to render success/failure.
    """
    reference = serializers.CharField(source="transaction_id", read_only=True)

    class Meta:
        model = PaymentTransaction
        fields = [
            "id", "status", "tokens_purchased", "amount_cents",
            "reference", "created_at", "completed_at",
        ]
        read_only_fields = fields


class AIPurchasePackageSerializer(serializers.ModelSerializer):
    class Meta:
        model = AIPurchasePackage
        fields = ['id', 'name', 'tokens', 'price_cents', 'is_active', 'created_at']
        read_only_fields = ['id', 'created_at']


# =============================================================================
# Document search result serializer
# =============================================================================

class DocumentSearchResultSerializer(DocumentSerializer):
    snippet = serializers.CharField(read_only=True, allow_null=True)
    rank = serializers.FloatField(read_only=True, allow_null=True)

    class Meta(DocumentSerializer.Meta):
        fields = DocumentSerializer.Meta.fields + ["snippet", "rank"]


# =============================================================================
# Correction report serializer
# =============================================================================

class CorrectionReportSerializer(serializers.ModelSerializer):
    file_url = serializers.SerializerMethodField()
    submission_id = serializers.IntegerField(source="submission.id", read_only=True)
    student_name = serializers.SerializerMethodField()
    document_title = serializers.CharField(
        source="submission.document.title", read_only=True
    )

    class Meta:
        model = CorrectionReport
        fields = [
            "id", "submission_id", "student_name", "document_title",
            "file_url", "status", "generated_at", "error",
        ]
        read_only_fields = fields

    def get_file_url(self, obj):
        if not obj.file:
            return None
        try:
            return obj.file.url
        except Exception:
            return None

    def get_student_name(self, obj):
        u = obj.submission.student.user
        return (u.get_full_name() or u.username).strip()