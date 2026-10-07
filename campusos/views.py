# views.py
"""
API views for the academic platform. The AI work itself lives in
ai_services.py and runs in Celery workers (tasks.py); these views validate,
enqueue, and return 202 with a task_id.

Route map is documented in campusos/urls.py.
"""
import logging
import os
import uuid
from datetime import datetime


from celery import states
from celery.result import AsyncResult

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Avg, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone

from rest_framework import status, viewsets, permissions, generics
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError as DRFValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle
from rest_framework.views import APIView

from .ai_services import (
    AIRequestError, can_afford, check_highlight_ready, check_research_ready,
    finalize_attempt, get_exam_question_marks, grade_mcq_answers,
    has_structured_answers, late_note, new_task_id, reset_config_cache,
    expire_attempt,
    HIGHLIGHT_MAX_TOKENS, RESEARCH_MAX_TOKENS,
)
from .auto_evaluate import kick_off_evaluation
from .models import (
    User, Course, Document, ExamAttempt, Enrollment, ResearchSubmission,
    AIServiceConfig, UserAICredit, PaymentTransaction, AIPurchasePackage,
    QuestionBank, Question, Exam, Rubric, RubricEvaluation,
    ExamQuestion, Answer,
    CorrectionReport, StudentProfile,
)
from .serializers import (
    DocumentSerializer,
    DocumentSearchResultSerializer,
    UserAICreditSerializer,
    AIServiceConfigSerializer,
    UserSerializer,
    RegisterSerializer,
    CourseSerializer,
    EnrollmentSerializer,
    QuestionBankSerializer,
    QuestionSerializer,
    ExamSerializer,
    ExamQuestionSerializer,
    RubricSerializer,
    ResearchSubmissionSerializer,
    ResearchSubmissionCreateSerializer,
    ResearchSubmissionApproveSerializer,
    SubmissionRosterSerializer,
    AnswerUpsertSerializer,
    AnswerDetailSerializer,
    ExamAttemptDetailSerializer,
    ExamAttemptForLecturerSerializer,
    ExamAttemptStartSerializer,
    CorrectionReportSerializer,
    PaymentTransactionStatusSerializer,
    AIPurchasePackageSerializer,
)
from .services.document_search import (
    SearchParams, search_documents, build_facets, suggest_documents,
)
from .services.pesapal_service import (
    PesapalError, register_ipn_url, get_transaction_status, submit_order,
)
from .tasks import (
    highlight_document_task, grade_exam_attempt_task, evaluate_research_task,
    generate_correction_report_task,
)
from .utils.text_extraction import extract_text, extract_text_and_pages

logger = logging.getLogger(__name__)

GENERIC_AI_ERROR = "AI processing failed. Please try again later."
INFLIGHT_STATES = {states.PENDING, states.RECEIVED, states.STARTED, states.RETRY}
INFLIGHT_TTL = 120


# -----------------------------------------------------------------------------
# Throttles
# -----------------------------------------------------------------------------

class AICallThrottle(UserRateThrottle):
    scope = "ai_calls"


class PurchaseThrottle(UserRateThrottle):
    scope = "ai_purchase"


class RegisterThrottle(AnonRateThrottle):
    scope = "register"


# -----------------------------------------------------------------------------
# Helpers & permissions
# -----------------------------------------------------------------------------

def parse_positive_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _parse_iso_date(raw):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def is_student(user):
    return user.is_authenticated and user.role == User.Role.STUDENT


def is_lecturer(user):
    return user.is_authenticated and user.role == User.Role.LECTURER


def is_admin(user):
    return user.is_authenticated and (user.is_superuser or user.role == User.Role.ADMIN)


def eager_mode():
    return getattr(settings, "CELERY_TASK_ALWAYS_EAGER", True)


def debug_mode():
    return bool(getattr(settings, "DEBUG", False))


class IsAppAdmin(permissions.BasePermission):
    def has_permission(self, request, view):
        return is_admin(request.user)


class IsLecturerOrAdmin(permissions.BasePermission):
    def has_permission(self, request, view):
        return is_lecturer(request.user) or is_admin(request.user)


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


def _run_inline(task, args, task_id):
    try:
        result = task.apply(args=args, task_id=task_id).get(disable_sync_subtasks=False)
        return "sync", result
    except Exception as exc:
        logger.exception(
            "Inline task %s FAILED | args=%r | error=%s",
            getattr(task, "name", task), args, exc,
        )
        return "error", exc


def dispatch(task, args, user, dedupe_key=None):
    task_id = new_task_id(user)

    if eager_mode():
        return _run_inline(task, args, task_id)

    if dedupe_key:
        try:
            existing = cache.get(dedupe_key)
            if existing and AsyncResult(existing).state in INFLIGHT_STATES:
                return "async", existing
        except Exception as exc:
            logger.warning("Dedupe lookup failed (%s); enqueueing anyway.", exc)

    try:
        ar = task.apply_async(args=args, task_id=task_id)
    except Exception as exc:
        logger.exception(
            "Enqueue %s FAILED | args=%r | error=%s",
            getattr(task, "name", task), args, exc,
        )
        if getattr(settings, "AI_FALLBACK_TO_EAGER", False):
            logger.warning("Broker unreachable; running %s inline.", getattr(task, "name", task))
            return _run_inline(task, args, task_id)
        return "error", exc

    if dedupe_key:
        try:
            cache.set(dedupe_key, ar.id, INFLIGHT_TTL)
        except Exception as exc:
            logger.warning("Dedupe store failed: %s", exc)
    return "async", ar.id


def task_error_response(exc):
    if debug_mode():
        return Response(
            {"error": f"AI task failed: {type(exc).__name__}: {exc}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    return Response(
        {"error": "The AI service is temporarily unavailable. Please try again shortly."},
        status=status.HTTP_503_SERVICE_UNAVAILABLE,
    )


def task_failed_response(payload):
    if not isinstance(payload, dict):
        payload = {}
    code = payload.get("code")
    if not isinstance(code, int) or not 400 <= code <= 599:
        code = status.HTTP_503_SERVICE_UNAVAILABLE
    return Response({"error": payload.get("error") or GENERIC_AI_ERROR}, status=code)


def task_ok(payload):
    return isinstance(payload, dict) and payload.get("ok") is True


def accepted(task_id):
    return Response(
        {"task_id": task_id, "status": "queued"},
        status=status.HTTP_202_ACCEPTED,
    )


def _perform_exam_submit(request, attempt):
    if attempt.status != ExamAttempt.Status.IN_PROGRESS:
        return Response(
            {"error": "Attempt is already submitted or graded."},
            status=status.HTTP_400_BAD_REQUEST,
        )

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

    if not has_structured_answers(attempt, exam_marks):
        result = finalize_attempt(attempt.id, exam_marks)
        attempt.refresh_from_db()
        return Response(
            {
                "message": "Exam submitted and graded.",
                "attempt_id": attempt.id,
                "status": attempt.status,
                "score": str(attempt.score) if attempt.score is not None else None,
                **result,
            },
            status=status.HTTP_200_OK,
        )

    mode, payload = dispatch(grade_exam_attempt_task, [attempt.id], request.user)

    if mode == "sync" and task_ok(payload):
        attempt.refresh_from_db()
        graded = attempt.status == ExamAttempt.Status.GRADED
        return Response(
            {
                "message": ("Exam submitted and graded." if graded else
                            "Exam submitted. Some answers await manual grading."),
                "attempt_id": attempt.id,
                "status": attempt.status,
                "score": str(attempt.score) if attempt.score is not None else None,
                "pending_manual_grading":
                    (payload.get("data") or {}).get("pending_manual_grading"),
            },
            status=status.HTTP_200_OK,
        )
    if mode == "async":
        return Response(
            {"message": "Exam submitted. Marking is in progress.",
             "attempt_id": attempt.id, "status": attempt.status,
             "task_id": payload},
            status=status.HTTP_202_ACCEPTED,
        )

    resp = (task_failed_response(payload) if mode == "sync"
            else task_error_response(payload))
    resp.data["attempt_id"] = attempt.id
    resp.data["status"] = attempt.status
    resp.data["message"] = "Exam submitted but AI marking failed."
    return resp


def _grade_band(pct):
    if pct is None:
        return None
    if pct >= 80:
        return "A"
    if pct >= 70:
        return "B"
    if pct >= 60:
        return "C"
    if pct >= 50:
        return "D"
    return "F"


# =============================================================================
# Current user
# =============================================================================

class UserMeView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)


# =============================================================================
# Registration
# =============================================================================

class RegisterView(generics.CreateAPIView):
    serializer_class = RegisterSerializer
    permission_classes = [AllowAny]
    throttle_classes = [RegisterThrottle]


# =============================================================================
# Task status
# =============================================================================

