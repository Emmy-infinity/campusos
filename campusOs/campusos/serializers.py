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
- DocumentSerializer: removed `search_vector` from read_only_fields (it
  was not declared in Meta.fields, which DRF resolves against) and
  removed the dead `validate()` — content_text is read-only, so the
  check inside validate() could never fire.
- ExamSerializer: removed the redundant `source='exam_questions'` on the
  `exam_questions` field (name and source were identical).
- HighlightSerializer: `create()` no longer relies on
  super().create() to accept a `user` kwarg — it pops the derived user
  straight into the model constructor, which is both clearer and
  version-agnostic.
"""
from rest_framework import serializers
from django.contrib.auth import get_user_model
from .models import (
    User, StudentProfile, LecturerProfile, Course, Enrollment,
    Document, Highlight, QuestionBank, Question, Exam, ExamQuestion,
    ExamAttempt, Answer, Rubric, ResearchSubmission, RubricEvaluation,
    AIServiceConfig, UserAICredit, UserAIUsage, PaymentTransaction,
    AIPurchasePackage
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
        source='lecturer', queryset=LecturerProfile.objects.all(),
        write_only=True, allow_null=True,
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
# Document & Highlight serializers
# =============================================================================

class DocumentSerializer(serializers.ModelSerializer):
    """
    `uploaded_by` is always set from request.user in the view. There is
    no writable `uploaded_by_id`. `content_text` is populated by a
    background extraction task and is read-only. `search_vector` is a
    Postgres tsvector and is intentionally NOT exposed.
    """
    uploaded_by = UserSerializer(read_only=True)
    course = CourseSerializer(read_only=True)
    course_id = serializers.PrimaryKeyRelatedField(
        source='course', queryset=Course.objects.all(), write_only=True, allow_null=True
    )

    class Meta:
        model = Document
        fields = [
            'id', 'title', 'description', 'file', 'uploaded_by',
            'course', 'course_id', 'category', 'visibility', 'content_text',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'uploaded_by', 'content_text',
                            'created_at', 'updated_at']


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
# Question bank, Question, Exam serializers
# =============================================================================

class QuestionBankSerializer(serializers.ModelSerializer):
    course = CourseSerializer(read_only=True)
    course_id = serializers.PrimaryKeyRelatedField(
        source='course', queryset=Course.objects.all(), write_only=True
    )
    created_by = UserSerializer(read_only=True)

    class Meta:
        model = QuestionBank
        fields = ['id', 'course', 'course_id', 'title', 'description',
                  'created_by', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_by', 'created_at', 'updated_at']


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


class ExamQuestionSerializer(serializers.ModelSerializer):
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
        if hasattr(obj, 'total_marks_annotated'):
            return obj.total_marks_annotated
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
    created_by = UserSerializer(read_only=True)
    course = CourseSerializer(read_only=True)
    course_id = serializers.PrimaryKeyRelatedField(
        source='course', queryset=Course.objects.all(), write_only=True, allow_null=True
    )

    class Meta:
        model = Rubric
        fields = ['id', 'title', 'description', 'created_by', 'course',
                  'course_id', 'criteria', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_by', 'created_at', 'updated_at']


class ResearchSubmissionSerializer(serializers.ModelSerializer):
    student = StudentProfileSerializer(read_only=True)
    course = CourseSerializer(read_only=True)
    document = DocumentSerializer(read_only=True)
    rubric = RubricSerializer(read_only=True)
    document_id = serializers.PrimaryKeyRelatedField(
        source='document', queryset=Document.objects.all(), write_only=True
    )
    rubric_id = serializers.PrimaryKeyRelatedField(
        source='rubric', queryset=Rubric.objects.all(), write_only=True, allow_null=True
    )

    class Meta:
        model = ResearchSubmission
        fields = [
            'id', 'student', 'course', 'document', 'document_id', 'rubric',
            'rubric_id', 'submitted_at', 'evaluation_status', 'ai_feedback',
            'overall_score', 'lecturer_comments',
        ]
        read_only_fields = ['id', 'student', 'submitted_at', 'evaluation_status',
                            'ai_feedback', 'overall_score']


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


class AIPurchasePackageSerializer(serializers.ModelSerializer):
    class Meta:
        model = AIPurchasePackage
        fields = ['id', 'name', 'tokens', 'price_cents', 'is_active', 'created_at']
        read_only_fields = ['id', 'created_at']