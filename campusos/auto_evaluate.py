# campusos/auto_evaluate.py
"""
Fire-and-forget research evaluation.

Called from ResearchSubmissionViewSet.create() so students never wait for
the AI inside the HTTP request. The submission row is created first
(PENDING); this module spawns a background thread that runs the AI
evaluation and lets the post_save signal flip the status to EVALUATED.

Execution strategy
------------------
We use a short-lived daemon thread instead of a Celery task because:

  1. On the current deployment (Render free tier) there is no Celery
     worker and CELERY_TASK_ALWAYS_EAGER=True, so .delay() would block
     the HTTP request until the evaluation finished — exactly the
     behaviour we are trying to avoid.
  2. A duplicate RubricEvaluation (if a retry races the original) is
     harmless: the post_save signal does last-write-wins on the
     submission row, so the user still sees one coherent result.

TRADE-OFFS (fix before scaling beyond a demo):
  - The thread dies with the process; there is no automatic retry.
  - Each thread opens its own DB connection; under heavy load this can
    exhaust the Postgres connection pool.
  - No Celery monitoring / Flower visibility.
  - A per-submission cache lock prevents duplicate work across
    gunicorn workers; if the cache backend is down we fail open.

To migrate to Celery, see the sketch at the bottom of this file. The
worker function `_worker(submission_id)` needs no changes either way.

Error reporting
---------------
On failure we store a STRUCTURED error in `ai_feedback` so the frontend
can: stop polling, show why the submission is stuck, and offer an
actionable next step (top up tokens, retry, etc.).

Every stored error is prefixed with `__error__: True` so a consumer can
distinguish it from a real evaluation payload (which the
RubricEvaluation post_save signal writes to the same field on success).

Failures are logged but never bubble up to the caller.
"""
import logging
import threading

from django.conf import settings
from django.db import connections

logger = logging.getLogger(__name__)

# Fallback when settings doesn't define AI_MAX_INPUT_CHARS (tests, or a
# stripped-down settings module). Keeps this file importable standalone.
DEFAULT_MAX_INPUT_CHARS = 30000

