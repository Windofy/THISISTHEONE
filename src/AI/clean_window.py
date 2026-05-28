"""
src/AI/clean_window.py — Pre-processing: auto-remove existing window treatments.

Uses Gemini image editing to inpaint existing blinds/curtains/shutters out of
the window area before the main analysis pipeline runs. If the call fails for
any reason the original image is kept and the pipeline continues normally.
"""

import os
import io
import base64
import logging
from typing import Optional

from PIL import Image

log = logging.getLogger(__name__)

_REMOVAL_PROMPT = (
    "Remove ALL existing window treatments from this image. "
    "This includes venetian blinds, roller blinds, pleated shades, curtains, shutters, "
    "drapes, and any other window covering attached to or hanging in front of the window. "
    "Replace them with clean, bare window glass showing the natural outside view or "
    "neutral daylight as it would appear without any covering. "
    "The window frame, reveals, and sill must remain completely visible and unchanged. "
    "Keep EVERYTHING ELSE in the image absolutely identical: walls, floor, ceiling, "
    "furniture, lighting, plants, and all other room elements must remain pixel-perfect "
    "unchanged. Only remove the window treatment itself."
)


def _resize_for_api(image_b64: str, image_mime: str, max_side: int = 1536) -> tuple[bytes, str]:
    """Decode, optionally downscale, re-encode as JPEG. Returns (bytes, mime)."""
    raw = base64.b64decode(image_b64)
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    w, h = img.size
    if w > max_side or h > max_side:
        if w >= h:
            img = img.resize((max_side, round(h * max_side / w)), Image.LANCZOS)
        else:
            img = img.resize((round(w * max_side / h), max_side), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue(), "image/jpeg"


def remove_existing_treatment(image_b64: str, image_mime: str) -> Optional[str]:
    """
    Inpaint existing window treatments out of the image using Gemini.

    Returns a bare base64 string (image/jpeg, no data-URL prefix) on success,
    or None if the call fails — the caller should then fall back to the original image.
    """
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        log.warning("google-generativeai not installed; skipping window treatment removal.")
        return None

    api_key = os.getenv("RENDER_KEY_ALPHA") or os.getenv("GEMINI_API_KEY")
    if not api_key:
        log.warning("No Gemini API key configured; skipping window treatment removal.")
        return None

    try:
        img_bytes, mime = _resize_for_api(image_b64, image_mime)
    except Exception as exc:
        log.warning("Image prep for treatment removal failed: %s", exc)
        return None

    try:
        model_id   = os.getenv("RENDER_MODEL_ALPHA", "gemini-3-pro-image-preview")
        client     = genai.Client(api_key=api_key)
        image_part = types.Part(inline_data=types.Blob(data=img_bytes, mime_type=mime))

        response = client.models.generate_content(
            model=model_id,
            contents=[image_part, _REMOVAL_PROMPT],
            config=types.GenerateContentConfig(max_output_tokens=8192),
        )

        candidates = getattr(response, "candidates", None) or []
        if not candidates or not getattr(candidates[0], "content", None):
            log.warning("Treatment removal: no candidates in Gemini response.")
            return None

        for part in (candidates[0].content.parts or []):
            inline = getattr(part, "inline_data", None)
            if inline and inline.data and inline.mime_type:
                data = inline.data
                if isinstance(data, (bytes, bytearray)):
                    data = base64.b64encode(data).decode()
                log.info("Existing window treatment removed successfully via Gemini.")
                return data  # bare base64, no data-URL prefix

        log.warning("Treatment removal: no inline image returned by Gemini.")
        return None

    except Exception as exc:
        log.warning("Treatment removal Gemini call failed (%s): %s", type(exc).__name__, exc)
        return None
