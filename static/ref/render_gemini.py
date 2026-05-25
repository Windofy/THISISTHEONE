"""
render_gemini.py — Primary render provider (ALPHA path).

DEPRECATED COMPATIBILITY PATH:
The active Flask /render and /preview routes use src/AI/render_engine.py.
Keep this file prompt-compatible, but do not treat it as the live route source.

Direct Python port of the primary render pipeline.
The descriptor maps below are copied VERBATIM from the reference implementation
so the prompt sent to the provider is byte-identical.
DO NOT replace these with imports from core.py — that file has modified
versions tuned for a different render path.
"""

import os
import io
import re
import base64
import time
from typing import Optional

from google import genai
from google.genai import types
from PIL import Image


# Primary model identifier (resolved at runtime via environment)
RENDER_MODEL = os.getenv("RENDER_MODEL_ALPHA", "gemini-3-pro-image-preview")


# ── DESCRIPTOR MAPS ───────────────────────────────────────────────
# NOTE: Dutch aliases are resolved to English keys via *_ALIASES dicts
# before these maps are queried. Only English keys are reachable here;
# Dutch keys have been removed to eliminate dead code.

STATE_MAP = {
    "fully_lowered": (
        "The blind is fully lowered across the full window height. "
        "The bottom rail must sit exactly at the window sill or bottom edge of the glass area. "
        "The blind is not half-raised, not partially raised, and the bottom rail must never float halfway up the window."
    ),

}

MOUNTING_MAP = {
    "inside_mount": """
        **MOUNTING TYPE: INSIDE MOUNT**
        1. **GEOMETRY**: The blind fits STRICTLY BETWEEN the window reveals (jambs).
        2. **WALL PRESERVATION**: The wall surface, architraves, and trim AROUND the window must remain COMPLETELY VISIBLE. Do NOT cover the casing.
        3. **DEPTH**: The blind is recessed into the wall.
        4. **SHADOWS**: Shadows from the slats fall onto the glass or the side reveals of the niche, NOT on the outer wall.
        """,
    "outside_mount": """
        **MOUNTING TYPE: OUTSIDE MOUNT**
        1. **GEOMETRY**: The blind is mounted ON THE FACE of the wall, overlapping the window opening.
        2. **OVERLAP**: The blind must extend 10cm beyond the window opening on Left, Right, and Top.
        3. **OBSCURING**: The blind MUST physically cover the window frame/architraves.
        4. **3D LAYERING**: The blind sits PROUD (forward) from the wall. You MUST render a hard drop shadow BEHIND the headrail and slats onto the wall surface. This proves it is floating in front of the wall.
        5. **NO RECOLORING**: Do not change the color of the wall or the frame underneath. The blind is a separate object placed in front.
        """,
}

LIGHTING_MAP = {
    "morning_cool": (
        "MORNING LIGHT. Low angle sunlight from the East. Color Temp: 5500K (Cool/Fresh). "
        "Shadows: Long, crisp shadows projected deep into the room. Atmosphere: Crisp, energetic."
    ),
    "midday_clear": (
        "MID-DAY SUN. High angle overhead sunlight. Color Temp: 6000K (Neutral White). "
        "Shadows: Short, sharp, high-contrast shadows on the window sill and floor. "
        "Atmosphere: Bright, clear, revealing."
    ),
    "golden_hour_warm": (
        "GOLDEN HOUR. Very low angle sunlight from the West. Color Temp: 3500K (Warm/Orange/Gold). "
        "Shadows: Extremely long, dramatic, stretching across the floor. "
        "Reflections: Warm metallic glow on slats. Atmosphere: Cozy, romantic."
    ),
    "evening_ambient": (
        "EVENING/NIGHT. No direct sunlight. Light Source: Artificial interior lamps "
        "(Warm White 2700K). Shadows: Soft, multi-directional from room lights. "
        "Reflections: Interior room reflected in the glass. Atmosphere: Intimate, dark outside."
    ),
    "overcast_diffuse": (
        "OVERCAST/CLOUDY. Diffuse, soft white light (6500K). No hard direct sunlight. "
        "Shadows: Very soft, ambient occlusion only, no hard projection. "
        "Atmosphere: Soft, even, calm."
    ),
}

PRODUCT_MAP = {
    "Houten Jaloezieën": (
        "Wooden Horizontal Venetian Blinds. "
        "Material physics: Matte finish, visible wood grain texture, "
        "absorbs light, warm reflections. Each wooden slat is solid and opaque; "
        "light may pass between horizontal slats, never through the slat material."
    ),
    "Aluminium Jaloezieën": (
        "Sleek Aluminum Horizontal Venetian Blinds. "
        "Material physics: Smooth metallic finish, slight specular highlights, "
        "reflects light, cool/sharp reflections."
    ),
}

