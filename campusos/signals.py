# campusos/signals.py
"""
Signal handlers for the CampusOS project.

Two responsibilities:

1. AI config propagation — when an admin saves AIServiceConfig, bump a
   Redis counter so every process (gunicorn workers, Celery workers)
   invalidates its per-process cache on the next get_config() call. This
   gives sub-second propagation without a broadcast channel.

2. Existing model signals — UserAICredit auto-creation, Document search
   vector sync, RubricEvaluation → ResearchSubmission sync. These are
   defined alongside the models in models.py; this file only wires the
   AI config bump.

If you later move the other @receiver blocks out of models.py into this
file, keep them alongside `bump_ai_config_version` below. Nothing in this
file imports models at module scope except AIServiceConfig (safe — it's a
plain model with no imports that could loop).
"""
from django.core.cache import cache
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import AIServiceConfig


CONFIG_VERSION_KEY = "ai:config_version"


@receiver(post_save, sender=AIServiceConfig)
def bump_ai_config_version(sender, instance, created=False, **kwargs):
    """
    Increment a Redis counter on every AIServiceConfig.save().

    Reads of this counter happen in ai_services.get_config(); when a
    process sees the counter changed since it last fetched the config,
    it reloads. `timeout=None` means the counter never expires — a
    Redis restart sets it back to None, which get_config() treats as
    "version 0", triggering one reload and then a stable state again.

    If the cache backend is unavailable, this is a no-op — get_config()
    falls back to time-based refresh (AI_CONFIG_TTL).
    """
    try:
        cache.incr(CONFIG_VERSION_KEY)
    except ValueError:
        # Counter missing (first save, or Redis flushed).
        cache.set(CONFIG_VERSION_KEY, 1, timeout=None)
    except Exception:
        # Cache backend down — non-fatal. Time-based refresh handles it.
        import logging
        logging.getLogger(__name__).warning(
            "Could not bump AI config version counter; "
            "processes will fall back to AI_CONFIG_TTL refresh.",
            exc_info=True,
        )