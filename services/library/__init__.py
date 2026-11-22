"""Offline reference library: Kiwix collections (Wikipedia, medical, repair,
Q&A sites…) plus a page-level index of the PDF manuals inside them.

Works with the internet down — that is the point. See ``docs/offline-library.md``.
"""

from .search import (
    VALID_SCOPES,
    available,
    collection_summary,
    fetch_content,
    format_for_model,
    library_search,
    owns_url,
    status,
)

__all__ = [
    "VALID_SCOPES",
    "available",
    "collection_summary",
    "fetch_content",
    "format_for_model",
    "library_search",
    "owns_url",
    "status",
]
