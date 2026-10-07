# campusos/auto_evaluate.py
"""
Fire-and-forget research evaluation.

Called from ResearchSubmissionViewSet.create() so students never wait for
the AI inside the HTTP request. The submission row is created first
(PENDING); this module spawns a background thread that runs the AI
evaluation and lets the post_save signal flip the status to EVALUATED.

Safe to call more than once — the worker skips submissions that are
already evaluated.

On failure, we store a STRUCTURED error in `ai_feedback` so the frontend
can:
  - stop polling,
  - show why the submission is stuck,
  - offer an actionable next step (top up tokens, retry, etc.)

Failures are logged but never bubble up to the caller.
"""
import logging
import threading

from django.db import connections

logger = logging.getLogger(__name__)

# Matches settings.AI_MAX_INPUT_CHARS by default. Kept as a fallback so
# this module has no import-time dependency on Django settings.
DEFAULT_MAX_INPUT_CHARS = 30000


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _estimate_required_tokens(text: str, output_tokens: int) -> int:
    """
    Rough estimate of how many tokens a research evaluation will consume.

    Uses the same math as ai_services.can_afford():
      - ~4 characters per token for English text,
      - capped at settings.AI_MAX_INPUT_CHARS,
      - plus the max output budget.
    """
    try:
        from django.conf import settings
        cap = getattr(settings, "AI_MAX_INPUT_CHARS", DEFAULT_MAX_INPUT_CHARS)
    except Exception:
        cap = DEFAULT_MAX_INPUT_CHARS

    chars = min(len(text or ""), cap)
    return (chars // 4) + int(output_tokens or 0)


def _store_failure(submission_id, *, error_code, message, **extra):
    """
    Best-effort: mark the submission with a structured error so the
    frontend can display a specific, actionable message.

    Never raises — a failure here must not break the caller.
    """
    try:
        from .models import ResearchSubmission
        ResearchSubmission.objects.filter(pk=submission_id).update(
            ai_feedback={"error": error_code, "message": message, **extra}
        )
    except Exception:
        logger.exception(
            "auto-eval: could not store failure marker for submission %s",
            submission_id,
        )


def _clear_failure(submission_id):
    """
    Best-effort: clear any previously stored error marker.
    Called before a fresh evaluation attempt so the UI reflects the
    new state (still PENDING, but no stale error).
    """
    try:
        from .models import ResearchSubmission
        ResearchSubmission.objects.filter(
            pk=submission_id, ai_feedback__has_key="error"
        ).update(ai_feedback=None)
    except Exception:
        # Non-fatal — worst case, the UI shows a stale error until the
        # evaluation completes and overwrites it.
        pass


# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------

def kick_off_evaluation(submission_id):
    """
    Start a daemon thread that evaluates the submission. Returns
    immediately. Returns the Thread object so tests can join() it.
    """
    # Clear any stale error marker before we start a fresh attempt.
    # This ensures the UI sees a clean "Evaluating..." state.
    _clear_failure(submission_id)

    t = threading.Thread(
        target=_worker,
        args=(int(submission_id),),
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
    try:
        from .models import ResearchSubmission, UserAICredit
        from .ai_services import (
            AIRequestError, AIError, InsufficientTokens,
            evaluate_research_submission,
            RESEARCH_MAX_TOKENS,
        )

        # Load the submission with everything the error paths need.
        try:
            sub = (
                ResearchSubmission.objects
                .select_related("rubric", "document", "student__user")
                .get(pk=submission_id)
            )
        except ResearchSubmission.DoesNotExist:
            logger.warning("auto-eval: submission %s vanished", submission_id)
            return

        # ── Preconditions ────────────────────────────────────────────────
        if not sub.rubric_id:
            logger.info(
                "auto-eval: submission %s has no rubric yet; skipping",
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

        # ── Evaluate ─────────────────────────────────────────────────────
        logger.info("auto-eval: evaluating submission %s", submission_id)
        result = evaluate_research_submission(submission_id)
        logger.info(
            "auto-eval: submission %s done — score=%s findings=%s",
            submission_id,
            result.get("overall_score"),
            result.get("findings_count"),
        )

    # ── Error handling ───────────────────────────────────────────────────
    except InsufficientTokens:
        # Compute the exact shortfall and store it, so the UI can say
        # "you need X more tokens" instead of the useless "out of tokens".
        try:
            required = _estimate_required_tokens(
                getattr(sub.document, "content_text", "") or "",
                RESEARCH_MAX_TOKENS,
            )
            credit = (
                UserAICredit.objects.filter(user=sub.student.user).first()
                if sub and sub.student_id else None
            )
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
        # long, rubric empty, etc.). The message is usually already
        # user-facing, so we pass it through.
        logger.warning(
            "auto-eval: submission %s precondition failed: %s",
            submission_id, exc,
        )
        _store_failure(
            submission_id,
            error_code="precondition_failed",
            message=str(exc) or "This submission cannot be evaluated yet.",
        )

    except AIError as exc:
        logger.warning(
            "auto-eval: submission %s AI error: %s", submission_id, exc,
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
        # Each thread owns its own DB connections; close them on exit
        # so we don't leak connections from the pool.
        try:
            connections.close_all()
        except Exception:
            pass