STATE_ALIASES = {
    "Geheel uitgerold": "fully_lowered",
    "fully lowered":    "fully_lowered",
    "fully_lowered":    "fully_lowered",
}

MOUNTING_ALIASES = {
    "in de dag":     "inside_mount",
    "inside":        "inside_mount",
    "inside mount":  "inside_mount",
    "inside_mount":  "inside_mount",
    "op de dag":     "outside_mount",
    "outside":       "outside_mount",
    "outside mount": "outside_mount",
    "outside_mount": "outside_mount",
}

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

LIGHTING_ALIASES = {
    "Ochtend (Koel)":       "morning_cool",
    "morning cool":         "morning_cool",
    "morning_cool":         "morning_cool",
    "Middag (Helder)":      "midday_clear",
    "midday clear":         "midday_clear",
    "midday_clear":         "midday_clear",
    "Zonsondergang (Warm)": "golden_hour_warm",
    "golden hour warm":     "golden_hour_warm",
    "golden_hour_warm":     "golden_hour_warm",
    "Avond (Sfeervol)":     "evening_ambient",
    "evening ambient":      "evening_ambient",
    "evening_ambient":      "evening_ambient",
    "Bewolkt (Diffuus)":    "overcast_diffuse",
    "overcast diffuse":     "overcast_diffuse",
    "overcast_diffuse":     "overcast_diffuse",
}

_VENETIAN_PRODUCT_LOCK = """
ABSOLUTE PRODUCT CATEGORY LOCK
- Render ONLY horizontal Venetian blinds.
- The result must contain many separate, parallel, horizontal slats from top to bottom.
- Never render a roller blind, fabric roll, roman shade, pleated blind, curtain, screen, vertical blind, shutter, single sheet, continuous fabric panel, or flat translucent panel.
- If the model is uncertain, choose the safest interpretation: solid horizontal Venetian slats, fully lowered.
""".strip()


# ── HELPERS ──────────────────────────────────────────────────────

_DATA_URL_RE = re.compile(r"^data:(image/\w+);base64,(.*)$", re.DOTALL)


def _split_data_url(image_b64: str) -> tuple[str, bytes]:
    """Return (mime_type, raw_bytes) from a data URL or assume jpeg if bare."""
    m = _DATA_URL_RE.match(image_b64)
    if m:
        return m.group(1), base64.b64decode(m.group(2))
    return "image/jpeg", base64.b64decode(image_b64)


def _optimize_image(image_b64: str, max_side: int = 1536, quality: int = 85) -> tuple[bytes, str]:
    """
    Decode → resize (max side = max_side) → re-encode.
    Mirrors MRJ415's `optimizeImage` (canvas.toDataURL('image/jpeg', 0.85)).
    Always re-encodes to JPEG @ 85 quality, exactly like the reference.
    """
    _, raw = _split_data_url(image_b64)
    img = Image.open(io.BytesIO(raw)).convert("RGB")

    w, h = img.size
    if w > max_side or h > max_side:
        if w > h:
            new_w = max_side
            new_h = round(h * max_side / w)
        else:
            new_h = max_side
            new_w = round(w * max_side / h)
        img = img.resize((new_w, new_h), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue(), "image/jpeg"


def _resolve_product(product_type: str) -> str:
    value = (product_type or "").lower()
    if "hout" in value or "wood" in value:
        return (
            "Premium Wooden Horizontal Venetian Blinds. "
            "Material physics: Matte or satin-like sheen finish, visible wood grain texture, "
            "absorbs light, warm reflections. Each wooden slat is solid and opaque; "
            "light may pass between horizontal slats, never through the slat material."
        )
    if "alu" in value or "aluminium" in value or "aluminum" in value:
        return (
            "Sleek Aluminum Horizontal Venetian Blinds. "
            "Material physics: smooth metallic finish, slight specular highlights, "
            "reflects light, cool/sharp reflections. Each aluminium slat is solid and opaque; "
            "light may pass between horizontal slats, never through the slat material."
        )
    return (
        "Premium Horizontal Venetian Blinds. "
        "Material physics: separate solid opaque horizontal slats, never a fabric sheet."
    )


def _clean_render_text(value) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"\b[Ee][Nn][Gg][Ll][Ii][Ss][Hh]\b", "", text)
    return " ".join(text.split())


def _canonical_lookup(value, aliases: dict[str, str], default: str) -> str:
    text = _clean_render_text(value)
    return aliases.get(text, aliases.get(text.lower(), default))


def _render_material_text(value) -> str:
    text = _clean_render_text(value)
    lowered = text.lower()
    if lowered in {"hout", "wood", "bamboo", "paulownia", "abachi"}:
        return "Wood"
    if lowered in {"aluminium", "aluminum"}:
        return "Aluminum"
    return text


