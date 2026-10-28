# tasks.py
"""
Celery tasks for the AI features.

Every task returns a plain dict instead of raising, so exception text (which
can contain provider details) never lands in the result backend or reaches
clients:

    {"ok": True,  "data": {...}}
    {"ok": False, "error": "<client-safe message>", "code": <http status hint>}

Transient provider failures (network, timeout, 429, 5xx) are retried with
backoff. While a task is waiting to retry its Celery state is RETRY, so the
status endpoint must treat RETRY like PENDING/STARTED (i.e. "still running").
After the final retry the task gives up and returns the generic error dict
(for grading, it finalizes with "awaiting manual grading" feedback instead).

The grading task also retries on `django.db.OperationalError` (Postgres
deadlocks). The `propagate` parameter on run_safely lets that exception
escape the catch-all so Celery's autoretry_for can handle it with backoff.

Recommended Celery settings (settings.py, with the usual `CELERY_` namespace):

    CELERY_BROKER_URL = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND = "redis://localhost:6379/1"   # needed by the status endpoint
    CELERY_RESULT_EXPIRES = 60 * 60 * 24
    CELERY_TASK_TRACK_STARTED = True
    CELERY_TASK_TIME_LIMIT = 15 * 60                      # hard cap per task
    CELERY_WORKER_PREFETCH_MULTIPLIER = 1                 # long tasks: don't hoard

    CELERY_BEAT_SCHEDULE = {
        "sweep-expired-attempts": {
            "task": "ai.sweep_expired_attempts",
            "schedule": 120.0,          # every 2 minutes
        },
        "reset-monthly-ai-allowances": {
            "task": "ai.reset_monthly_allowances",
            "schedule": crontab(hour=0, minute=0, day_of_month=1),
        },
    }

    # The grading lock below needs a cache shared by all workers (Redis),
    # not the default per-process locmem cache.

Optional: run these on a dedicated queue (task_routes + `celery -Q ai`) so slow
AI jobs can't starve other tasks, and size that worker's concurrency to your
OpenRouter rate limit.
"""
import logging
import random

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.core.cache import cache
from django.db import OperationalError

from .ai_services import (
    AIError, AIRequestError, AITransientError, InsufficientTokens,
    generate_ai_highlights, grade_attempt, finalize_attempt,
    evaluate_research_submission,
    sweep_expired_attempts,
    generate_correction_report,
)

logger = logging.getLogger(__name__)

GENERIC_ERROR = "AI processing failed. Please try again later."
TIMEOUT_ERROR = "AI processing timed out. Please try again."

MAX_RETRIES = 3
GRADING_LOCK_TTL = 16 * 60  # just above the grading hard time limit


def _fail(message, code=503):
    """Client-safe failure; `code` is the HTTP status the view should return."""
    return {"ok": False, "error": message, "code": code}


def _max_retries(task):
    # Eager mode runs inside the HTTP request: retries would fire back-to-back
    # (no countdown) and stall the request, so fail fast instead.
    return 0 if getattr(task.request, "is_eager", False) else task.max_retries


def run_safely(task, label, func, *args, propagate=()):
    """
    Run func, returning the {"ok": ...} dict; retry transient AI failures.

    `propagate` is a tuple of exception types that should be re-raised
    instead of being swallowed. This is used to let Celery's `autoretry_for`
    handle specific exceptions (e.g. django.db.OperationalError for
    Postgres deadlock retries on the grading task).
    """
    propagate = tuple(propagate) if propagate else ()

    try:
        return {"ok": True, "data": func(*args)}
    except InsufficientTokens:
        return _fail("Insufficient AI tokens. Please purchase more.", 402)
    except AIRequestError as exc:
        return _fail(str(exc), exc.status_code)
    except AITransientError as exc:
        if task.request.retries < _max_retries(task):
            countdown = min(5 * 2 ** task.request.retries, 60) + random.randint(0, 3)
            logger.info(
                "AI %s transient failure, retry %s/%s in %ss: %s",
                label, task.request.retries + 1, _max_retries(task), countdown, exc,
            )
            raise task.retry(exc=exc, countdown=countdown)
        logger.warning("AI %s failed after %s retries: %s",
                       label, _max_retries(task), exc)
        return _fail(GENERIC_ERROR)
    except AIError as exc:
        logger.warning("AI %s failed: %s", label, exc)
        return _fail(GENERIC_ERROR)
    except SoftTimeLimitExceeded:
        # run_metered_json_call has already released the reserved tokens.
        logger.warning("AI %s hit the soft time limit", label)
        return _fail(TIMEOUT_ERROR, 504)
    except Exception as exc:
        # If the caller asked us to propagate certain exception types
        # (for Celery's autoretry_for), do that before swallowing.
        if propagate and isinstance(exc, propagate):
            logger.info(
                "AI %s propagating %s for Celery autoretry",
                label, type(exc).__name__,
            )
            raise
        logger.error("Unexpected error in AI %s", label, exc_info=exc)
        return _fail(GENERIC_ERROR)


