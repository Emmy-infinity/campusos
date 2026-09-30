# views.py
"""
API views for the academic platform. The AI work itself lives in
ai_services.py and runs in Celery workers (tasks.py); these views validate,
enqueue, and return 202 with a task_id.

Async endpoints
---------------
POST /api/ai/highlight/      -> 202 {"task_id"}   (or 402/403/404/400 up front)
POST /api/exam/submit/       -> 200 if fully graded inline (MCQ-only exam),
                                202 {"task_id"} if AI marking was queued
POST /api/research/guidance/ -> 202 {"task_id"}
GET  /api/ai/tasks/<task_id>/ -> {"state": "PENDING|STARTED|SUCCESS|FAILURE",
                                  "ok": bool, "data"/"error": ...}

Exam progress can also be read from the attempt itself: status is SUBMITTED
while marking runs and GRADED when finished. Research progress is visible via
ResearchSubmission.evaluation_status (PENDING -> EVALUATED).

Do NOT enable ATOMIC_REQUESTS: tasks must see committed rows.

Optional settings: AI_SIMULATE_PAYMENTS (defaults to DEBUG),
DOCUMENT_ALLOWED_EXTENSIONS, DOCUMENT_MAX_UPLOAD_BYTES; throttle scopes
"ai_calls" (30/min) and "ai_purchase" (10/hour) may be overridden in
REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]. See ai_services.py for AI settings.

Security notes carried over from the previous revision: ownership-filtered
lookups (404), no internal error text to clients, server-side token pricing,
purchase endpoint disabled outside simulation, document writes limited to
uploader/admin.
"""
import logging
import os
import uuid

from celery.result import AsyncResult

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone

from rest_framework import status, viewsets, permissions
from rest_framework.exceptions import PermissionDenied, ValidationError as DRFValidationError
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from .ai_services import (
    AIRequestError, can_afford, check_highlight_ready, check_research_ready,
    finalize_attempt, get_exam_question_marks, grade_mcq_answers,
    has_structured_answers, late_note, new_task_id,
    HIGHLIGHT_MAX_TOKENS, RESEARCH_MAX_TOKENS,
)
from .models import (
    User, Course, Document, ExamAttempt, Enrollment, ResearchSubmission,
    AIServiceConfig, UserAICredit, PaymentTransaction, AIPurchasePackage,
)
from .serializers import (
    DocumentSerializer, UserAICreditSerializer, AIServiceConfigSerializer,
)
from .tasks import (
    highlight_document_task, grade_exam_attempt_task, evaluate_research_task,
)

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Throttles (work even if the scope has no configured rate)
# -----------------------------------------------------------------------------

class AICallThrottle(UserRateThrottle):
    scope = "ai_calls"

    def get_rate(self):
        return self.THROTTLE_RATES.get(self.scope) or "30/min"


class PurchaseThrottle(UserRateThrottle):
    scope = "ai_purchase"

    def get_rate(self):
        return self.THROTTLE_RATES.get(self.scope) or "10/hour"


# -----------------------------------------------------------------------------
# Helpers & permissions
# -----------------------------------------------------------------------------

def parse_positive_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def is_student(user):
    return user.is_authenticated and user.role == User.Role.STUDENT


def is_lecturer(user):
    return user.is_authenticated and user.role == User.Role.LECTURER


def is_admin(user):
    return user.is_authenticated and (user.is_superuser or user.role == User.Role.ADMIN)


class IsAppAdmin(permissions.BasePermission):
    """Matches this app's definition of admin (is_superuser OR role == ADMIN)."""
    def has_permission(self, request, view):
        return is_admin(request.user)


class IsUploaderOrAdminForWrites(permissions.BasePermission):
    def has_object_permission(self, request, view, obj):
        if request.method in permissions.SAFE_METHODS:
            return True
        return is_admin(request.user) or obj.uploaded_by_id == request.user.id


def user_can_use_course(user, course):
    if is_admin(user):
        return True
    if is_lecturer(user):
        return Course.objects.filter(pk=course.pk, lecturer__user=user).exists()
    profile = getattr(user, "student_profile", None)
    if profile is None:
        return False
    return Enrollment.objects.filter(student=profile, course=course).exists()


