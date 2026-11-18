"""Read-only Commons candidates for an *illustrative* literal-dish fallback.

The caller must exhaust official venue photos first and subsequently perform
image decoding, visual dish validation, and perceptual duplicate checks. This
module neither downloads images nor establishes ownership or venue provenance.
A process/module-lifetime counter bounds attempted API requests; 403/429 disables
further requests for that lifetime. There are no retries or pagination.
"""

from __future__ import annotations

import html
from html.parser import HTMLParser
import json
import os
import re
import threading
import unicodedata
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import requests

COMMONS_API_URL = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = (
    "CulineraIllustrativeDishPhotoReview/1.0 (+https://seo-phase.emergent.host)"
)
IMAGE_CAPTION = "Illustrative image — not photographed at this venue"
MODULE_WARNINGS: list[str] = []
requests_count = 0
_COUNTER_LOCK = threading.Lock()
_BLOCKED = False


def _warn(message: str) -> None:
    # Messages are generated locally, never interpolated from untrusted API HTML.
    with _COUNTER_LOCK:
        if message not in MODULE_WARNINGS:
            MODULE_WARNINGS.append(message)


def _request_budget() -> int:
    try:
        configured = int(os.environ.get("MAX_FREE_IMAGE_REQUESTS", "30"))
    except (TypeError, ValueError):
        _warn("Wikimedia Commons: invalid MAX_FREE_IMAGE_REQUESTS; using 30.")
        return 30
    return max(0, min(configured, 30))


MAX_FREE_IMAGE_REQUESTS = _request_budget()
HTTP = requests.Session()
# No netrc credentials, proxy credentials, accounts, redirects, or retry adapters.
HTTP.trust_env = False
HTTP.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))