@shared_task(bind=True, name="ai.highlight_document",
             max_retries=MAX_RETRIES, soft_time_limit=300, time_limit=330)
def highlight_document_task(self, document_id, user_id):
    return run_safely(self, "highlighting", generate_ai_highlights, document_id, user_id)


# Grading is idempotent (only unscored answers are processed, each written with
# a conditional UPDATE), so it is safe to redeliver after a worker crash:
# acks_late + reject_on_worker_lost.
#
# `autoretry_for=(OperationalError,)` catches Postgres deadlocks that can
# occur when a save-answer races with grading. The two-phase submit view
# removes most of these, but the retry is a safety net.
@shared_task(
    bind=True,
    name="ai.grade_exam_attempt",
    acks_late=True,
    reject_on_worker_lost=True,
    autoretry_for=(OperationalError,),
    retry_backoff=2,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=MAX_RETRIES,
    soft_time_limit=900,
    time_limit=960,
)
def grade_exam_attempt_task(self, attempt_id):
    # One grader per attempt: a double-click or duplicate enqueue just reports
    # the current state instead of paying the provider twice. The lock stores
    # the task id so a redelivery of THIS task (worker crash) may re-enter.
    lock_key = f"ai:lock:grade:{attempt_id}"
    task_id = self.request.id
    if not cache.add(lock_key, task_id, GRADING_LOCK_TTL) and cache.get(lock_key) != task_id:
        return run_safely(self, "exam grading status", finalize_attempt, attempt_id)

    try:
        # On the last attempt, stop raising transient errors so unscored answers
        # get their "awaiting manual grading" feedback and the attempt finalizes.
        final_attempt = self.request.retries >= _max_retries(self)
        return run_safely(
            self,
            "exam grading",
            grade_attempt,
            attempt_id,
            not final_attempt,
            # Let deadlocks escape run_safely so autoretry_for can retry.
            propagate=(OperationalError,),
        )
    finally:
        cache.delete(lock_key)


@shared_task(bind=True, name="ai.evaluate_research",
             max_retries=MAX_RETRIES, soft_time_limit=400, time_limit=430)
def evaluate_research_task(self, submission_id):
    return run_safely(self, "research evaluation",
                      evaluate_research_submission, submission_id)


@shared_task(name="ai.reset_monthly_allowances")
def reset_monthly_allowances_task():
    """Daily (celery beat): top balances up to the free monthly allowance."""
    from .models import UserAICredit
    return {"ok": True, "data": {"updated": UserAICredit.reset_monthly_allowances()}}


@shared_task(name="ai.sweep_expired_attempts")
def sweep_expired_attempts_task():
    """
    Runs every ~2 minutes via Celery Beat. Finalizes every IN_PROGRESS
    attempt whose deadline (min(start_time + duration, exam.end_time))
    has passed, marking it timed_out=True and grading whatever the
    student answered before time ran out.

    Returns {"ok": True, "data": {"expired": N, "attempt_ids": [...]}}
    """
    try:
        result = sweep_expired_attempts()
        return {"ok": True, "data": result}
    except Exception:
        logger.exception("sweep_expired_attempts_task failed")
        return _fail(GENERIC_ERROR)


@shared_task(bind=True, name="ai.generate_correction_report",
             acks_late=True, reject_on_worker_lost=True,
             max_retries=2, soft_time_limit=240, time_limit=300)
def generate_correction_report_task(self, report_id):
    """
    Renders a CorrectionReport PDF. Idempotent: if the report is already
    READY, this is a no-op that returns the current URL.
    """
    from .models import CorrectionReport

    try:
        report = CorrectionReport.objects.get(pk=report_id)
    except CorrectionReport.DoesNotExist:
        return _fail("Correction report not found.", 404)

    if report.status == CorrectionReport.Status.READY and report.file:
        return {"ok": True, "data": {
            "report_id": report.id,
            "status": report.status,
            "url": report.file.url,
            "already_ready": True,
        }}

    return run_safely(self, "correction report",
                      generate_correction_report, report_id)