def enqueue(task, args, user):
    """Queue `task` with an owner-prefixed id; return the id or None if the broker is down."""
    task_id = new_task_id(user)
    try:
        task.apply_async(args=args, task_id=task_id)
    except Exception:
        logger.exception("Could not enqueue %s", task.name)
        return None
    return task_id


def queue_unavailable():
    return Response({"error": "The AI service is temporarily unavailable. Please try again shortly."},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE)


def accepted(task_id):
    return Response({"task_id": task_id, "status": "queued"}, status=status.HTTP_202_ACCEPTED)


# =============================================================================
# Task status
# =============================================================================






from .serializers import UserSerializer

class UserMeView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def get(self, request):
        return Response(UserSerializer(request.user).data)





from rest_framework import generics
from rest_framework.permissions import AllowAny
from .serializers import RegisterSerializer


class RegisterView(generics.CreateAPIView):
    """
    POST /api/register/   (public — no auth required)

    Body:
        {
            "username": "jdoe",
            "email": "jdoe@example.com",
            "password": "StrongPass123",
            "password_confirm": "StrongPass123",
            "first_name": "Jane",
            "last_name": "Doe",
            "role": "STUDENT",
            "student_id": "2024-CS-001",
            "department": "Computer Science",
            "enrollment_year": 2024
        }

    Or for a lecturer:
        "role": "LECTURER", "staff_id": "STF-102", "designation": "Senior Lecturer"

    Returns the created user plus JWT tokens.
    """
    serializer_class = RegisterSerializer
    permission_classes = [AllowAny]
    throttle_classes = []   # Add a throttle here if you want rate limiting