# Per-submission lock TTL. Matches CELERY_TASK_TIME_LIMIT (960 s) so a
# stuck thread is released before the platform timeout fires.
EVAL_LOCK_TIMEOUT = 960
EVAL_LOCK_PREFIX = "auto_eval:sub"


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _estimate_required_tokens(text, output_tokens):
    """
    Rough estimate of how many tokens a research evaluation will consume.

    Matches ai_services.estimate_tokens() (len / 3, conservative for
    English + markup) so the shortfall shown to the user is consistent
    with the reservation actually made by run_metered_json_call().
    """
    cap = getattr(settings, "AI_MAX_INPUT_CHARS", DEFAULT_MAX_INPUT_CHARS)
    chars = min(len(text or ""), cap)
    return (chars // 3) + int(output_tokens or 0)


def _store_failure(submission_id, *, error_code, message, **extra):
    """
    Best-effort: mark the submission with a structured error so the
    frontend can display a specific, actionable message.

    The `__error__: True` sentinel distinguishes this payload from a real
    evaluation result (which the RubricEvaluation post_save signal writes
    to the same `ai_feedback` field on success).

    Never raises — a failure here must not break the caller.
    """
    try:
        from .models import ResearchSubmission
        ResearchSubmission.objects.filter(pk=submission_id).update(
            ai_feedback={
                "__error__": True,
                "error": error_code,
                "message": message,
                **extra,
            }
        )
    except Exception:
        logger.exception(
            "auto-eval: could not store failure marker for submission %s",
            submission_id,
        )


def _clear_failure(submission_id):
    """
    Best-effort: clear a previously stored error marker — and only that.

    A real evaluation result (also stored in `ai_feedback`) is left
    untouched. Called before a fresh attempt so the UI reflects the new
    state (still PENDING, no stale error).

    Uses `__has_key` on Postgres; falls back to a Python-side check on
    other backends (SQLite for tests, etc.).
    """
    try:
        from .models import ResearchSubmission
        try:
            # Postgres: single query filtering by JSONB key presence.
            ResearchSubmission.objects.filter(
                pk=submission_id, ai_feedback__has_key="__error__",
            ).update(ai_feedback=None)
        except Exception:
            # Non-Postgres: fetch, check, update.
            sub = (
                ResearchSubmission.objects
                .filter(pk=submission_id)
                .only("ai_feedback")
                .first()
            )
            if sub and isinstance(sub.ai_feedback, dict) \
                    and sub.ai_feedback.get("__error__"):
                ResearchSubmission.objects.filter(pk=submission_id).update(
                    ai_feedback=None,
                )
    except Exception:
        # Non-fatal — worst case, the UI shows a stale error until the
        # next evaluation overwrites it.
        logger.debug(
            "auto-eval: could not clear stale error for submission %s",
            submission_id, exc_info=True,
        )


def _acquire_lock(submission_id):
    """
    Best-effort per-submission lock via the cache backend.

    Returns True if we hold the lock (caller must release), False if
    another evaluation is already running. If the cache backend is down
    we fail open — a duplicate evaluation is less bad than silently
    dropping every request.
    """
    try:
        from django.core.cache import cache
        return cache.add(
            f"{EVAL_LOCK_PREFIX}:{submission_id}",
            "locked",
            timeout=EVAL_LOCK_TIMEOUT,
        )
    except Exception:
        logger.warning(
            "auto-eval: cache unavailable, running without lock for %s",
            submission_id, exc_info=True,
        )
        return True


def _release_lock(submission_id):
    try:
        from django.core.cache import cache
        cache.delete(f"{EVAL_LOCK_PREFIX}:{submission_id}")
    except Exception:
        pass


# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------

def kick_off_evaluation(submission_id):
    """
    Start a daemon thread that evaluates the submission. Returns
    immediately. Returns the Thread object so tests can join() it.

    When you move to a real Celery worker, swap the body for the sketch
    at the bottom of this file.
    """
    submission_id = int(submission_id)

    # Clear any stale error marker before we start a fresh attempt so
    # the UI shows a clean "Evaluating..." state.
    _clear_failure(submission_id)

    t = threading.Thread(
        target=_worker,
        args=(submission_id,),
        daemon=True,
        name=f"auto-eval-{submission_id}",
    )
    t.start()
    logger.info("auto-eval: thread started for submission %s", submission_id)
    return t


# -----------------------------------------------------------------------------
# Worker
# -----------------------------------------------------------------------------

def _worker(submission_id):
    sub = None

    if not _acquire_lock(submission_id):
        logger.info(
            "auto-eval: submission %s already being evaluated; skipping",
            submission_id,
        )
        return

    try:
        from .models import ResearchSubmission, UserAICredit
        from .ai_services import (
            AIError,
            AIPermanentError,
            AIRequestError,
            AITruncatedError,
            InsufficientTokens,
            evaluate_research_submission,
            resolve_max_tokens,
        )

        # Load with everything the error paths need.
        try:
            sub = (
                ResearchSubmission.objects
                .select_related("rubric", "document", "student__user")
                .get(pk=submission_id)
            )
        except ResearchSubmission.DoesNotExist:
            logger.warning("auto-eval: submission %s vanished", submission_id)
            return

        # ── Preconditions ─────────────────────────────────────────────
        if not sub.rubric_id:
            logger.info(
                "auto-eval: submission %s has no rubric; skipping",
                submission_id,
            )
            _store_failure(
                submission_id,
                error_code="no_rubric",
                message=(
                    "No rubric is attached to this submission. "
                    "Attach a rubric and retry."
                ),
            )
            return

        if sub.evaluation_status != ResearchSubmission.Status.PENDING:
            logger.info(
                "auto-eval: submission %s already %s; skipping",
                submission_id, sub.evaluation_status,
            )
            return

        # ── Evaluate ──────────────────────────────────────────────────
        logger.info("auto-eval: evaluating submission %s", submission_id)
        result = evaluate_research_submission(submission_id)
        logger.info(
            "auto-eval: submission %s done — score=%s findings=%s",
            submission_id,
            result.get("overall_score"),
            result.get("findings_count"),
        )

    # ── Error handling ────────────────────────────────────────────────
    # Order matters: subclasses must be caught before their parents.
    #   InsufficientTokens (Exception)
    #   AIRequestError     (Exception)
    #   AITruncatedError   (AIPermanentError → AIError)
    #   AIPermanentError   (AIError)
    #   AIError            (Exception)
    #   Exception          (catch-all)

    except InsufficientTokens:
        # Compute the exact shortfall so the UI can say "you need X more
        # tokens" instead of the useless "out of tokens".
        try:
            if not sub:
                # Defensive: sub should always be loaded by now. If it
                # isn't we can't estimate — fall back to a generic message.
                _store_failure(
                    submission_id,
                    error_code="insufficient_tokens",
                    message=(
                        "Not enough AI tokens to evaluate this submission."
                    ),
                )
                return

            required = _estimate_required_tokens(
                sub.document.content_text or "",
                resolve_max_tokens("research_guidance"),
            )
            credit = UserAICredit.objects.filter(
                user=sub.student.user,
            ).first()
            available = credit.remaining_tokens if credit else 0
            shortfall = max(0, required - available)

            logger.warning(
                "auto-eval: submission %s — insufficient tokens "
                "(need %s, have %s, short %s)",
                submission_id, required, available, shortfall,
            )

            _store_failure(
                submission_id,
                error_code="insufficient_tokens",
                message=(
                    f"This document needs approximately {required:,} tokens "
                    f"to evaluate, but you only have {available:,} remaining. "
                    f"Top up by {shortfall:,} tokens to continue."
                ),
                required_tokens=required,
                available_tokens=available,
                shortfall=shortfall,
            )
        except Exception:
            logger.exception(
                "auto-eval: submission %s — insufficient tokens "
                "(could not compute details)",
                submission_id,
            )
            _store_failure(
                submission_id,
                error_code="insufficient_tokens",
                message="Not enough AI tokens to evaluate this submission.",
            )

    except AIRequestError as exc:
        # Precondition failure raised from ai_services (no text, doc too
        # long, rubric empty, etc.). Message is already user-facing.
        logger.warning(
            "auto-eval: submission %s precondition failed: %s",
            submission_id, exc,
        )
        _store_failure(
            submission_id,
            error_code="precondition_failed",
            message=str(exc) or "This submission cannot be evaluated yet.",
        )

    except AITruncatedError as exc:
        # Model produced more than max_tokens allows. Admin needs to
        # raise the budget, or the student needs to shorten the doc.
        logger.warning(
            "auto-eval: submission %s truncated (spent %s tokens): %s",
            submission_id, exc.total_tokens, exc,
        )
        _store_failure(
            submission_id,
            error_code="truncated",
            message=(
                "The AI response was too long for the configured token "
                "budget. Please contact the administrator to raise the "
                "research evaluation limit, or shorten your document."
            ),
            tokens_spent=getattr(exc, "total_tokens", 0),
        )

    except AIPermanentError as exc:
        # Bad key, invalid model, 4xx from provider — retrying won't help.
        logger.error(
            "auto-eval: submission %s permanent AI error: %s",
            submission_id, exc,
        )
        _store_failure(
            submission_id,
            error_code="configuration_error",
            message=(
                "The AI service is not available right now. "
                "Please contact the administrator."
            ),
        )

    except AIError as exc:
        # Transient: network, rate limit, 5xx. Caller can retry.
        logger.warning(
            "auto-eval: submission %s transient AI error: %s",
            submission_id, exc,
        )
        _store_failure(
            submission_id,
            error_code="ai_error",
            message=(
                "The AI service failed to evaluate this submission. "
                "Please retry in a few minutes."
            ),
        )

    except Exception as exc:
        logger.exception(
            "auto-eval: submission %s crashed: %s", submission_id, exc,
        )
        _store_failure(
            submission_id,
            error_code="unknown_error",
            message=(
                "An unexpected error occurred while evaluating this "
                "submission. Please retry."
            ),
        )

    finally:
        _release_lock(submission_id)
        # Each thread owns its own DB connections; close them on exit so
        # we don't leak connections from the pool.
        try:
            connections.close_all()
        except Exception:
            pass


# -----------------------------------------------------------------------------
# Celery migration sketch (leave commented; enable when a worker is ready)
# -----------------------------------------------------------------------------
#
# When you deploy with a real Celery worker (i.e. CELERY_TASK_ALWAYS_EAGER
# is False), replace `kick_off_evaluation` with:
#
#     def kick_off_evaluation(submission_id):
#         _clear_failure(int(submission_id))
#         from .tasks import evaluate_research_task
#         try:
#             evaluate_research_task.delay(int(submission_id))
#         except Exception:
#             # Broker unreachable — fall back to inline execution so the
#             # student isn't left with a PENDING submission and no worker.
#             logger.exception(
#                 "auto-eval: broker unavailable, running inline for %s",
#                 submission_id,
#             )
#             _worker(int(submission_id))
#
# And add to campusos/tasks.py:
#
#     from celery import shared_task
#
#     @shared_task(bind=True, max_retries=2, default_retry_delay=60)
#     def evaluate_research_task(self, submission_id):
#         from .auto_evaluate import _worker
#         try:
#             _worker(submission_id)
#         except Exception as exc:
#             logger.exception(
#                 "eval task for submission %s failed", submission_id,
#             )
#             raise self.retry(exc=exc)
#
# `_worker` needs no changes: the lock, the error markers, and
# `connections.close_all()` all work identically under Celery. Just
# remove the daemon-thread wrapper and let the broker handle retries.