class AITaskStatusView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, task_id):
        if not is_admin(request.user) and not task_id.startswith(f"u{request.user.id}-"):
            return Response({"error": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        result = AsyncResult(task_id)
        state = result.state
        ready = state in states.READY_STATES
        if state == states.SUCCESS:
            payload = result.result if isinstance(result.result, dict) else {}
            return Response({"state": state, "ready": True, **payload})
        if state in (states.FAILURE, states.REVOKED):
            return Response({"state": state, "ready": True, "ok": False,
                             "error": GENERIC_AI_ERROR})
        return Response({"state": state, "ready": ready})


# =============================================================================
# AI Highlighting
# =============================================================================

class AIHighlightView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [AICallThrottle]

    def post(self, request):
        document_id = parse_positive_int(request.data.get("document_id"))
        if document_id is None:
            return Response(
                {"error": "document_id is required and must be a positive integer."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        document = get_object_or_404(
            Document.objects.visible_to(request.user), pk=document_id
        )

        try:
            check_highlight_ready(document)
        except AIRequestError as exc:
            return Response({"error": str(exc)}, status=exc.status_code)

        if not can_afford(request.user, document.content_text, HIGHLIGHT_MAX_TOKENS):
            return Response(
                {"error": "Insufficient AI tokens. Please purchase more."},
                status=status.HTTP_402_PAYMENT_REQUIRED,
            )

        mode, payload = dispatch(
            highlight_document_task,
            [document.id, request.user.id],
            request.user,
            dedupe_key=f"ai:inflight:highlight:{request.user.id}:{document.id}",
        )

        if mode == "sync":
            if not task_ok(payload):
                return task_failed_response(payload)
            return Response(
                {"status": "completed", "result": payload},
                status=status.HTTP_200_OK,
            )
        if mode == "async":
            return accepted(payload)
        return task_error_response(payload)


# =============================================================================
# Exam submission (legacy alias)
# =============================================================================

class ExamSubmitView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [AICallThrottle]

    def post(self, request):
        attempt_id = parse_positive_int(request.data.get("attempt_id"))
        if attempt_id is None:
            return Response(
                {"error": "attempt_id is required and must be a positive integer."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        attempts = ExamAttempt.objects.select_related("exam", "student__user")
        if not is_admin(request.user):
            attempts = attempts.filter(student__user=request.user)

        with transaction.atomic():
            attempt = get_object_or_404(
                attempts.select_for_update(of=("self",)), pk=attempt_id
            )
            return _perform_exam_submit(request, attempt)


# =============================================================================
# Research guidance
# =============================================================================

class ResearchGuidanceView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [AICallThrottle]

    def post(self, request):
        submission_id = parse_positive_int(request.data.get("submission_id"))
        if submission_id is None:
            return Response(
                {"error": "submission_id is required and must be a positive integer."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        submissions = ResearchSubmission.objects.select_related(
            "student__user", "document", "rubric"
        )
        if not is_admin(request.user):
            submissions = submissions.filter(student__user=request.user)
        submission = get_object_or_404(submissions, pk=submission_id)

        try:
            check_research_ready(submission)
        except AIRequestError as exc:
            return Response({"error": str(exc)}, status=exc.status_code)

        if not can_afford(
            submission.student.user,
            submission.document.content_text,
            RESEARCH_MAX_TOKENS,
        ):
            return Response(
                {"error": "Insufficient AI tokens."},
                status=status.HTTP_402_PAYMENT_REQUIRED,
            )

        mode, payload = dispatch(
            evaluate_research_task, [submission.id], request.user,
            dedupe_key=f"ai:inflight:research:{request.user.id}:{submission.id}",
        )

        if mode == "sync":
            if not task_ok(payload):
                return task_failed_response(payload)
            submission.refresh_from_db()
            return Response(
                {
                    "status": "completed",
                    "submission_id": submission.id,
                    "evaluation_status": submission.evaluation_status,
                    "overall_score": (
                        str(submission.overall_score)
                        if submission.overall_score is not None else None
                    ),
                },
                status=status.HTTP_200_OK,
            )
        if mode == "async":
            return accepted(payload)
        return task_error_response(payload)


# =============================================================================
# Token balance & purchase
# =============================================================================

class TokenBalanceView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        credit, _ = UserAICredit.objects.get_or_create(user=request.user)
        return Response(UserAICreditSerializer(credit).data)


class AIPurchasePackageListView(generics.ListAPIView):
    """
    GET /api/ai/packages/
    Read-only list of active token bundles for the /tokens page.
    """
    serializer_class = AIPurchasePackageSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = None

    def get_queryset(self):
        return AIPurchasePackage.objects.filter(is_active=True)


class PurchaseTokensView(APIView):
    """
    POST /api/ai/purchase/   { package_id }   OR   { amount_cents }

    Response 200:
        { payment_url, transaction_id, reference }

    The frontend redirects the browser to `payment_url`. When the student
    pays, Pesapal redirects them back to the callback URL and separately
    fires the IPN webhook (PesapalIPNView), which credits the tokens.
    """
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [PurchaseThrottle]

    def post(self, request):
        if (not settings.PESAPAL_CONSUMER_KEY
                or not settings.PESAPAL_CONSUMER_SECRET):
            return Response(
                {"error": "Payments are not configured. Contact the administrator."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        if not settings.PESAPAL_IPN_ID:
            return Response(
                {"error": "Payment gateway is not ready (missing IPN ID). "
                          "Ask the admin to run /api/payments/register-ipn/."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        config = AIServiceConfig.load()
        raw_package_id = request.data.get("package_id")

        # ── Resolve amount + tokens ────────────────────────────
        if raw_package_id not in (None, ""):
            package_id = parse_positive_int(raw_package_id)
            if package_id is None:
                return Response(
                    {"error": "package_id must be a positive integer."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            package = get_object_or_404(
                AIPurchasePackage, pk=package_id, is_active=True
            )
            amount_cents, tokens = package.price_cents, package.tokens
        else:
            if not config.allow_pay_as_you_go:
                return Response(
                    {"error": "Custom token purchases are currently disabled."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            amount_cents = parse_positive_int(request.data.get("amount_cents"))
            if amount_cents is None:
                return Response(
                    {"error": "Provide package_id, or a positive integer "
                              "amount_cents."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            tokens = amount_cents // max(1, config.token_cost_cents)
            if tokens < 1:
                return Response(
                    {"error": "Amount is too small to buy any tokens."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if (config.max_purchasable_tokens is not None
                    and tokens > config.max_purchasable_tokens):
                return Response(
                    {"error": f"Cannot purchase more than "
                              f"{config.max_purchasable_tokens} tokens at once."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # ── Billing info from the student ──────────────────────
        billing = {
            "email_address": request.user.email or "noemail@campusos.local",
            "phone_number": request.data.get("phone_number") or "0700000000",
            "country_code": request.data.get("country_code") or "UG",
            "first_name": request.user.first_name or request.user.username,
            "last_name": request.user.last_name or "User",
            "line1": request.data.get("line1") or "Kampala",
        }

        # ── Create the PENDING transaction row ─────────────────
        reference = f"campusos-{uuid.uuid4().hex[:20]}"
        amount_major = amount_cents / 100.0   # Pesapal expects major units

        payment = PaymentTransaction.objects.create(
            user=request.user,
            amount_cents=amount_cents,
            tokens_purchased=tokens,
            status=PaymentTransaction.Status.PENDING,
            transaction_id=reference,
            pesapal_merchant_reference=reference,
        )

        # ── Submit to Pesapal ──────────────────────────────────
        try:
            data = submit_order(
                merchant_reference=reference,
                amount=amount_major,
                currency="UGX",  # change to KES/TZS as needed
                description=f"{tokens} CampusOS AI tokens",
                callback_url=settings.PESAPAL_CALLBACK_URL,
                notification_id=settings.PESAPAL_IPN_ID,
                billing=billing,
            )
        except PesapalError as exc:
            logger.exception("Pesapal submit failed for %s", reference)
            payment.mark_failed(reason=str(exc))
            return Response(
                {"error": str(exc)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        # ── Persist Pesapal identifiers for the IPN handler ────
        payment.pesapal_tracking_id = data.get("order_tracking_id")
        payment.redirect_url = data.get("redirect_url")
        payment.raw_pesapal_response = data
        payment.save(update_fields=[
            "pesapal_tracking_id", "redirect_url", "raw_pesapal_response",
        ])

        return Response(
            {
                "payment_url": data.get("redirect_url"),
                "transaction_id": payment.id,
                "reference": reference,
            },
            status=status.HTTP_200_OK,
        )


# =============================================================================
# Pesapal payment views
# =============================================================================

class PesapalIPNView(APIView):
    """
    Webhook endpoint Pesapal calls when a transaction status changes.
    Must respond 200 quickly; Pesapal retries on failure.

    Accepts both GET query params and POST body — Pesapal's IPN shape
    depends on how the URL was registered.
    """
    permission_classes = [AllowAny]
    authentication_classes = []

    def _extract_ids(self, request):
        data = request.data or {}
        query = request.query_params or {}

        def pick(key):
            return data.get(key) or query.get(key)

        return {
            "tracking_id": pick("OrderTrackingId")
                           or pick("pesapal_transaction_tracking_id"),
            "merchant_ref": pick("OrderMerchantReference")
                            or pick("pesapal_merchant_reference"),
            "notification_type": pick("OrderNotificationType")
                                 or pick("pesapal_notification_type"),
        }

    def _handle(self, request):
        ids = self._extract_ids(request)
        tracking_id = ids["tracking_id"]
        merchant_ref = ids["merchant_ref"]

        logger.info(
            "Pesapal IPN received: tracking=%s ref=%s type=%s",
            tracking_id, merchant_ref, ids["notification_type"],
        )

        if not tracking_id or not merchant_ref:
            return Response(
                {"error": "Missing tracking id or merchant reference."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        payment = PaymentTransaction.objects.filter(
            pesapal_merchant_reference=merchant_ref
        ).first()
        if payment is None:
            logger.warning("Pesapal IPN for unknown reference %s", merchant_ref)
            # Still respond 200 so Pesapal doesn't retry forever.
            return Response({"status": "ignored"})

        # Idempotent: if already SUCCESS, don't double-credit
        if payment.status == PaymentTransaction.Status.SUCCESS:
            return Response({"status": "already-processed"})

        # ── Verify with Pesapal (never trust the webhook body) ─
        try:
            status_data = get_transaction_status(tracking_id)
        except PesapalError as exc:
            logger.exception("IPN status verify failed for %s", merchant_ref)
            return Response(
                {"error": str(exc)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        payment.raw_pesapal_response = status_data
        payment.save(update_fields=["raw_pesapal_response"])

        status_code = int(status_data.get("status_code", 0))
        description = (
            status_data.get("payment_status_description") or ""
        ).lower()

        if status_code == 1 and "completed" in description:
            payment.mark_successful()
            logger.info(
                "Pesapal payment SUCCESS: %s → +%s tokens",
                merchant_ref, payment.tokens_purchased,
            )
        elif status_code in (2, 0) and (
            "failed" in description or "invalid" in description
        ):
            payment.mark_failed(reason=description or "Pesapal reported failure")
            logger.warning("Pesapal payment FAILED: %s", merchant_ref)
        else:
            logger.info(
                "Pesapal payment still pending: %s (%s)",
                merchant_ref, status_data.get("payment_status_description"),
            )

        return Response({
            "orderNotificationType": ids["notification_type"] or "IPNCHANGE",
            "orderTrackingId": tracking_id,
            "orderMerchantReference": merchant_ref,
            "status": 200,
        })

    def get(self, request):
        return self._handle(request)

    def post(self, request):
        return self._handle(request)


class PaymentStatusView(APIView):
    """
    Frontend polls this after returning from the Pesapal redirect.
    GET /api/payments/<transaction_id>/status/

    If the local row is still PENDING, this view actively asks Pesapal
    for the current status and syncs the row. That way a broken/missing
    IPN (or a dead tunnel) doesn't leave payments stuck forever.

    A short cache guard prevents the frontend's ~2s poll from hammering
    Pesapal's API — we only hit them once per cooldown window per row.
    """
    permission_classes = [permissions.IsAuthenticated]

    # Minimum seconds between Pesapal lookups for the same transaction.
    PESAPAL_LOOKUP_COOLDOWN = 10

    def get(self, request, transaction_id):
        payment = get_object_or_404(
            PaymentTransaction, pk=transaction_id, user=request.user
        )

        # Terminal states — nothing to do, return what we have.
        if payment.status in (
            PaymentTransaction.Status.SUCCESS,
            PaymentTransaction.Status.FAILED,
            PaymentTransaction.Status.REFUNDED,
        ):
            return Response(PaymentTransactionStatusSerializer(payment).data)

        # PENDING: only hit Pesapal if we haven't checked recently.
        cache_key = f"pesapal:status-check:{payment.id}"
        if payment.pesapal_tracking_id and not cache.get(cache_key):
            cache.set(cache_key, 1, self.PESAPAL_LOOKUP_COOLDOWN)
            try:
                status_data = get_transaction_status(
                    payment.pesapal_tracking_id
                )
                code = int(status_data.get("status_code", 0))
                desc = (
                    status_data.get("payment_status_description") or ""
                ).lower()
                err = status_data.get("error") or {}
                err_code = (err.get("code") or "").lower()

                payment.raw_pesapal_response = status_data
                payment.save(update_fields=["raw_pesapal_response"])

                if code == 1 and "completed" in desc:
                    payment.mark_successful()
                    logger.info(
                        "Payment %s self-healed via poll → SUCCESS, +%s tokens",
                        payment.id, payment.tokens_purchased,
                    )
                elif code in (2, 3) and (
                    "failed" in desc
                    or "invalid" in desc
                    or "reversed" in desc
                ):
                    payment.mark_failed(
                        reason=desc or "Pesapal reported failure"
                    )
                    logger.info(
                        "Payment %s marked FAILED via poll", payment.id
                    )
                elif (
                    err_code == "payment_details_not_found"
                    or "invalid" in desc
                ):
                    # Order exists on Pesapal, but no payment was ever
                    # attempted against it. Student opened the Pesapal
                    # page and left without paying. Resolve to FAILED so
                    # the frontend stops polling.
                    payment.mark_failed(
                        reason="No payment attempted"
                    )
                    logger.info(
                        "Payment %s marked FAILED (abandoned) via poll",
                        payment.id,
                    )
                # else: still genuinely pending on Pesapal — leave as-is.
            except PesapalError as exc:
                logger.warning(
                    "Poll could not verify payment %s with Pesapal: %s",
                    payment.id, exc,
                )

        payment.refresh_from_db()
        return Response(PaymentTransactionStatusSerializer(payment).data)


class RegisterIPNView(APIView):
    """
    One-time admin endpoint: register our IPN URL with Pesapal.
    POST /api/payments/register-ipn/  →  { ipn_id, message }
    """
    permission_classes = [permissions.IsAuthenticated, IsAppAdmin]

    def post(self, request):
        try:
            ipn_id = register_ipn_url(settings.PESAPAL_IPN_URL)
        except PesapalError as exc:
            return Response(
                {"error": str(exc)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response({
            "ipn_id": ipn_id,
            "message": "Copy this into .env as PESAPAL_IPN_ID and restart Django.",
        })


# =============================================================================
# AI Configuration
# =============================================================================

class AIServiceConfigView(APIView):
    def get_permissions(self):
        if self.request.method in permissions.SAFE_METHODS:
            return [permissions.IsAuthenticated()]
        return [permissions.IsAuthenticated(), IsAppAdmin()]

    def get(self, request):
        return Response(AIServiceConfigSerializer(AIServiceConfig.load()).data)

    def _update(self, request):
        serializer = AIServiceConfigSerializer(
            AIServiceConfig.load(), data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        reset_config_cache()
        return Response(serializer.data)

    def put(self, request):
        return self._update(request)

    def patch(self, request):
        return self._update(request)


# =============================================================================
# Document viewset
# =============================================================================

class DocumentViewSet(viewsets.ModelViewSet):
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
            raise DRFValidationError(
                {"file": [f"File type '{extension}' is not allowed."]}
            )
        if upload.size > max_bytes:
            raise DRFValidationError(
                {"file": [f"File is too large (max {max_bytes // (1024 * 1024)} MB)."]}
            )

    def perform_create(self, serializer):
        self._validate_course(serializer)
        self._validate_file(serializer)
        document = serializer.save(uploaded_by=self.request.user)
        text, pages = extract_text_and_pages(document.file)
        document.content_text = text
        document.content_pages = pages
        document.save(update_fields=["content_text", "content_pages"])

    def perform_update(self, serializer):
        self._validate_course(serializer)
        self._validate_file(serializer)
        replacing_file = "file" in serializer.validated_data
        document = serializer.save()
        if replacing_file:
            text, pages = extract_text_and_pages(document.file)
            document.content_text = text
            document.content_pages = pages
            document.save(update_fields=["content_text", "content_pages"])


# =============================================================================
# Document search & suggestions
# =============================================================================

class DocumentSearchView(generics.ListAPIView):
    serializer_class = DocumentSearchResultSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs, _ = search_documents(self.request.user, self._params())
        return qs

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        try:
            response.data["facets"] = build_facets(request.user, self._params())
        except Exception:
            logger.exception("Facet computation failed")
            response.data["facets"] = []
        return response

    def _params(self):
        qp = self.request.query_params
        return SearchParams(
            query=qp.get("q"),
            category=qp.get("category"),
            visibility=qp.get("visibility"),
            course_id=parse_positive_int(qp.get("course_id")),
            uploaded_by_id=parse_positive_int(qp.get("uploaded_by")),
            ordering=qp.get("ordering", "relevance"),
        )


class DocumentSuggestView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        prefix = request.query_params.get("q", "")
        limit = min(parse_positive_int(request.query_params.get("limit")) or 10, 25)
        suggestions = suggest_documents(request.user, prefix, limit)
        return Response({"suggestions": suggestions})


# =============================================================================
# Lecturer dashboard
# =============================================================================

class LecturerDashboardView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user

        if not (is_lecturer(user) or is_admin(user)):
            return Response(
                {"error": "Lecturer access required."},
                status=status.HTTP_403_FORBIDDEN,
            )

        my_courses = Course.objects.filter(lecturer__user=user)
        my_documents = Document.objects.filter(uploaded_by=user)
        my_banks = QuestionBank.objects.filter(created_by=user)
        my_exams = Exam.objects.filter(course__lecturer__user=user)
        my_rubrics = Rubric.objects.filter(created_by=user)

        # Match submissions by course lecturer OR rubric author, so any
        # legacy course-less submissions still surface to their rubric author.
        to_review = (
            ResearchSubmission.objects
            .filter(
                Q(course__lecturer__user=user) | Q(rubric__created_by=user),
                evaluation_status=ResearchSubmission.Status.EVALUATED,
            )
            .distinct()
        )

        credit, _ = UserAICredit.objects.get_or_create(user=user)

        stats = {
            "courses": my_courses.count(),
            "documents": my_documents.count(),
            "question_banks": my_banks.count(),
            "questions": Question.objects.filter(bank__created_by=user).count(),
            "exams": my_exams.count(),
            "published_exams": my_exams.filter(is_published=True).count(),
            "rubrics": my_rubrics.count(),
            "pending_submissions": to_review.count(),
            "tokens_remaining": credit.remaining_tokens,
        }

        recent_docs = list(
            my_documents.order_by("-created_at")[:5]
            .values("id", "title", "category", "created_at")
        )

        recent_exams = list(
            my_exams.order_by("-created_at")[:5].values(
                "id", "title", "is_published", "created_at",
                "course__code", "course__title",
            )
        )
        for e in recent_exams:
            e["course"] = {
                "code": e.pop("course__code"),
                "title": e.pop("course__title"),
            }

        pending = list(
            to_review.order_by("-submitted_at")[:5].values(
                "id", "submitted_at", "evaluation_status",
                "student__student_id",
                "student__user__first_name",
                "student__user__last_name",
                "document__title",
            )
        )
        for p in pending:
            sid = p.pop("student__student_id")
            fn = p.pop("student__user__first_name")
            ln = p.pop("student__user__last_name")
            p["student"] = {
                "student_id": sid,
                "name": f"{fn} {ln}".strip() or sid,
            }
            p["document_title"] = p.pop("document__title")

        courses = list(
            my_courses.order_by("code")[:6].values("id", "code", "title", "department")
        )

        return Response({
            "stats": stats,
            "courses": courses,
            "recent_documents": recent_docs,
            "recent_exams": recent_exams,
            "pending_submissions": pending,
        })


# =============================================================================
# Student dashboard
# =============================================================================

class StudentDashboardView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user

        if not is_student(user):
            return Response(
                {"error": "Student access required."},
                status=status.HTTP_403_FORBIDDEN,
            )

        profile = getattr(user, "student_profile", None)
        if profile is None:
            return Response(
                {"error": "No student profile found for this account."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        my_enrollments = Enrollment.objects.filter(student=profile).select_related(
            "course", "course__lecturer__user"
        )
        my_submissions = ResearchSubmission.objects.filter(student=profile)
        my_documents = Document.objects.filter(uploaded_by=user)
        my_attempts = ExamAttempt.objects.filter(student=profile).select_related(
            "exam", "exam__course"
        )

        credit, _ = UserAICredit.objects.get_or_create(user=user)

        stats = {
            "enrolled_courses": my_enrollments.count(),
            "documents": my_documents.count(),
            "submissions": my_submissions.count(),
            "pending_evaluation": my_submissions.filter(
                evaluation_status=ResearchSubmission.Status.PENDING
            ).count(),
            "evaluated": my_submissions.filter(
                evaluation_status=ResearchSubmission.Status.EVALUATED
            ).count(),
            "reviewed": my_submissions.filter(
                evaluation_status=ResearchSubmission.Status.REVIEWED
            ).count(),
            "tokens_remaining": credit.remaining_tokens,
            "exams_attempted": my_attempts.count(),
            "exams_graded": my_attempts.filter(
                status=ExamAttempt.Status.GRADED
            ).count(),
            "exams_in_progress": my_attempts.filter(
                status=ExamAttempt.Status.IN_PROGRESS
            ).count(),
        }

        recent_docs = list(
            my_documents.order_by("-created_at")[:5]
            .values("id", "title", "category", "created_at")
        )

        recent_subs = list(
            my_submissions.order_by("-submitted_at")[:5].values(
                "id", "submitted_at", "evaluation_status", "overall_score",
                "document__title",
                "course__code", "course__title",
            )
        )
        for s in recent_subs:
            s["document_title"] = s.pop("document__title")
            code = s.pop("course__code")
            title = s.pop("course__title")
            s["course"] = {"code": code, "title": title} if code else None

        courses = list(
            my_enrollments.order_by("course__code").values(
                "course__id", "course__code", "course__title",
                "course__department", "semester",
                "course__lecturer__user__first_name",
                "course__lecturer__user__last_name",
            )[:6]
        )
        for c in courses:
            cid = c.pop("course__id")
            code = c.pop("course__code")
            title = c.pop("course__title")
            dept = c.pop("course__department")
            fn = c.pop("course__lecturer__user__first_name") or ""
            ln = c.pop("course__lecturer__user__last_name") or ""
            c["course"] = {"id": cid, "code": code, "title": title, "department": dept}
            c["lecturer_name"] = f"{fn} {ln}".strip() or None

        exam_performance = self._build_exam_performance(my_attempts)
        available_exams = self._build_available_exams(profile)
        in_progress = self._build_in_progress_attempts(my_attempts)

        return Response({
            "stats": stats,
            "courses": courses,
            "recent_documents": recent_docs,
            "recent_submissions": recent_subs,
            "exam_performance": exam_performance,
            "available_exams": available_exams,
            "in_progress_attempts": in_progress,
        })

    @staticmethod
    def _build_available_exams(profile):
        now = timezone.now()

        enrolled_course_ids = list(
            Enrollment.objects.filter(student=profile)
            .values_list("course_id", flat=True)
        )
        if not enrolled_course_ids:
            return []

        exams = (
            Exam.objects.filter(
                course_id__in=enrolled_course_ids,
                is_published=True,
            )
            .filter(Q(start_time__isnull=True) | Q(start_time__lte=now))
            .filter(Q(end_time__isnull=True) | Q(end_time__gte=now))
            .select_related("course")
            .order_by("end_time", "start_time", "title")
        )

        attempts = ExamAttempt.objects.filter(
            student=profile, exam__in=exams
        ).values("exam_id", "status", "attempt_number")

        by_exam = {}
        for a in attempts:
            by_exam.setdefault(a["exam_id"], []).append(a)

        rows = []
        for e in exams:
            exam_attempts = by_exam.get(e.id, [])
            in_progress = any(
                a["status"] == ExamAttempt.Status.IN_PROGRESS
                for a in exam_attempts
            )
            graded = any(
                a["status"] == ExamAttempt.Status.GRADED
                for a in exam_attempts
            )
            if graded and not in_progress:
                continue

            rows.append({
                "id": e.id,
                "title": e.title,
                "course_code": e.course.code if e.course else None,
                "course_title": e.course.title if e.course else None,
                "duration_minutes": e.duration_minutes,
                "total_marks": e.total_marks,
                "start_time": e.start_time.isoformat() if e.start_time else None,
                "end_time": e.end_time.isoformat() if e.end_time else None,
                "status": "in_progress" if in_progress else "not_started",
                "attempts_count": len(exam_attempts),
            })
        return rows

    @staticmethod
    def _build_in_progress_attempts(attempts_qs):
        rows = []
        for a in attempts_qs.filter(
            status=ExamAttempt.Status.IN_PROGRESS
        ).select_related("exam", "exam__course").order_by("-start_time"):
            rows.append({
                "attempt_id": a.id,
                "exam_id": a.exam_id,
                "exam_title": a.exam.title,
                "course_code": a.exam.course.code if a.exam.course else None,
                "attempt_number": a.attempt_number,
                "start_time": a.start_time.isoformat() if a.start_time else None,
                "duration_minutes": a.exam.duration_minutes,
                "total_marks": a.exam.total_marks,
                "answered_count": a.answers.count(),
                "seconds_remaining": a.seconds_remaining,
            })
        return rows

    @staticmethod
    def _build_exam_performance(attempts_qs):
        attempts = list(attempts_qs.order_by("-start_time"))

        def _percentage(attempt):
            total = attempt.exam.total_marks or 0
            if attempt.score is None or not total:
                return None
            try:
                return round(float(attempt.score) / float(total) * 100, 2)
            except (TypeError, ValueError, ZeroDivisionError):
                return None

        def _summary(attempt):
            if attempt is None:
                return None
            return {
                "attempt_id": attempt.id,
                "exam_id": attempt.exam_id,
                "exam_title": attempt.exam.title,
                "course_code": (
                    attempt.exam.course.code if attempt.exam.course else None
                ),
                "course_title": (
                    attempt.exam.course.title if attempt.exam.course else None
                ),
                "attempt_number": attempt.attempt_number,
                "status": attempt.status,
                "score": (
                    float(attempt.score) if attempt.score is not None else None
                ),
                "total_marks": attempt.exam.total_marks,
                "percentage": _percentage(attempt),
                "submitted_at": (
                    attempt.submitted_at.isoformat()
                    if attempt.submitted_at else None
                ),
                "start_time": (
                    attempt.start_time.isoformat()
                    if attempt.start_time else None
                ),
                "timed_out": attempt.timed_out,
                "abandoned_at": (
                    attempt.abandoned_at.isoformat()
                    if attempt.abandoned_at else None
                ),
                "is_flagged": attempt.is_flagged,
            }

        per_course = {}
        graded_attempts = []
        percentages = []

        for a in attempts:
            pct = _percentage(a)
            if pct is None:
                continue
            graded_attempts.append(a)
            percentages.append(pct)

            code = a.exam.course.code if a.exam.course else "—"
            title = a.exam.course.title if a.exam.course else ""
            bucket = per_course.get(code)
            if bucket is None:
                bucket = {
                    "course_code": code,
                    "course_title": title,
                    "attempts": 0,
                    "total_pct": 0.0,
                    "best": None,
                    "worst": None,
                }
                per_course[code] = bucket
            bucket["attempts"] += 1
            bucket["total_pct"] += pct
            bucket["best"] = pct if bucket["best"] is None else max(bucket["best"], pct)
            bucket["worst"] = pct if bucket["worst"] is None else min(bucket["worst"], pct)

        per_course_list = [
            {
                "course_code": b["course_code"],
                "course_title": b["course_title"],
                "attempts": b["attempts"],
                "average_percentage": round(b["total_pct"] / b["attempts"], 2),
                "best_percentage": round(b["best"], 2),
                "worst_percentage": round(b["worst"], 2),
            }
            for b in per_course.values()
        ]
        per_course_list.sort(key=lambda x: x["course_code"])

        best_attempt = None
        worst_attempt = None
        if graded_attempts:
            best_attempt = max(graded_attempts, key=lambda a: _percentage(a) or 0)
            worst_attempt = min(graded_attempts, key=lambda a: _percentage(a) or 100)

        recent_attempts = [_summary(a) for a in attempts[:5]]

        graded_chrono = sorted(
            graded_attempts,
            key=lambda a: a.submitted_at or a.start_time,
        )
        score_trend = {"x": [], "y": [], "text": [], "course_codes": []}
        for a in graded_chrono:
            ts = a.submitted_at or a.start_time
            code = a.exam.course.code if a.exam.course else "—"
            score_trend["x"].append(ts.isoformat() if ts else None)
            score_trend["y"].append(_percentage(a))
            score_trend["course_codes"].append(code)
            score_trend["text"].append(
                f"{a.exam.title} (#{a.attempt_number}) — {code}"
            )

        per_course_avg = {
            "x": [c["course_code"] for c in per_course_list],
            "y": [c["average_percentage"] for c in per_course_list],
            "text": [c["course_title"] for c in per_course_list],
        }

        per_course_spread = {
            "x": [c["course_code"] for c in per_course_list],
            "best": [c["best_percentage"] for c in per_course_list],
            "avg": [c["average_percentage"] for c in per_course_list],
            "worst": [c["worst_percentage"] for c in per_course_list],
        }

        buckets = {"A": 0, "B": 0, "C": 0, "D": 0, "F": 0}
        for p in percentages:
            if p >= 80:
                buckets["A"] += 1
            elif p >= 70:
                buckets["B"] += 1
            elif p >= 60:
                buckets["C"] += 1
            elif p >= 50:
                buckets["D"] += 1
            else:
                buckets["F"] += 1
        grade_distribution = {
            "labels": ["A (80–100)", "B (70–79)", "C (60–69)", "D (50–59)", "F (<50)"],
            "values": [
                buckets["A"], buckets["B"], buckets["C"],
                buckets["D"], buckets["F"],
            ],
        }

        charts = {
            "score_trend": score_trend,
            "per_course_avg": per_course_avg,
            "per_course_spread": per_course_spread,
            "grade_distribution": grade_distribution,
        }

        return {
            "attempts_total": len(attempts),
            "attempts_graded": len(graded_attempts),
            "average_percentage": (
                round(sum(percentages) / len(percentages), 2)
                if percentages else None
            ),
            "best_percentage": max(percentages) if percentages else None,
            "worst_percentage": min(percentages) if percentages else None,
            "best_attempt": _summary(best_attempt),
            "worst_attempt": _summary(worst_attempt),
            "per_course": per_course_list,
            "recent_attempts": recent_attempts,
            "charts": charts,
        }


# =============================================================================
# Lecturer resources
# =============================================================================

class CourseViewSet(viewsets.ModelViewSet):
    serializer_class = CourseSerializer
    queryset = Course.objects.select_related("lecturer__user")

    def get_permissions(self):
        if self.request.method in permissions.SAFE_METHODS:
            return [permissions.IsAuthenticated()]
        return [permissions.IsAuthenticated(), IsLecturerOrAdmin()]

    def get_queryset(self):
        user = self.request.user
        if is_admin(user):
            return self.queryset
        if is_lecturer(user):
            return self.queryset.filter(lecturer__user=user)
        return self.queryset

    def perform_create(self, serializer):
        user = self.request.user
        if is_admin(user):
            serializer.save()
            return

        profile = getattr(user, "lecturer_profile", None)
        if profile is None:
            raise PermissionDenied(
                "Only lecturers with a lecturer profile can create courses."
            )
        serializer.save(lecturer=profile)

    @action(detail=True, methods=["get"], url_path="available-students")
    def available_students(self, request, pk=None):
        if not (is_lecturer(request.user) or is_admin(request.user)):
            return Response(
                {"error": "Lecturer or admin access required."},
                status=status.HTTP_403_FORBIDDEN,
            )

        course = self.get_object()

        qs = StudentProfile.objects.select_related("user").exclude(
            id__in=Enrollment.objects.filter(course=course).values("student_id")
        )

        q = (request.query_params.get("q") or "").strip()
        if q:
            qs = qs.filter(
                Q(student_id__icontains=q)
                | Q(user__first_name__icontains=q)
                | Q(user__last_name__icontains=q)
                | Q(user__username__icontains=q)
                | Q(user__email__icontains=q)
            )

        limit = min(parse_positive_int(request.query_params.get("limit")) or 20, 50)
        qs = qs.order_by("user__first_name", "student_id")[:limit]

        return Response([
            {
                "id": s.id,
                "student_id": s.student_id,
                "name": (s.user.get_full_name() or s.user.username).strip(),
                "email": s.user.email,
                "department": s.department,
            }
            for s in qs
        ])


class EnrollmentViewSet(viewsets.ModelViewSet):
    serializer_class = EnrollmentSerializer
    permission_classes = [permissions.IsAuthenticated]
    http_method_names = ["get", "post", "delete", "head", "options"]
    queryset = Enrollment.objects.select_related(
        "student__user", "course", "course__lecturer__user"
    )

    def get_queryset(self):
        user = self.request.user
        if is_admin(user):
            qs = self.queryset
        elif is_lecturer(user):
            qs = self.queryset.filter(course__lecturer__user=user)
        elif is_student(user):
            qs = self.queryset.filter(student__user=user)
        else:
            return self.queryset.none()

        course_id = parse_positive_int(self.request.query_params.get("course"))
        if course_id is not None:
            qs = qs.filter(course_id=course_id)

        student_id = parse_positive_int(self.request.query_params.get("student"))
        if student_id is not None:
            qs = qs.filter(student_id=student_id)

        return qs.order_by("student__user__first_name", "student__student_id")

    def create(self, request, *args, **kwargs):
        if is_student(request.user):
            profile = getattr(request.user, "student_profile", None)
            if profile is None:
                return Response(
                    {"error": "No student profile on this account."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            payload = dict(request.data)
            course_id = parse_positive_int(payload.get("course_id"))
            if course_id is None:
                return Response(
                    {"error": "course_id is required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            payload["student_id"] = profile.id
            serializer = self.get_serializer(data=payload)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        course_id = parse_positive_int(
            (request.data or {}).get("course_id")
            or (request.data or {}).get("course")
        )
        if course_id is None:
            return Response(
                {"error": "course_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        course = get_object_or_404(Course, pk=course_id)
        if not is_admin(request.user):
            if (course.lecturer is None
                    or course.lecturer.user_id != request.user.id):
                return Response(
                    {"error": "You can only enroll students in your own courses."},
                    status=status.HTTP_403_FORBIDDEN,
                )
        return super().create(request, *args, **kwargs)


class LecturerOwnedViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated, IsLecturerOrAdmin]
    owner_field = "created_by"

    def get_queryset(self):
        if is_admin(self.request.user):
            return self.queryset
        return self.queryset.filter(**{self.owner_field: self.request.user})

    def perform_create(self, serializer):
        serializer.save(**{self.owner_field: self.request.user})


class QuestionBankViewSet(LecturerOwnedViewSet):
    serializer_class = QuestionBankSerializer
    queryset = (
        QuestionBank.objects
        .select_related("course", "created_by")
        .prefetch_related("questions")
    )


class QuestionViewSet(viewsets.ModelViewSet):
    serializer_class = QuestionSerializer
    permission_classes = [permissions.IsAuthenticated, IsLecturerOrAdmin]
    queryset = Question.objects.select_related("bank", "bank__course", "bank__created_by")

    def get_queryset(self):
        qs = self.queryset
        user = self.request.user
        if is_admin(user):
            qs = qs
        else:
            qs = qs.filter(bank__created_by=user)

        bank_id = parse_positive_int(self.request.query_params.get("bank"))
        if bank_id is not None:
            qs = qs.filter(bank_id=bank_id)
        return qs


class RubricViewSet(viewsets.ModelViewSet):
    serializer_class = RubricSerializer
    queryset = Rubric.objects.select_related("course", "created_by")

    def get_permissions(self):
        if self.request.method in permissions.SAFE_METHODS:
            return [permissions.IsAuthenticated()]
        return [permissions.IsAuthenticated(), IsLecturerOrAdmin()]

    # ← FIXED: rubrics are now always tied to a course (enforced at the
    # serializer level). Students see only rubrics for courses they're
    # enrolled in — legacy course-less rubrics are hidden automatically.
    def get_queryset(self):
        user = self.request.user
        if is_admin(user):
            return self.queryset
        if is_lecturer(user):
            return self.queryset.filter(created_by=user)

        profile = getattr(user, "student_profile", None)
        if profile is None:
            return self.queryset.none()
        course_ids = profile.enrollments.values_list("course_id", flat=True)
        return self.queryset.filter(course_id__in=course_ids)

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


class ExamViewSet(viewsets.ModelViewSet):
    serializer_class = ExamSerializer
    permission_classes = [permissions.IsAuthenticated, IsLecturerOrAdmin]
    queryset = (
        Exam.objects
        .select_related("course", "created_by")
        .prefetch_related("exam_questions__question")
    )

    def get_queryset(self):
        qs = self.queryset.with_total_marks()
        if is_admin(self.request.user):
            return qs
        return qs.filter(course__lecturer__user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


class ExamQuestionViewSet(viewsets.ModelViewSet):
    serializer_class = ExamQuestionSerializer
    permission_classes = [permissions.IsAuthenticated, IsLecturerOrAdmin]
    queryset = ExamQuestion.objects.select_related(
        "exam", "exam__course", "exam__course__lecturer__user", "question"
    )

    def get_queryset(self):
        qs = self.queryset
        user = self.request.user
        if is_admin(user):
            pass
        else:
            qs = qs.filter(exam__course__lecturer__user=user)

        exam_id = parse_positive_int(self.request.query_params.get("exam"))
        if exam_id is not None:
            qs = qs.filter(exam_id=exam_id)

        return qs.order_by("exam_id", "order")

    def perform_create(self, serializer):
        exam = serializer.validated_data.get("exam")
        if not is_admin(self.request.user):
            if (exam is None
                    or exam.course is None
                    or exam.course.lecturer is None
                    or exam.course.lecturer.user_id != self.request.user.id):
                raise PermissionDenied(
                    "You can only add questions to exams you own."
                )
        serializer.save()


# =============================================================================
# Research submissions
# =============================================================================

class ResearchSubmissionViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated]
    queryset = ResearchSubmission.objects.select_related(
        "student__user", "document", "rubric", "course", "approved_by"
    )
    http_method_names = ["get", "post", "head", "options"]

    def get_serializer_class(self):
        if self.action == "create":
            return ResearchSubmissionCreateSerializer
        if self.action == "roster":
            return SubmissionRosterSerializer
        if self.action == "approve":
            return ResearchSubmissionApproveSerializer
        return ResearchSubmissionSerializer

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx["request"] = self.request
        return ctx

    def get_queryset(self):
        qs = self.queryset
        user = self.request.user

        if is_admin(user):
            return qs

        if is_lecturer(user):
            return qs.filter(
                Q(course__lecturer__user=user)
                | Q(rubric__created_by=user)
            ).distinct()

        if is_student(user):
            return qs.filter(student__user=user)

        return qs.none()

    def create(self, request, *args, **kwargs):
        if not is_student(request.user):
            return Response(
                {"error": "Only students can create research submissions."},
                status=status.HTTP_403_FORBIDDEN,
            )

        response = super().create(request, *args, **kwargs)

        try:
            submission_id = (response.data or {}).get("id")
            if submission_id:
                transaction.on_commit(
                    lambda sid=submission_id: kick_off_evaluation(sid)
                )
        except Exception:
            logger.exception("Could not schedule auto-evaluation")

        return response

    @action(detail=False, methods=["get"], url_path="roster")
    def roster(self, request):
        if not (is_lecturer(request.user) or is_admin(request.user)):
            return Response(
                {"error": "Lecturer access required."},
                status=status.HTTP_403_FORBIDDEN,
            )

        course_id = parse_positive_int(request.query_params.get("course_id"))
        qs = self.get_queryset()
        if course_id is not None:
            qs = qs.filter(course_id=course_id)

        qs = qs.order_by("-submitted_at")
        rows = SubmissionRosterSerializer(qs, many=True).data

        counts = {
            "total": len(rows),
            "pending": sum(1 for r in rows if r["evaluation_status"] == "PENDING"),
            "evaluated": sum(1 for r in rows if r["evaluation_status"] == "EVALUATED"),
            "reviewed": sum(1 for r in rows if r["evaluation_status"] == "REVIEWED"),
        }
        return Response({"counts": counts, "results": rows})

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, pk=None):
        submission = self.get_object()
        user = request.user

        if not is_admin(user):
            is_course_lecturer = (
                submission.course is not None
                and submission.course.lecturer is not None
                and submission.course.lecturer.user_id == user.id
            )
            is_rubric_author = (
                submission.rubric is not None
                and submission.rubric.created_by_id == user.id
            )
            if not (is_course_lecturer or is_rubric_author):
                return Response(
                    {"error": "Only the course lecturer or the rubric author "
                              "can approve this submission."},
                    status=status.HTTP_403_FORBIDDEN,
                )

        serializer = self.get_serializer(
            data=request.data,
            context={"request": request, "submission": submission},
        )
        serializer.is_valid(raise_exception=True)

        comments = serializer.validated_data.get("lecturer_comments")

        with transaction.atomic():
            submission.evaluation_status = ResearchSubmission.Status.REVIEWED
            submission.approved_by = user
            submission.approved_at = timezone.now()
            update_fields = [
                "evaluation_status", "approved_by", "approved_at",
            ]
            if comments is not None:
                submission.lecturer_comments = comments
                update_fields.append("lecturer_comments")
            submission.save(update_fields=update_fields)

        return Response(
            ResearchSubmissionSerializer(submission, context={"request": request}).data,
            status=status.HTTP_200_OK,
        )

    # ─── NEW: revert an approval ─────────────────────────────────────
    @action(detail=True, methods=["post"], url_path="unapprove")
    def unapprove(self, request, pk=None):
        """
        Revert a REVIEWED submission back to EVALUATED.
        Same permission model as approve: only the course lecturer or
        the rubric author (or an admin) can revert.
        """
        submission = self.get_object()
        user = request.user

        if not is_admin(user):
            is_course_lecturer = (
                submission.course is not None
                and submission.course.lecturer is not None
                and submission.course.lecturer.user_id == user.id
            )
            is_rubric_author = (
                submission.rubric is not None
                and submission.rubric.created_by_id == user.id
            )
            if not (is_course_lecturer or is_rubric_author):
                return Response(
                    {"error": "Only the course lecturer or the rubric "
                              "author can revert this submission."},
                    status=status.HTTP_403_FORBIDDEN,
                )

        if submission.evaluation_status != ResearchSubmission.Status.REVIEWED:
            return Response(
                {"error": "Only a reviewed submission can be reverted. "
                          f"Current status: {submission.evaluation_status}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            submission.evaluation_status = ResearchSubmission.Status.EVALUATED
            submission.approved_by = None
            submission.approved_at = None
            submission.save(update_fields=[
                "evaluation_status", "approved_by", "approved_at",
            ])

        return Response(
            ResearchSubmissionSerializer(
                submission, context={"request": request}
            ).data,
            status=status.HTTP_200_OK,
        )

    # ─── NEW: update lecturer comments only ──────────────────────────
    @action(detail=True, methods=["post"], url_path="comments")
    def comments(self, request, pk=None):
        """
        Update lecturer comments on a submission without changing
        its evaluation status. Useful for adding notes after approval.
        """
        submission = self.get_object()
        user = request.user

        if not is_admin(user):
            is_course_lecturer = (
                submission.course is not None
                and submission.course.lecturer is not None
                and submission.course.lecturer.user_id == user.id
            )
            is_rubric_author = (
                submission.rubric is not None
                and submission.rubric.created_by_id == user.id
            )
            if not (is_course_lecturer or is_rubric_author):
                return Response(
                    {"error": "Only the course lecturer or the rubric "
                              "author can edit comments."},
                    status=status.HTTP_403_FORBIDDEN,
                )

        text = (request.data or {}).get("lecturer_comments", "")
        submission.lecturer_comments = text
        submission.save(update_fields=["lecturer_comments"])

        return Response(
            ResearchSubmissionSerializer(
                submission, context={"request": request}
            ).data,
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="retry-eval")
    def retry_eval(self, request, pk=None):
        submission = self.get_object()

        if submission.evaluation_status == ResearchSubmission.Status.REVIEWED:
            return Response(
                {"error": "Already reviewed — no need to re-evaluate."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not submission.rubric_id:
            return Response(
                {"error": "No rubric attached. Attach one before retrying."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not submission.document.content_text:
            return Response(
                {"error": "No text extracted from the document. Re-upload the file."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        kick_off_evaluation(submission.id)

        return Response(
            {
                "status": "queued",
                "submission_id": submission.id,
                "message": "Evaluation started. Refresh in ~30 s.",
            },
            status=status.HTTP_202_ACCEPTED,
        )

    @action(detail=True, methods=["post"], url_path="correction")
    def correction(self, request, pk=None):
        submission = self.get_object()

        if not submission.evaluations.exists():
            return Response(
                {"error": "Submission has not been evaluated yet. "
                          "Run an AI evaluation before requesting a report."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        existing = (
            CorrectionReport.objects
            .filter(submission=submission, status=CorrectionReport.Status.READY)
            .order_by("-generated_at")
            .first()
        )
        if existing:
            return Response(
                CorrectionReportSerializer(existing, context={"request": request}).data,
                status=status.HTTP_200_OK,
            )

        inflight = (
            CorrectionReport.objects
            .filter(submission=submission,
                    status__in=[CorrectionReport.Status.PENDING,
                                CorrectionReport.Status.GENERATING])
            .order_by("-id")
            .first()
        )
        if inflight:
            return Response(
                CorrectionReportSerializer(inflight, context={"request": request}).data,
                status=status.HTTP_202_ACCEPTED,
            )

        evaluation = submission.evaluations.order_by("-evaluated_at").first()
        report = CorrectionReport.objects.create(
            submission=submission,
            evaluation=evaluation,
            status=CorrectionReport.Status.PENDING,
        )

        mode, payload = dispatch(
            generate_correction_report_task, [report.id], request.user,
            dedupe_key=f"ai:inflight:correction:{report.id}",
        )

        if mode == "sync":
            report.refresh_from_db()
            code = (status.HTTP_200_OK if report.status == CorrectionReport.Status.READY
                    else status.HTTP_500_INTERNAL_SERVER_ERROR)
            return Response(
                CorrectionReportSerializer(report, context={"request": request}).data,
                status=code,
            )
        if mode == "async":
            return Response(
                {
                    "report_id": report.id,
                    "task_id": payload,
                    "status": report.status,
                },
                status=status.HTTP_202_ACCEPTED,
            )
        return task_error_response(payload)

    @action(detail=True, methods=["get"], url_path="correction/status")
    def correction_status(self, request, pk=None):
        submission = self.get_object()
        report = (
            CorrectionReport.objects
            .filter(submission=submission)
            .order_by("-id")
            .first()
        )
        if not report:
            return Response(
                {"error": "No correction report has been requested yet."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(
            CorrectionReportSerializer(report, context={"request": request}).data,
            status=status.HTTP_200_OK,
        )


# =============================================================================
# Exam attempts & answers
# =============================================================================

class ExamAttemptViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated]
    http_method_names = ["get", "post", "head", "options"]
    queryset = ExamAttempt.objects.select_related(
        "exam", "exam__course", "student__user"
    ).prefetch_related("answers__question")

    def get_serializer_class(self):
        if self.action == "start":
            return ExamAttemptStartSerializer
        if self.action == "save_answer":
            return AnswerUpsertSerializer
        if is_lecturer(self.request.user) or is_admin(self.request.user):
            return ExamAttemptForLecturerSerializer
        return ExamAttemptDetailSerializer

    def get_queryset(self):
        qs = self.queryset
        user = self.request.user
        if is_admin(user):
            return qs
        if is_lecturer(user):
            return qs.filter(exam__course__lecturer__user=user)
        if is_student(user):
            return qs.filter(student__user=user)
        return qs.none()

    @action(detail=False, methods=["post"], url_path="start")
    def start(self, request):
        if not is_student(request.user):
            return Response(
                {"error": "Only students can start exam attempts."},
                status=status.HTTP_403_FORBIDDEN,
            )

        ser = ExamAttemptStartSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        exam_id = ser.validated_data["exam_id"]

        exam = get_object_or_404(Exam, pk=exam_id)

        if not exam.is_published and not is_admin(request.user):
            return Response(
                {"error": "This exam is not published."},
                status=status.HTTP_403_FORBIDDEN,
            )

        now = timezone.now()
        if exam.start_time and now < exam.start_time:
            return Response(
                {"error": "This exam has not started yet."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if exam.end_time and now > exam.end_time:
            return Response(
                {"error": "This exam has already closed."},
                status=status.HTTP_403_FORBIDDEN,
            )

        profile = request.user.student_profile

        if not is_admin(request.user):
            if not Enrollment.objects.filter(
                student=profile, course=exam.course
            ).exists():
                return Response(
                    {"error": "You are not enrolled in this course."},
                    status=status.HTTP_403_FORBIDDEN,
                )

        with transaction.atomic():
            existing = ExamAttempt.objects.select_for_update().filter(
                exam=exam, student=profile,
                status=ExamAttempt.Status.IN_PROGRESS,
            ).first()
            if existing:
                if existing.deadline <= now:
                    expire_attempt(existing.id, reason="timed_out")
                    existing = None
                else:
                    return Response(
                        ExamAttemptDetailSerializer(existing).data,
                        status=status.HTTP_200_OK,
                    )

            last = ExamAttempt.objects.filter(
                exam=exam, student=profile
            ).order_by("-attempt_number").first()
            next_number = (last.attempt_number + 1) if last else 1

            try:
                attempt = ExamAttempt.objects.create(
                    exam=exam,
                    student=profile,
                    attempt_number=next_number,
                    status=ExamAttempt.Status.IN_PROGRESS,
                )
            except IntegrityError:
                attempt = ExamAttempt.objects.filter(
                    exam=exam, student=profile,
                    status=ExamAttempt.Status.IN_PROGRESS,
                ).first()
                if attempt is None:
                    return Response(
                        {"error": "Could not start attempt. Please retry."},
                        status=status.HTTP_409_CONFLICT,
                    )
                return Response(
                    ExamAttemptDetailSerializer(attempt).data,
                    status=status.HTTP_200_OK,
                )

        return Response(
            ExamAttemptDetailSerializer(attempt).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="save-answer")
    def save_answer(self, request, pk=None):
        attempt = self.get_object()

        if (attempt.status == ExamAttempt.Status.IN_PROGRESS
                and attempt.deadline <= timezone.now()):
            expire_attempt(attempt.id, reason="timed_out")
            attempt.refresh_from_db()
            return Response(
                {
                    "error": "Time is up. The exam was automatically submitted.",
                    "status": attempt.status,
                    "timed_out": True,
                },
                status=status.HTTP_410_GONE,
            )

        if attempt.status != ExamAttempt.Status.IN_PROGRESS:
            return Response(
                {
                    "error": "This attempt is no longer editable.",
                    "status": attempt.status,
                    "timed_out": attempt.timed_out,
                    "abandoned_at": (
                        attempt.abandoned_at.isoformat()
                        if attempt.abandoned_at else None
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        ser = AnswerUpsertSerializer(
            data=request.data, context={"attempt": attempt}
        )
        ser.is_valid(raise_exception=True)

        with transaction.atomic():
            answer, created = Answer.objects.update_or_create(
                attempt=attempt,
                question=ser.validated_data["question"],
                defaults={
                    "selected_option": ser.validated_data.get("selected_option"),
                    "answer_text": ser.validated_data.get("answer_text", ""),
                },
            )
            ExamAttempt.objects.filter(pk=attempt.pk).update(
                last_activity_at=timezone.now()
            )

        return Response(
            AnswerDetailSerializer(answer).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="heartbeat")
    def heartbeat(self, request, pk=None):
        attempt = self.get_object()

        if attempt.status != ExamAttempt.Status.IN_PROGRESS:
            return Response(
                {
                    "status": attempt.status,
                    "seconds_remaining": 0,
                    "timed_out": attempt.timed_out,
                },
                status=status.HTTP_200_OK,
            )

        if attempt.deadline <= timezone.now():
            expire_attempt(attempt.id, reason="timed_out")
            attempt.refresh_from_db()
            return Response(
                {
                    "status": attempt.status,
                    "seconds_remaining": 0,
                    "timed_out": True,
                    "message": "Time is up. The exam was automatically submitted.",
                },
                status=status.HTTP_410_GONE,
            )

        ExamAttempt.objects.filter(pk=attempt.pk).update(
            last_activity_at=timezone.now()
        )
        return Response({
            "status": attempt.status,
            "seconds_remaining": attempt.seconds_remaining,
            "timed_out": False,
        })

    @action(detail=True, methods=["post"], url_path="abandon")
    def abandon(self, request, pk=None):
        attempt = self.get_object()

        if attempt.status != ExamAttempt.Status.IN_PROGRESS:
            return Response(
                {
                    "status": attempt.status,
                    "abandoned_at": (
                        attempt.abandoned_at.isoformat()
                        if attempt.abandoned_at else None
                    ),
                    "timed_out": attempt.timed_out,
                    "already_finalized": True,
                },
                status=status.HTTP_200_OK,
            )

        result = expire_attempt(attempt.id, reason="abandoned")
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="submit")
    def submit(self, request, pk=None):
        attempts = ExamAttempt.objects.select_related("exam", "student__user")
        if not is_admin(request.user):
            attempts = attempts.filter(student__user=request.user)

        with transaction.atomic():
            attempt = get_object_or_404(
                attempts.select_for_update(of=("self",)), pk=pk
            )
            return _perform_exam_submit(request, attempt)


# =============================================================================
# Lecturer analytics
# =============================================================================

class LecturerExamAnalyticsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        exam = get_object_or_404(
            Exam.objects.select_related("course", "course__lecturer__user"),
            pk=pk,
        )

        if not is_admin(request.user):
            if not (is_lecturer(request.user)
                    and exam.course
                    and exam.course.lecturer
                    and exam.course.lecturer.user_id == request.user.id):
                return Response(
                    {"error": "Only the course lecturer can view this."},
                    status=status.HTTP_403_FORBIDDEN,
                )

        total_marks = exam.total_marks or 0

        attempts = list(
            ExamAttempt.objects
            .filter(exam=exam)
            .select_related("student__user")
            .order_by("student_id", "-attempt_number")
        )

        best_by_student = {}
        for a in attempts:
            if a.score is None or not total_marks:
                continue
            pct = round(float(a.score) / total_marks * 100, 2)
            current = best_by_student.get(a.student_id)
            if current is None or pct > current["percentage"]:
                best_by_student[a.student_id] = {
                    "student_id": a.student.student_id,
                    "name": (a.student.user.get_full_name()
                             or a.student.user.username),
                    "percentage": pct,
                    "score": float(a.score),
                    "attempt_number": a.attempt_number,
                    "timed_out": a.timed_out,
                    "abandoned": a.abandoned_at is not None,
                    "is_flagged": a.is_flagged,
                }

        student_rows = sorted(
            best_by_student.values(),
            key=lambda r: r["percentage"],
            reverse=True,
        )

        percentages = [r["percentage"] for r in student_rows]

        def _median(vals):
            if not vals:
                return None
            s = sorted(vals)
            n = len(s)
            mid = n // 2
            return (s[mid] if n % 2 else round((s[mid - 1] + s[mid]) / 2, 2))

        passes = sum(1 for p in percentages if p >= 50)
        summary = {
            "exam_id": exam.id,
            "exam_title": exam.title,
            "course_code": exam.course.code if exam.course else None,
            "total_marks": total_marks,
            "attempts_total": len(attempts),
            "attempts_in_progress": sum(
                1 for a in attempts if a.status == ExamAttempt.Status.IN_PROGRESS
            ),
            "attempts_submitted": sum(
                1 for a in attempts if a.status == ExamAttempt.Status.SUBMITTED
            ),
            "attempts_graded": sum(
                1 for a in attempts if a.status == ExamAttempt.Status.GRADED
            ),
            "attempts_timed_out": sum(1 for a in attempts if a.timed_out),
            "attempts_abandoned": sum(
                1 for a in attempts if a.abandoned_at is not None
            ),
            "attempts_flagged": sum(1 for a in attempts if a.is_flagged),
            "students_count": len(student_rows),
            "average_percentage": (
                round(sum(percentages) / len(percentages), 2)
                if percentages else None
            ),
            "median_percentage": _median(percentages),
            "best_percentage": max(percentages) if percentages else None,
            "worst_percentage": min(percentages) if percentages else None,
            "pass_rate": (
                round(passes / len(percentages) * 100, 2)
                if percentages else None
            ),
            "passes": passes,
            "failures": len(percentages) - passes,
        }

        eqs = list(
            exam.exam_questions.select_related("question").order_by("order")
        )
        difficulty = []
        for eq in eqs:
            q = eq.question
            answers = Answer.objects.filter(
                attempt__exam=exam, question=q,
                attempt__status=ExamAttempt.Status.GRADED,
            )
            total_scored = answers.count()
            correct = answers.filter(is_correct=True).count()
            avg_score = answers.aggregate(avg=Avg("score"))["avg"]
            max_possible = eq.marks or q.marks or 1
            difficulty.append({
                "question_id": q.id,
                "order": eq.order,
                "question_type": q.question_type,
                "text": (q.text[:80] + "…") if len(q.text) > 80 else q.text,
                "answered": total_scored,
                "correct": correct,
                "correct_rate": (
                    round(correct / total_scored * 100, 2)
                    if total_scored else None
                ),
                "average_score": (
                    round(float(avg_score), 2) if avg_score is not None else None
                ),
                "max_score": max_possible,
            })

        bins = [0] * 10
        for p in percentages:
            idx = min(9, int(p // 10))
            bins[idx] += 1

        score_histogram = {
            "x": [f"{i*10}–{i*10+9}%" for i in range(10)],
            "y": bins,
        }

        ranked = {
            "x": [r["name"] for r in student_rows],
            "y": [r["percentage"] for r in student_rows],
            "labels": [r["student_id"] for r in student_rows],
        }

        q_difficulty = {
            "x": [f"Q{d['order']}" for d in difficulty],
            "y": [d["correct_rate"] for d in difficulty],
            "text": [d["text"] for d in difficulty],
        }

        bands = {"A": 0, "B": 0, "C": 0, "D": 0, "F": 0}
        for p in percentages:
            band = _grade_band(p)
            if band:
                bands[band] += 1
        grade_distribution = {
            "labels": ["A (80–100)", "B (70–79)", "C (60–69)", "D (50–59)", "F (<50)"],
            "values": [bands["A"], bands["B"], bands["C"], bands["D"], bands["F"]],
        }

        status_breakdown = {
            "labels": ["In progress", "Submitted", "Graded"],
            "values": [
                summary["attempts_in_progress"],
                summary["attempts_submitted"],
                summary["attempts_graded"],
            ],
        }

        charts = {
            "score_histogram": score_histogram,
            "ranked_students": ranked,
            "question_difficulty": q_difficulty,
            "grade_distribution": grade_distribution,
            "status_breakdown": status_breakdown,
        }

        return Response({
            "summary": summary,
            "students": student_rows,
            "questions": difficulty,
            "charts": charts,
        })


class LecturerCourseAnalyticsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        course = get_object_or_404(
            Course.objects.select_related("lecturer__user"), pk=pk
        )

        if not is_admin(request.user):
            if not (is_lecturer(request.user)
                    and course.lecturer
                    and course.lecturer.user_id == request.user.id):
                return Response(
                    {"error": "Only the course lecturer can view this."},
                    status=status.HTTP_403_FORBIDDEN,
                )

        exams = list(course.exams.order_by("id"))
        exam_ids = [e.id for e in exams]

        attempts = list(
            ExamAttempt.objects
            .filter(exam_id__in=exam_ids)
            .select_related("exam", "student__user")
            .order_by("submitted_at")
        )

        per_exam = {}
        for e in exams:
            per_exam[e.id] = {
                "exam_id": e.id,
                "exam_title": e.title,
                "total_marks": e.total_marks or 0,
                "attempts": 0,
                "graded": 0,
                "total_pct": 0.0,
                "best": None,
                "worst": None,
            }

        per_student = {}
        trend_points = []

        for a in attempts:
            total = a.exam.total_marks or 0
            pct = (
                round(float(a.score) / total * 100, 2)
                if a.score is not None and total else None
            )

            bucket = per_exam[a.exam_id]
            bucket["attempts"] += 1
            if pct is not None:
                bucket["graded"] += 1
                bucket["total_pct"] += pct
                bucket["best"] = pct if bucket["best"] is None else max(bucket["best"], pct)
                bucket["worst"] = pct if bucket["worst"] is None else min(bucket["worst"], pct)

                key = a.student_id
                row = per_student.get(key)
                if row is None:
                    row = {
                        "student_id": a.student.student_id,
                        "name": (a.student.user.get_full_name()
                                 or a.student.user.username),
                        "attempts": 0,
                        "total_pct": 0.0,
                        "best": None,
                        "worst": None,
                    }
                    per_student[key] = row
                row["attempts"] += 1
                row["total_pct"] += pct
                row["best"] = pct if row["best"] is None else max(row["best"], pct)
                row["worst"] = pct if row["worst"] is None else min(row["worst"], pct)

                trend_points.append({
                    "x": (a.submitted_at or a.start_time).isoformat(),
                    "y": pct,
                    "exam_title": a.exam.title,
                    "student": a.student.student_id,
                })

        per_exam_list = []
        for b in per_exam.values():
            per_exam_list.append({
                "exam_id": b["exam_id"],
                "exam_title": b["exam_title"],
                "attempts": b["attempts"],
                "graded": b["graded"],
                "average_percentage": (
                    round(b["total_pct"] / b["graded"], 2)
                    if b["graded"] else None
                ),
                "best_percentage": b["best"],
                "worst_percentage": b["worst"],
            })

        per_student_list = sorted(
            [
                {
                    "student_id": r["student_id"],
                    "name": r["name"],
                    "attempts": r["attempts"],
                    "average_percentage": round(r["total_pct"] / r["attempts"], 2),
                    "best_percentage": r["best"],
                    "worst_percentage": r["worst"],
                }
                for r in per_student.values()
            ],
            key=lambda r: r["average_percentage"],
            reverse=True,
        )

        all_pcts = [p["y"] for p in trend_points]
        passes = sum(1 for p in all_pcts if p >= 50)
        summary = {
            "course_id": course.id,
            "course_code": course.code,
            "course_title": course.title,
            "exams_count": len(exams),
            "attempts_total": len(attempts),
            "students_count": len(per_student_list),
            "average_percentage": (
                round(sum(all_pcts) / len(all_pcts), 2)
                if all_pcts else None
            ),
            "best_percentage": max(all_pcts) if all_pcts else None,
            "worst_percentage": min(all_pcts) if all_pcts else None,
            "pass_rate": (
                round(passes / len(all_pcts) * 100, 2) if all_pcts else None
            ),
        }

        avg_per_exam = {
            "x": [e["exam_title"] for e in per_exam_list],
            "y": [
                e["average_percentage"] if e["average_percentage"] is not None else 0
                for e in per_exam_list
            ],
            "text": [e["exam_title"] for e in per_exam_list],
        }

        score_trend = {
            "x": [p["x"] for p in trend_points],
            "y": [p["y"] for p in trend_points],
            "text": [
                f"{p['exam_title']} — {p['student']}" for p in trend_points
            ],
        }

        student_avg = {
            "x": [s["name"] for s in per_student_list],
            "y": [s["average_percentage"] for s in per_student_list],
            "labels": [s["student_id"] for s in per_student_list],
        }

        bands = {"A": 0, "B": 0, "C": 0, "D": 0, "F": 0}
        for p in all_pcts:
            band = _grade_band(p)
            if band:
                bands[band] += 1
        grade_distribution = {
            "labels": ["A (80–100)", "B (70–79)", "C (60–69)", "D (50–59)", "F (<50)"],
            "values": [bands["A"], bands["B"], bands["C"], bands["D"], bands["F"]],
        }

        attempts_per_exam = {
            "x": [e["exam_title"] for e in per_exam_list],
            "y": [e["attempts"] for e in per_exam_list],
        }

        charts = {
            "avg_per_exam": avg_per_exam,
            "score_trend": score_trend,
            "student_avg": student_avg,
            "grade_distribution": grade_distribution,
            "attempts_per_exam": attempts_per_exam,
        }

        return Response({
            "summary": summary,
            "exams": per_exam_list,
            "students": per_student_list,
            "charts": charts,
        })