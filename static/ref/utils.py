"""
src/AI/utils.py — Shared utilities for MRJ4.15

Handles:
- Supabase image upload
- Local image upload
- Base64 helpers
"""

import os
import base64
import binascii
import io
import re
import uuid
from pathlib import Path
from typing import Optional

from PIL import Image

# ── PATHS ───────────────────────────────────────────────────────

ROOT       = Path(__file__).resolve().parents[2]
UPLOAD_DIR = ROOT / "data" / "uploads"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp"}
DATA_URL_RE = re.compile(r"^data:(image/(?:jpeg|png|webp));base64,(.+)$", re.IGNORECASE | re.DOTALL)
PIL_FORMAT_MIMES = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}


# ── BASE64 HELPERS ──────────────────────────────────────────────

def strip_data_url(data_url: str) -> tuple[str, str]:
    """
    Strip the data URI header from a base64 string.
    Returns (mime_type, raw_base64).
    """
    if data_url.startswith("data:"):
        header, b64 = data_url.split(",", 1)
        mime = header.split(";")[0].replace("data:", "")
        return mime, b64
    return "image/jpeg", data_url


def base64_to_bytes(b64: str) -> bytes:
    return base64.b64decode(b64)


def bytes_to_base64(data: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def normalize_image_data_url(data_url: str) -> str:
    """
    Return a data URL whose MIME header matches the actual image bytes.
    Browsers can occasionally provide a stale data:image/png header for JPEG bytes.
    """
    mime, data = validate_image_data_url(data_url)
    return bytes_to_base64(data, mime)


def validate_image_data_url(
    data_url: str,
    *,
    max_bytes: int = 4 * 1024 * 1024,
    max_pixels: int = 16_000_000,
) -> tuple[str, bytes]:
    """
    Validate an uploaded data URL before storage or AI processing.
    Returns (mime, bytes). Raises ValueError with client-safe messages.
    """
    if not isinstance(data_url, str):
        raise ValueError("Ongeldige afbeelding.")

    match = DATA_URL_RE.match(data_url.strip())
    if not match:
        raise ValueError("Gebruik een JPG, PNG of WEBP afbeelding.")

    mime = match.group(1).lower()
    raw_b64 = match.group(2)
    if mime not in ALLOWED_IMAGE_MIMES:
        raise ValueError("Gebruik een JPG, PNG of WEBP afbeelding.")

    try:
        data = base64.b64decode(raw_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("De afbeelding kon niet worden gelezen.") from exc

    if not data:
        raise ValueError("De afbeelding is leeg.")
    if len(data) > max_bytes:
        raise ValueError("De afbeelding is te groot. Maximaal 4MB toegestaan.")

    try:
        with Image.open(io.BytesIO(data)) as img:
            actual_mime = PIL_FORMAT_MIMES.get((img.format or "").upper())
            width, height = img.size
            img.verify()
    except Exception as exc:
        raise ValueError("De afbeelding kon niet worden gelezen.") from exc

    if actual_mime not in ALLOWED_IMAGE_MIMES:
        raise ValueError("Gebruik een JPG, PNG of WEBP afbeelding.")

    if width <= 0 or height <= 0 or width * height > max_pixels:
        raise ValueError("De afbeelding heeft een te hoge resolutie.")

    return actual_mime, data


# ── LOCAL UPLOAD ────────────────────────────────────────────────

def save_upload_locally(data_url: str, ext: str = "jpg") -> Path:
    """
    Save a base64 image to data/uploads/<uuid>.<ext>.
    Returns the file path.
    """
    mime, data = validate_image_data_url(data_url)
    ext_map = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
    ext = ext_map.get(mime, "jpg")
    filename = f"{uuid.uuid4()}.{ext}"
    path = UPLOAD_DIR / filename
    path.write_bytes(data)
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
        mime, data = validate_image_data_url(data_url)
        ext_map   = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
        ext       = ext_map.get(mime, "jpg")
        filename  = f"{uuid.uuid4()}.{ext}"

        response = client.storage.from_(SUPABASE_BUCKET).upload(
            path=filename,
            file=data,
            file_options={"content-type": mime},
        )
        public_url = client.storage.from_(SUPABASE_BUCKET).get_public_url(filename)
        return public_url
    except Exception:
        return None
