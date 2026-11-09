"""Bounded, evidence-first discovery from public Alberta editorial and tourism sites.

This module deliberately discovers source documents, not a prebuilt restaurant list.
It uses Scrapling's static ``Fetcher`` both to make HTTP requests and to parse the
resulting response.  The returned documents retain article text, public links, and
only literal structured-data evidence for a later editorial/LLM verification step.

No login, authentication, proxy, browser automation, or anti-bot bypass is used.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import unescape
import ipaddress
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from scrapling import Selector
from scrapling.fetchers import Fetcher

LOG = logging.getLogger(__name__)

USER_AGENT = "Culinera-Discovery/3.0 (+https://seo-phase.emergent.host)"
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.7",
    "Accept-Language": "en-CA,en;q=0.9",
}
REQUEST_TIMEOUT = 18
REQUEST_RETRIES = 2
DEFAULT_CONFIG_PATH = Path(__file__).with_name("sources.json")

# Exclude static files and account/end-point paths from the bounded crawl queue.
NON_DOCUMENT_SUFFIXES = {
    ".7z",
    ".avi",
    ".bin",
    ".css",
    ".csv",
    ".doc",
    ".docx",
    ".eot",
    ".exe",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".m4a",
    ".mov",
    ".mp3",
    ".mp4",
    ".mpeg",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".rar",
    ".rss",
    ".svg",
    ".tar",
    ".tgz",
    ".ttf",
    ".wav",
    ".webm",
    ".webp",
    ".woff",
    ".woff2",
    ".xls",
    ".xlsx",
    ".xml",
    ".zip",
}
TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "referrer",
    "source",
}
INTERESTING_TERMS = {
    "alberta",
    "article",
    "bar",
    "beer",
    "blog",
    "brewery",
    "cafe",
    "culinary",
    "dine",
    "dining",
    "distillery",
    "drink",
    "eat",
    "event",
    "experience",
    "farm",
    "festival",
    "food",
    "market",
    "menu",
    "news",
    "plate",
    "recipe",
    "restaurant",
    "story",
    "taste",
    "tour",
    "travel",
    "wine",
    "winery",
}
PAGINATION_TERMS = {"next", "newer", "older", "previous", "more", "page"}
SKIP_LINK_TERMS = {
    "accessibility",
    "account",
    "advertise",
    "advertising",
    "cart",
    "checkout",
    "contact",
    "cookie",
    "donate",
    "facebook",
    "instagram",
    "linkedin",
    "login",
    "logout",
    "my account",
    "newsletter",
    "password",
    "pinterest",
    "privacy",
    "register",
    "sign in",
    "sign up",
    "subscribe",
    "tiktok",
    "twitter",
    "youtube",
}
GENERIC_EXTERNAL_LINK_NAMES = {
    "book now",
    "click here",
    "directions",
    "discover more",
    "find out more",
    "learn more",
    "flickr",
    "home",
    "more",
    "our website",
    "read more",
    "restaurant",
    "restaurants",
    "see more",
    "their menu",
    "visit",
    "visit website",
    "website",
}
GENERIC_LISTING_SEGMENTS = {
    "articles",
    "blog",
    "blogs",
    "chefs",
    "directory",
    "dining",
    "dining-out",
    "food-drink",
    "news",
    "restaurants",
    "stories",
    "story",
}
ARTICLE_CONTAINER_SELECTORS = (
    "article",
    "[itemprop='articleBody']",
    ".article-content",
    ".article__content",
    ".entry-content",
    ".post-content",
    ".post__content",
    ".story-content",
    ".story__content",
    "main",
    "[role='main']",
    "body",
)
PUBLISHED_META_SELECTORS = (
    "meta[property='article:published_time']::attr(content)",
    "meta[property='og:published_time']::attr(content)",
    "meta[name='date']::attr(content)",
    "meta[name='publish-date']::attr(content)",
    "meta[name='publication_date']::attr(content)",
    "meta[name='parsely-pub-date']::attr(content)",
    "meta[name='DC.date']::attr(content)",
    "meta[name='dc.date']::attr(content)",
    "meta[name='date.created']::attr(content)",
    "meta[name='citation_publication_date']::attr(content)",
    "meta[itemprop='datePublished']::attr(content)",
)


@dataclass
class RobotsRules:
    """Parsed public robots directives for a single origin."""

    allowed: bool = True
    crawl_delay: float | None = None
    groups: list[tuple[list[str], list[tuple[str, str]]]] = field(default_factory=list)
    error: str | None = None

    def can_fetch(self, user_agent: str, url: str) -> bool:
        if not self.allowed:
            return False
        path = urlsplit(url).path or "/"
        query = urlsplit(url).query
        if query:
            path = f"{path}?{query}"
        user_agent = user_agent.lower()
        selected: list[tuple[str, str]] = []
        best_match = -1
        for agents, directives in self.groups:
            match_length = max(
                (len(agent) for agent in agents if agent == "*" or agent in user_agent),
                default=-1,
            )
            if match_length > best_match:
                best_match = match_length
                selected = directives
            elif match_length == best_match and match_length >= 0:
                selected.extend(directives)
        if best_match < 0:
            return True

        best_rule_length = -1
        decision = True
        for directive, value in selected:
            if directive not in {"allow", "disallow"}:
                continue
            if not value:
                continue
            if _robots_pattern_matches(value, path):
                # Longest matching rule wins.  Allow wins an equally specific tie.
                rule_length = len(value.replace("*", "").rstrip("$"))
                if rule_length > best_rule_length or (
                    rule_length == best_rule_length and directive == "allow"
                ):
                    best_rule_length = rule_length
                    decision = directive == "allow"
        return decision


def _robots_pattern_matches(pattern: str, path: str) -> bool:
    escaped = re.escape(pattern).replace(r"\*", ".*")
    if escaped.endswith(r"\$"):
        return bool(re.match(escaped[:-2] + r"$", path))
    return bool(re.match(escaped, path))


def _parse_robots(text: str) -> RobotsRules:
    """Parse the small, relevant subset of robots.txt without another HTTP client."""
    groups: list[tuple[list[str], list[tuple[str, str]]]] = []
    agents: list[str] = []
    directives: list[tuple[str, str]] = []
    saw_directive = False

    def flush() -> None:
        nonlocal agents, directives, saw_directive
        if agents:
            groups.append((agents, directives))
        agents, directives, saw_directive = [], [], False

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            # A new User-agent after directives begins a new group. Consecutive
            # User-agent fields belong to the same group.
            if saw_directive:
                flush()
            agents.append(value.lower())
        elif agents:
            saw_directive = True
            if key in {"allow", "disallow", "crawl-delay"}:
                directives.append((key, value))
    flush()

    rules = RobotsRules(groups=groups)
    relevant: list[tuple[str, str]] = []
    best_match = -1
    ua = USER_AGENT.lower()
    for group_agents, group_directives in groups:
        match_length = max(
            (len(agent) for agent in group_agents if agent == "*" or agent in ua),
            default=-1,
        )
        if match_length > best_match:
            best_match, relevant = match_length, list(group_directives)
        elif match_length == best_match and match_length >= 0:
            relevant.extend(group_directives)
    for key, value in relevant:
        if key == "crawl-delay":
            try:
                delay = float(value)
            except ValueError:
                continue
            if delay >= 0:
                rules.crawl_delay = max(rules.crawl_delay or 0.0, delay)
    return rules


def _decode_body(body: Any) -> str:
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    if isinstance(body, str):
        return body
    return ""


def _canonical_url(url: str, base_url: str | None = None) -> str | None:
    if base_url:
        url = urljoin(base_url, url)
    try:
        parts = urlsplit(url)
        # Accessing ``.port`` validates malformed ``:port`` values as well.
        port = parts.port
    except ValueError:
        return None
    if (
        parts.scheme.lower() not in {"http", "https"}
        or not parts.netloc
        or parts.username is not None
        or parts.password is not None
    ):
        return None
    hostname = parts.hostname.lower() if parts.hostname else ""
    if not hostname or hostname == "localhost" or hostname.endswith(".local"):
        return None
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        return None
    netloc = hostname
    if port and not (
        (parts.scheme.lower() == "https" and port == 443)
        or (parts.scheme.lower() == "http" and port == 80)
    ):
        netloc = f"{hostname}:{port}"
    kept_query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_QUERY_KEYS and not key.lower().startswith("utm_")
    ]
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    return urlunsplit(
        (parts.scheme.lower(), netloc, path, urlencode(kept_query, doseq=True), "")
    )


def _domain_key(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def _same_domain(left: str, right: str) -> bool:
    return _domain_key(left) == _domain_key(right)


def _normalise_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _first_css_text(page: Any, selectors: Iterable[str]) -> str | None:
    for selector in selectors:
        try:
            value = page.css(selector).get()
        except Exception:
            continue
        if value:
            value = _normalise_space(str(value))
            if value:
                return value
    return None


def _page_html(page: Any) -> str:
    """Preserve the fetched HTML for caller evidence; use Scrapling as parser."""
    html = _decode_body(getattr(page, "body", b""))
    if html:
        return html
    # Synthetic selectors and future response variants may not expose body.
    return str(getattr(page, "html_content", ""))


def _container_text(page: Any) -> str:
    usable_text_nodes = (
        ".//*[not(ancestor-or-self::script or ancestor-or-self::style or "
        "ancestor-or-self::noscript or ancestor-or-self::template or ancestor-or-self::nav or "
        "ancestor-or-self::footer or ancestor-or-self::aside or ancestor-or-self::form or "
        "ancestor-or-self::*[@aria-hidden='true'])]/text()"
    )
    for css_selector in ARTICLE_CONTAINER_SELECTORS:
        try:
            containers = page.css(css_selector)
        except Exception:
            continue
        if not containers:
            continue
        # A page sometimes has more than one article card. Prefer the largest
        # plausible content container rather than concatenate navigation cards.
        choices: list[str] = []
        for container in containers:
            try:
                # Select text nodes only from usable elements.  This formulation
                # works with Scrapling's lxml-backed XPath for synthetic fixtures
                # and public pages, unlike ancestor predicates on text nodes.
                pieces = container.xpath(usable_text_nodes).getall()
            except Exception:
                try:
                    pieces = container.css("::text").getall()
                except Exception:
                    pieces = []
            text = _normalise_space(" ".join(str(piece) for piece in pieces))
            if text:
                choices.append(text)
        if choices:
            best = max(choices, key=len)
            # Do not let a tiny visual article card outrank main page content.
            if len(best) >= 80 or css_selector in {"main", "[role='main']", "body"}:
                return best
    return ""


def _json_ld_values(page: Any) -> list[Any]:
    values: list[Any] = []
    try:
        scripts = page.css("script[type*='ld+json']::text").getall()
    except Exception:
        return values
    for raw in scripts:
        raw = str(raw).strip()
        if not raw:
            continue
        try:
            values.append(json.loads(raw))
        except json.JSONDecodeError:
            # Some publishers concatenate valid JSON objects without an array.
            decoder = json.JSONDecoder()
            position = 0
            while position < len(raw):
                try:
                    item, position = decoder.raw_decode(raw, position)
                except json.JSONDecodeError:
                    break
                values.append(item)
                while position < len(raw) and raw[position].isspace():
                    position += 1
    return values


def _walk_json(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk_json(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_json(nested)


def _types(value: Any) -> set[str]:
    if not isinstance(value, list):
        value = [value]
    return {
        str(item).rsplit("/", 1)[-1].rsplit("#", 1)[-1].lower()
        for item in value
        if isinstance(item, str)
    }


def _literal_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    name = _normalise_space(value)
    if not name or len(name) > 160:
        return None
    folded = name.casefold()
    if folded in GENERIC_EXTERNAL_LINK_NAMES or folded in SKIP_LINK_TERMS:
        return None
    return name


def _structured_candidates(
    page: Any, source_name: str, source_root: str, page_url: str
) -> list[dict[str, str | None]]:
    candidates: list[dict[str, str | None]] = []
    seen: set[tuple[str, str, str]] = set()
    source_folded = _normalise_space(source_name).casefold()
    for data in _json_ld_values(page):
        for node in _walk_json(data):
            type_names = _types(node.get("@type"))
            matching_type = next(
                (
                    name
                    for name in ("restaurant", "foodestablishment", "localbusiness")
                    if name in type_names
                ),
                None,
            )
            if not matching_type:
                continue
            name = _literal_name(node.get("name"))
            if not name or name.casefold() == source_folded:
                # Source roots are evidence sources, never candidate records.
                continue
            address = node.get("address")
            city: str | None = None
            if isinstance(address, dict):
                city = _literal_name(address.get("addressLocality"))
            website: str | None = None
            raw_website = node.get("url") or node.get("sameAs")
            if isinstance(raw_website, list):
                raw_website = next(
                    (item for item in raw_website if isinstance(item, str)), None
                )
            if isinstance(raw_website, str):
                website = _canonical_url(raw_website, page_url)
            fingerprint = (name.casefold(), city or "", website or "")
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            candidates.append(
                {
                    "name": name,
                    "city": city,
                    "website": website,
                    "provenance": f"json-ld:{matching_type}; {page_url}",
                }
            )
    return candidates


def _published_date(page: Any) -> str | None:
    date_value = _first_css_text(page, PUBLISHED_META_SELECTORS)
    if date_value:
        return date_value
    for data in _json_ld_values(page):
        for node in _walk_json(data):
            for key in ("datePublished", "dateCreated", "uploadDate"):
                value = node.get(key)
                if isinstance(value, str) and _normalise_space(value):
                    return _normalise_space(value)
    return _first_css_text(page, ("time[datetime]::attr(datetime)", "time::text"))


def _title(page: Any) -> str:
    return (
        _first_css_text(
            page,
            (
                "meta[property='og:title']::attr(content)",
                "meta[name='twitter:title']::attr(content)",
                "h1::text",
                "title::text",
            ),
        )
        or "Untitled public page"
    )


def _external_evidence_links(page: Any, page_url: str) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    try:
        anchors = page.css("a[href]")
    except Exception:
        return links
    for anchor in anchors:
        href = anchor.attrib.get("href") if getattr(anchor, "attrib", None) else None
        if not isinstance(href, str):
            continue
        absolute = _canonical_url(href, page_url)
        if not absolute or _same_domain(absolute, page_url):
            continue
        try:
            name = _normalise_space(" ".join(anchor.css("::text").getall()))
        except Exception:
            name = ""
        literal = _literal_name(name)
        # A raw URL and a generic CTA are not literal venue evidence.
        if (
            not literal
            or literal.casefold() in GENERIC_EXTERNAL_LINK_NAMES
            or not re.match(r"[A-Z0-9]", literal)
        ):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        links.append({"name": literal, "url": absolute})
    return links


def _image_options(page: Any, page_url: str) -> list[dict[str, str]]:
    raw_urls: list[str] = []
    for selector in (
        "meta[property='og:image']::attr(content)",
        "meta[name='twitter:image']::attr(content)",
        "meta[itemprop='image']::attr(content)",
        "article img::attr(src)",
        "main img::attr(src)",
        "img::attr(data-src)",
        "img::attr(data-lazy-src)",
        "img::attr(data-original)",
        "img::attr(src)",
    ):
        try:
            raw_urls.extend(str(value) for value in page.css(selector).getall())
        except Exception:
            continue
    options: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in raw_urls:
        absolute = _canonical_url(raw, page_url)
        if not absolute or absolute in seen:
            continue
        seen.add(absolute)
        options.append(
            {
                "url": absolute,
                "provider": "official website",
                "source_page": page_url,
                "attribution": "Official website page image",
            }
        )
        if len(options) == 8:
            break
    return options


def _document_from_page(
    page: Any, source_name: str, source_root: str
) -> dict[str, Any]:
    page_url = _canonical_url(str(getattr(page, "url", ""))) or source_root
    return {
        "source_name": source_name,
        "title": _title(page),
        "text": _container_text(page),
        "url": page_url,
        "published": _published_date(page),
        "links": _external_evidence_links(page, page_url),
        "structured_candidates": _structured_candidates(
            page, source_name, source_root, page_url
        ),
    }


def _is_interesting_same_domain_link(
    href: str, anchor_text: str, page_url: str, source_root: str, rel: str = ""
) -> bool:
    candidate = _canonical_url(href, page_url)
    if not candidate or not _same_domain(candidate, source_root):
        return False
    parts = urlsplit(candidate)
    path = parts.path.lower()
    if any(path.endswith(suffix) for suffix in NON_DOCUMENT_SUFFIXES):
        return False
    combined = f"{path} {parts.query} {anchor_text.lower()} {rel.lower()}"
    if any(term in combined for term in SKIP_LINK_TERMS):
        return False
    if "next" in rel.lower() or any(term in combined for term in PAGINATION_TERMS):
        return True
    return any(term in combined for term in INTERESTING_TERMS)


def _link_priority(url: str, anchor_text: str, rel: str = "") -> int:
    """Rank likely individual editorial pages above navigation and taxonomy pages."""
    parts = urlsplit(url)
    path = parts.path.lower()
    text = anchor_text.lower()
    score = 0
    if re.search(r"/20\d{2}/(?:0?[1-9]|1[0-2])/", path):
        score += 30  # Common dated WordPress article permalink.
    if any(word in path for word in ("/article", "/story", "/post", "/news/")):
        score += 12
    if re.search(r"/(?:experience|restaurants)/[^/]+/?$", path):
        score += 25  # Tourism venue-detail pages outrank menu/category navigation.
    if any(
        word in text
        for word in ("food notes", "opening", "restaurant", "dining", "food")
    ):
        score += 6
    if "next" in rel.lower() or any(word in text for word in PAGINATION_TERMS):
        score += 3
    if any(word in path for word in ("/category/", "/tag/", "/author/", "/page/")):
        score -= 8
    return score


def _dated_permalink_key(url: str) -> int:
    """Return YYYYMMDD for dated public permalinks, otherwise zero.

    This makes current feed/article URLs deterministically outrank a source
    homepage's old sidebar or popular-post links. It is an ordering hint only;
    full pages and their own publication metadata remain the actual evidence.
    """
    match = re.search(
        r"/((?:19|20)\d{2})/(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01])(?:/|$)",
        urlsplit(url).path,
    )
    if not match:
        return 0
    return int(
        f"{int(match.group(1)):04d}{int(match.group(2)):02d}{int(match.group(3)):02d}"
    )


def _recent_first_urls(urls: Iterable[str]) -> list[str]:
    """Deduplicate URLs and sort dated current items ahead of undated/old links."""
    seen: set[str] = set()
    ordered: list[tuple[int, int, str]] = []
    for index, url in enumerate(urls):
        if url not in seen:
            seen.add(url)
            ordered.append((-_dated_permalink_key(url), index, url))
    return [url for _, _, url in sorted(ordered)]


def _interesting_links(page: Any, page_url: str, source_root: str) -> list[str]:
    scored: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    try:
        anchors = page.css("a[href]")
    except Exception:
        return []
    for anchor in anchors:
        href = anchor.attrib.get("href") if getattr(anchor, "attrib", None) else None
        if not isinstance(href, str):
            continue
        try:
            anchor_text = _normalise_space(" ".join(anchor.css("::text").getall()))
        except Exception:
            anchor_text = ""
        rel = (
            str(anchor.attrib.get("rel", "")) if getattr(anchor, "attrib", None) else ""
        )
        if not _is_interesting_same_domain_link(
            href, anchor_text, page_url, source_root, rel
        ):
            continue
        absolute = _canonical_url(href, page_url)
        if absolute and absolute not in seen:
            seen.add(absolute)
            scored.append(
                (-_link_priority(absolute, anchor_text, rel), len(scored), absolute)
            )
    return _recent_first_urls(url for _, _, url in sorted(scored))


def _rss_article_links(page: Any, feed_url: str, source_root: str) -> list[str]:
    """Read public RSS/Atom item links after Fetcher has retrieved and parsed it.

    Scrapling's documented static selector intentionally is not an XML parser.
    ``Fetcher`` still performs the sole HTTP request, while this helper scopes its
    literal-link parsing to raw public ``<item>`` elements.  HTML-mode parsing turns
    XML ``<link>`` into a void element, so extracting it from ``page.css('item')``
    would incorrectly absorb the rest of the item.
    """
    # Confirm/anchor the response in Scrapling's parsed document.  The original
    # response body is then used only to preserve XML's non-HTML link semantics.
    try:
        parsed_item_count = len(page.css("item"))
    except Exception:
        parsed_item_count = 0
    raw_items = re.findall(r"<item\b[^>]*>.*?</item\s*>", _page_html(page), re.I | re.S)
    item_markup = raw_items[:parsed_item_count] if parsed_item_count else raw_items
    links: list[str] = []
    seen: set[str] = set()
    for item in item_markup:
        match = re.search(
            r"(?<!:)<link\b[^>]*>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</link\s*>",
            str(item),
            re.I | re.S,
        )
        if not match:
            continue
        candidate = _canonical_url(unescape(match.group(1).strip()), feed_url)
        if not candidate or not _same_domain(candidate, source_root):
            continue
        if candidate == _canonical_url(feed_url) or candidate in seen:
            continue
        if any(
            urlsplit(candidate).path.lower().endswith(suffix)
            for suffix in NON_DOCUMENT_SUFFIXES
        ):
            continue
        seen.add(candidate)
        links.append(candidate)
    return _recent_first_urls(links)


def _is_source_listing_page(page_url: str, source_root: str, page_kind: str) -> bool:
    """Roots and feeds seed article discovery; they are never dated evidence docs."""
    if page_kind in {"root", "feed"}:
        return True
    if _canonical_url(page_url) == _canonical_url(source_root):
        return True
    segments = [
        segment for segment in urlsplit(page_url).path.lower().split("/") if segment
    ]
    return bool(segments and segments[-1] in GENERIC_LISTING_SEGMENTS)


class DomainPolicy:
    """A synchronous robots cache and polite per-domain request governor."""

    def __init__(self, minimum_delay: float = 2.0) -> None:
        self.minimum_delay = max(2.0, float(minimum_delay))
        self._robots: dict[str, RobotsRules] = {}
        self._last_request: dict[str, float] = {}
        self._lock = threading.Lock()

    def _wait_for_slot(self, url: str, delay: float) -> None:
        key = _domain_key(url)
        with self._lock:
            now = time.monotonic()
            last = self._last_request.get(key)
            wait = max(0.0, delay - (now - last)) if last is not None else 0.0
        if wait:
            time.sleep(wait)
        with self._lock:
            self._last_request[key] = time.monotonic()

    def _fetch(self, url: str, delay: float) -> Any:
        self._wait_for_slot(url, delay)
        # This is intentionally static HTTP only: no stealth headers, cookies,
        # proxies, logins, browser automation, or fallback path for a block.
        return Fetcher.get(
            url,
            timeout=REQUEST_TIMEOUT,
            retries=REQUEST_RETRIES,
            headers=REQUEST_HEADERS,
            stealthy_headers=False,
            follow_redirects="safe",
        )

    def robots_for(self, url: str, errors: list[str] | None = None) -> RobotsRules:
        key = _domain_key(url)
        if key in self._robots:
            return self._robots[key]
        parts = urlsplit(url)
        robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
        try:
            response = self._fetch(robots_url, self.minimum_delay)
            status = int(getattr(response, "status", 0))
        except (
            Exception
        ) as exc:  # Network failures must be visible and block conservatively.
            rules = RobotsRules(
                allowed=False, error=f"robots_fetch_error: {type(exc).__name__}: {exc}"
            )
            if errors is not None:
                errors.append(rules.error)
            self._robots[key] = rules
            return rules
        if status == 404:
            rules = RobotsRules()
        elif status == 200:
            rules = _parse_robots(_decode_body(getattr(response, "body", b"")))
        else:
            rules = RobotsRules(
                allowed=False,
                error=f"robots_status_{status}: cannot verify permission",
            )
            if errors is not None:
                errors.append(rules.error)
        self._robots[key] = rules
        return rules

    def get_allowed(
        self, url: str, errors: list[str] | None = None
    ) -> tuple[Any | None, str | None]:
        rules = self.robots_for(url, errors)
        if not rules.can_fetch(USER_AGENT, url):
            return None, "robots_disallowed"
        delay = max(self.minimum_delay, rules.crawl_delay or 0.0)
        try:
            return self._fetch(url, delay), None
        except Exception as exc:
            message = f"network_error: {type(exc).__name__}: {exc}"
            if errors is not None:
                errors.append(message)
            return None, message


# A persistent gate makes one-off official-page calls polite when callers repeat them.
_OFFICIAL_PAGE_POLICY = DomainPolicy()


def fetch_official_page(url: str) -> dict[str, Any] | None:
    """Fetch one public official page, honoring robots and no-bypass domain policy.

    Returns full public page evidence for image gathering. ``None`` means a robots
    refusal, network failure, redirect/block status, or non-successful HTTP response.
    """
    canonical = _canonical_url(url)
    if not canonical:
        return None
    errors: list[str] = []
    response, problem = _OFFICIAL_PAGE_POLICY.get_allowed(canonical, errors)
    if response is None or problem:
        return None
    status = int(getattr(response, "status", 0))
    if status != 200:
        # Explicitly do not retry blocks with stealth/proxies or attempt login.
        return None
    final_url = _canonical_url(str(getattr(response, "url", canonical))) or canonical
    return {
        "title": _title(response),
        "text": _container_text(response),
        "html": _page_html(response),
        "url": final_url,
        "image_options": _image_options(response, final_url),
        "structured_candidates": _structured_candidates(
            response, "Official page", final_url, final_url
        ),
    }


def _load_config(
    config_path: str | Path | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    with path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config, dict):
        raise ValueError("Discovery config must be a JSON object")
    sources: list[dict[str, Any]] = []
    for group_name in ("sources_food_news", "tourism"):
        group = config.get(group_name, [])
        if not isinstance(group, list):
            raise ValueError(f"{group_name} must be a list")
        for source in group:
            if not isinstance(source, dict):
                continue
            name = _normalise_space(str(source.get("name", "")))
            root = _canonical_url(str(source.get("root", "")))
            if name and root:
                sources.append({"name": name, "root": root, **source})
    if not sources:
        raise ValueError("Discovery config has no usable source roots")
    return config, sources


def collect_documents(
    config_path: str | Path | None = None, max_pages: int | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Collect public source documents within fixed page/depth limits.

    ``max_pages`` can lower the configured global cap for a smoke run, but cannot
    make a source exceed its configured (or default 12-page) cap.  The return value
    is JSON serializable and deliberately contains evidence rather than inferred
    restaurant records.
    """
    config, sources = _load_config(config_path)
    crawl = config.get("crawl", {}) if isinstance(config.get("crawl", {}), dict) else {}
    configured_total = int(crawl.get("max_pages", 90))
    total_limit = min(90, configured_total)
    if max_pages is not None:
        total_limit = min(total_limit, max(0, int(max_pages)))
    per_source_default = min(12, max(1, int(crawl.get("max_pages_per_source", 10))))
    max_depth = min(2, max(0, int(crawl.get("max_depth", 2))))
    minimum_delay = max(2.0, float(crawl.get("min_domain_delay_seconds", 2.0)))
    max_elapsed_seconds = min(
        900.0, max(1.0, float(crawl.get("max_elapsed_seconds", 900)))
    )
    policy = DomainPolicy(minimum_delay)
    started_monotonic = time.monotonic()

    source_stats: list[dict[str, Any]] = [
        {
            "name": source["name"],
            "root": source["root"],
            "requests": 0,
            "documents": 0,
            "errors": [],
        }
        for source in sources
    ]
    stats: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "page_count": 0,
        "document_count": 0,
        "request_count": 0,
        "blocked_count": 0,
        "robots_blocked_count": 0,
        "network_error_count": 0,
        "max_pages": total_limit,
        "max_pages_per_source": per_source_default,
        "max_depth": max_depth,
        "max_elapsed_seconds": max_elapsed_seconds,
        "elapsed_seconds": 0.0,
        "time_limit_reached": False,
        "skipped_short_document_count": 0,
        "sources": source_stats,
    }
    documents: list[dict[str, Any]] = []
    fetched_pages = 0

    for source, source_stat in zip(sources, source_stats):
        if (
            fetched_pages >= total_limit
            or time.monotonic() - started_monotonic >= max_elapsed_seconds
        ):
            stats["time_limit_reached"] = (
                time.monotonic() - started_monotonic >= max_elapsed_seconds
            )
            break
        # The configured source maximum includes the source root/feed pages and
        # individual article requests. It is never allowed above 12.
        source_page_limit = min(
            12,
            max(1, int(source.get("max_pages", per_source_default))),
        )
        queue: deque[tuple[str, int, str]] = deque([(source["root"], 0, "root")])
        feed_url = _canonical_url(str(source.get("feed", "")), source["root"])
        if feed_url and _same_domain(feed_url, source["root"]):
            queue.append((feed_url, 0, "feed"))
        seen: set[str] = set()
        source_fetches = 0
        while (
            queue and fetched_pages < total_limit and source_fetches < source_page_limit
        ):
            if time.monotonic() - started_monotonic >= max_elapsed_seconds:
                stats["time_limit_reached"] = True
                break
            requested_url, depth, page_kind = queue.popleft()
            canonical = _canonical_url(requested_url)
            if not canonical or canonical in seen:
                continue
            seen.add(canonical)

            # A target URL is a bounded crawl page even when it returns a block
            # or error. Robots requests remain internal policy checks.
            source_fetches += 1
            fetched_pages += 1
            response, problem = policy.get_allowed(canonical, source_stat["errors"])
            if response is None:
                if problem == "robots_disallowed":
                    stats["robots_blocked_count"] += 1
                else:
                    stats["network_error_count"] += 1
                continue
            source_stat["requests"] += 1
            stats["request_count"] += 1
            status = int(getattr(response, "status", 0))
            if status in {401, 403, 429}:
                stats["blocked_count"] += 1
                source_stat["errors"].append(f"http_{status}: no bypass attempted")
                continue
            if status != 200:
                source_stat["errors"].append(f"http_{status}")
                continue

            final_url = (
                _canonical_url(str(getattr(response, "url", canonical))) or canonical
            )
            # A redirect outside the source domain is not a same-domain crawl target.
            if not _same_domain(final_url, source["root"]):
                source_stat["errors"].append(
                    f"redirected_off_source_domain: {final_url}"
                )
                continue

            # Source roots, taxonomy/listing entry points, and feeds are useful
            # traversal evidence but cannot masquerade as individual, dated news.
            if not _is_source_listing_page(final_url, source["root"], page_kind):
                document = _document_from_page(response, source["name"], source["root"])
                if len(document["text"]) >= 80:
                    documents.append(document)
                    source_stat["documents"] += 1
                else:
                    stats["skipped_short_document_count"] += 1
                    source_stat["errors"].append(f"skipped_short_text: {final_url}")
            if depth >= max_depth:
                continue
            child_urls = _interesting_links(response, final_url, source["root"])
            if page_kind == "feed":
                # Feed item URLs are deliberately added first; full public pages
                # are fetched rather than treating syndication snippets as evidence.
                feed_links = _rss_article_links(response, final_url, source["root"])
                child_urls = feed_links + child_urls
            queued: set[str] = set()
            queue_entries: list[tuple[str, int, str]] = []
            for child_url in child_urls:
                if child_url not in seen and child_url not in queued:
                    queued.add(child_url)
                    queue_entries.append((child_url, depth + 1, "content"))
            if page_kind == "feed":
                # The feed itself is a discovery hint only. Process its item
                # pages before previously queued category/navigation pages.
                for entry in reversed(queue_entries):
                    queue.appendleft(entry)
            else:
                queue.extend(queue_entries)

    stats["page_count"] = fetched_pages
    stats["document_count"] = len(documents)
    stats["elapsed_seconds"] = round(time.monotonic() - started_monotonic, 3)
    stats["finished_at"] = datetime.now(timezone.utc).isoformat()
    return documents, stats


__all__ = ["collect_documents", "fetch_official_page"]
