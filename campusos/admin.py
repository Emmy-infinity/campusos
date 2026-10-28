# admin.py
from django.conf import settings
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.html import format_html

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
    readonly_fields = ('search_vector', 'content_pages', 'created_at', 'updated_at')
    date_hierarchy = 'created_at'
    
    save_on_top = True  # ← Keeps the Save button visible at the top


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
    
    save_on_top = True  # ← Keeps the Save button visible at the top


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
    
    save_on_top = True  # ← Keeps the Save button visible at the top


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
    
    save_on_top = True  # ← Keeps the Save button visible at the top


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
    readonly_fields = ('submitted_at', 'approved_at', 'ai_feedback')
    date_hierarchy = 'submitted_at'
    
    save_on_top = True  # ← FIXED: The submission form now has buttons at the top


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
    """
    Singleton admin for AI service configuration.
    """
    list_display = (
        "ai_highlighting_enabled",
        "ai_marking_enabled",
        "ai_research_guidance_enabled",
        "token_cost_cents",
        "updated_at",
    )
    readonly_fields = (
        "updated_at",
        "updated_by",
        "env_highlight_model",
        "env_grading_model",
        "env_research_model",
        "env_highlight_max_tokens",
        "env_grading_max_tokens",
        "env_research_max_tokens",
        "env_repair_attempts",
        "env_temperature",
    )
    
    # The tuple syntax ("field", "env_field") creates a responsive 2-column grid.
    fieldsets = (
        ("Feature toggles", {
            "fields": (
                "ai_highlighting_enabled",
                "ai_marking_enabled",
                "ai_research_guidance_enabled",
            ),
            "description": "Users get a 403 with a clear message when a feature is disabled.",
        }),
        ("Models (leave blank to inherit from .env)", {
            "fields": (
                ("highlight_model", "env_highlight_model"),
                ("grading_model", "env_grading_model"),
                ("research_model", "env_research_model"),
            ),
            "description": "Format: provider/model-name. The block on the right shows the inherited .env value.",
        }),
        ("Token budgets (leave blank to inherit from .env)", {
            "fields": (
                ("highlight_max_tokens", "env_highlight_max_tokens"),
                ("grading_max_tokens", "env_grading_max_tokens"),
                ("research_max_tokens", "env_research_max_tokens"),
            ),
            "description": "Caps the model's output length per request.",
        }),
        ("Reliability & sampling (leave blank to inherit from .env)", {
            "fields": (
                ("repair_attempts", "env_repair_attempts"),
                ("temperature", "env_temperature"),
            ),
            "description": "0 repair attempts disables repair. Temperature 0.0 is deterministic.",
        }),
        ("Pricing & allowance", {
            "fields": (
                "token_cost_cents",
                "free_tokens_per_month",
                "allow_pay_as_you_go",
                "max_purchasable_tokens",
            ),
        }),
        ("Audit", {
            "fields": ("updated_at", "updated_by"),
            "classes": ("collapse",),
        }),
    )
    
    save_on_top = True  # ← Keeps the Save button visible at the top

    # ── env display (readonly; renders as compact grey blocks) ──────

    def _env_html(self, key, default="(unset)"):
        val = getattr(settings, key, default)
        return format_html(
            '<div style="padding: 8px 12px; background-color: var(--unfold-bg-secondary, #f3f4f6); '
            'border: 1px solid var(--unfold-border, #d1d5db); border-radius: 6px; '
            'font-family: monospace; font-size: 0.85em; line-height: 1.4; '
            'word-break: break-all; color: var(--unfold-text-secondary, #4b5563);">'
            '<div style="font-weight: 600; margin-bottom: 4px;">{}</div>'
            '<div>{}</div>'
            '</div>',
            key, val
        )

    @admin.display(description="")
    def env_highlight_model(self, obj):
        return self._env_html("AI_DEFAULT_MODEL")

    @admin.display(description="")
    def env_grading_model(self, obj):
        return self._env_html("AI_GRADING_MODEL")

    @admin.display(description="")
    def env_research_model(self, obj):
        return self._env_html("AI_DEFAULT_MODEL")

    @admin.display(description="")
    def env_highlight_max_tokens(self, obj):
        return self._env_html("AI_HIGHLIGHT_MAX_TOKENS")

    @admin.display(description="")
    def env_grading_max_tokens(self, obj):
        return self._env_html("AI_GRADING_MAX_TOKENS")

    @admin.display(description="")
    def env_research_max_tokens(self, obj):
        return self._env_html("AI_RESEARCH_MAX_TOKENS")

    @admin.display(description="")
    def env_repair_attempts(self, obj):
        return self._env_html("AI_REPAIR_ATTEMPTS", 1)

    @admin.display(description="")
    def env_temperature(self, obj):
        return self._env_html("AI_TEMPERATURE", 0.0)

    # ── singleton guards ─────────────────────────────────────────────

    def has_add_permission(self, request):
        return not AIServiceConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(UserAICredit)
class UserAICreditAdmin(ModelAdmin):
    list_display = ('user', 'remaining_tokens', 'total_purchased_tokens', 'last_reset_at')
    search_fields = ('user__username', 'user__email')
    autocomplete_fields = ('user',)
    readonly_fields = ('last_reset_at',)


@admin.register(UserAIUsage)
class UserAIUsageAdmin(ModelAdmin):
    list_display = (
        'user', 'feature', 'tokens_used', 'cost_display',
        'related_document', 'related_answer', 'created_at',
    )
    list_filter = ('feature',)
    search_fields = ('user__username', 'user__email', 'feature')
    autocomplete_fields = ('user', 'related_document', 'related_answer')
    readonly_fields = ('tokens_used', 'cost_cents', 'created_at')
    date_hierarchy = 'created_at'

    @admin.display(description="Cost (cents)", ordering='cost_cents')
    def cost_display(self, obj):
        return f"{obj.cost_cents:.6f}"


@admin.register(PaymentTransaction)
class PaymentTransactionAdmin(ModelAdmin):
    list_display = (
        'id', 'user', 'amount_cents', 'tokens_purchased', 'status',
        'transaction_id', 'created_at', 'completed_at',
    )
    list_filter = ('status',)
    search_fields = ('user__username', 'user__email', 'transaction_id',
                     'pesapal_tracking_id', 'pesapal_merchant_reference')
    autocomplete_fields = ('user',)
    readonly_fields = (
        'created_at', 'completed_at',
        'pesapal_tracking_id', 'pesapal_merchant_reference',
        'redirect_url', 'raw_pesapal_response',
    )
    date_hierarchy = 'created_at'


@admin.register(AIPurchasePackage)
class AIPurchasePackageAdmin(ModelAdmin):
    list_display = ('name', 'tokens', 'price_cents', 'is_active', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('name',)
    readonly_fields = ('created_at',)