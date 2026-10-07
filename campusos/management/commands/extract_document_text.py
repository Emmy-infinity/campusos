# campusos/management/commands/extract_document_text.py
"""
Backfill Document.content_text and Document.content_pages for files uploaded
before extraction was wired up, or that failed the first time.

Usage:
    python manage.py extract_document_text                    # only empty ones
    python manage.py extract_document_text --all              # re-extract everything
    python manage.py extract_document_text --id 5             # one document
    python manage.py extract_document_text --pages-only       # fill pages, keep text
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from campusos.models import Document
from campusos.utils.text_extraction import extract_text_and_pages


class Command(BaseCommand):
    help = "Extract content_text and content_pages for Document rows."

    def add_arguments(self, parser):
        parser.add_argument(
            "--all",
            action="store_true",
            help="Re-extract every document, not just empty ones.",
        )
        parser.add_argument(
            "--id",
            type=int,
            default=None,
            help="Extract a single document by id.",
        )
        parser.add_argument(
            "--pages-only",
            action="store_true",
            help="Only fill content_pages where it is currently NULL; "
                 "leave content_text untouched.",
        )

    def handle(self, *args, **options):
        if options["id"]:
            qs = Document.objects.filter(pk=options["id"])
        elif options["pages_only"]:
            qs = Document.objects.filter(content_pages__isnull=True)
        elif options["all"]:
            qs = Document.objects.all()
        else:
            qs = Document.objects.filter(content_text="")

        total = qs.count()
        if total == 0:
            self.stdout.write(self.style.WARNING("Nothing to process."))
            return

        self.stdout.write(f"Processing {total} document(s)...")
        ok = failed = empty = 0

        for i, doc in enumerate(qs, 1):
            try:
                text, pages = extract_text_and_pages(doc.file)
            except Exception as exc:
                failed += 1
                self.stdout.write(self.style.ERROR(f"[{i}/{total}] {doc.id} — {exc}"))
                continue

            update_fields = []
            if not options["pages_only"]:
                doc.content_text = text
                update_fields.append("content_text")
            doc.content_pages = pages
            update_fields.append("content_pages")

            with transaction.atomic():
                doc.save(update_fields=update_fields)

            page_count = len(pages) if pages else 0
            if text or page_count:
                ok += 1
                self.stdout.write(
                    f"[{i}/{total}] {doc.id} {doc.title!r} "
                    f"→ {len(text)} chars, {page_count} page(s)"
                )
            else:
                empty += 1
                self.stdout.write(self.style.WARNING(
                    f"[{i}/{total}] {doc.id} {doc.title!r} → nothing extracted"
                ))

        self.stdout.write(self.style.SUCCESS(
            f"Done. ok={ok} empty={empty} failed={failed}"
        ))