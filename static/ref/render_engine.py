"""
render_engine.py — Unified visualization engine with automatic failover.

Internal codenames:
  ALPHA  → primary render provider
  BETA   → secondary / fallback render provider

No provider, model, or vendor names appear in logs, errors, or HTTP responses.
"""

from __future__ import annotations

import os
import io
import re
import base64
import json
import logging
import time
import warnings
import urllib.request
import urllib.error
from typing import Optional

from PIL import Image, ImageOps

log = logging.getLogger(__name__)

_ALPHA = "ALPHA"
_BETA  = "BETA"


# ── DESCRIPTOR MAPS ───────────────────────────────────────────────────────────
# NOTE: Maps use Dutch keys directly (no alias layer in this engine).
# English keys passed in will fall through to the raw string — use Dutch keys
# at the call site, or add an alias layer if English keys are needed.

STATE_MAP = {
    "Geheel uitgerold": (
        "The blind is fully lowered across the full window height. "
        "The bottom rail must sit exactly at the window sill or bottom edge of the glass area. "
        "The blind is not half-raised, not partially raised, and the bottom rail must never float halfway up the window."
    ),
}

MOUNTING_MAP = {
    "in de dag": """
        **MOUNTING TYPE: INSIDE MOUNT (In de dag)**
        1. **GEOMETRY**: The blind fits STRICTLY BETWEEN the window reveals (jambs).
        2. **WALL PRESERVATION**: The wall surface, architraves, and trim AROUND the window must remain COMPLETELY VISIBLE.
        3. **DEPTH**: The blind is recessed into the wall.
        4. **SHADOWS**: Shadows fall onto the glass or the side reveals of the niche, NOT on the outer wall.
        """,
    "op de dag": """
        **MOUNTING TYPE: OUTSIDE MOUNT (Op de dag)**
        1. **GEOMETRY**: The blind is mounted ON THE FACE of the wall, overlapping the window opening.
        2. **OVERLAP**: The blind must extend 10cm beyond the window opening on Left, Right, and Top.
        3. **OBSCURING**: The blind MUST physically cover the window frame/architraves.
        4. **3D LAYERING**: The blind sits PROUD (forward) from the wall with a hard drop shadow.
        5. **NO RECOLORING**: Do not change the color of the wall or the frame underneath.
        """,
}

LIGHTING_MAP = {
    "Ochtend (Koel)": (
        "MORNING LIGHT. Low angle sunlight from the East. Color Temp: 5500K (Cool/Fresh). "
        "Shadows: Long, crisp shadows projected deep into the room. Atmosphere: Crisp, energetic."
    ),
    "Middag (Helder)": (
        "MID-DAY SUN. High angle overhead sunlight. Color Temp: 6000K (Neutral White). "
        "Shadows: Short, sharp, high-contrast shadows on the window sill and floor. "
        "Atmosphere: Bright, clear, revealing."
    ),
    "Zonsondergang (Warm)": (
        "GOLDEN HOUR. Very low angle sunlight from the West. Color Temp: 3500K (Warm/Orange/Gold). "
        "Shadows: Extremely long, dramatic, stretching across the floor. "
        "Reflections: Warm metallic glow on slats. Atmosphere: Cozy, romantic."
    ),
    "Avond (Sfeervol)": (
        "EVENING/NIGHT. No direct sunlight. Light Source: Artificial interior lamps "
        "(Warm White 2700K). Shadows: Soft, multi-directional from room lights. "
        "Atmosphere: Intimate, dark outside."
    ),
    "Bewolkt (Diffuus)": (
        "OVERCAST/CLOUDY. Diffuse, soft white light (6500K). No hard direct sunlight. "
        "Shadows: Very soft, ambient occlusion only, no hard projection. "
        "Atmosphere: Soft, even, calm."
    ),
}

