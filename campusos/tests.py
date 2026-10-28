# campusos/tests/test_ai_flow.py
from decimal import Decimal
from django.test import TestCase
from django.core.cache import cache
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate
from rest_framework import status
from unittest.mock import patch

from campusos.models import (
    User, UserAICredit, AIServiceConfig, PaymentTransaction,
    ResearchSubmission, Rubric, Course, Document, StudentProfile
)
from campusos.ai_services import resolve_max_tokens, reset_config_cache
from campusos.serializers import SubmissionRosterSerializer
from campusos.views import ResearchSubmissionViewSet


class AIConfigFallbackTests(TestCase):
    def setUp(self):
        cache.clear()
        reset_config_cache()
        self.cfg = AIServiceConfig.load()

    def test_env_fallback_when_null(self):
        """If DB is NULL, it should fall back to env."""
        self.cfg.grading_max_tokens = None
        self.cfg.save()
        reset_config_cache()
        
        # Should match the env default (1000)
        self.assertEqual(resolve_max_tokens("marking"), 1000)

    def test_db_override(self):
        """If DB has a value, it should override env."""
        self.cfg.grading_max_tokens = 2500
        self.cfg.save()
        reset_config_cache()
        
        self.assertEqual(resolve_max_tokens("marking"), 2500)


class NegativeBalanceTests(TestCase):
    def test_charge_anyway_goes_negative(self):
        """Exam grading overage should push balance below zero."""
        user = User.objects.create_user(username="testuser", email="t@t.com", password="x")
        
        # The post_save signal automatically creates UserAICredit
        credit = UserAICredit.objects.get(user=user)
        credit.remaining_tokens = 100
        credit.save()
        
        # Consume 100 (balance hits 0)
        credit.try_consume(100)
        
        # Now force a 50-token overage
        credit.charge_anyway(50)
        
        credit.refresh_from_db()
        self.assertEqual(credit.remaining_tokens, -50)


class AutoEvalErrorSentinelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="s", email="s@t.com", password="x")
        self.profile = StudentProfile.objects.create(user=self.user, student_id="S1")
        self.course = Course.objects.create(code="CS1", title="Test")
        self.doc = Document.objects.create(title="D", file="test.pdf", uploaded_by=self.user)
        self.rubric = Rubric.objects.create(title="R", criteria=[], course=self.course, created_by=self.user)

    def test_roster_has_ai_error(self):
        """Serializer should flag __error__ correctly."""
        sub = ResearchSubmission.objects.create(
            student=self.profile, course=self.course, document=self.doc, rubric=self.rubric,
            ai_feedback={"__error__": True, "message": "Boom"}
        )
        self.assertTrue(SubmissionRosterSerializer(sub).data["has_ai_error"])

        # Change feedback to success and verify it flips to False
        sub.ai_feedback = {"scores": {"A": 100}}
        sub.save()
        self.assertFalse(SubmissionRosterSerializer(sub).data["has_ai_error"])


class RetryEvalStatusTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="s", email="s@t.com", password="x")
        # Explicitly set role so is_student() check passes
        self.user.role = User.Role.STUDENT
        self.user.save()
        
        self.profile = StudentProfile.objects.create(user=self.user, student_id="S1")
        self.course = Course.objects.create(code="CS1", title="Test")
        self.doc = Document.objects.create(
            title="D", 
            file="test.pdf", 
            uploaded_by=self.user, 
            content_text="Valid text to prevent validation error."
        )
        self.rubric = Rubric.objects.create(title="R", criteria=[], course=self.course, created_by=self.user)
        
        self.sub = ResearchSubmission.objects.create(
            student=self.profile, course=self.course, document=self.doc, rubric=self.rubric,
            evaluation_status=ResearchSubmission.Status.EVALUATED
        )
        
        # Setup the view directly to bypass URL routing issues
        self.factory = APIRequestFactory()
        self.view = ResearchSubmissionViewSet.as_view({'post': 'retry_eval'})

    @patch("campusos.views.kick_off_evaluation")
    def test_retry_eval_resets_to_pending(self, mock_kick):
        """Clicking Retry must reset status to PENDING so worker picks it up."""
        request = self.factory.post(f'/api/research-submissions/{self.sub.id}/retry-eval/')
        force_authenticate(request, user=self.user)
        
        # Call the view directly!
        response = self.view(request, pk=self.sub.id)
        
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        mock_kick.assert_called_once_with(self.sub.id)
        
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.evaluation_status, ResearchSubmission.Status.PENDING)