class AITaskStatusView(APIView):
    """GET /api/ai/tasks/<task_id>/ — owner (or admin) only."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, task_id):
        if not is_admin(request.user) and not task_id.startswith(f"u{request.user.id}-"):
            return Response({"error": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        result = AsyncResult(task_id)
        state = result.state
        if state == "SUCCESS":
            payload = result.result if isinstance(result.result, dict) else {}
            return Response({"state": state, **payload})
        if state == "FAILURE":
            return Response({"state": state, "ok": False,
                             "error": "AI processing failed. Please try again later."})
        # PENDING (queued or unknown id), STARTED, RETRY
        return Response({"state": state})


# =============================================================================
# AI Highlighting
# =============================================================================

class AIHighlightView(APIView):
    """POST /api/ai/highlight/  Body: {"document_id": 1}"""
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [AICallThrottle]

    def post(self, request):
        document_id = parse_positive_int(request.data.get("document_id"))
        if document_id is None:
            return Response({"error": "document_id is required and must be a positive integer."},
                            status=status.HTTP_400_BAD_REQUEST)

        document = get_object_or_404(Document.objects.visible_to(request.user), pk=document_id)

        try:
            check_highlight_ready(document)
        except AIRequestError as exc:
            return Response({"error": str(exc)}, status=exc.status_code)

        if not can_afford(request.user, document.content_text, HIGHLIGHT_MAX_TOKENS):
            return Response({"error": "Insufficient AI tokens. Please purchase more."},
                            status=status.HTTP_402_PAYMENT_REQUIRED)

        task_id = enqueue(highlight_document_task, [document.id, request.user.id], request.user)
        return accepted(task_id) if task_id else queue_unavailable()


# =============================================================================
# Exam submission
# =============================================================================

class ExamSubmitView(APIView):
    """
    POST /api/exam/submit/  Body: {"attempt_id": 1}

    Claims the attempt (row-locked) and grades MCQs synchronously; both are
    fast and deterministic. Structured answers are marked by AI in a worker.
    AI marking is billed to the student who owns the attempt.
    """
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [AICallThrottle]

    def post(self, request):
        attempt_id = parse_positive_int(request.data.get("attempt_id"))
        if attempt_id is None:
            return Response({"error": "attempt_id is required and must be a positive integer."},
                            status=status.HTTP_400_BAD_REQUEST)

        attempts = ExamAttempt.objects.select_related("exam", "student__user")
        if not is_admin(request.user):
            attempts = attempts.filter(student__user=request.user)

        with transaction.atomic():
            attempt = get_object_or_404(attempts.select_for_update(of=("self",)), pk=attempt_id)
            if attempt.status != ExamAttempt.Status.IN_PROGRESS:
                return Response({"error": "Attempt is already submitted or graded."},
                                status=status.HTTP_400_BAD_REQUEST)

            now = timezone.now()
            attempt.status = ExamAttempt.Status.SUBMITTED
            attempt.submitted_at = now
            attempt.end_time = now
            note = late_note(attempt, now)
            if note:
                attempt.feedback = note
            attempt.save(update_fields=["status", "submitted_at", "end_time", "feedback"])

            exam_marks = get_exam_question_marks(attempt.exam)
            grade_mcq_answers(attempt, exam_marks)

        # Committed. Nothing for the AI to do -> finish inline.
        if not has_structured_answers(attempt, exam_marks):
            result = finalize_attempt(attempt.id)
            return Response({"message": "Exam submitted and graded.", **result},
                            status=status.HTTP_200_OK)

        task_id = enqueue(grade_exam_attempt_task, [attempt.id], request.user)
        if task_id is None:
            # The attempt is safely SUBMITTED; grading can be re-queued later
            # (the task is idempotent), so don't fail the student's submission.
            return Response(
                {"message": "Exam submitted. Marking is delayed and will complete shortly.",
                 "attempt_id": attempt.id, "status": attempt.status},
                status=status.HTTP_202_ACCEPTED,
            )
        return Response(
            {"message": "Exam submitted. Marking is in progress.",
             "attempt_id": attempt.id, "status": attempt.status, "task_id": task_id},
            status=status.HTTP_202_ACCEPTED,
        )


# =============================================================================
# Research guidance
# =============================================================================

class ResearchGuidanceView(APIView):
    """POST /api/research/guidance/  Body: {"submission_id": 1}"""
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [AICallThrottle]

    def post(self, request):
        submission_id = parse_positive_int(request.data.get("submission_id"))
        if submission_id is None:
            return Response({"error": "submission_id is required and must be a positive integer."},
                            status=status.HTTP_400_BAD_REQUEST)

        submissions = ResearchSubmission.objects.select_related("student__user", "document", "rubric")
        if not is_admin(request.user):
            submissions = submissions.filter(student__user=request.user)
        submission = get_object_or_404(submissions, pk=submission_id)

        try:
            check_research_ready(submission)
        except AIRequestError as exc:
            return Response({"error": str(exc)}, status=exc.status_code)

        # Billed to the submission's owner, so check *their* balance.
        if not can_afford(submission.student.user, submission.document.content_text,
                          RESEARCH_MAX_TOKENS):
            return Response({"error": "Insufficient AI tokens."},
                            status=status.HTTP_402_PAYMENT_REQUIRED)

        task_id = enqueue(evaluate_research_task, [submission.id], request.user)
        return accepted(task_id) if task_id else queue_unavailable()


# =============================================================================
# Token balance & purchase
# =============================================================================

class TokenBalanceView(APIView):
    """GET /api/ai/balance/"""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        credit, _ = UserAICredit.objects.get_or_create(user=request.user)
        return Response(UserAICreditSerializer(credit).data)


class PurchaseTokensView(APIView):
    """
    POST /api/ai/purchase/  Body: {"package_id": 1} OR {"amount_cents": 500}

    SIMULATION ONLY (settings.AI_SIMULATE_PAYMENTS, default DEBUG). Tokens are
    computed server-side. In production, credit tokens only from a verified
    payment-gateway webhook that calls PaymentTransaction.mark_successful().
    """
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [PurchaseThrottle]

    def post(self, request):
        if not getattr(settings, "AI_SIMULATE_PAYMENTS", settings.DEBUG):
            return Response({"error": "Online payments are not available yet."},
                            status=status.HTTP_501_NOT_IMPLEMENTED)

        config = AIServiceConfig.load()
        raw_package_id = request.data.get("package_id")

        if raw_package_id not in (None, ""):
            package_id = parse_positive_int(raw_package_id)
            if package_id is None:
                return Response({"error": "package_id must be a positive integer."},
                                status=status.HTTP_400_BAD_REQUEST)
            package = get_object_or_404(AIPurchasePackage, pk=package_id, is_active=True)
            amount_cents, tokens = package.price_cents, package.tokens
        else:
            if not config.allow_pay_as_you_go:
                return Response({"error": "Custom token purchases are currently disabled."},
                                status=status.HTTP_403_FORBIDDEN)
            amount_cents = parse_positive_int(request.data.get("amount_cents"))
            if amount_cents is None:
                return Response({"error": "Provide package_id, or a positive integer amount_cents."},
                                status=status.HTTP_400_BAD_REQUEST)
            tokens = amount_cents // max(1, config.token_cost_cents)
            if tokens < 1:
                return Response({"error": "Amount is too small to buy any tokens."},
                                status=status.HTTP_400_BAD_REQUEST)
            claimed = request.data.get("tokens")
            if claimed not in (None, "") and parse_positive_int(claimed) != tokens:
                return Response({"error": "tokens does not match the current price; "
                                          "omit it and send amount_cents only."},
                                status=status.HTTP_400_BAD_REQUEST)
            if (config.max_purchasable_tokens is not None
                    and tokens > config.max_purchasable_tokens):
                return Response({"error": f"Cannot purchase more than "
                                          f"{config.max_purchasable_tokens} tokens at once."},
                                status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            payment = PaymentTransaction.objects.create(
                user=request.user,
                amount_cents=amount_cents,
                tokens_purchased=tokens,
                status=PaymentTransaction.Status.PENDING,
                transaction_id=f"SIM-{uuid.uuid4().hex}",
            )
            payment.mark_successful()

        credit, _ = UserAICredit.objects.get_or_create(user=request.user)
        return Response(
            {"message": "Payment successful (simulated).", "tokens_purchased": tokens,
             "balance": UserAICreditSerializer(credit).data},
            status=status.HTTP_200_OK,
        )


# =============================================================================
# AI Configuration (admin only for writes)
# =============================================================================

class AIServiceConfigView(APIView):
    """GET: any authenticated user. PUT/PATCH: admins only (partial updates)."""

    def get_permissions(self):
        if self.request.method in permissions.SAFE_METHODS:
            return [permissions.IsAuthenticated()]
        return [permissions.IsAuthenticated(), IsAppAdmin()]

    def get(self, request):
        return Response(AIServiceConfigSerializer(AIServiceConfig.load()).data)

    def _update(self, request):
        serializer = AIServiceConfigSerializer(AIServiceConfig.load(), data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def put(self, request):
        return self._update(request)

    def patch(self, request):
        return self._update(request)


# =============================================================================
# Document viewset
# =============================================================================

class DocumentViewSet(viewsets.ModelViewSet):
    """Reads filtered by visibility; writes limited to uploader/admin."""
    serializer_class = DocumentSerializer
    permission_classes = [permissions.IsAuthenticated, IsUploaderOrAdminForWrites]

    def get_queryset(self):
        return Document.objects.visible_to(self.request.user).with_uploader()

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["request"] = self.request
        return context

    def _validate_course(self, serializer):
        course = serializer.validated_data.get("course")
        if course is not None and not user_can_use_course(self.request.user, course):
            raise PermissionDenied("You cannot attach documents to this course.")

    @staticmethod
    def _validate_file(serializer):
        upload = serializer.validated_data.get("file")
        if upload is None:
            return
        allowed = getattr(settings, "DOCUMENT_ALLOWED_EXTENSIONS",
                          {".pdf", ".doc", ".docx", ".txt", ".md", ".ppt", ".pptx"})
        max_bytes = getattr(settings, "DOCUMENT_MAX_UPLOAD_BYTES", 25 * 1024 * 1024)
        extension = os.path.splitext(upload.name)[1].lower()
        if extension not in allowed:
            raise DRFValidationError({"file": [f"File type '{extension}' is not allowed."]})
        if upload.size > max_bytes:
            raise DRFValidationError(
                {"file": [f"File is too large (max {max_bytes // (1024 * 1024)} MB)."]}
            )

    def perform_create(self, serializer):
        self._validate_course(serializer)
        self._validate_file(serializer)
        serializer.save(uploaded_by=self.request.user)
        # TODO: queue a Celery task to extract text into content_text
        # (the post_save signal then refreshes search_vector).

    def perform_update(self, serializer):
        self._validate_course(serializer)
        self._validate_file(serializer)
        serializer.save()
