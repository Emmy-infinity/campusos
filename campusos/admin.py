# admin.py
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from unfold.admin import ModelAdmin

from .models import (
    User, StudentProfile, LecturerProfile, Course, Enrollment,
    Document, Highlight, QuestionBank, Question, Exam, ExamQuestion,
    ExamAttempt, Answer, Rubric, ResearchSubmission, RubricEvaluation,
    AIServiceConfig, UserAICredit, UserAIUsage, PaymentTransaction,
    AIPurchasePackage, CorrectionReport,
)


# =============================================================================
# Users & profiles
# =============================================================================

# --- Custom user model needs UserAdmin + Unfold's ModelAdmin combined ---
@admin.register(User)
class UserAdmin(ModelAdmin, BaseUserAdmin):
    list_display = ('username', 'email', 'first_name', 'last_name', 'role', 'is_staff', 'is_active')
    list_filter = ('role', 'is_staff', 'is_superuser', 'is_active')
    search_fields = ('username', 'email', 'first_name', 'last_name')
    ordering = ('-date_joined',)
    readonly_fields = ('last_login', 'date_joined')

    fieldsets = BaseUserAdmin.fieldsets + (
        ('Academic role', {'fields': ('role',)}),
    )
    add_fieldsets = BaseUserAdmin.add_fieldsets + (
        ('Academic role', {'fields': ('role',)}),
    )


@admin.register(StudentProfile)
class StudentProfileAdmin(ModelAdmin):
    list_display = ('student_id', 'user', 'department', 'enrollment_year')
    list_filter = ('department', 'enrollment_year')
    search_fields = ('student_id', 'user__username', 'user__email', 'user__first_name', 'user__last_name')
    autocomplete_fields = ('user',)


@admin.register(LecturerProfile)
class LecturerProfileAdmin(ModelAdmin):
    list_display = ('staff_id', 'user', 'department', 'designation')
    list_filter = ('department', 'designation')
    search_fields = ('staff_id', 'user__username', 'user__email', 'user__first_name', 'user__last_name')
    autocomplete_fields = ('user',)


# =============================================================================
# Courses & enrollment
# =============================================================================

@admin.register(Course)
class CourseAdmin(ModelAdmin):
    list_display = ('code', 'title', 'lecturer', 'department', 'created_at')
    list_filter = ('department',)
    search_fields = ('code', 'title', 'description', 'lecturer__staff_id', 'lecturer__user__email')
    autocomplete_fields = ('lecturer',)
    readonly_fields = ('created_at',)


@admin.register(Enrollment)
class EnrollmentAdmin(ModelAdmin):
    list_display = ('student', 'course', 'semester', 'enrolled_at')
    list_filter = ('semester', 'course')
    search_fields = ('student__student_id', 'student__user__email', 'course__code', 'course__title')
    autocomplete_fields = ('student', 'course')
    readonly_fields = ('enrolled_at',)


# =============================================================================
# Documents
# =============================================================================

@admin.register(Document)
class DocumentAdmin(ModelAdmin):
    list_display = ('title', 'category', 'visibility', 'course', 'uploaded_by', 'created_at')
    list_filter = ('category', 'visibility', 'course')
    search_fields = ('title', 'description', 'content_text', 'uploaded_by__email')
    autocomplete_fields = ('uploaded_by', 'course')
    readonly_fields = ('search_vector', 'created_at', 'updated_at')
    date_hierarchy = 'created_at'


@admin.register(Highlight)
class HighlightAdmin(ModelAdmin):
    list_display = ('document', 'user', 'start_offset', 'end_offset', 'color', 'is_ai_generated', 'created_at')
    list_filter = ('color', 'is_ai_generated')
    search_fields = ('document__title', 'user__email', 'text', 'note')
    autocomplete_fields = ('document', 'user')
    readonly_fields = ('created_at',)


# =============================================================================
# Question banks, questions, exams
# =============================================================================

@admin.register(QuestionBank)
class QuestionBankAdmin(ModelAdmin):
    list_display = ('title', 'course', 'created_by', 'created_at', 'updated_at')
    list_filter = ('course',)
    search_fields = ('title', 'description', 'course__code')
    autocomplete_fields = ('course', 'created_by')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(Question)
class QuestionAdmin(ModelAdmin):
    list_display = ('__str__', 'bank', 'question_type', 'marks', 'created_at')
    list_filter = ('question_type', 'bank__course')
    search_fields = ('text', 'explanation', 'model_answer', 'bank__title')
    autocomplete_fields = ('bank',)
    readonly_fields = ('created_at', 'updated_at')


# ExamQuestion uses a `through` relationship on Exam.questions, so
# filter_horizontal / filter_vertical are NOT allowed on ExamAdmin.
# Instead, edit exam questions via this inline on the Exam change page.
class ExamQuestionInline(admin.TabularInline):
    model = ExamQuestion
    extra = 1
    autocomplete_fields = ('question',)
    ordering = ('order',)


@admin.register(Exam)
class ExamAdmin(ModelAdmin):
    list_display = ('title', 'course', 'is_published', 'duration_minutes', 'start_time', 'end_time', 'created_by')
    list_filter = ('is_published', 'course')
    search_fields = ('title', 'description', 'instructions', 'course__code')
    autocomplete_fields = ('course', 'created_by')
    readonly_fields = ('created_at', 'updated_at')
    date_hierarchy = 'start_time'
    inlines = (ExamQuestionInline,)