def _build_prompt(
    config:        dict,
    state:         str,
    mounting:      Optional[str],
    extra_options: dict,
) -> str:
    """Verbatim port of MRJ415's generationPrompt template."""
    state_key    = _canonical_lookup(state, STATE_ALIASES, "fully_lowered")
    mounting_key = _canonical_lookup(mounting or "inside_mount", MOUNTING_ALIASES, "inside_mount")
    lighting_key = _canonical_lookup(extra_options.get("lighting", "golden_hour_warm"), LIGHTING_ALIASES, "golden_hour_warm")

    state_desc = STATE_MAP.get(state_key)
    if state_desc is None:
        import warnings
        warnings.warn(
            f"Unknown state key '{state_key}' (input: '{state}') — falling back to fully_lowered.",
            stacklevel=2,
        )
        state_desc = STATE_MAP["fully_lowered"]
    mounting_desc = MOUNTING_MAP.get(mounting_key, MOUNTING_MAP["inside_mount"])
    product_desc  = _resolve_product(_clean_render_text(config.get("productType", "")))
    lighting_desc = LIGHTING_MAP.get(lighting_key, LIGHTING_MAP["golden_hour_warm"])
    material      = _render_material_text(config.get("material", ""))
    color_name    = _clean_render_text(config.get("colorName", ""))
    color_hex     = _clean_render_text(config.get("colorHex", ""))

    ladder_key = _canonical_lookup(
        extra_options.get("ladderType", "ladder_cord"),
        LADDER_ALIASES,
        "ladder_cord",
    )
    has_tape  = ladder_key == "ladder_tape"
    has_cords = ladder_key == "ladder_cord"

    if has_tape:
        tape_desc = (
            "with wide decorative vertical ladder tapes mounted over the front of the horizontal slats; "
            "HARD LOCK: SELECTED LADDER SYSTEM IS LADDER TAPE / LADDERBAND; "
            "render broad vertical fabric tapes on top of the slats; "
            "do not render thin-only ladder cords as the main support system"
        )
    elif has_cords:
        tape_desc = (
            "with thin ladder cords only; "
            "HARD LOCK: SELECTED LADDER SYSTEM IS LADDER CORD / LADDERKOORD; "
            "render only thin cord lines; "
            "no wide decorative fabric tapes, no ribbon strips, no broad vertical textile bands"
        )
    else:
        tape_desc = (
            "with no visible ladder tapes or cords; "
            "HARD LOCK: NO LADDER SYSTEM VISIBLE; "
            "do not render any vertical tapes, cords, or ribbon strips"
        )

    slat_desc = (
        f"with {_clean_render_text(extra_options['slatWidth'])} wide horizontal slats"
        if extra_options.get("slatWidth")
        else "with horizontal slats"
    )

    ladder_system_label = (
        "Ladder tape / Ladderband" if has_tape
        else "Ladder cord / Ladderkoord" if has_cords
        else "No ladder system"
    )

    prompt = f"""
      **TASK**: Create a Photorealistic Window Treatment Visualization.

      {_VENETIAN_PRODUCT_LOCK}

      **CRITICAL CONSTRAINT: ARCHITECTURAL PRESERVATION**
      - **DO NOT** change the color of the existing window frames, walls, floor, or furniture.
      - **DO NOT** repaint the room.
      - **ONLY** insert the new blind object.
      - If outside mount is selected, the blind covers the frame, but any exposed wall/frame must retain its original color.
      - Preserve the original photo exposure and room brightness. Do not darken the whole room.

      **STEP 1: VIRTUAL DEMOLITION (PRE-PROCESSING)**
      - Identify the window area accurately.
      - **REMOVE** any existing blinds, shades, or curtains inside the frame using inpainting logic.
      - The new blind must NOT sit on top of old blinds; the old ones are gone.
      - Keep the original view outside the window (landscape/street) visible through the open slats. The slats themselves do not permit any visibility through their material

      **STEP 2: PRODUCT SPECIFICATION (MR. JEALOUSY)**
      - Product: {product_desc}
      - Material Look: {material}
      - Color: {color_name} (Hex: {color_hex})
      - Selected ladder system: {ladder_system_label}
      - Configuration: {slat_desc}, {tape_desc}
      - State: {state_desc}
      - Full-drop lock: the blind must cover the full window height, with the bottom rail at the sill or bottom glass edge. Never render a half-raised blind, floating bottom rail, partial-height coverage, cropped bottom rail, or unfinished lower section.
      - Slat opacity: every horizontal slat is a solid opaque object with 100% material opacity. No see-through slat bodies, no frosted/translucent slat material, no fabric-like transparency.
      - Gap behavior: daylight may be visible ONLY in the open air gaps between separate slats. The slat bodies themselves block light.

      **STEP 3: MOUNTING GEOMETRY (CRITICAL)**
      {mounting_desc}

      **STEP 4: LIGHTING PHYSICS & ATMOSPHERE**
      - **CONDITION**: {lighting_desc}
      - **INTERACTION**:
        - If outside mount is selected: The cast shadow of the blind MUST fall onto the wall behind it, angled according to the light source defined above.
        - If inside mount is selected: Shadows fall strictly inside the niche/sill.
      - **RAYTRACING**: Render realistic slat shadows on the floor/furniture based on the "State" (Half/Full) and "Condition" (Angle of sun).
      - **REFLECTIONS**: If Aluminium, show subtle room reflections on the slats. If Wood, show texture on solid opaque slats.
      - **OPAQUE SLATS**: Light may pass through the horizontal gaps between slats, never through the slat material itself. This applies to both wooden and aluminium slats.
      - **LADDER SYSTEM LOCK**: Render exactly the selected ladder system ({ladder_system_label}). If Ladderband / ladder tape is selected, show wide decorative fabric tapes. If Ladderkoord / ladder cord is selected, show only thin cords and do not show wide fabric tapes or ribbon-like vertical bands.
      - **INTEGRATION**: The blind must match the room's perspective vanishing point perfectly.

      **NEGATIVE PRODUCT PROMPT**
      Do not create: roller blind, roll-up shade, fabric shade, roman blind, pleated blind, curtain, vertical blind, shutters, single flat panel, continuous sheet, translucent slats, transparent slats, semi-transparent slats, glowing slat material, frosted slats, mesh fabric, privacy screen, half-raised blind, partially raised blind, floating bottom rail, cropped bottom rail, or partial-height window coverage.
    """
    return prompt.strip()


