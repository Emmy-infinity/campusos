# campusos/services/document_search.py
"""High-speed document search + suggestions on top of PostgreSQL FTS."""
import logging
from dataclasses import dataclass
from typing import Optional

from django.contrib.postgres.search import (
    SearchQuery, SearchRank, SearchHeadline,
)
from django.db.models import Count, F, Q
from django.db.models.functions import Coalesce

from campusos.models import Document

logger = logging.getLogger(__name__)

SEARCH_CONFIG = "english"
RANK_WEIGHTS = [0.1, 0.2, 0.4, 1.0]
SNIPPET_MAX_WORDS = 30
SNIPPET_MIN_WORDS = 8
MAX_QUERY_CHARS = 200
SUGGEST_LIMIT = 10


@dataclass
class SearchParams:
    query: Optional[str] = None
    category: Optional[str] = None
    visibility: Optional[str] = None
    course_id: Optional[int] = None
    uploaded_by_id: Optional[int] = None
    ordering: str = "relevance"


def _normalize(raw: Optional[str]) -> str:
    if not raw:
        return ""
    return " ".join(raw.split())[:MAX_QUERY_CHARS]


def search_documents(user, params: SearchParams):
    qs = Document.objects.visible_to(user).with_uploader()

    if params.category:
        qs = qs.filter(category=params.category)
    if params.visibility:
        qs = qs.filter(visibility=params.visibility)
    if params.course_id:
        qs = qs.filter(course_id=params.course_id)
    if params.uploaded_by_id:
        qs = qs.filter(uploaded_by_id=params.uploaded_by_id)

    query = _normalize(params.query)

    if not query:
        ordering = {
            "newest": "-created_at",
            "oldest": "created_at",
            "title": "title",
        }.get(params.ordering, "-created_at")
        return qs.order_by(ordering), False

    search_query = SearchQuery(query, config=SEARCH_CONFIG, search_type="websearch")
    rank = SearchRank(F("search_vector"), search_query, weights=RANK_WEIGHTS)

    qs = (
        qs.annotate(rank=rank)
        .filter(search_vector=search_query)
        .annotate(
            snippet=SearchHeadline(
                "content_text",
                search_query,
                config=SEARCH_CONFIG,
                start_sel="<mark>",
                stop_sel="</mark>",
                max_words=SNIPPET_MAX_WORDS,
                min_words=SNIPPET_MIN_WORDS,
                fragment_delimiter=" … ",
            )
        )
    )

    if params.ordering == "newest":
        qs = qs.order_by("-created_at", "-rank")
    elif params.ordering == "oldest":
        qs = qs.order_by("created_at", "-rank")
    elif params.ordering == "title":
        qs = qs.order_by("title")
    else:
        qs = qs.order_by("-rank", "-created_at")

    return qs, True


def build_facets(user, params: SearchParams):
    qs = Document.objects.visible_to(user)

    if params.visibility:
        qs = qs.filter(visibility=params.visibility)
    if params.course_id:
        qs = qs.filter(course_id=params.course_id)
    if params.uploaded_by_id:
        qs = qs.filter(uploaded_by_id=params.uploaded_by_id)

    query = _normalize(params.query)
    if query:
        sq = SearchQuery(query, config=SEARCH_CONFIG, search_type="websearch")
        qs = qs.filter(search_vector=sq)

    return list(qs.values("category").annotate(count=Count("id")).order_by("-count"))


def suggest_documents(user, prefix: str, limit: int = SUGGEST_LIMIT):
    """
    Two-tier autocomplete:
      1. Title substring match (alphabetical) — most intuitive.
      2. FTS fallback (ranked) fills the rest if fewer than `limit` title hits.
    """
    prefix = _normalize(prefix)
    if len(prefix) < 2:
        return []

    base = Document.objects.visible_to(user)

    # ── Tier 1: title contains the prefix (case-insensitive) ───────
    title_hits = list(
        base.filter(title__icontains=prefix)
        .order_by("title")
        .values("id", "title", "category")[:limit]
    )

    if len(title_hits) >= limit:
        return title_hits

    # ── Tier 2: fill remaining slots with FTS matches ──────────────
    seen = {d["id"] for d in title_hits}
    terms = prefix.split()
    prefix_query_str = " ".join(terms[:-1] + [f"{terms[-1]}:*"])
    sq = SearchQuery(prefix_query_str, config=SEARCH_CONFIG, search_type="raw")
    rank = SearchRank(F("search_vector"), sq, weights=RANK_WEIGHTS)

    remaining = limit - len(title_hits)
    fts_hits = (
        base.exclude(id__in=seen)
        .filter(search_vector=sq)
        .annotate(rank=rank)
        .order_by("-rank")
        .values("id", "title", "category")[:remaining]
    )
    return title_hits + list(fts_hits)