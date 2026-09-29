# tasks.py
"""
Celery tasks for the AI features.

Every task returns a plain dict instead of raising, so exception text (which
can contain provider details) never lands in the result backend or reaches
clients:

    {"ok": True,  "data": {...}}
    {"ok": False, "error": "<client-safe message>"}

Recommended Celery settings (settings.py, with the usual `CELERY_` namespace):

    CELERY_BROKER_URL = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND = "redis://localhost:6379/1"   # needed by the status endpoint
    CELERY_RESULT_EXPIRES = 60 * 60 * 24
    CELERY_TASK_TRACK_STARTED = True
    CELERY_TASK_TIME_LIMIT = 15 * 60                      # hard cap per task
    CELERY_WORKER_PREFETCH_MULTIPLIER = 1                 # long tasks: don't hoard
"""
import logging

from celery import shared_task

from .ai_services import (
    AIError, AIRequestError, InsufficientTokens,
    generate_ai_highlights, grade_attempt, evaluate_research_submission,
)

logger = logging.getLogger(__name__)

GENERIC_ERROR = "AI processing failed. Please try again later."


def run_safely(label, func, *args):
    try:
        return {"ok": True, "data": func(*args)}
    except InsufficientTokens:
        return {"ok": False, "error": "Insufficient AI tokens. Please purchase more."}
    except AIRequestError as exc:
        return {"ok": False, "error": str(exc)}
    except AIError as exc:
        logger.warning("AI %s failed: %s", label, exc)
        return {"ok": False, "error": GENERIC_ERROR}
    except Exception as exc:
        logger.error("Unexpected error in AI %s", label, exc_info=exc)
        return {"ok": False, "error": GENERIC_ERROR}


@shared_task(name="ai.highlight_document")
def highlight_document_task(document_id, user_id):
    return run_safely("highlighting", generate_ai_highlights, document_id, user_id)


# Grading is idempotent (only unscored answers are processed), so it is safe
# to redeliver after a worker crash: acks_late + reject_on_worker_lost.
@shared_task(name="ai.grade_exam_attempt", acks_late=True, reject_on_worker_lost=True)
def grade_exam_attempt_task(attempt_id):
    return run_safely("exam grading", grade_attempt, attempt_id)


@shared_task(name="ai.evaluate_research")
def evaluate_research_task(submission_id):
    return run_safely("research evaluation", evaluate_research_submission, submission_id)