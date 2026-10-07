# campusOs/celery.py
"""
Celery app for the campusOs project.

Loaded automatically by campusOs/__init__.py so that:
  - @shared_task decorators are discovered at import time
  - Django settings are wired into Celery via CELERY_* namespace
  - Both eager mode (dev / free tier) and async mode (worker) work
    without any code changes.

Mode is controlled by settings.CELERY_TASK_ALWAYS_EAGER:
  True  -> tasks run inline in the HTTP request (no broker/worker needed)
  False -> tasks are queued to the broker and run in a separate worker
"""
import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "campusOs.settings")

app = Celery("campusOs")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    """Sanity-check task — call with `.delay()` to confirm the worker is up."""
    print(f"Request: {self.request!r}")