@admin.register(ExamQuestion)
class ExamQuestionAdmin(ModelAdmin):
    list_display = ('exam', 'question', 'order', 'marks')
    list_filter = ('exam',)
    search_fields = ('exam__title', 'question__text')
    autocomplete_fields = ('exam', 'question')


# =============================================================================
# Attempts & answers
# =============================================================================

@admin.register(ExamAttempt)
class ExamAttemptAdmin(ModelAdmin):
    list_display = (
        'exam', 'student', 'attempt_number', 'status',
        'score', 'timed_out', 'abandoned_at', 'start_time', 'submitted_at',
    )
    list_filter = ('status', 'timed_out', 'exam')
    search_fields = ('exam__title', 'student__student_id', 'student__user__email')
    autocomplete_fields = ('exam', 'student')
    readonly_fields = (
        'start_time', 'end_time', 'submitted_at',
        'last_activity_at', 'abandoned_at',
    )
    date_hierarchy = 'start_time'


@admin.register(Answer)
class AnswerAdmin(ModelAdmin):
    list_display = ('attempt', 'question', 'is_correct', 'score', 'created_at')
    list_filter = ('is_correct',)
    search_fields = ('attempt__student__student_id', 'question__text', 'answer_text')
    autocomplete_fields = ('attempt', 'question')
    readonly_fields = ('created_at',)


# =============================================================================
# Rubrics & research submissions
# =============================================================================

@admin.register(Rubric)
class RubricAdmin(ModelAdmin):
    list_display = ('title', 'course', 'created_by', 'created_at', 'updated_at')
    list_filter = ('course',)
    search_fields = ('title', 'description', 'course__code')
    autocomplete_fields = ('course', 'created_by')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(ResearchSubmission)
class ResearchSubmissionAdmin(ModelAdmin):
    list_display = (
        'document', 'student', 'course', 'rubric',
        'evaluation_status', 'overall_score',
        'approved_by', 'approved_at', 'submitted_at',
    )
    list_filter = ('evaluation_status', 'course', 'rubric')
    search_fields = ('document__title', 'student__student_id', 'student__user__email', 'lecturer_comments')
    autocomplete_fields = ('student', 'course', 'document', 'rubric', 'approved_by')
    readonly_fields = ('submitted_at', 'approved_at')
    date_hierarchy = 'submitted_at'


@admin.register(RubricEvaluation)
class RubricEvaluationAdmin(ModelAdmin):
    list_display = ('submission', 'rubric', 'overall_score', 'evaluated_at')
    list_filter = ('rubric',)
    search_fields = ('submission__document__title', 'submission__student__student_id')
    autocomplete_fields = ('submission', 'rubric')
    readonly_fields = ('evaluated_at',)


@admin.register(CorrectionReport)
class CorrectionReportAdmin(ModelAdmin):
    list_display = ('id', 'submission', 'evaluation', 'status', 'generated_at')
    list_filter = ('status',)
    search_fields = (
        'submission__document__title',
        'submission__student__student_id',
        'submission__student__user__email',
    )
    autocomplete_fields = ('submission', 'evaluation')
    readonly_fields = ('generated_at', 'error')
    date_hierarchy = 'generated_at'


# =============================================================================
# AI usage & billing
# =============================================================================

@admin.register(AIServiceConfig)
class AIServiceConfigAdmin(ModelAdmin):
    list_display = (
        'id',
        'ai_highlighting_enabled',
        'ai_marking_enabled',
        'ai_research_guidance_enabled',
        'token_cost_cents',
        'free_tokens_per_month',
        'allow_pay_as_you_go',
        'max_purchasable_tokens',
        'updated_at',
    )
    readonly_fields = ('updated_at',)

    def has_add_permission(self, request):
        # Singleton — only allow editing the existing row.
        return not AIServiceConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(UserAICredit)
class UserAICreditAdmin(ModelAdmin):
    list_display = ('user', 'remaining_tokens', 'total_purchased_tokens', 'last_reset_at')
    search_fields = ('user__username', 'user__email')
    autocomplete_fields = ('user',)
    readonly_fields = ('last_reset_at',)


@admin.register(UserAIUsage)
class UserAIUsageAdmin(ModelAdmin):
    list_display = ('user', 'feature', 'tokens_used', 'cost_cents', 'related_document', 'related_answer', 'created_at')
    list_filter = ('feature',)
    search_fields = ('user__username', 'user__email', 'feature')
    autocomplete_fields = ('user', 'related_document', 'related_answer')
    readonly_fields = ('created_at',)
    date_hierarchy = 'created_at'


@admin.register(PaymentTransaction)
class PaymentTransactionAdmin(ModelAdmin):
    list_display = ('id', 'user', 'amount_cents', 'tokens_purchased', 'status', 'transaction_id', 'created_at', 'completed_at')
    list_filter = ('status',)
    search_fields = ('user__username', 'user__email', 'transaction_id')
    autocomplete_fields = ('user',)
    readonly_fields = ('created_at', 'completed_at')
    date_hierarchy = 'created_at'


@admin.register(AIPurchasePackage)
class AIPurchasePackageAdmin(ModelAdmin):
    list_display = ('name', 'tokens', 'price_cents', 'is_active', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('name',)
    readonly_fields = ('created_at',)