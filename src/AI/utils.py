"""
src/AI/utils.py — Shared utilities for MRJ4.15

Handles:
- Supabase image upload
- Local image upload
- Base64 helpers
"""

import os
import base64
import uuid
import logging
from io import BytesIO
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── PATHS ───────────────────────────────────────────────────────

ROOT       = Path(__file__).resolve().parents[2]
UPLOAD_DIR = ROOT / "data" / "uploads"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


# ── IMAGE FORMAT NORMALISATION ──────────────────────────────────

# Formats natively accepted by both Claude and Gemini APIs.
_API_SUPPORTED: frozenset[str] = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})

# PIL format name → MIME type (for supported formats only)
_PIL_TO_MIME: dict[str, str] = {
    "JPEG": "image/jpeg",
    "PNG":  "image/png",
    "WEBP": "image/webp",
    "GIF":  "image/gif",
}

# Quick magic-byte check so PIL isn't needed for common cases
def _magic_mime(data: bytes) -> Optional[str]:
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def _normalise_image(data: bytes) -> tuple[str, bytes]:
    """
    Detect the true image format and ensure it is accepted by Claude / Gemini.

    - Supported formats (JPEG, PNG, WEBP, GIF) → returned as-is with correct MIME.
    - Unsupported formats (HEIC, BMP, TIFF, AVIF, …) → converted to PNG via PIL.
    - Corrupt / unreadable data → raises ValueError with a user-friendly message.

    Returns (mime_type, image_bytes).
    """
    from PIL import Image, UnidentifiedImageError  # local import — optional dep

    # Fast path: magic bytes for the four common formats
    fast_mime = _magic_mime(data)

    try:
        img = Image.open(BytesIO(data))
        img.load()  # fully decode — raises on truncated / corrupt files
    except UnidentifiedImageError:
        raise ValueError(
            "Onbekend afbeeldingsformaat. Upload een PNG, JPG, JPEG of WEBP bestand."
        )
    except Exception as exc:
        raise ValueError(f"Afbeelding kan niet worden geopend: {exc}")

    pil_fmt  = img.format or ""
    real_mime = _PIL_TO_MIME.get(pil_fmt)

    if real_mime:
        # PIL confirmed a supported format — trust it over magic bytes if they differ
        return real_mime, data

    # Unsupported format (HEIC, HEIF, BMP, TIFF, AVIF, PCX, …) → convert to PNG
    logger.info("Converting unsupported format %r to PNG for API compatibility.", pil_fmt)
    if img.mode not in ("RGB", "RGBA", "L", "LA"):
        img = img.convert("RGBA" if img.mode in ("PA", "RGBA") else "RGB")
    out = BytesIO()
    img.save(out, format="PNG", optimize=False)
    return "image/png", out.getvalue()


def strip_data_url(data_url: str) -> tuple[str, str]:
    """
    Strip the data URI header, normalise the image format, and return
    (mime_type, base64_string).

    - Declared MIME type in the data URL header is ignored; the actual bytes decide.
    - Unsupported formats are transparently converted to PNG.
    - Raises ValueError for corrupt or completely unreadable files.
    """
    if data_url.startswith("data:"):
        _, b64 = data_url.split(",", 1)
    else:
        b64 = data_url

    raw              = base64.b64decode(b64)
    mime, normalised = _normalise_image(raw)

    # Re-encode only when conversion happened (avoids a useless round-trip)
    if normalised is not raw:
        b64 = base64.b64encode(normalised).decode()

    return mime, b64


def base64_to_bytes(b64: str) -> bytes:
    return base64.b64decode(b64)


def bytes_to_base64(data: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


# ── LOCAL UPLOAD ────────────────────────────────────────────────

def save_upload_locally(data_url: str, ext: str = "jpg") -> Path:
    """
    Save a base64 image to data/uploads/<uuid>.<ext>.
    Returns the file path.
    """
    mime, b64 = strip_data_url(data_url)
    ext_map = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
    ext = ext_map.get(mime, "jpg")
    filename = f"{uuid.uuid4()}.{ext}"
    path = UPLOAD_DIR / filename
    path.write_bytes(base64_to_bytes(b64))
    return path


# ── SUPABASE UPLOAD ─────────────────────────────────────────────

def upload_to_supabase(data_url: str) -> Optional[str]:
    """
    Upload an image to Supabase Storage.
    Returns the public URL on success, None if Supabase is not configured.
    Requires SUPABASE_URL and SUPABASE_KEY environment variables.
    """
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_KEY")

    if not supabase_url or not supabase_key:
        # Supabase not configured — fall back to local save only
        return None

    try:
        from supabase import create_client  # type: ignore
        from core import SUPABASE_BUCKET

        client = create_client(supabase_url, supabase_key)
        mime, b64 = strip_data_url(data_url)
        ext_map   = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
        ext       = ext_map.get(mime, "jpg")
        filename  = f"{uuid.uuid4()}.{ext}"

        response = client.storage.from_(SUPABASE_BUCKET).upload(
            path=filename,
            file=base64_to_bytes(b64),
            file_options={"content-type": mime},
        )
        public_url = client.storage.from_(SUPABASE_BUCKET).get_public_url(filename)
        return public_url
    except Exception:
        return None


