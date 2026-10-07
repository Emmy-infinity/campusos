# campusos/urls.py
"""
CampusOS URL configuration.
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from . import views

router = DefaultRouter()
router.register(r"documents", views.DocumentViewSet, basename="document")
router.register(r"courses", views.CourseViewSet, basename="course")
router.register(r"enrollments", views.EnrollmentViewSet, basename="enrollment")
router.register(r"question-banks", views.QuestionBankViewSet, basename="question-bank")
router.register(r"questions", views.QuestionViewSet, basename="question")
router.register(r"exams", views.ExamViewSet, basename="exam")
router.register(r"exam-questions", views.ExamQuestionViewSet, basename="exam-question")
router.register(r"rubrics", views.RubricViewSet, basename="rubric")
router.register(r"submissions", views.ResearchSubmissionViewSet, basename="submission")
router.register(r"attempts", views.ExamAttemptViewSet, basename="attempt")

urlpatterns = [
    # ── Search (must come before router include) ────────────────
    path("documents/search/", views.DocumentSearchView.as_view(), name="document-search"),
    path("documents/suggest/", views.DocumentSuggestView.as_view(), name="document-suggest"),

    # ── DRF routers ─────────────────────────────────────────────
    path("", include(router.urls)),

    # ── Auth ────────────────────────────────────────────────────
    path("register/", views.RegisterView.as_view(), name="register"),
    path("token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("users/me/", views.UserMeView.as_view(), name="user-me"),

    # ── Dashboards ──────────────────────────────────────────────
    path("lecturer/dashboard/", views.LecturerDashboardView.as_view(), name="lecturer-dashboard"),
    path("student/dashboard/", views.StudentDashboardView.as_view(), name="student-dashboard"),

    # ── Exam analytics ──────────────────────────────────────────
    path("exams/<int:pk>/analytics/",
         views.LecturerExamAnalyticsView.as_view(),
         name="exam-analytics"),
    path("courses/<int:pk>/analytics/",
         views.LecturerCourseAnalyticsView.as_view(),
         name="course-analytics"),

    # ── AI ──────────────────────────────────────────────────────
    path("ai/highlight/", views.AIHighlightView.as_view(), name="ai-highlight"),
    path("ai/tasks/<str:task_id>/", views.AITaskStatusView.as_view(), name="ai-task-status"),
    path("ai/balance/", views.TokenBalanceView.as_view(), name="ai-balance"),
    path("ai/purchase/", views.PurchaseTokensView.as_view(), name="purchase-tokens"),
    path("ai/config/", views.AIServiceConfigView.as_view(), name="ai-config"),
    path("ai/packages/", views.AIPurchasePackageListView.as_view(), name="ai-packages"),   # ← NEW

    # ── Pesapal ─────────────────────────────────────────────────
    path("payments/ipn/", views.PesapalIPNView.as_view(), name="pesapal-ipn"),
    path("payments/<int:transaction_id>/status/",
         views.PaymentStatusView.as_view(), name="payment-status"),
    path("payments/register-ipn/",
         views.RegisterIPNView.as_view(), name="register-ipn"),

    # ── Exam / research ─────────────────────────────────────────
    path("exam/submit/", views.ExamSubmitView.as_view(), name="exam-submit"),
    path("research/guidance/", views.ResearchGuidanceView.as_view(), name="research-guidance"),
]