PRODUCT_MAP = {
    "Houten Jaloezieën": (
        "Horizontal wooden Venetian blinds with many separate, solid wooden slats. "
        "The slats run horizontally from left to right and are stacked vertically from top to bottom. "
        "Material appearance: natural wood, opaque solid slats, visible but subtle wood grain, "
        "matte to low-satin surface, soft diffuse reflections, warm natural tone, and slight organic variation per slat. "
        "Do not make the wood glossy, painted, heavily stained, plastic-like, translucent, fabric-like, or metallic. "
        "Light may pass only through the narrow open gaps between separate horizontal slats, never through the wooden slat material itself."
    ),
    "Aluminium Jaloezieën": (
        "Horizontal aluminum Venetian blinds with many separate, solid metal slats. "
        "The slats run horizontally from left to right and are stacked vertically from top to bottom. "
        "Material appearance: smooth opaque aluminum, clean crisp edges, thin lightweight slats, "
        "subtle metallic sheen, controlled specular highlights, cool neutral reflections, and a precise modern finish. "
        "Do not make the aluminum look like fabric, plastic, wood, glass, mesh, translucent material, or a continuous sheet. "
        "Light may pass only through the narrow open gaps between separate horizontal slats, never through the metal slat material itself."
    ),
}

# LADDER_ALIASES — canonical ladder type resolution.
# Mirrors LADDER_ALIASES in render_gemini.py and the ladderType key in render_blind.py.
LADDER_ALIASES = {
    # Ladder tape
    "ladderband":    "ladder_tape",
    "ladder tape":   "ladder_tape",
    "ladder_tape":   "ladder_tape",
    "tape":          "ladder_tape",
    # Ladder cord
    "ladderkoord":   "ladder_cord",
    "ladder cord":   "ladder_cord",
    "ladder_cord":   "ladder_cord",
    "cord":          "ladder_cord",
    "koord":         "ladder_cord",
    # No ladder
    "none":          "no_ladder",
    "geen":          "no_ladder",
    "no ladder":     "no_ladder",
    "no_ladder":     "no_ladder",
}

_VENETIAN_PRODUCT_LOCK = """
ABSOLUTE PRODUCT CATEGORY LOCK

- Render ONLY horizontal Venetian blinds / jaloezieën.
- The blind must consist of many separate, parallel, horizontal slats.
- Every slat must run left-to-right across the window.
- The slats must be vertically stacked from top to bottom.
- The result must clearly show individual solid slats with small gaps between them.
- The slats must be opaque. Never allow light to pass through the slat material itself.
- Light may only pass through the open spaces between separate horizontal slats.
- Never render a roller blind, fabric roll, Roman shade, pleated blind, curtain, screen, vertical blind, shutter, louvered door, single sheet, continuous fabric panel, translucent panel, mesh, or flat overlay.
- Never merge the slats into one flat surface.
- Never replace the Venetian blind structure with fabric, glass, plastic film, or a smooth panel.
- If uncertain, render fully lowered horizontal Venetian blinds with clearly separated, solid, opaque slats.
""".strip()


# ── HELPERS ───────────────────────────────────────────────────────────────────

_DATA_URL_RE = re.compile(r"^data:(image/\w+);base64,(.*)$", re.DOTALL)


def _split_data_url(image_b64: str) -> tuple[str, bytes]:
    m = _DATA_URL_RE.match(image_b64)
    if m:
        return m.group(1), base64.b64decode(m.group(2))
    return "image/jpeg", base64.b64decode(image_b64)


