# campusos/apps.py
"""
AppConfig for the campusos app.

The ready() hook imports signals.py so the @receiver decorators run at
startup and the post_save hook on AIServiceConfig is registered. Without
this, admin changes to AI settings would not propagate to other workers
until the per-process cache TTL expired.
"""
from django.apps import AppConfig


class CampusosConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "campusos"
    verbose_name = "CampusOS"

    def ready(self):
        # Importing the module registers the receivers. The # noqa below
        # keeps linters from flagging it as unused.
        import campusos.signals  # noqa: F401