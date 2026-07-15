"""Closed deterministic resolution for reviewed grounding redirect sources.

Vertex AI Search Grounding can return an opaque Google redirect URI together
with only the original source's domain label.  This module never contacts that
redirect.  It resolves only exact venue/year/domain entries whose canonical
source URL is already known by the repository.
"""

from __future__ import annotations

from urllib.parse import urlparse


GROUNDING_REDIRECT_DOMAIN = "vertexaisearch.cloud.google.com"
GROUNDING_REDIRECT_PATH_PREFIX = "/grounding-api-redirect/"

_KNOWN_CATALOG_SOURCE_URLS = {
    ("colt", 2025, "learningtheory.org"): "https://learningtheory.org/colt2025/",
    (
        "colt",
        2025,
        "proceedings.mlr.press",
    ): "https://proceedings.mlr.press/v291/",
}


def resolve_known_grounding_redirect(
    *,
    venue_id: str,
    year: int,
    provider_uri: str,
    source_domain: str | None,
) -> str | None:
    """Return one exact reviewed source URL, or ``None`` without guessing."""
    try:
        parsed = urlparse(provider_uri)
        hostname = (parsed.hostname or "").lower().rstrip(".")
    except (TypeError, ValueError):
        return None
    domain = (source_domain or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != GROUNDING_REDIRECT_DOMAIN
        or parsed.username is not None
        or parsed.password is not None
        or hostname != GROUNDING_REDIRECT_DOMAIN
        or not parsed.path.startswith(GROUNDING_REDIRECT_PATH_PREFIX)
        or len(parsed.path) == len(GROUNDING_REDIRECT_PATH_PREFIX)
        or parsed.query
        or parsed.fragment
    ):
        return None
    return _KNOWN_CATALOG_SOURCE_URLS.get((venue_id, year, domain))


def is_known_colt_pmlr_volume(*, venue_id: str, year: int, url: str) -> bool:
    """Identify the one P2.9-reviewed COLT/PMLR volume source shape."""
    return url == _KNOWN_CATALOG_SOURCE_URLS.get(
        (venue_id, year, "proceedings.mlr.press")
    )


def is_known_colt_official_page(*, venue_id: str, year: int, url: str) -> bool:
    """Identify the one P2.10-reviewed COLT official conference page.

    This is the retained page P2.10's deterministic verifier may inspect for
    an embedded PMLR volume link once its own venue/year identity is
    confirmed; it grants no PMLR authority by itself.
    """
    return url == _KNOWN_CATALOG_SOURCE_URLS.get(
        (venue_id, year, "learningtheory.org")
    )