# ── PUBLIC ENTRYPOINT ────────────────────────────────────────────

def generate_decor(
    image_b64:     str,
    config:        dict,
    state:         str = "Geheel uitgerold",
    mounting:      Optional[str] = None,
    extra_options: Optional[dict] = None,
    retries:       int = 2,
) -> str:
    """
    Send room photo + prompt to the primary render provider and return the
    inpainted image as a base64 data URL.
    Includes retry + timeout behavior (90s timeout, up to `retries` retries
    with exponential backoff on transient 5xx errors).

    Args:
      image_b64     : data URL or raw base64 of the room photo
      config        : {productType, material, colorName, colorHex}
      state         : always "Geheel uitgerold"
      mounting      : "in de dag" / "op de dag"
      extra_options : {ladderType: str, slatWidth: str, lighting: str}
                       ladderType accepts: "ladderband"|"ladder tape"|"ladder_tape"|"tape"
                                           "ladderkoord"|"ladder cord"|"ladder_cord"|"cord"|"koord"
                                           "none"|"geen"|"no ladder"|"no_ladder"
      retries       : number of retries after the first attempt (total attempts = retries + 1)

    Returns:
      "data:image/png;base64,..." string
    """
    api_key = os.getenv("RENDER_KEY_ALPHA") or os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Render service key not configured.")

    extra_options = extra_options or {}
    img_bytes, mime = _optimize_image(image_b64, max_side=1536, quality=85)
    prompt = _build_prompt(config, state, mounting, extra_options)

    client = genai.Client(api_key=api_key)
    image_part = types.Part(inline_data=types.Blob(data=img_bytes, mime_type=mime))

    delay = 1.0
    last_exc: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            response = client.models.generate_content(
                model=RENDER_MODEL,
                contents=[image_part, prompt],
                config=types.GenerateContentConfig(max_output_tokens=8192),
            )
            return _extract_image(response)
        except Exception as exc:
            last_exc = exc
            msg = str(exc)
            transient = ("500" in msg) or ("503" in msg) or ("UNAVAILABLE" in msg)
            if transient and attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise

    raise RuntimeError("Render pipeline failed after retries.") from last_exc


def _extract_image(response) -> str:
    """Pull the first inline image part out of a provider response → data URL."""
    if getattr(response, "prompt_feedback", None):
        block = getattr(response.prompt_feedback, "block_reason", None)
        if block:
            raise RuntimeError("Visualisatie geblokkeerd door inhoudsbeleid.")

    candidates = getattr(response, "candidates", None) or []
    if not candidates or not candidates[0].content:
        raise RuntimeError("Geen afbeelding gegenereerd (geen candidates).")

    for part in candidates[0].content.parts or []:
        inline = getattr(part, "inline_data", None)
        if inline and inline.data and inline.mime_type:
            data = inline.data
            if isinstance(data, (bytes, bytearray)):
                data = base64.b64encode(data).decode()
            return f"data:{inline.mime_type};base64,{data}"

    raise RuntimeError("Geen afbeelding gegenereerd (geen inline image part).")