class _PlainText(HTMLParser):
    """Discard markup, attributes, and active/hidden element contents."""

    _blocked_tags = {
        "script",
        "style",
        "iframe",
        "object",
        "embed",
        "svg",
        "math",
        "template",
        "noscript",
        "head",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.blocked: list[str] = []

    def handle_starttag(self, tag, attrs):
        if self.blocked or tag in self._blocked_tags:
            if tag not in {"br", "hr", "img", "input", "meta", "link", "embed"}:
                self.blocked.append(tag)
            return
        if tag in {"br", "p", "div", "li", "tr", "td", "hr"}:
            self.parts.append(" ")

    def handle_startendtag(self, tag, attrs):
        if not self.blocked and tag not in self._blocked_tags:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if self.blocked:
            if tag in self.blocked:
                reverse_index = self.blocked[::-1].index(tag)
                del self.blocked[len(self.blocked) - reverse_index - 1 :]
            return
        if tag in {"p", "div", "li", "tr", "td"}:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.blocked:
            self.parts.append(data)


def _plain(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if not isinstance(value, str):
        return ""
    # Decode nested entities before parsing so encoded tags cannot reappear later.
    text = value
    for _ in range(8):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    parser = _PlainText()
    try:
        parser.feed(text)
        parser.close()
        text = "".join(parser.parts)
    except (ValueError, RecursionError):
        return ""
    text = text.replace("<", " ").replace(">", " ")
    text = "".join(
        ch
        for ch in text
        if not unicodedata.category(ch).startswith("C") or ch.isspace()
    )
    return " ".join(text.split())


def _active_markup(value) -> bool:
    if not isinstance(value, str):
        return False
    for _ in range(8):
        decoded = html.unescape(value)
        if decoded == value:
            break
        value = decoded
    return bool(
        re.search(
            r"<\s*(?:script|style|iframe|object|embed|svg|math|template|noscript|head)\b",
            value,
            re.IGNORECASE,
        )
    )


def _plain_tree(value):
    """Preserve complete metadata structure without returning executable HTML."""
    if isinstance(value, dict):
        return {
            clean_key: _plain_tree(item)
            for key, item in value.items()
            if (clean_key := _plain(key))
        }
    if isinstance(value, list):
        return [_plain_tree(item) for item in value]
    return _plain(value)


def _literal(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(re.findall(r"[^\W_]+", text, flags=re.UNICODE))


def _fixed_url(value, hostname: str, *, allow_http=False) -> str:
    """No URL is ever fetched except the fixed API endpoint; validate return URLs."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not value or any(ch.isspace() or ord(ch) < 32 or ch in '\\<>"`' for ch in value):
        return ""
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ({"http", "https"} if allow_http else {"https"})
            or parsed.hostname != hostname
            or parsed.netloc.lower() != hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith("/")
            or re.search(r"%(?![0-9a-fA-F]{2})", parsed.path)
        ):
            return ""
        decoded = unquote(parsed.path, errors="strict")
        if (
            any(ord(ch) < 32 or ch in '\\<>"`' for ch in decoded)
            or any(piece in {".", ".."} for piece in decoded.split("/"))
            or re.search(r"%[0-9a-fA-F]{2}", decoded)
        ):
            return ""
        return urlunsplit(
            ("https", hostname, quote(decoded, safe="/:@-._~!$&'()+,;="), "", "")
        )
    except (ValueError, UnicodeError):
        return ""


def _licence(metadata: dict[str, str]):
    name = metadata.get("LicenseShortName", "")
    by_names = {f"CC BY {version}": version for version in ("2.0", "2.5", "3.0", "4.0")}
    if name in by_names:
        version = by_names[name]
        expected_path = f"/licenses/by/{version}/"
        expected_internal = {f"cc-by-{version}"}
        allowed_terms = {
            name,
            f"Creative Commons Attribution {version}",
            f"Creative Commons Attribution {version} Generic",
            f"Creative Commons Attribution {version} Unported",
            f"Creative Commons Attribution {version} International",
        }
        needs_artist = True
    elif name in {"CC0", "CC0 1.0"}:
        expected_path = "/publicdomain/zero/1.0/"
        expected_internal = {"cc-zero", "cc0", "cc0-1.0"}
        allowed_terms = {
            "CC0",
            "CC0 1.0",
            "CC0 1.0 Universal",
            "Creative Commons CC0 Waiver",
            "Creative Commons Zero, Public Domain Dedication",
            "Creative Commons CC0 1.0 Universal Public Domain Dedication",
        }
        needs_artist = False
    else:
        return None
    # Historical Commons records may link HTTP; normalize only the exact official
    # host to HTTPS. No deed, jurisdiction, query, or alternative licence paths.
    url = _fixed_url(metadata.get("LicenseUrl"), "creativecommons.org", allow_http=True)
    if not url or urlsplit(url).path.rstrip("/") + "/" != expected_path:
        return None
    internal = metadata.get("License", "")
    if internal and internal.casefold() not in expected_internal:
        return None
    terms = metadata.get("UsageTerms", "")
    if terms and terms not in allowed_terms:
        return None
    permission = metadata.get("Permission", "")
    # Permission is free-form: reject conflicting/multiple licence hints, retaining
    # all other instructions in the evidence instead of silently discarding them.
    if re.search(
        r"\bgfdl\b|gnu free documentation|\b(?:nc|nd|sa)\b|share[ -]?alike|"
        r"non[ -]?commercial|no[ -]?derivatives|multi[ -]?licen|dual[ -]?licen|"
        r"or later|all rights reserved",
        permission,
        flags=re.IGNORECASE,
    ):
        return None
    permission_versions = re.findall(
        r"\bcc[ -]?by[ -]?(\d\.\d)\b", permission, re.IGNORECASE
    )
    if needs_artist and re.search(
        r"\bcc[ -]?0\b|\bcc[ -]?zero\b", permission, re.IGNORECASE
    ):
        return None
    if permission_versions and (
        not needs_artist or any(v != by_names[name] for v in permission_versions)
    ):
        return None
    return name, "https://creativecommons.org" + expected_path, needs_artist


def _identifiable_artist(artist: str) -> bool:
    return bool(artist) and _literal(artist) not in {
        "own work",
        "self",
        "unknown",
        "unknown author",
        "anonymous",
        "n a",
        "none",
        "not provided",
        "not specified",
        "see above",
        "see below",
        "author",
        "photographer",
        "wikimedia commons",
    }


def _dimensions(width, height) -> bool:
    return type(width) is int and type(height) is int and width >= 600 and height >= 350


def _option(page, dish: str):
    if not isinstance(page, dict) or page.get("ns") != 6 or "missing" in page:
        return None
    title = page.get("title")
    if (
        not isinstance(title, str)
        or not title.startswith("File:")
        or _plain(title) != title
    ):
        return None
    infos = page.get("imageinfo")
    if not isinstance(infos, list) or not infos or not isinstance(infos[0], dict):
        return None
    info = infos[0]
    if info.get("mime") not in {"image/jpeg", "image/png", "image/webp"}:
        return None
    if info.get("mediatype") != "BITMAP" or not _dimensions(
        info.get("width"), info.get("height")
    ):
        return None
    original_url = _fixed_url(info.get("url"), "upload.wikimedia.org")
    source_page = _fixed_url(info.get("descriptionurl"), "commons.wikimedia.org")
    if not original_url or not urlsplit(original_url).path.startswith(
        "/wikipedia/commons/"
    ):
        return None
    if not source_page:
        return None
    source_path = unquote(urlsplit(source_page).path)
    if not source_path.startswith("/wiki/File:"):
        return None
    file_title = source_path[len("/wiki/") :]
    if (
        file_title.replace("_", " ") != title.replace("_", " ")
        or not re.search(r"\.(?:jpe?g|png|webp)$", file_title, re.IGNORECASE)
        or any(ch in file_title[len("File:") :] for ch in "#[]{}|:")
    ):
        return None
    url = original_url
    if "thumburl" in info:
        url = _fixed_url(info["thumburl"], "upload.wikimedia.org")
        if (
            not url
            or not urlsplit(url).path.startswith("/wikipedia/commons/")
            or not _dimensions(info.get("thumbwidth"), info.get("thumbheight"))
            or (
                "thumbmime" in info
                and info["thumbmime"] not in {"image/jpeg", "image/png", "image/webp"}
            )
        ):
            return None
    extmetadata = info.get("extmetadata")
    if not isinstance(extmetadata, dict):
        return None
    metadata: dict[str, str] = {}
    for key, record in extmetadata.items():
        if (
            not isinstance(key, str)
            or not isinstance(record, dict)
            or "value" not in record
        ):
            return None
        value = record["value"]
        if key in {"NonFree", "DeletionReason", "Restrictions"}:
            # A flag cannot become safe just because its HTML has no visible text.
            if value is not None and value not in ("", [], {}):
                if not isinstance(value, str) or value.strip():
                    return None
        if key in {
            "LicenseShortName",
            "LicenseUrl",
            "License",
            "UsageTerms",
            "Permission",
        }:
            if _active_markup(value):
                return None
        if key in {"LicenseShortName", "LicenseUrl", "License"}:
            if not isinstance(value, str) or _plain(value) != value.strip():
                return None
        if isinstance(value, (dict, list)) and key != "Restrictions":
            return None
        metadata[_plain(key)] = (
            json.dumps(_plain_tree(value), ensure_ascii=False)
            if isinstance(value, (dict, list)) and value
            else _plain(value)
        )
    # NonFree=false is deliberately still a nonempty flag: fail closed.
    if any(
        metadata.get(key, "") for key in ("NonFree", "DeletionReason", "Restrictions")
    ):
        return None
    needle = " " + _literal(dish) + " "
    if not any(
        needle in " " + _literal(text) + " "
        for text in (
            metadata.get("ImageDescription", ""),
            metadata.get("ObjectName", ""),
            title[len("File:") :],
        )
    ):
        return None
    licence = _licence(metadata)
    if licence is None:
        return None
    name, licence_url, needs_artist = licence
    artist = metadata.get("Artist", "")
    required = metadata.get("AttributionRequired", "").casefold()
    if required not in {"", "true", "false", "1", "0", "yes", "no"}:
        return None
    if needs_artist and required in {"false", "0", "no"}:
        return None
    if (needs_artist or required in {"true", "1", "yes"}) and not _identifiable_artist(
        artist
    ):
        return None
    parts = []
    if metadata.get("Attribution"):
        parts.append("Source attribution: " + metadata["Attribution"])
    parts.append(
        "Artist (as recorded): " + artist
        if _identifiable_artist(artist)
        else "Author not identified in source metadata"
    )
    if metadata.get("Credit"):
        parts.append("Source credit: " + metadata["Credit"])
    parts.extend(
        ["Wikimedia Commons", f"{name} ({licence_url})", "File page: " + source_page]
    )
    source_metadata = dict(metadata)
    source_metadata.update(
        {
            "title": _plain(title),
            "mime": info["mime"],
            "mediatype": info["mediatype"],
            "width": str(info["width"]),
            "height": str(info["height"]),
            "source_url": original_url,
            "source_page": source_page,
            "attribution_required": (
                "true" if needs_artist or required in {"true", "1", "yes"} else "false"
            ),
        }
    )
    evidence = json.dumps(
        {
            "notice": "Source metadata recorded only; ownership and depicted venue not verified.",
            "source_page": source_page,
            "title": _plain(title),
            "extmetadata": _plain_tree(extmetadata),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return {
        "provider": "Wikimedia Commons",
        "url": url,
        "source_page": source_page,
        "attribution": " — ".join(parts),
        "licence_name": name,
        "licence_url": licence_url,
        "rights_status": "source_licence_recorded",
        "is_illustrative": True,
        "image_caption": IMAGE_CAPTION,
        "dish": dish,
        "source_metadata": source_metadata,
        "licence_evidence": evidence,
    }


def search_licensed_dish_images(dish: str, limit: int = 3) -> list[dict]:
    """Return at most five licensed candidates, using at most one API GET.

    ``MODULE_WARNINGS`` and ``requests_count`` are accessible for caller reports.
    Empty, failed, blocked, malformed, or rights-ambiguous results return ``[]``.
    MAX_FREE_IMAGE_REQUESTS is read once at import and clamped to [0, 30].
    """
    global requests_count, _BLOCKED
    if not isinstance(dish, str) or type(limit) is not int or limit <= 0:
        _warn("Wikimedia Commons: invalid dish or result limit; no request made.")
        return []
    dish = _plain(dish)
    if not dish or not _literal(dish) or len(dish) > 200:
        _warn("Wikimedia Commons: empty or oversized dish; no request made.")
        return []
    limit = min(limit, 5)
    with _COUNTER_LOCK:
        if _BLOCKED:
            return []
        if requests_count >= MAX_FREE_IMAGE_REQUESTS:
            message = "Wikimedia Commons: free-image request budget exhausted."
            if message not in MODULE_WARNINGS:
                MODULE_WARNINGS.append(message)
            return []
        requests_count += 1
    response = None
    try:
        response = HTTP.get(
            COMMONS_API_URL,
            params={
                "action": "query",
                "generator": "search",
                "gsrsearch": dish,
                "gsrnamespace": 6,
                "gsrlimit": limit,
                "prop": "imageinfo",
                "iiprop": "url|mime|size|mediatype|extmetadata",
                "iiurlwidth": 1000,
                "iilimit": 1,
                "iiextmetadatalanguage": "en",
                "format": "json",
            },
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=(5, 12),
            allow_redirects=False,
        )
        if response.status_code in {403, 429}:
            with _COUNTER_LOCK:
                _BLOCKED = True
            _warn(
                f"Wikimedia Commons: HTTP {response.status_code}; lookups disabled for this run (no retries)."
            )
            return []
        if response.status_code != 200:
            _warn("Wikimedia Commons: non-200 response; no retries.")
            return []
        try:
            payload = response.json()
        except (ValueError, TypeError, RecursionError):
            _warn("Wikimedia Commons: malformed JSON response; no retries.")
            return []
        if not isinstance(payload, dict) or "error" in payload:
            _warn("Wikimedia Commons: malformed or API-error response.")
            return []
        if "warnings" in payload:
            _warn(
                "Wikimedia Commons: API returned a warning; source warning HTML not exposed."
            )
        query = payload.get("query")
        if query is None and "batchcomplete" in payload:
            return []
        if not isinstance(query, dict) or not isinstance(query.get("pages"), dict):
            _warn("Wikimedia Commons: malformed pages response.")
            return []
        options = []
        seen = set()
        # Never follow continuation. Even unexpected extra pages are bounded.
        for page in list(query["pages"].values())[:limit]:
            option = _option(page, dish)
            if option and option["source_page"] not in seen:
                seen.add(option["source_page"])
                options.append(option)
        if not options:
            _warn(
                "Wikimedia Commons: no eligible literal-dish candidate in this response."
            )
        return options
    except requests.Timeout:
        _warn("Wikimedia Commons: request timed out; no retries.")
        return []
    except requests.RequestException:
        _warn("Wikimedia Commons: network request failed; no retries.")
        return []
    except (ValueError, TypeError, RecursionError):
        _warn("Wikimedia Commons: malformed source metadata; no candidates returned.")
        return []
    finally:
        if response is not None:
            response.close()
        HTTP.cookies.clear()
