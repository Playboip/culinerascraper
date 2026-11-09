"""Real-image validation and conservative perceptual duplicate rejection."""

from __future__ import annotations
import base64
import io
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit
import imagehash
import requests
from PIL import Image, ImageStat, UnidentifiedImageError

HTTP = requests.Session()
REQUEST_TIMEOUT = 18
CULINERA_PUBLIC_URL = "https://seo-phase.emergent.host"
IMAGE_MIN_WIDTH, IMAGE_MIN_HEIGHT = 600, 350
IMAGE_PHASH_DISTANCE = 4


def clean_url(url):
    url = str(url or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    try:
        p = urlsplit(url)
        if (
            p.scheme not in {"https", "http"}
            or not p.hostname
            or p.username
            or p.password
        ):
            return ""
        return url
    except ValueError:
        return ""


def public_url(url):
    url = clean_url(url)
    if not url:
        return False
    try:
        p = urlsplit(url)
        return all(
            ipaddress.ip_address(x[4][0]).is_global
            for x in socket.getaddrinfo(
                p.hostname,
                p.port or (443 if p.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        )
    except (ValueError, OSError):
        return False


def image_url_identity(url):
    p = urlsplit(clean_url(url))
    return (
        urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, "", ""))
        if p.netloc
        else ""
    )


@dataclass
class ImageFingerprint:
    url: str
    identity: str
    phash: imagehash.ImageHash
    width: int
    height: int
    bytes_data: bytes


def fetch_image_fingerprint(
    url: str, existing: bool = False
) -> ImageFingerprint | None:
    """Candidates need good dimensions; old images only need to be decodable.

    Redirect targets are checked too. No cookies/login, no source-referrer trick
    to work around hotlink blocking: use the Culinera origin that will show it.
    """
    url = clean_url(url)
    try:
        for _ in range(6):
            if not public_url(url):
                return None
            response = HTTP.get(
                url,
                headers={"Referer": CULINERA_PUBLIC_URL + "/", "Accept": "image/*"},
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
                stream=True,
            )
            if response.is_redirect:
                next_url = urljoin(url, response.headers.get("Location", ""))
                response.close()
                url = next_url
                continue
            response.raise_for_status()
            if (
                not response.headers.get("Content-Type", "")
                .lower()
                .startswith("image/")
            ):
                response.close()
                return None
            content = bytearray()
            for chunk in response.iter_content(65536):
                content.extend(chunk)
                if len(content) > 8_000_000:
                    response.close()
                    return None
            response.close()
            image = Image.open(io.BytesIO(content))
            image.load()
            width, height = image.size
            if not existing:
                if (
                    width < IMAGE_MIN_WIDTH
                    or height < IMAGE_MIN_HEIGHT
                    or not 0.55 <= width / max(height, 1) <= 2.6
                ):
                    return None
                if sum(ImageStat.Stat(image.convert("RGB").resize((64, 64))).var) < 180:
                    return None
            return ImageFingerprint(
                url,
                image_url_identity(url),
                imagehash.phash(image.convert("RGB")),
                width,
                height,
                bytes(content),
            )
        return None
    except (
        requests.RequestException,
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
    ):
        return None


def fingerprint_is_duplicate(fingerprint, used, used_identities):
    return fingerprint.identity in used_identities or any(
        (x.phash - fingerprint.phash) <= IMAGE_PHASH_DISTANCE for x in used
    )


def image_preview_data_url(bytes_data):
    image = Image.open(io.BytesIO(bytes_data)).convert("RGB")
    image.thumbnail((512, 512))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=72, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode(
        "ascii"
    )
