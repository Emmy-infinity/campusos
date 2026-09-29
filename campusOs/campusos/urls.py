# campusos/urls.py
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from . import views   # ← this is `campusos.views`, correct

router = DefaultRouter()
router.register(r'documents', views.DocumentViewSet, basename='document')

urlpatterns = [
    # DRF viewsets
    path('', include(router.urls)),

    # JWT auth
    path('token/', TokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),

    # Current user
    path('users/me/', views.UserMeView.as_view(), name='user-me'),

    # AI
    path('ai/highlight/', views.AIHighlightView.as_view(), name='ai-highlight'),
    path('ai/tasks/<str:task_id>/', views.AITaskStatusView.as_view(), name='ai-task-status'),
    path('ai/balance/', views.TokenBalanceView.as_view(), name='token-balance'),
    path('ai/purchase/', views.PurchaseTokensView.as_view(), name='purchase-tokens'),
    path('ai/config/', views.AIServiceConfigView.as_view(), name='ai-config'),

    # Exam / research
    path('exam/submit/', views.ExamSubmitView.as_view(), name='exam-submit'),
    path('research/guidance/', views.ResearchGuidanceView.as_view(), name='research-guidance'),
]