"""P2.2 deterministic redirect and HTML evidence verification.

This module composes the Phase 2.1 effect boundaries with bounded, profile-
driven HTML inspection. It has no live transport, PDF inspection, state write,
or action-routing capability.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from html.parser import HTMLParser
from typing import Any, Mapping, Sequence
from urllib.parse import urljoin, urlparse

from automation.contracts import (
    ContractName,
    artifact_fingerprint,
    validate_contract,
)
from automation.domain import Permission
from automation.verification import (
    CrawlDecision,
    CrawlDecisionStatus,
    CrawlPolicyError,
    CrawlPolicyGate,
    EvidenceFetcher,
    FetchResponse,
    SnapshotProvenance,
    SnapshotReference,
    SnapshotStore,
    SourceClassification,
    SourceTrust,
    VerificationError,
    build_verification_result,
    classify_source,
    validate_request_against_discovery,
)


MAX_HTML_BYTES = 5 * 1024 * 1024
MAX_HTML_NODES = 100_000
MAX_HTML_DEPTH = 128
MAX_HTML_TEXT_CHARS = 10_000_000
MAX_REDIRECTS = 5
_HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})
_IGNORED_TEXT_TAGS = frozenset({"script", "style", "template"})
_IDENTITY_TAGS = frozenset({"title", "h1", "h2", "h3"})
_TAG_PATTERN = re.compile(r"^[a-z][a-z0-9:-]{0,63}$")
_ATTRIBUTE_PATTERN = re.compile(r"^[a-z_:][a-z0-9_.:-]{0,127}$")
_YEAR_PATTERN_TEMPLATE = r"(?<!\d){year}(?!\d)"
_ISO_DATE_PATTERN = re.compile(r"(?<!\d)(\d{4})[-/](\d{2})[-/](\d{2})(?!\d)")
_MONTH_NAMES = (
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
)
_MONTH_LOOKUP = {name: index for index, name in enumerate(_MONTH_NAMES, 1)}
_MONTH_PATTERN = "|".join(_MONTH_NAMES)
_MONTH_FIRST_PATTERN = re.compile(
    rf"\b({_MONTH_PATTERN})\s+(\d{{1,2}})(?:st|nd|rd|th)?[,]?\s+(\d{{4}})\b"
)
_DAY_FIRST_PATTERN = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_PATTERN})[,]?\s+(\d{{4}})\b"
)


class HtmlVerificationError(VerificationError):
    """Raised when P2.2 cannot safely inspect supplied HTML evidence."""


class RedirectChainError(HtmlVerificationError):
    """A redirect chain stopped after retaining zero or more safe hops."""

    def __init__(
        self,
        message: str,
        *,
        hops: Sequence["RetainedHtmlHop"] = (),
        blocked_url: str | None = None,
    ) -> None:
        self.hops = tuple(hops)
        self.blocked_url = blocked_url
        super().__init__(message)


@dataclass(frozen=True)
class ElementSelector:
    """A bounded exact element selector, intentionally smaller than CSS."""

    tag: str
    classes: tuple[str, ...] = ()
    attribute: str | None = None
    attribute_value: str | None = None

    def __post_init__(self) -> None:
        tag = self.tag.lower()
        if not _TAG_PATTERN.fullmatch(tag):
            raise HtmlVerificationError(f"invalid selector tag: {self.tag!r}")
        object.__setattr__(self, "tag", tag)
        normalized_classes = tuple(sorted(set(self.classes)))
        if any(
            not item or len(item) > 128 or any(character.isspace() for character in item)
            for item in normalized_classes
        ):
            raise HtmlVerificationError("selector classes must be bounded tokens")
        object.__setattr__(self, "classes", normalized_classes)
        if self.attribute is not None:
            attribute = self.attribute.lower()
            if not _ATTRIBUTE_PATTERN.fullmatch(attribute):
                raise HtmlVerificationError("selector attribute is invalid")
            object.__setattr__(self, "attribute", attribute)
        elif self.attribute_value is not None:
            raise HtmlVerificationError(
                "selector attribute_value requires an attribute"
            )
        if self.attribute_value is not None and len(self.attribute_value) > 4096:
            raise HtmlVerificationError("selector attribute value is too long")


@dataclass(frozen=True)
class HtmlVerificationProfile:
    """Declarative rules for one reviewed HTML source shape."""

    paper_entry_selector: ElementSelector | None = None
    paper_title_selector: ElementSelector | None = None
    paper_author_selector: ElementSelector | None = None
    paper_abstract_selector: ElementSelector | None = None
    minimum_paper_count: int = 1
    maximum_paper_count: int | None = None
    proceedings_entry_selector: ElementSelector | None = None
    minimum_proceedings_count: int = 1
    proceedings_status: str = "archival"

    def __post_init__(self) -> None:
        if self.minimum_paper_count < 1:
            raise HtmlVerificationError("minimum paper count must be positive")
        if (
            self.maximum_paper_count is not None
            and self.maximum_paper_count < self.minimum_paper_count
        ):
            raise HtmlVerificationError(
                "maximum paper count cannot be below the minimum"
            )
        if self.minimum_proceedings_count < 1:
            raise HtmlVerificationError(
                "minimum proceedings count must be positive"
            )
        if self.proceedings_status not in {"provisional", "archival"}:
            raise HtmlVerificationError(
                "proceedings status must be provisional or archival"
            )
        paper_fields = (
            self.paper_title_selector,
            self.paper_author_selector,
            self.paper_abstract_selector,
        )
        if self.paper_entry_selector is None and any(
            selector is not None for selector in paper_fields
        ):
            raise HtmlVerificationError(
                "paper field selectors require a paper entry selector"
            )


COLT_PMLR_VOLUME_PROFILE = HtmlVerificationProfile(
    paper_entry_selector=ElementSelector("div", classes=("paper",)),
    paper_title_selector=ElementSelector("p", classes=("title",)),
    paper_author_selector=ElementSelector("span", classes=("authors",)),
    minimum_paper_count=100,
    maximum_paper_count=500,
    proceedings_entry_selector=ElementSelector("p", classes=("title",)),
    minimum_proceedings_count=100,
    proceedings_status="archival",
)


@dataclass(frozen=True)
class RetainedHtmlHop:
    """One classified, policy-authorized, immutable redirect-chain hop."""

    classification: SourceClassification
    decision: CrawlDecision
    response: FetchResponse
    snapshot: SnapshotReference

    def __post_init__(self) -> None:
        if (
            self.classification.url != self.response.requested_url
            or self.decision.url != self.response.requested_url
            or self.classification.domain != self.decision.domain
        ):
            raise HtmlVerificationError(
                "retained HTML hop identity does not match its response"
            )
        if (
            self.decision.status is not CrawlDecisionStatus.ALLOWED
            or self.decision.permission is not Permission.METADATA_FETCH
            or not self.decision.policy_domain
        ):
            raise HtmlVerificationError(
                "retained HTML hop lacks metadata-fetch authorization"
            )
        content_sha256 = hashlib.sha256(self.response.body).hexdigest()
        if (
            self.snapshot.content_sha256 != content_sha256
            or self.snapshot.size_bytes != len(self.response.body)
        ):
            raise HtmlVerificationError(
                "retained HTML snapshot does not match response content"
            )

    @property
    def source_id(self) -> str:
        identity = {
            "url": self.response.requested_url,
            "status_code": self.response.status_code,
            "redirect_target_url": (
                self.response.redirect_hop.target_url
                if self.response.redirect_hop is not None else None
            ),
            "snapshot_id": self.snapshot.snapshot_id,
        }
        return f"source:{artifact_fingerprint(identity)[:32]}"

    def observation(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "url": self.response.requested_url,
            "redirect_target_url": (
                self.response.redirect_hop.target_url
                if self.response.redirect_hop is not None else None
            ),
            "source_trust": self.classification.trust.value,
            "policy_decision": self.decision.status.value,
            "policy_domain": self.decision.policy_domain,
            "permission": self.decision.permission.value,
            "fetch_status": "fetched",
            "http_status": self.response.status_code,
            "snapshot_id": self.snapshot.snapshot_id,
            "observed_at": self.response.fetched_at,
            "reason_code": "source_observed",
        }


@dataclass(frozen=True)
class HtmlEvidenceBundle:
    """A cited initial URL and every retained response through its final hop."""

    initial_url: str
    venue_id: str
    year: int
    discovery_id: str
    hops: tuple[RetainedHtmlHop, ...]

    def __post_init__(self) -> None:
        if not self.hops:
            raise HtmlVerificationError("HTML evidence requires at least one hop")
        if self.hops[0].response.requested_url != self.initial_url:
            raise HtmlVerificationError("first HTML hop must match the cited URL")
        for previous, current in zip(self.hops, self.hops[1:]):
            redirect = previous.response.redirect_hop
            if redirect is None or redirect.target_url != current.response.requested_url:
                raise HtmlVerificationError("HTML hops do not form a redirect chain")
        if self.hops[-1].response.redirect_hop is not None:
            raise HtmlVerificationError("HTML evidence cannot end on a redirect")

    @property
    def final_hop(self) -> RetainedHtmlHop:
        return self.hops[-1]


@dataclass(frozen=True)
class HtmlEvidence:
    bundle: HtmlEvidenceBundle
    profile: HtmlVerificationProfile


@dataclass(frozen=True)
class HtmlPageAnalysis:
    venue_present: bool
    identity_matches: bool
    observed_dates: frozenset[str]
    paper_count: int | None
    announced_complete_count: int | None
    metadata_complete_count: int | None
    proceedings_count: int | None


@dataclass
class _Node:
    tag: str
    attributes: dict[str, str | None]
    children: list["_Node"] = field(default_factory=list)
    text: list[str] = field(default_factory=list)


class _BoundedHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {})
        self._stack = [self.root]
        self._node_count = 0
        self._text_chars = 0

    def _start(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.lower()
        if not _TAG_PATTERN.fullmatch(normalized_tag):
            raise HtmlVerificationError("HTML contains an invalid tag name")
        if self._node_count >= MAX_HTML_NODES:
            raise HtmlVerificationError("HTML node limit exceeded")
        if len(self._stack) > MAX_HTML_DEPTH:
            raise HtmlVerificationError("HTML nesting limit exceeded")
        normalized_attrs: dict[str, str | None] = {}
        for key, value in attrs:
            normalized_key = key.lower()
            if not _ATTRIBUTE_PATTERN.fullmatch(normalized_key):
                raise HtmlVerificationError("HTML contains an invalid attribute name")
            if normalized_key in normalized_attrs:
                raise HtmlVerificationError("HTML contains duplicate attributes")
            if value is not None and len(value) > 4096:
                raise HtmlVerificationError("HTML attribute value is too long")
            normalized_attrs[normalized_key] = value
        node = _Node(normalized_tag, normalized_attrs)
        self._stack[-1].children.append(node)
        self._node_count += 1
        if normalized_tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self._start(tag, attrs)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self._start(tag, attrs)
        if self._stack[-1].tag == tag.lower():
            self._stack.pop()

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == normalized:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if any(node.tag in _IGNORED_TEXT_TAGS for node in self._stack):
            return
        self._text_chars += len(data)
        if self._text_chars > MAX_HTML_TEXT_CHARS:
            raise HtmlVerificationError("HTML text limit exceeded")
        self._stack[-1].text.append(data)


def _selector_matches(node: _Node, selector: ElementSelector) -> bool:
    if node.tag != selector.tag:
        return False
    class_value = node.attributes.get("class") or ""
    classes = frozenset(class_value.split())
    if not set(selector.classes).issubset(classes):
        return False
    if selector.attribute is None:
        return True
    if selector.attribute not in node.attributes:
        return False
    if selector.attribute_value is None:
        return True
    return node.attributes[selector.attribute] == selector.attribute_value


def _walk(node: _Node) -> list[_Node]:
    result: list[_Node] = []
    stack = list(reversed(node.children))
    while stack:
        current = stack.pop()
        if current.tag in _IGNORED_TEXT_TAGS:
            continue
        result.append(current)
        stack.extend(reversed(current.children))
    return result


def _select(node: _Node, selector: ElementSelector) -> list[_Node]:
    return [candidate for candidate in _walk(node) if _selector_matches(candidate, selector)]


def _node_text(node: _Node) -> str:
    parts = list(node.text)
    for child in _walk(node):
        parts.extend(child.text)
    return " ".join(" ".join(parts).split())


def _normalized_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _phrase_pattern(value: str) -> re.Pattern[str]:
    parts = [re.escape(part) for part in _normalized_text(value).split()]
    return re.compile(r"(?<!\w)" + r"\s+".join(parts) + r"(?!\w)")


def _catalog_venue(catalog: Mapping[str, Any], venue_id: str) -> Mapping[str, Any]:
    validate_contract(ContractName.VENUE_CATALOG, catalog)
    for venue in catalog["venues"]:
        if venue["venue_id"] == venue_id:
            return venue
    raise HtmlVerificationError(f"unknown catalog venue: {venue_id}")


def _identity(
    root: _Node,
    catalog: Mapping[str, Any],
    venue_id: str,
    year: int,
) -> tuple[bool, bool]:
    venue = _catalog_venue(catalog, venue_id)
    patterns = [_phrase_pattern(alias) for alias in venue["aliases"]]
    patterns.append(_phrase_pattern(venue["display_name"]))
    year_pattern = re.compile(_YEAR_PATTERN_TEMPLATE.format(year=year))
    regions = [
        _normalized_text(_node_text(node))
        for node in _walk(root)
        if node.tag in _IDENTITY_TAGS and _node_text(node)
    ]
    venue_present = any(
        pattern.search(region) for pattern in patterns for region in regions
    )
    identity_matches = any(
        year_pattern.search(region) and any(pattern.search(region) for pattern in patterns)
        for region in regions
    )
    return venue_present, identity_matches


def _valid_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _dates(root: _Node) -> frozenset[str]:
    observed: set[date] = set()
    for node in _walk(root):
        if node.tag == "time":
            value = node.attributes.get("datetime")
            if value:
                try:
                    observed.add(date.fromisoformat(value[:10]))
                except ValueError:
                    pass
    visible = _normalized_text(_node_text(root))
    for match in _ISO_DATE_PATTERN.finditer(visible):
        parsed = _valid_date(*(int(part) for part in match.groups()))
        if parsed is not None:
            observed.add(parsed)
    for match in _MONTH_FIRST_PATTERN.finditer(visible):
        month, day, year = match.groups()
        parsed = _valid_date(int(year), _MONTH_LOOKUP[month], int(day))
        if parsed is not None:
            observed.add(parsed)
    for match in _DAY_FIRST_PATTERN.finditer(visible):
        day, month, year = match.groups()
        parsed = _valid_date(int(year), _MONTH_LOOKUP[month], int(day))
        if parsed is not None:
            observed.add(parsed)
    return frozenset(item.isoformat() for item in observed)


def _content_type(response: FetchResponse) -> tuple[str, str]:
    raw = response.headers.get("content-type", "")
    parts = [part.strip() for part in raw.split(";")]
    media_type = parts[0].lower()
    charset = "utf-8"
    for part in parts[1:]:
        if part.lower().startswith("charset="):
            charset = part.split("=", 1)[1].strip(" \"'").lower()
    return media_type, charset


def _parse_html(response: FetchResponse) -> _Node:
    if response.status_code != 200:
        raise HtmlVerificationError(
            f"HTML source returned HTTP {response.status_code}"
        )
    media_type, charset = _content_type(response)
    if media_type not in _HTML_CONTENT_TYPES:
        raise HtmlVerificationError(
            f"source is not recognized HTML: {media_type or 'missing content type'}"
        )
    if len(response.body) > MAX_HTML_BYTES:
        raise HtmlVerificationError("HTML byte limit exceeded")
    charset_aliases = {
        "utf-8": "utf-8",
        "utf8": "utf-8",
        "us-ascii": "ascii",
        "ascii": "ascii",
        "iso-8859-1": "iso-8859-1",
        "latin-1": "iso-8859-1",
        "windows-1252": "windows-1252",
    }
    codec = charset_aliases.get(charset)
    if codec is None:
        raise HtmlVerificationError(f"unsupported HTML charset: {charset}")
    try:
        decoded = response.body.decode(codec, errors="strict")
    except UnicodeDecodeError as exc:
        raise HtmlVerificationError("HTML body is not valid declared text") from exc
    if "\x00" in decoded:
        raise HtmlVerificationError("HTML body contains NUL characters")
    parser = _BoundedHtmlParser()
    try:
        parser.feed(decoded)
        parser.close()
    except (AssertionError, ValueError) as exc:
        raise HtmlVerificationError("HTML parser rejected the document") from exc
    return parser.root


def _first_text(node: _Node, selector: ElementSelector | None) -> str:
    if selector is None:
        return ""
    for match in _select(node, selector):
        value = _node_text(match)
        if value:
            return value
    return ""


def _all_text(node: _Node, selector: ElementSelector | None) -> list[str]:
    if selector is None:
        return []
    return [value for match in _select(node, selector) if (value := _node_text(match))]


def analyze_html(
    response: FetchResponse,
    *,
    catalog: Mapping[str, Any],
    venue_id: str,
    year: int,
    profile: HtmlVerificationProfile,
) -> HtmlPageAnalysis:
    """Parse one final HTML response and return deterministic page facts."""
    root = _parse_html(response)
    venue_present, identity_matches = _identity(root, catalog, venue_id, year)

    paper_count: int | None = None
    announced_count: int | None = None
    metadata_count: int | None = None
    if profile.paper_entry_selector is not None:
        papers: dict[str, tuple[bool, bool]] = {}
        for entry in _select(root, profile.paper_entry_selector):
            title = _first_text(entry, profile.paper_title_selector)
            if not title:
                continue
            key = _normalized_text(title)
            has_authors = bool(_all_text(entry, profile.paper_author_selector))
            has_abstract = bool(_first_text(entry, profile.paper_abstract_selector))
            previous = papers.get(key, (False, False))
            papers[key] = (
                previous[0] or has_authors,
                previous[1] or has_abstract,
            )
        paper_count = len(papers)
        announced_count = sum(has_authors for has_authors, _ in papers.values())
        metadata_count = sum(
            has_authors and has_abstract
            for has_authors, has_abstract in papers.values()
        )

    proceedings_count: int | None = None
    if profile.proceedings_entry_selector is not None:
        entries = {
            _normalized_text(value)
            for value in _all_text(root, profile.proceedings_entry_selector)
        }
        proceedings_count = len(entries)

    return HtmlPageAnalysis(
        venue_present=venue_present,
        identity_matches=identity_matches,
        observed_dates=_dates(root),
        paper_count=paper_count,
        announced_complete_count=announced_count,
        metadata_complete_count=metadata_count,
        proceedings_count=proceedings_count,
    )


def extract_pmlr_pdf_urls(
    response: FetchResponse,
    *,
    minimum_count: int = 100,
    maximum_count: int = 500,
) -> tuple[str, ...]:
    """Return bounded same-volume PDF links from retained PMLR HTML."""
    if not 1 <= minimum_count <= maximum_count <= 5000:
        raise HtmlVerificationError("PMLR PDF count bounds are invalid")
    listing = urlparse(response.requested_url)
    if (
        listing.scheme != "https"
        or listing.username is not None
        or listing.password is not None
        or (listing.hostname or "").lower().rstrip(".")
        != "proceedings.mlr.press"
        or re.fullmatch(r"/v[1-9][0-9]*/", listing.path) is None
        or listing.query
        or listing.fragment
    ):
        raise HtmlVerificationError("PMLR listing URL is not a volume root")
    root = _parse_html(response)
    paper_selector = COLT_PMLR_VOLUME_PROFILE.paper_entry_selector
    assert paper_selector is not None
    urls: set[str] = set()
    for paper in _select(root, paper_selector):
        for anchor in _walk(paper):
            if anchor.tag != "a" or "pdf" not in _normalized_text(_node_text(anchor)):
                continue
            raw = anchor.attributes.get("href")
            if not raw:
                continue
            resolved = urljoin(response.requested_url, raw)
            parsed = urlparse(resolved)
            if (
                parsed.scheme != "https"
                or parsed.username is not None
                or parsed.password is not None
                or (parsed.hostname or "").lower().rstrip(".")
                != "proceedings.mlr.press"
                or not parsed.path.startswith(listing.path)
                or not parsed.path.lower().endswith(".pdf")
                or "%" in parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise HtmlVerificationError(
                    "PMLR listing contains an unsafe PDF link"
                )
            urls.add(resolved)
    if not minimum_count <= len(urls) <= maximum_count:
        raise HtmlVerificationError("PMLR listing PDF count is implausible")
    return tuple(sorted(urls))


_PMLR_VOLUME_ROOT_PATTERN = re.compile(r"/v[1-9][0-9]*/")


def extract_pmlr_volume_link(response: FetchResponse) -> str | None:
    """Return the one exact unsigned PMLR volume-root link, or ``None``.

    This inspects an already-fetched, already identity-verified page (an
    official conference page, not a PMLR listing) for an embedded link to a
    PMLR proceedings volume root. Cross-host, signed, percent-encoded, and
    non-root-shaped links are never counted as candidates; a missing or
    ambiguous (more than one distinct) candidate returns ``None`` rather than
    guessing. This function never fetches or follows any link itself.
    """
    root = _parse_html(response)
    candidates: set[str] = set()
    for node in _walk(root):
        if node.tag != "a":
            continue
        raw = node.attributes.get("href")
        if not raw:
            continue
        resolved = urljoin(response.requested_url, raw)
        parsed = urlparse(resolved)
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or (parsed.hostname or "").lower().rstrip(".")
            != "proceedings.mlr.press"
            or _PMLR_VOLUME_ROOT_PATTERN.fullmatch(parsed.path) is None
            or "%" in parsed.path
            or parsed.query
            or parsed.fragment
        ):
            continue
        candidates.add(resolved)
    if len(candidates) != 1:
        return None
    return next(iter(candidates))


def fetch_html_evidence(
    *,
    gate: CrawlPolicyGate,
    fetcher: EvidenceFetcher,
    snapshot_store: SnapshotStore,
    catalog: Mapping[str, Any],
    venue_id: str,
    year: int,
    discovery_id: str,
    initial_url: str,
    max_redirects: int = MAX_REDIRECTS,
    max_bytes: int = MAX_HTML_BYTES,
    timeout_seconds: float = 30.0,
) -> HtmlEvidenceBundle:
    """Fetch and retain a chain, independently gating every exact hop."""
    if max_redirects < 0 or max_redirects > MAX_REDIRECTS:
        raise HtmlVerificationError(
            f"max_redirects must be between 0 and {MAX_REDIRECTS}"
        )
    if max_bytes < 1 or max_bytes > MAX_HTML_BYTES:
        raise HtmlVerificationError(
            f"HTML max_bytes must be between 1 and {MAX_HTML_BYTES}"
        )
    if not 1900 <= year <= 2200:
        raise HtmlVerificationError("HTML evidence year is invalid")
    if not discovery_id:
        raise HtmlVerificationError("HTML evidence discovery ID is required")

    url = initial_url
    visited: set[str] = set()
    hops: list[RetainedHtmlHop] = []
    while True:
        if url in visited:
            raise RedirectChainError(
                "redirect loop detected", hops=hops, blocked_url=url
            )
        visited.add(url)
        classification = classify_source(catalog, venue_id, url)
        try:
            response, decision = gate.fetch(
                fetcher,
                url=url,
                permission=Permission.METADATA_FETCH,
                max_bytes=max_bytes,
                timeout_seconds=timeout_seconds,
            )
        except CrawlPolicyError as exc:
            raise RedirectChainError(
                f"redirect target stopped by crawl policy: {exc.decision.status.value}",
                hops=hops,
                blocked_url=url,
            ) from exc
        assert decision.policy_domain is not None
        snapshot = snapshot_store.retain(
            response,
            SnapshotProvenance(
                venue_id=venue_id,
                year=year,
                discovery_id=discovery_id,
                source_trust=classification.trust,
                permission=Permission.METADATA_FETCH,
                policy_domain=decision.policy_domain,
            ),
        )
        hops.append(RetainedHtmlHop(classification, decision, response, snapshot))
        redirect = response.redirect_hop
        if redirect is None:
            return HtmlEvidenceBundle(
                initial_url=initial_url,
                venue_id=venue_id,
                year=year,
                discovery_id=discovery_id,
                hops=tuple(hops),
            )
        if len(hops) > max_redirects:
            raise RedirectChainError(
                "redirect limit exceeded",
                hops=hops,
                blocked_url=redirect.target_url,
            )
        url = redirect.target_url


@dataclass(frozen=True)
class _Assessment:
    status: str
    reason_code: str
    source_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    metrics: Mapping[str, int] | None = None
    facet_value: str | None = None
    milestone: Mapping[str, Any] | None = None


def _target_source(
    discovery: Mapping[str, Any], target_kind: str, target_id: str
) -> Mapping[str, Any]:
    key = "claims" if target_kind == "claim" else "candidate_milestones"
    id_key = "claim_id" if target_kind == "claim" else "milestone_id"
    for item in discovery.get(key, []):
        if item[id_key] == target_id:
            return item
    raise HtmlVerificationError("request target is absent from discovery")


def _plausible_paper_count(
    analysis: HtmlPageAnalysis, profile: HtmlVerificationProfile
) -> bool:
    if analysis.paper_count is None:
        return False
    if analysis.paper_count < profile.minimum_paper_count:
        return False
    return (
        profile.maximum_paper_count is None
        or analysis.paper_count <= profile.maximum_paper_count
    )


def _assess(
    evidence: HtmlEvidence,
    analysis: HtmlPageAnalysis | None,
    analysis_error: str | None,
    *,
    verification_kind: str,
    target: Mapping[str, Any],
) -> _Assessment:
    bundle = evidence.bundle
    source_ids = tuple(hop.source_id for hop in bundle.hops)
    evidence_ids = tuple(hop.snapshot.snapshot_id for hop in bundle.hops)
    final = bundle.final_hop
    if final.classification.trust is SourceTrust.UNTRUSTED:
        return _Assessment(
            "review_required", "unsupported_source_shape", source_ids, evidence_ids
        )
    if analysis_error is not None or analysis is None:
        status = "error" if final.response.status_code != 200 else "review_required"
        reason = "fetch_failed" if status == "error" else "unsupported_source_shape"
        return _Assessment(status, reason, source_ids, evidence_ids)
    if not analysis.venue_present:
        return _Assessment("rejected", "identity_mismatch", source_ids, evidence_ids)
    if not analysis.identity_matches:
        return _Assessment("rejected", "year_mismatch", source_ids, evidence_ids)
    if verification_kind == "source_identity":
        return _Assessment("verified", "supported", source_ids, evidence_ids)
    if verification_kind == "conference_milestone":
        if target["date"] not in analysis.observed_dates:
            return _Assessment(
                "rejected", "unsupported_source_shape", source_ids, evidence_ids
            )
        source_type = final.classification.trust.value
        return _Assessment(
            "verified",
            "supported",
            source_ids,
            evidence_ids,
            milestone={
                "candidate_milestone_id": target["milestone_id"],
                "milestone_type": target["milestone_type"],
                "scope": target.get(
                    "scope",
                    "main_track"
                    if target["milestone_type"] == "acceptance_notification"
                    else "conference",
                ),
                "date": target["date"],
                "source_type": source_type,
                "source_url": final.response.requested_url,
                "evidence_ids": list(evidence_ids),
            },
        )
    if verification_kind == "paper_list":
        if analysis.paper_count is None:
            return _Assessment(
                "review_required", "unsupported_source_shape", source_ids, evidence_ids
            )
        metrics = {"paper_count": analysis.paper_count}
        if not _plausible_paper_count(analysis, evidence.profile):
            return _Assessment(
                "rejected",
                "implausible_paper_count",
                source_ids,
                evidence_ids,
                metrics,
                "partial" if analysis.paper_count > 0 else None,
            )
        return _Assessment(
            "verified", "supported", source_ids, evidence_ids, metrics, "released"
        )
    if verification_kind == "metadata":
        if analysis.paper_count is None or analysis.metadata_complete_count is None:
            return _Assessment(
                "review_required", "unsupported_source_shape", source_ids, evidence_ids
            )
        metrics = {
            "paper_count": analysis.paper_count,
            "metadata_complete_count": analysis.metadata_complete_count,
        }
        if (
            _plausible_paper_count(analysis, evidence.profile)
            and analysis.metadata_complete_count == analysis.paper_count
        ):
            return _Assessment(
                "verified", "supported", source_ids, evidence_ids, metrics, "ready"
            )
        partial = (
            "partial"
            if analysis.announced_complete_count is not None
            and analysis.announced_complete_count > 0
            else None
        )
        return _Assessment(
            "rejected",
            "metadata_incomplete",
            source_ids,
            evidence_ids,
            metrics,
            partial,
        )
    if verification_kind == "proceedings":
        if analysis.proceedings_count is None:
            return _Assessment(
                "review_required", "unsupported_source_shape", source_ids, evidence_ids
            )
        if analysis.proceedings_count < evidence.profile.minimum_proceedings_count:
            return _Assessment(
                "rejected", "proceedings_not_found", source_ids, evidence_ids
            )
        return _Assessment(
            "verified",
            "supported",
            source_ids,
            evidence_ids,
            facet_value=evidence.profile.proceedings_status,
        )
    raise HtmlVerificationError(
        f"verification kind is outside P2.2 HTML scope: {verification_kind}"
    )


def _merge_assessments(items: Sequence[_Assessment]) -> _Assessment:
    if not items:
        return _Assessment("review_required", "unsupported_source_shape", (), ())
    statuses = {item.status for item in items}
    verified = [item for item in items if item.status == "verified"]
    verified_metrics = {
        tuple(sorted((item.metrics or {}).items())) for item in verified
    }
    verified_facets = {
        item.facet_value for item in verified if item.facet_value is not None
    }
    verified_milestone_shapes = {
        (
            item.milestone["candidate_milestone_id"],
            item.milestone["milestone_type"],
            item.milestone["scope"],
            item.milestone["date"],
        )
        for item in verified
        if item.milestone is not None
    }
    verified_content_conflicts = (
        len(verified_metrics) > 1
        or len(verified_facets) > 1
        or len(verified_milestone_shapes) > 1
    )
    if "conflicting" in statuses or (
        "verified" in statuses and bool(statuses & {"rejected", "error"})
    ) or verified_content_conflicts:
        status = "conflicting"
        reason = "conflicting_evidence"
    elif statuses == {"verified"}:
        status = "verified"
        reason = "supported"
    elif "error" in statuses:
        status = "error"
        reason = "fetch_failed"
    elif "rejected" in statuses:
        status = "rejected"
        reasons = {item.reason_code for item in items if item.status == "rejected"}
        reason = next(iter(reasons)) if len(reasons) == 1 else "conflicting_evidence"
    else:
        status = "review_required"
        reason = "unsupported_source_shape"
    metrics: dict[str, int] = {}
    for item in items:
        for key, value in (item.metrics or {}).items():
            metrics[key] = min(metrics.get(key, value), value)
    facet_values = {item.facet_value for item in items if item.facet_value is not None}
    facet = next(iter(facet_values)) if len(facet_values) == 1 else None
    milestone_items = [item.milestone for item in items if item.milestone is not None]
    milestone = None
    if status == "verified" and milestone_items:
        selected = min(
            milestone_items,
            key=lambda item: (item["source_type"], item["source_url"]),
        )
        milestone = dict(selected)
        milestone["evidence_ids"] = sorted({
            evidence_id
            for item in milestone_items
            for evidence_id in item["evidence_ids"]
        })
    return _Assessment(
        status,
        reason,
        tuple(sorted({value for item in items for value in item.source_ids})),
        tuple(sorted({value for item in items for value in item.evidence_ids})),
        metrics or None,
        facet if status in {"verified", "rejected"} else None,
        milestone,
    )


def _overall_status(findings: Sequence[Mapping[str, Any]], has_facets: bool) -> str:
    statuses = {finding["status"] for finding in findings}
    if "conflicting" in statuses:
        return "conflicting"
    has_positive = "verified" in statuses or has_facets
    if has_positive and bool(statuses & {"error", "rejected", "review_required"}):
        return "partially_verified"
    if has_positive:
        return "verified"
    if "error" in statuses:
        return "error"
    if "rejected" in statuses:
        return "rejected"
    return "review_required"


def _set_facet(
    facets: dict[str, Any], name: str, value: str, evidence_ids: Sequence[str]
) -> None:
    current = facets[name]
    if current is not None and current["value"] != value:
        raise HtmlVerificationError(f"conflicting verified {name} facets")
    combined = set(evidence_ids)
    if current is not None:
        combined.update(current["evidence_ids"])
    facets[name] = {"value": value, "evidence_ids": sorted(combined)}


def verify_html_evidence(
    request: Mapping[str, Any],
    discovery: Mapping[str, Any],
    *,
    catalog: Mapping[str, Any],
    evidence: Sequence[HtmlEvidence],
    verified_at: datetime | str,
) -> dict[str, Any]:
    """Build a strict v2 result for P2.2 targets from retained HTML evidence."""
    validate_request_against_discovery(request, discovery)
    if request["schema_version"] != 2:
        raise HtmlVerificationError("P2.2 emits results only for v2 requests")
    unsupported = set(request["verification_kinds"]) - {
        "source_identity", "conference_milestone", "paper_list", "metadata",
        "proceedings",
    }
    if unsupported:
        raise HtmlVerificationError(
            "verification kinds are outside P2.2 HTML scope: "
            + ", ".join(sorted(unsupported))
        )

    cited_urls = {
        url
        for target in request["targets"]
        for url in _target_source(
            discovery, target["target_kind"], target["target_id"]
        )["evidence_urls"]
    }
    inputs_by_url: dict[
        str,
        list[tuple[HtmlEvidence, HtmlPageAnalysis | None, str | None]],
    ] = {}
    observations: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    seen_snapshots: set[str] = set()
    for item in evidence:
        bundle = item.bundle
        if (
            bundle.discovery_id != discovery["discovery_id"]
            or bundle.venue_id != discovery["venue_id"]
            or bundle.year != discovery["year"]
        ):
            raise HtmlVerificationError("HTML evidence identity does not match discovery")
        if bundle.initial_url not in cited_urls:
            raise HtmlVerificationError("HTML evidence URL was not cited by a target")
        try:
            analysis = analyze_html(
                bundle.final_hop.response,
                catalog=catalog,
                venue_id=discovery["venue_id"],
                year=discovery["year"],
                profile=item.profile,
            )
            analysis_error = None
        except HtmlVerificationError as exc:
            analysis = None
            analysis_error = str(exc)
        inputs_by_url.setdefault(bundle.initial_url, []).append(
            (item, analysis, analysis_error)
        )
        for hop in bundle.hops:
            expected_classification = classify_source(
                catalog, discovery["venue_id"], hop.response.requested_url
            )
            if hop.classification != expected_classification:
                raise HtmlVerificationError(
                    "HTML evidence source classification does not match catalog"
                )
            if hop.source_id in seen_sources or hop.snapshot.snapshot_id in seen_snapshots:
                raise HtmlVerificationError("duplicate HTML evidence observation")
            seen_sources.add(hop.source_id)
            seen_snapshots.add(hop.snapshot.snapshot_id)
            observations.append(hop.observation())

    facets: dict[str, Any] = {
        "conference_status": None,
        "paper_list_status": None,
        "metadata_status": None,
        "pdf_status": None,
        "proceedings_status": None,
    }
    findings: list[dict[str, Any]] = []
    milestones: list[dict[str, Any]] = []
    for target_binding in request["targets"]:
        target = _target_source(
            discovery, target_binding["target_kind"], target_binding["target_id"]
        )
        candidates = [
            candidate
            for url in target["evidence_urls"]
            for candidate in inputs_by_url.get(url, [])
        ]
        assessments = [
            _assess(
                item,
                analysis,
                error,
                verification_kind=target_binding["verification_kind"],
                target=target,
            )
            for item, analysis, error in candidates
        ]
        merged = _merge_assessments(assessments)
        finding_identity = artifact_fingerprint({
            "request_id": request["request_id"],
            "target_kind": target_binding["target_kind"],
            "target_id": target_binding["target_id"],
            "verification_kind": target_binding["verification_kind"],
        })
        findings.append({
            "finding_id": f"finding:{finding_identity[:32]}",
            "target_kind": target_binding["target_kind"],
            "target_id": target_binding["target_id"],
            "verification_kind": target_binding["verification_kind"],
            "status": merged.status,
            "source_ids": list(merged.source_ids),
            "evidence_ids": list(merged.evidence_ids),
            "reason_code": merged.reason_code,
            "metrics": dict(merged.metrics) if merged.metrics is not None else None,
        })
        facet_name = {
            "paper_list": "paper_list_status",
            "metadata": "metadata_status",
            "proceedings": "proceedings_status",
        }.get(target_binding["verification_kind"])
        if facet_name is not None and merged.facet_value is not None:
            _set_facet(facets, facet_name, merged.facet_value, merged.evidence_ids)
        if merged.milestone is not None:
            milestones.append(dict(merged.milestone))

    has_facets = any(value is not None for value in facets.values()) or bool(milestones)
    return build_verification_result(
        request,
        discovery,
        overall_status=_overall_status(findings, has_facets),
        verified_at=verified_at,
        source_observations=observations,
        findings=findings,
        verified_facets=facets,
        verified_milestones=milestones,
    )
