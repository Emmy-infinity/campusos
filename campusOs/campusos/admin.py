from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from unfold.admin import ModelAdmin

from .models import (
    User, StudentProfile, LecturerProfile, Course, Enrollment,
    Document, Highlight, QuestionBank, Question, Exam, ExamQuestion,
    ExamAttempt, Answer, Rubric, ResearchSubmission, RubricEvaluation,
    AIServiceConfig, UserAICredit, UserAIUsage, PaymentTransaction,
    AIPurchasePackage
)


# --- Custom user model needs UserAdmin + Unfold's ModelAdmin combined ---
@admin.register(User)
class UserAdmin(ModelAdmin, BaseUserAdmin):
    pass


# --- Everything else: plain Unfold ModelAdmin ---
@admin.register(StudentProfile)
class StudentProfileAdmin(ModelAdmin):
    pass


@admin.register(LecturerProfile)
class LecturerProfileAdmin(ModelAdmin):
    pass


@admin.register(Course)
class CourseAdmin(ModelAdmin):
    pass


@admin.register(Enrollment)
class EnrollmentAdmin(ModelAdmin):
    pass


@admin.register(Document)
class DocumentAdmin(ModelAdmin):
    pass


@admin.register(Highlight)
class HighlightAdmin(ModelAdmin):
    pass


@admin.register(QuestionBank)
class QuestionBankAdmin(ModelAdmin):
    pass


@admin.register(Question)
class QuestionAdmin(ModelAdmin):
    pass


@admin.register(Exam)
class ExamAdmin(ModelAdmin):
    pass


@admin.register(ExamQuestion)
class ExamQuestionAdmin(ModelAdmin):
    pass


@admin.register(ExamAttempt)
class ExamAttemptAdmin(ModelAdmin):
    pass


@admin.register(Answer)
class AnswerAdmin(ModelAdmin):
    pass


@admin.register(Rubric)
class RubricAdmin(ModelAdmin):
    pass


@admin.register(ResearchSubmission)
class ResearchSubmissionAdmin(ModelAdmin):
    pass


@admin.register(RubricEvaluation)
class RubricEvaluationAdmin(ModelAdmin):
    pass


@admin.register(AIServiceConfig)
class AIServiceConfigAdmin(ModelAdmin):
    pass


@admin.register(UserAICredit)
class UserAICreditAdmin(ModelAdmin):
    pass


@admin.register(UserAIUsage)
class UserAIUsageAdmin(ModelAdmin):
    pass


@admin.register(PaymentTransaction)
class PaymentTransactionAdmin(ModelAdmin):
    pass


@admin.register(AIPurchasePackage)
class AIPurchasePackageAdmin(ModelAdmin):
    pass