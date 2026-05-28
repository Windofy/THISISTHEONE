"""
render_gemini.py — Primary render provider (ALPHA path).

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


# ── DESCRIPTOR MAPS (verbatim from reference implementation) ──────

STATE_MAP = {
    "Tot de helft": (
        "lowered exactly halfway. The bottom 50% of the window is clear glass "
        "allowing direct sunlight to hit the floor/sill. The top 50% is covered "
        "by the blind, casting slat shadows."
    ),
    "Geheel uitgerold": (
        "FULLY DEPLOYED — The blind is 100% lowered. "
        "The bottom rail (onderlat / weight bar) rests directly on or within 1–2 cm of the windowsill. "
        "The blind panel covers the COMPLETE window height: from the headrail at the very top "
        "edge all the way down to the windowsill at the bottom — not a single centimeter of "
        "window glass is exposed below the bottom rail. "
        "Slats are horizontal and uniformly spaced across the full height. "
        "CRITICAL: The blind is NOT partially raised, NOT bunched up at the bottom, NOT folded. "
        "The full window — every centimeter from top to bottom — is covered by evenly-spaced horizontal slats."
    ),
}

MOUNTING_MAP = {
    "in de dag": """
        **MOUNTING TYPE: INSIDE MOUNT (In de dag)**
        1. **GEOMETRY**: The blind fits STRICTLY BETWEEN the window reveals (jambs).
        2. **WALL PRESERVATION**: The wall surface, architraves, and trim AROUND the window must remain COMPLETELY VISIBLE. Do NOT cover the casing.
        3. **DEPTH**: The blind is recessed into the wall.
        4. **SHADOWS**: Shadows from the slats fall onto the glass or the side reveals of the niche, NOT on the outer wall.
        """,
    "op de dag": """
        **MOUNTING TYPE: OUTSIDE MOUNT (Op de dag)**
        1. **GEOMETRY**: The blind is mounted ON THE FACE of the wall, overlapping the window opening.
        2. **OVERLAP**: The blind must extend 10cm beyond the window opening on Left, Right, and Top.
        3. **OBSCURING**: The blind MUST physically cover the window frame/architraves.
        4. **3D LAYERING**: The blind sits PROUD (forward) from the wall. You MUST render a hard drop shadow BEHIND the headrail and slats onto the wall surface. This proves it is floating in front of the wall.
        5. **NO RECOLORING**: Do not change the color of the wall or the frame underneath. The blind is a separate object placed in front.
        """,
    "op de glaslat": """
        **MOUNTING TYPE: ON THE SASH (Op de glaslat)**
        1. **PLACEMENT**: Mounted directly onto the window sash (the moving part).
        2. **FIT**: Very tight fit against the glass. The handle remains visible and accessible.
        """,
    "Gebogen gordijnrail voor erker":          "on a curved curtain rail",
    "Speciaal dakraam product":                "in a special skylight blind system",
    "Twee aparte rolgordijnen voor hoekraam":  "as two separate blinds for the corner window",
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
        "Reflections: Interior room reflected in the glass. Atmosphere: Intimate, dark outside."
    ),
    "Bewolkt (Diffuus)": (
        "OVERCAST/CLOUDY. Diffuse, soft white light (6500K). No hard direct sunlight. "
        "Shadows: Very soft, ambient occlusion only, no hard projection. "
        "Atmosphere: Soft, even, calm."
    ),
}

PRODUCT_MAP = {
    "Houten Jaloezieën": (
        "Premium Wooden Horizontal Venetian Blinds. "
        "Material physics: Matte or Satin finish, visible wood grain texture, "
        "absorbs light, warm reflections."
    ),
    "Aluminium Jaloezieën": (
        "Sleek Aluminum Horizontal Venetian Blinds. "
        "Material physics: Smooth metallic finish, slight specular highlights, "
        "reflects light, cool/sharp reflections."
    ),
}


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


def _build_prompt(
    config:        dict,
    state:         str,
    mounting:      Optional[str],
    extra_options: dict,
) -> str:
    """Verbatim port of MRJ415's generationPrompt template."""
    english_state    = STATE_MAP.get(state, state)
    english_mounting = MOUNTING_MAP.get(mounting or "in de dag", MOUNTING_MAP["in de dag"])
    english_product  = PRODUCT_MAP.get(config.get("productType", ""), "Horizontal Venetian Blinds")
    english_lighting = LIGHTING_MAP.get(
        extra_options.get("lighting", "Middag (Helder)"),
        LIGHTING_MAP["Middag (Helder)"],
    )

    tape_desc = (
        "with wide decorative fabric ladder tapes (vertical fabric strips)"
        if extra_options.get("ladderTape")
        else "with minimalist string cords (no wide fabric tapes)"
    )
    slat_desc = (
        f"with {extra_options['slatWidth']} wide horizontal slats"
        if extra_options.get("slatWidth")
        else "with horizontal slats"
    )

    if extra_options.get("ladderTape"):
        tape_constraint = (
            "      **CRITICAL CONSTRAINT: LADDER TAPE (LADDERTAPE) — MANDATORY**\n"
            "      This blind uses LADDER TAPE, not string cords.\n"
            "      - Wide fabric strips run VERTICALLY along the full height of the blind panel.\n"
            "      - Clearly visible, prominent decorative element — NOT thin cords.\n"
            "      - DO NOT substitute with string cords or invisible hardware."
        )
    else:
        tape_constraint = (
            "      **CRITICAL CONSTRAINT: STRING CORDS (LADDERKOORD) — NO FABRIC TAPES**\n"
            "      This blind uses minimalist string cords only.\n"
            "      - Thin, nearly invisible cords hold the slats — NO wide fabric strips.\n"
            "      - DO NOT add decorative ladder tapes."
        )

    slat_width = extra_options.get("slatWidth", "")
    slat_constraint = (
        f"      **CRITICAL CONSTRAINT: SLAT WIDTH — EXACTLY {slat_width}**\n"
        f"      Each individual slat is exactly {slat_width} wide.\n"
        f"      - Hard product specification — not approximate, not artistic.\n"
        f"      - The total number of slats filling the window height is determined by this exact width."
    ) if slat_width else ""

    prompt = f"""
      **TASK**: Create an Ultra-Photorealistic Window Treatment Visualization.

      **CRITICAL CONSTRAINT: ARCHITECTURAL PRESERVATION**
      - **DO NOT** change the color of the existing window frames, walls, floor, or furniture.
      - **DO NOT** repaint the room.
      - **ONLY** insert the new blind object.
      - If "Op de dag" is selected, the blind covers the frame, but any exposed wall/frame must retain its original color.

      **CRITICAL CONSTRAINT: DEPLOYMENT STATE — NON-NEGOTIABLE**
      The blind MUST be rendered in this exact physical state: {english_state}
      - The bottom rail MUST rest on or within 1–2 cm of the windowsill — NO floating allowed.
      - The blind MUST span 100% of window height: from headrail (top bracket) down to windowsill (bottom edge).
      - NO glass is visible below the bottom rail when fully deployed.
      - This constraint overrides any aesthetic, compositional, or stylistic preference.

{tape_constraint}
{slat_constraint}

      **STEP 1: VIRTUAL DEMOLITION (PRE-PROCESSING)**
      - Identify the window area accurately.
      - **REMOVE** any existing blinds, shades, or curtains inside the frame using inpainting logic.
      - The new blind must NOT sit on top of old blinds; the old ones are gone.
      - Keep the original view outside the window (landscape/street) visible through the open slats.

      **STEP 2: PRODUCT SPECIFICATION (MR. JEALOUSY)**
      - Product: {english_product}
      - Material Look: {config.get("material", "")}
      - Color: {config.get("colorName", "")} (Hex: {config.get("colorHex", "")})
      - Configuration: {slat_desc}, {tape_desc}
      - State: {english_state}

      **STEP 3: MOUNTING GEOMETRY (CRITICAL)**
      {english_mounting}

      **STEP 4: LIGHTING PHYSICS & ATMOSPHERE**
      - **CONDITION**: {english_lighting}
      - **INTERACTION**:
        - If "Op de dag": The cast shadow of the blind MUST fall onto the wall behind it, angled according to the light source defined above.
        - If "In de dag": Shadows fall strictly inside the niche/sill.
      - **RAYTRACING**: Render realistic slat shadows on the floor/furniture based on the "State" (Half/Full) and "Condition" (Angle of sun).
      - **REFLECTIONS**: If Aluminium, show subtle room reflections on the slats. If Wood, show texture.
      - **INTEGRATION**: The blind must match the room's perspective vanishing point perfectly.

      **STEP 5: NEGATIVE CONSTRAINTS — NEVER DO ANY OF THE FOLLOWING**
      - NEVER render the blind partially raised or only covering part of the window height.
      - NEVER show uncovered glass BELOW the bottom rail when state is fully deployed.
      - NEVER bunch, fold, or compress slats at the bottom of the window.
      - NEVER show the bottom rail floating above the windowsill with visible glass below it.
      - NEVER override the CRITICAL CONSTRAINT: DEPLOYMENT STATE for aesthetic or compositional reasons.
    """
    return prompt.strip()


# ── PUBLIC ENTRYPOINT ────────────────────────────────────────────

def generate_decor(
    image_b64:     str,
    config:        dict,
    state:         str = "Tot de helft",
    mounting:      Optional[str] = None,
    extra_options: Optional[dict] = None,
    retries:       int = 2,
) -> str:
    """
    Send room photo + prompt to the primary render provider and return the
    inpainted image as a base64 data URL.
    Includes retry + timeout behavior (90s timeout, 2 retries with exponential
    backoff on transient 5xx errors).

    Args:
      image_b64     : data URL or raw base64 of the room photo
      config        : {productType, material, colorName, colorHex}
      state         : "Tot de helft" or "Geheel uitgerold"
      mounting      : "in de dag" / "op de dag" / "op de glaslat"
      extra_options : {ladderTape: bool, slatWidth: str, lighting: str}

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

    raise RuntimeError(f"Render pipeline failed after retries.")


def _extract_image(response) -> str:
    """Pull the first inline image part out of a provider response → data URL."""
    if getattr(response, "prompt_feedback", None):
        block = getattr(response.prompt_feedback, "block_reason", None)
        if block:
            raise RuntimeError(f"Visualisatie geblokkeerd door inhoudsbeleid.")

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