def _optimize_image(image_b64: str, max_side: int = 1536, quality: int = 85) -> tuple[bytes, str, int, int]:
    """Decode, resize (longest side ≤ max_side), re-encode to JPEG.
    Returns (bytes, mime, width, height) so callers don't need a second decode."""
    _, raw = _split_data_url(image_b64)
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    w, h = img.size
    if w > max_side or h > max_side:
        if w > h:
            img = img.resize((max_side, round(h * max_side / w)), Image.LANCZOS)
        else:
            img = img.resize((round(w * max_side / h), max_side), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue(), "image/jpeg", img.width, img.height


def _normalise_output(data_url: str, target_w: int, target_h: int) -> str:
    """
    Validate, resize, and re-encode the provider response to a consistent
    JPEG output at the exact input dimensions.

    - Catches corrupt or truncated responses before they propagate upstream.
    - Ensures output dimensions always match the optimised input dimensions,
      preventing misalignment at the SAM2 compositing stage.
    - Normalises format to JPEG so render_blind.py (PNG/RGBA) and the AI
      providers (JPEG or PNG) produce a consistent format at the seam.
    - Preserves aspect ratio: if the provider returned a different ratio,
      ImageOps.fit centre-crops before resizing rather than stretching.
    """
    try:
        mime, raw = _split_data_url(data_url)
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        # Integrity check — re-open and verify separately (PIL gotcha)
        with Image.open(io.BytesIO(raw)) as check:
            check.verify()
    except Exception as exc:
        raise RuntimeError(
            "Ongeldige afbeelding ontvangen van render provider."
        ) from exc

    if img.size != (target_w, target_h):
        in_aspect  = target_w / target_h
        out_aspect = img.width / img.height

        if abs(in_aspect - out_aspect) > 0.02:
            # Aspect ratio drifted >2% — centre-crop to target ratio first,
            # then resize. Avoids visible stretching at the cost of a slight
            # edge crop, which is far less noticeable than distortion.
            log.warning(
                "[RENDER] aspect ratio mismatch: input %.3f output %.3f "
                "— centre-cropping to fit %s",
                in_aspect, out_aspect, (target_w, target_h),
            )
            img = ImageOps.fit(img, (target_w, target_h), Image.LANCZOS)
        else:
            # Same ratio, different size — clean resize, no distortion.
            log.info(
                "[RENDER] output resized %s → %s",
                img.size, (target_w, target_h),
            )
            img = img.resize((target_w, target_h), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return f"data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode()}"


def _resolve_product(product_type: str) -> str:
    value = (product_type or "").lower()
    if "hout" in value or "wood" in value:
        return PRODUCT_MAP["Houten Jaloezieën"]
    if "alu" in value or "aluminium" in value or "aluminum" in value:
        return PRODUCT_MAP["Aluminium Jaloezieën"]
    return (
        "Horizontal Venetian blinds with many separate, solid, opaque slats. "
        "The slats run horizontally from left to right and are stacked vertically from top to bottom. "
        "Light may pass only through the narrow open gaps between separate horizontal slats, never through the slat material itself. "
        "Never render fabric, a roller blind, a curtain, a flat panel, a translucent screen, or a continuous sheet."
    )


def _resolve_ladder_type(extra_options: dict) -> str:
    """
    Resolve ladder type from extra_options to a canonical key.
    Reads 'ladderType' first (current standard), then falls back to legacy
    'ladderTape' / 'ladderSystem' keys for backward compatibility.
    """
    # Current standard key
    if "ladderType" in extra_options:
        raw = str(extra_options["ladderType"]).strip().lower()
        return LADDER_ALIASES.get(raw, "ladder_cord")

    # Legacy boolean key (ladderTape: true → ladder_tape)
    legacy = extra_options.get("ladderTape", extra_options.get("ladderSystem"))
    if legacy is None:
        return "ladder_cord"
    if isinstance(legacy, bool):
        return "ladder_tape" if legacy else "ladder_cord"
    text = str(legacy).strip().lower()
    if text in {"true", "1", "yes", "ja", "ladderband", "tape", "band"}:
        return "ladder_tape"
    if text in {"false", "0", "no", "nee", "ladderkoord", "koord", "cord", "none", ""}:
        return "ladder_cord"
    return "ladder_cord"


def _build_prompt(
    config: dict,
    state: str,
    mounting: Optional[str],
    extra_options: dict,
) -> str:
    # ── State ──
    state_desc = STATE_MAP.get(state)
    if state_desc is None:
        warnings.warn(
            f"Unknown state '{state}', falling back to 'Geheel uitgerold'.",
            stacklevel=2,
        )
        state_desc = STATE_MAP["Geheel uitgerold"]

    # ── Mounting ──
    mounting_key  = (mounting or "in de dag").lower().strip()
    mounting_desc = MOUNTING_MAP.get(mounting_key)
    if mounting_desc is None:
        warnings.warn(
            f"Unknown mounting '{mounting_key}', falling back to 'in de dag'.",
            stacklevel=2,
        )
        mounting_desc = MOUNTING_MAP["in de dag"]

    # ── Product / lighting ──
    product_desc  = _resolve_product(config.get("productType", ""))
    lighting_key  = extra_options.get("lighting", "Zonsondergang (Warm)")
    lighting_desc = LIGHTING_MAP.get(lighting_key)
    if lighting_desc is None:
        warnings.warn(
            f"Unknown lighting '{lighting_key}', falling back to 'Zonsondergang (Warm)'.",
            stacklevel=2,
        )
        lighting_desc = LIGHTING_MAP["Zonsondergang (Warm)"]

    # ── Ladder type (unified with render_gemini.py / render_blind.py) ──
    ladder_type     = _resolve_ladder_type(extra_options)
    has_ladder_tape = ladder_type == "ladder_tape"
    has_cords       = ladder_type == "ladder_cord"

    if has_ladder_tape:
        tape_desc = (
            "with wide decorative vertical ladder tapes mounted over the front of the horizontal slats; "
            "the tapes are visible textile support bands, evenly spaced, and clearly distinct from the slats; "
            "HARD LOCK: SELECTED LADDER SYSTEM IS LADDER TAPE / LADDERBAND; "
            "render broad vertical fabric tapes on top of the slats; "
            "do not render thin-only ladder cords as the main support system"
        )
        ladder_system_label = "Ladder tape / Ladderband"
    elif has_cords:
        tape_desc = (
            "with thin ladder cords only, using fine cord lines that support the horizontal slats; "
            "HARD LOCK: SELECTED LADDER SYSTEM IS LADDER CORD / LADDERKOORD; "
            "render only thin cord lines; "
            "no wide decorative fabric tapes, no ribbon bands, no broad vertical textile strips, no broad vertical support bands"
        )
        ladder_system_label = "Ladder cord / Ladderkoord"
    else:
        tape_desc           = "with no visible ladder tapes or cords"
        ladder_system_label = "No ladder system"

    # ── Slat width — guard against falsy non-string values ──
    raw_slat_width = str(extra_options.get("slatWidth") or "").strip()
    if raw_slat_width:
        slat_width = raw_slat_width if raw_slat_width.lower().endswith("mm") else f"{raw_slat_width}mm"
        slat_desc  = f"with {slat_width} horizontal slats"
    else:
        slat_desc = "with clearly separated horizontal slats"

    return f"""
TASK:
Create a photorealistic window treatment visualization by inserting only the selected horizontal Venetian blind system into the existing photo.

{_VENETIAN_PRODUCT_LOCK}

CRITICAL CONSTRAINT: ARCHITECTURAL PRESERVATION

- Do not change the color of the existing window frames, walls, floor, ceiling, furniture, or exterior view.
- Do not repaint, restyle, renovate, or redesign the room.
- Do not alter the room layout, camera angle, perspective, or exposure.
- Preserve the original photo brightness and natural lighting balance.
- Only insert the new Venetian blind object.
- The final image must look like the blind was physically present when the photo was taken.

STEP 1: WINDOW AREA DETECTION AND CLEANUP

- Identify the exact window area, frame geometry, glass area, and visible mounting boundaries.
- Remove any existing blinds, shades, curtains, fabric panels, or window coverings inside the target window area using realistic inpainting logic.
- Preserve the original window frame, wall edges, sill, handles, vents, glass reflections, and visible exterior view.
- Keep the outside view visible only through the open air gaps between the slats where applicable.
- Do not cover the entire window with a flat overlay.

STEP 2: PRODUCT SPECIFICATION

- Product: {product_desc}
- Selected material: {config.get("material", "")}
- Selected color: {config.get("colorName", "")}
- Selected color hex: {config.get("colorHex", "")}
- Selected ladder system: {ladder_system_label}
- Configuration: {slat_desc}, {tape_desc}
- State: {state_desc}

Required slat behavior:
- The blind must cover the full window height from the headrail to the sill or bottom glass edge.
- The bottom rail must align with the sill or bottom window edge and must not stop halfway up the window.
- Never render a half-raised blind, floating bottom rail, partial-height coverage, cropped bottom rail, or unfinished lower section.
- Every horizontal slat must be a separate physical object.
- Every slat must be solid and opaque.
- Every slat must run left-to-right across the window.
- Slats must be stacked vertically from top to bottom.
- Small shadow gaps must remain visible between separate slats.
- The slat material must never become translucent, frosted, glass-like, mesh-like, glowing, or semi-transparent.
- Daylight may be visible only through the open air gaps between separate slats.
- Daylight must never pass through the body of a wooden or aluminum slat.

STEP 3: MOUNTING GEOMETRY

{mounting_desc}

Mounting requirements:
- Align the blind precisely with the detected window geometry.
- Match the room perspective and window vanishing point.
- Keep the blind physically plausible within the frame or over the frame according to the selected mounting type.
- Do not float the blind in front of the scene.
- Do not misalign the slats with the window edges.
- Preserve realistic depth, occlusion, and contact shadows around the mounting position.

STEP 4: LIGHTING, SHADOWS, AND MATERIAL PHYSICS

- Lighting condition: {lighting_desc}
- Render realistic shadows from the horizontal slats onto nearby glass, frame, sill, wall, floor, or furniture where physically plausible.
- The inserted blind must inherit the original room lighting direction, contrast, and exposure.
- Do not darken the entire room.
- Do not brighten the entire room.
- Do not add artificial studio lighting.
- If the product is aluminum, render subtle metallic highlights and controlled room reflections on the opaque slats.
- If the product is wood, render subtle natural wood grain and soft diffuse reflections on the opaque slats.
- Keep material behavior consistent across all slats.
- Avoid over-glossy, plastic-like, wet, painted, or synthetic material appearance unless explicitly selected.

STEP 5: LADDER SYSTEM LOCK

- Render exactly the selected ladder system: {ladder_system_label}.
- If the selected ladder system is Ladderband / ladder tape, show wide decorative vertical tapes as support bands placed over the horizontal slats.
- If the selected ladder system is Ladderkoord / ladder cord, show only thin ladder cords.
- If the selected ladder system is Ladderkoord / ladder cord, do not show wide fabric tapes, ribbon-like vertical bands, or broad textile strips.
- If the selected ladder system is Ladderkoord / ladder cord, any wide vertical band on top of the slats is a failed render.
- If the selected ladder system is Ladderband / ladder tape, thin cords alone are a failed render.
- The ladder system must support the Venetian blind structure, not become a separate window covering.
- Do not confuse ladder tapes with vertical blinds, curtains, or fabric strips.

NEGATIVE PRODUCT PROMPT

Do not create:
roller blind, rolgordijn, roll-up shade, fabric shade, Roman blind, vouwgordijn, pleated blind, plissé, curtain, gordijn, screen, vertical blind, verticale lamellen, shutter, louvered door, single flat panel, continuous sheet, translucent panel, privacy film, frosted glass, mesh fabric, transparent slats, semi-transparent slats, glowing slat material, fabric slats, plastic film, painted wall panel, flat pasted overlay, half-raised blind, partially raised blind, floating bottom rail, cropped bottom rail, or partial-height window coverage.

FINAL QUALITY REQUIREMENT

The output must be unmistakably a fully lowered horizontal Venetian blind system with many separate, solid, opaque, left-to-right slats stacked vertically across the full window height, with the bottom rail at the sill or bottom edge.
""".strip()


# ── PROVIDER ALPHA (primary) ──────────────────────────────────────────────────

def _render_alpha(img_bytes: bytes, mime: str, prompt: str) -> str:
    """Primary render path. Raises on any failure."""
    from google import genai
    from google.genai import types

    model_id = os.getenv("RENDER_MODEL_ALPHA", "gemini-3-pro-image-preview")
    api_key  = os.getenv("RENDER_KEY_ALPHA") or os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Primaire render key niet geconfigureerd.")

    client     = genai.Client(api_key=api_key)
    image_part = types.Part(inline_data=types.Blob(data=img_bytes, mime_type=mime))

    response = client.models.generate_content(
        model=model_id,
        contents=[image_part, prompt],
        config=types.GenerateContentConfig(max_output_tokens=8192),
    )

    candidates = getattr(response, "candidates", None) or []
    if not candidates or not getattr(candidates[0], "content", None):
        raise RuntimeError("Geen afbeelding in respons (geen candidates).")

    for part in candidates[0].content.parts or []:
        inline = getattr(part, "inline_data", None)
        if inline and inline.data and inline.mime_type:
            data = inline.data
            if isinstance(data, (bytes, bytearray)):
                data = base64.b64encode(data).decode()
            return f"data:{inline.mime_type};base64,{data}"

    raise RuntimeError("Geen inline afbeelding in provider-respons.")


# ── PROVIDER BETA (reserved — not used as automatic failover) ─────────────────
# BETA is implemented below but intentionally not called in generate_decor.
# Reason: product correctness (slat count, ladder type, color) cannot be
# guaranteed on the BETA provider, so failing visibly is preferable to a
# silent mis-render. Enable manually for testing only.

def _render_beta(img_bytes: bytes, mime: str, prompt: str) -> str:
    """
    Fallback render path — direct HTTP REST call, no SDK dependency.
    NOT called automatically. See note above.
    """
    api_key = os.getenv("RENDER_KEY_BETA") or os.getenv("FAL_KEY")
    if not api_key:
        raise RuntimeError("Fallback render key niet geconfigureerd.")

    b64str   = base64.b64encode(img_bytes).decode()
    data_url = f"data:{mime};base64,{b64str}"

    # Build a self-contained short prompt for BETA — independent of the full
    # ALPHA prompt, which is too long to truncate safely at 600 chars.
    short_prompt = (
        "Photorealistic interior room photo. "
        "Insert horizontal Venetian blinds (jaloezieën) at the window. "
        "Preserve all walls, floor, furniture, ceiling, and room colors exactly as-is. "
        "Only modify the window area. "
        "The blind must consist of many separate, solid, opaque horizontal slats "
        "stacked vertically across the full window height. "
        "Bottom rail must sit at the window sill. "
        "Do not render a roller blind, curtain, or flat panel."
    )

    payload = json.dumps({
        "prompt":         short_prompt,
        "image_url":      data_url,
        "guidance_scale": 3.5,
        "num_steps":      28,
        "output_format":  "jpeg",
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://fal.run/fal-ai/flux-kontext/dev",
        data=payload,
        headers={
            "Authorization": f"Key {api_key}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            result = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        log.error("[RENDER] %s HTTP %d: %s", _BETA, e.code, body[:300])
        raise RuntimeError(f"Fallback HTTP {e.code}: {body[:200]}") from e
    except Exception as e:
        log.error("[RENDER] %s verbindingsfout: %s — %s", _BETA, type(e).__name__, str(e)[:200])
        raise

    images = (result or {}).get("images") or []
    if not images:
        raise RuntimeError("Geen afbeelding ontvangen van fallback provider.")

    img_url = images[0].get("url", "")
    if img_url.startswith("data:"):
        return img_url

    with urllib.request.urlopen(img_url, timeout=60) as r:
        raw_data = r.read()
    return f"data:image/jpeg;base64,{base64.b64encode(raw_data).decode()}"


# ── OVERLOAD DETECTION ────────────────────────────────────────────────────────

def _is_overload(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in (
        "503", "unavailable", "high demand", "overloaded", "capacity",
        "quota", "rate limit", "rate_limit", "ratelimitexceeded",
    ))


# ── PUBLIC ENTRYPOINT ─────────────────────────────────────────────────────────

def generate_decor(
    image_b64:     str,
    config:        dict,
    state:         str = "Geheel uitgerold",
    mounting:      Optional[str] = None,
    extra_options: Optional[dict] = None,
) -> str:
    """
    Unified render entrypoint.

    - Uses ALPHA as the standard render path (2 attempts with retry).
    - BETA is NOT used as an automatic fallback — product correctness is
      stricter than provider failover. See _render_beta docstring.
    - All error messages are provider-neutral.

    Args:
      image_b64     : data URL or raw base64 of the room photo
      config        : {productType, material, colorName, colorHex}
      state         : "Geheel uitgerold"
      mounting      : "in de dag" / "op de dag"
      extra_options : {ladderType: str, slatWidth: str, lighting: str}
                       ladderType: "ladder_tape" | "ladder_cord" | "no_ladder"
                       Legacy keys "ladderTape" / "ladderSystem" still accepted.
    """
    extra_options = extra_options or {}
    img_bytes, mime, target_w, target_h = _optimize_image(image_b64, max_side=1536, quality=85)
    prompt = _build_prompt(config, state, mounting, extra_options)

    # ── ALPHA: 2 attempts ─────────────────────────────────────────────────────
    for attempt in range(2):
        try:
            result = _render_alpha(img_bytes, mime, prompt)
            result = _normalise_output(result, target_w, target_h)
            log.info("[RENDER] %s OK (poging %d)", _ALPHA, attempt + 1)
            return result
        except Exception as exc:
            is_overload = _is_overload(exc)
            log.warning(
                "[RENDER] %s fout poging %d (overload=%s): %s",
                _ALPHA, attempt + 1, is_overload, type(exc).__name__,
            )
            if attempt == 0:
                time.sleep(3)
                continue
            # Second attempt also failed — raise provider-neutral error
            raise RuntimeError(
                "Visualisatie tijdelijk niet beschikbaar. Probeer het later opnieuw."
            ) from exc

    # Should never be reached, but satisfies type checkers
    raise RuntimeError("Visualisatie tijdelijk niet beschikbaar. Probeer het later opnieuw.")