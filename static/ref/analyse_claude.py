"""
src/AI/analyse_claude.py — MRJ4.15 Claude Vision Pipeline

Executes phases 1-8 sequentially using Claude's vision API.
Imports all laws and constants from core.py — never defines its own.

Phase gate:
  Phase 2 (quality check) can abort the pipeline early and return an error response.
  Phases 3, 4, 5, 7 run concurrently (all image-only, mutually independent).
  Phase 8 runs after, using outputs of 3, 4 and 7 as context.
"""

import os
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import anthropic
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import core
from src.AI.utils import strip_data_url
logger = logging.getLogger(__name__)

_PROMPT_SEP = "─" * 60  # separator produced by core.get_phase_prompt() — used for cache split


def _sam2_analysis_enabled() -> bool:
    return os.getenv("USE_SAM2_ANALYSIS", "").strip().lower() in {"1", "true", "yes", "on"}


# ── CLAUDE CLIENT ───────────────────────────────────────────────

def _get_client() -> anthropic.Anthropic:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY environment variable is not set.")
    return anthropic.Anthropic(api_key=api_key)


# ── VISION CALL ─────────────────────────────────────────────────

def _call_claude_vision(
    client: anthropic.Anthropic,
    system_prompt: str,
    image_b64: str,
    image_mime: str,
    user_message: str,
    model: Optional[str] = None,
) -> str:
    """
    Send a single vision request to Claude.
    - model=None  → uses ANALYSIS_MODEL (Opus) with FALLBACK_MODEL (Sonnet) on overload.
    - model=X     → uses X only, no fallback (for phases where Sonnet is sufficient).
    The static base of the system prompt is marked for prompt caching, which reduces
    token-processing overhead on repeated calls within the same session.
    Returns the raw text response (expected to be JSON).
    """
    models_to_try = [model] if model else [core.ANALYSIS_MODEL, core.FALLBACK_MODEL]

    # Split system prompt into cacheable static base and phase-specific suffix.
    # The separator line (─────) is the natural split point produced by get_phase_prompt().
    if _PROMPT_SEP in system_prompt:
        idx = system_prompt.index(_PROMPT_SEP)
        system_content: Any = [
            {
                "type": "text",
                "text": system_prompt[:idx].rstrip(),
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": system_prompt[idx:],
            },
        ]
    else:
        system_content = system_prompt  # fallback: plain string (e.g. phase 2 simple prompt)

    for m in models_to_try:
        try:
            if m != models_to_try[0]:
                logger.warning("%s overloaded — falling back to %s", models_to_try[0], m)

            response = client.messages.create(
                model=m,
                max_tokens=4096,
                system=system_content,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type":       "base64",
                                    "media_type": image_mime,
                                    "data":       image_b64,
                                },
                            },
                            {
                                "type": "text",
                                "text": user_message,
                            },
                        ],
                    }
                ],
            )

            if not response.content:
                raise ValueError(f"Empty response received from model {m}.")

            return response.content[0].text.strip()

        except (anthropic.InternalServerError, anthropic.RateLimitError):
            if m == models_to_try[-1]:
                raise  # all options exhausted — propagate
            continue   # try next model

    raise RuntimeError("All models failed.")


def _parse_json(raw: str, phase: int = 0) -> Dict[str, Any]:
    """Strip markdown fences and parse JSON. Raises with phase context on failure."""
    cleaned = raw.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        phase_label = f"phase {phase}" if phase else "unknown phase"
        raise ValueError(
            f"JSON parse error in {phase_label}: {exc}\n"
            f"Raw response (first 400 chars): {raw[:400]}"
        ) from exc


# ── PHASE EXECUTORS ─────────────────────────────────────────────

def _phase_2_quality(client, image_b64, image_mime) -> Dict[str, Any]:
    """
    Phase 2: Image quality and compliance check.
    Returns a dict with 'passed': bool and 'feedback': str.
    """
    system = core.get_phase_prompt(2)
    user   = (
        "Run the quality and compliance check on this image. "
        "Do not be too strict on darkness: approve the photo as soon as the window, room, and mounting zone remain reliably analyzable. "
        "Reject only when the image is extremely dark, overexposed, or unreadable. "
        "Return your answer as a valid JSON object with this exact structure:\n"
        '{"passed": true/false, "feedback": "English feedback text if failed, otherwise an empty string"}'
    )
    # Sonnet is voldoende voor deze binaire ja/nee check — sneller dan Opus.
    raw    = _call_claude_vision(client, system, image_b64, image_mime, user,
                                 model=core.FALLBACK_MODEL)
    result = _parse_json(raw, phase=2)
    return {
        "passed":   bool(result.get("passed", False)),
        "feedback": result.get("feedback", ""),
    }


def _phase_3_style(client, image_b64, image_mime) -> Dict[str, Any]:
    """Phase 3: Extract interior thesis — style, mood, luxury level, material language."""
    system = core.get_phase_prompt(3)
    user   = (
        "Analyze the interior style of this room. "
        "Return your answer as a valid JSON object:\n"
        '{"style": "one style label (e.g. Japandi, Industrial, Hotel Chic)", '
        '"styleSummary": "Maximum 2 sentences in English.", '
        '"roomMood": "one English word or short phrase describing the room mood"}'
    )
    raw = _call_claude_vision(client, system, image_b64, image_mime, user)
    return _parse_json(raw, phase=3)


def _phase_4_colors(client, image_b64, image_mime) -> Dict[str, Any]:
    """Phase 4: Extract color DNA (5 room tones)."""
    # Catalog is already embedded in the master prompt via get_phase_prompt().
    system = core.get_phase_prompt(4)
    user   = (
        "Extract exactly 5 visible colors from the room. "
        "Each matched_catalog_color must literally exist in the catalog above. "
        "Return your answer as a valid JSON object:\n"
        '{"colour_palette": ['
        '{"hex_code": "#XXXXXX", "extracted_source": "...", "matched_catalog_color": "..."}'
        ", ...]}"
    )
    raw    = _call_claude_vision(client, system, image_b64, image_mime, user)
    return _parse_json(raw, phase=4)


def _phase_5_window(client, image_b64, image_mime) -> Dict[str, Any]:
    """Phase 5: Forensic window architecture analysis."""
    system = core.get_phase_prompt(5)
    user   = (
        "Run a forensic window analysis. "
        "Return your answer as a valid JSON object:\n"
        '{"windowType": "...", "detectedWindowCount": 1, "recessDepth": 10, '
        '"handlePresent": false, "handleSide": "...", "ventPresent": false, '
        '"openingMechanism": "...", "openingDirection": "...", "isOperable": true, '
        '"frameType": "...", "glazingType": "...", "stackHeightClearance": 0, '
        '"sillPresent": true, "cornerProximity": false, "collisionRisks": "...", '
        '"exceptions": "Max 1 concrete English sentence about deviations that affect mounting, or an empty string."}'
    )
    raw    = _call_claude_vision(client, system, image_b64, image_mime, user)
    return _parse_json(raw, phase=5)


def _phase_6_mounting(window_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Phase 6: Determine mounting strategy from window data.
    Pure logic — no Claude call needed. Applies the 5 rules from core.PHASE_LAWS[6].
    """
    recess_depth     = float(window_data.get("recessDepth", 0))
    handle_present   = bool(window_data.get("handlePresent", False))
    vent_present     = bool(window_data.get("ventPresent", False))
    window_type      = str(window_data.get("windowType", "")).lower()
    opening_dir      = str(window_data.get("openingDirection", "")).lower()
    stack_clearance  = float(window_data.get("stackHeightClearance", 999))
    corner_proximity = bool(window_data.get("cornerProximity", False))
    obstacle         = handle_present or vent_present

    # Rule 1: depth threshold
    if recess_depth < 5:
        return {"recommendation": "op de dag", "rule": "RULE_1_DEPTH",
                "reasoning": f"Recess depth {recess_depth}cm is below 5cm, so outside mounting is mandatory."}

    # Rule 2: protrusion & clearance
    if obstacle:
        if recess_depth <= 15:
            return {"recommendation": "in de dag", "rule": "RULE_2_PROTRUSION_FRONT",
                    "reasoning": "An obstacle is present; mount on the front edge of the reveal plane."}
        return {"recommendation": "op de dag", "rule": "RULE_2_PROTRUSION_OUTSIDE",
                "reasoning": "An obstacle is present and the recess is too deep, so outside mounting is mandatory."}

    # Rule 3: kinematic collision (tilt & turn)
    is_tilt_turn = "tilt" in window_type or "draai" in window_type or "inward" in opening_dir
    if is_tilt_turn:
        if stack_clearance < 20:
            return {"recommendation": "op de dag", "rule": "RULE_3_KINEMATIC",
                    "reasoning": "Tilt or inward opening requires extra stack clearance, so outside mounting is mandatory."}

    # Rule 4: lateral collision
    if corner_proximity:
        return {"recommendation": "op de dag", "rule": "RULE_4_LATERAL",
                "reasoning": "WARNING: Insufficient lateral space for overlap; check for corner collision.",
                "error": True}

    # Rule 5: default
    return {"recommendation": "in de dag", "rule": "RULE_5_DEFAULT",
            "reasoning": "All rules pass: inside mounting 10mm from the wall edge creates a clean architectural shadow gap."}


def _phase_7_lighting(client, image_b64, image_mime) -> Dict[str, Any]:
    """Phase 7: Lighting conditions analysis."""
    system = core.get_phase_prompt(7)
    user   = (
        "Analyze the lighting conditions in this room. "
        "Return your answer as a valid JSON object:\n"
        '{"lightDirection": "...", "lightIntensity": "...", "lightSoftness": "...", '
        '"lightTemperature": "...", "naturalContribution": 80, "artificialContribution": 20, '
        '"glassReflection": "...", "shadowBehavior": "...", '
        '"recommendedMaterial": "Houten Jaloezieën of Aluminium Jaloezieën", '
        '"lightingConditions": "one-sentence English summary"}'
    )
    raw    = _call_claude_vision(client, system, image_b64, image_mime, user)
    return _parse_json(raw, phase=7)


def _phase_8_catalog(client, image_b64, image_mime, context: Dict[str, Any]) -> Dict[str, Any]:
    """Phase 8: Catalog match — select best products from the catalog."""
    # Catalog is already embedded in the master prompt via get_phase_prompt().
    style_ctx    = context.get("style", "")
    mood_ctx     = context.get("roomMood", "")
    palette_ctx  = json.dumps(context.get("colour_palette", []), ensure_ascii=False)
    material_rec = context.get("recommendedMaterial", "")

    system = core.get_phase_prompt(8)
    user   = (
        f"Interior style: {style_ctx}\n"
        f"Room mood: {mood_ctx}\n"
        f"Color palette: {palette_ctx}\n"
        f"Recommended material based on light: {material_rec}\n\n"
        "Select the 3 best matching products from the catalog. "
        "ONLY products that literally exist in the catalog are allowed. "
        "Return your answer as a valid JSON object:\n"
        '{"materialSuggestions": ["Hout", "Aluminium"], "suggestions": ['
        '{"productType": "...", "material": "...", "colorName": "...", '
        '"colorHex": "#XXXXXX", "suitabilityScore": 10, "reasoning": "English technical reasoning"}'
        ", ...]}"
    )
    raw    = _call_claude_vision(client, system, image_b64, image_mime, user)
    return _parse_json(raw, phase=8)


# ── MAIN PIPELINE ───────────────────────────────────────────────

def run_analysis_pipeline(image_data_url: str) -> Dict[str, Any]:
    """
    Execute phases 1-8 sequentially.
    Phase 1: handled by the caller (upload to Supabase, pass base64 here).
    Phase 2: quality gate — returns error dict immediately if image fails.
    Phases 3-8: run in order, accumulating context.

    Returns an AnalysisResult-compatible dict.
    """
    client = _get_client()
    image_mime, image_b64 = strip_data_url(image_data_url)

    # ── PHASE 2: Quality gate ──────────────────────────────────
    quality = _phase_2_quality(client, image_b64, image_mime)
    if not quality["passed"]:
        return {
            "qualityFailed":   True,
            "qualityFeedback": quality["feedback"],
        }

    # ── PHASES 4, 5, 7: run concurrently ──────────
    # Phase 3 (interior thesis) is skipped for visualization speed; it only
    # feeds descriptive UI/style context, not geometry or render placement.
    # SAM2 is opt-in only; the standard path renders directly with Gemini.
    with ThreadPoolExecutor(max_workers=3) as executor:
        f4   = executor.submit(_phase_4_colors,   client, image_b64, image_mime)
        f5   = executor.submit(_phase_5_window,   client, image_b64, image_mime)
        f7   = executor.submit(_phase_7_lighting, client, image_b64, image_mime)
        colors   = f4.result()
        window   = f5.result()
        lighting = f7.result()

    sam = {"success": False, "error": "SAM2 disabled in standard Gemini-direct flow"}
    if _sam2_analysis_enabled():
        try:
            from src.AI.sam2_segment import detect_window_bounds
            sam = detect_window_bounds(image_data_url)
        except Exception as exc:
            logger.warning("SAM2 detection skipped/failed: %s", exc)

    # ── PHASE 6: Mounting strategy (pure logic, no API call) ──
    mounting = _phase_6_mounting(window)

    # ── PHASE 8: Catalog match ─────────────────────────────────
    context = {
        "style":               "",
        "roomMood":            "",
        "colour_palette":      colors.get("colour_palette", []),
        "recommendedMaterial": lighting.get("recommendedMaterial", ""),
    }
    catalog_match = _phase_8_catalog(client, image_b64, image_mime, context)

    # ── Assemble final result ──────────────────────────────────
    window_check = {
        "obstacles":             window.get("handlePresent", False) or window.get("ventPresent", False),
        "windowType":            window.get("windowType", "—"),
        "detectedWindowCount":   window.get("detectedWindowCount", 1),
        "recommendation":        mounting.get("recommendation", "in de dag"),
        "reasoning":             mounting.get("reasoning", ""),
        "specialConsiderations": window.get("exceptions", ""),
    }

    # Optional SAM2 window region — not used by the standard Gemini-direct render path.
    window_bounds = None
    window_mask_b64 = None
    if sam.get("success"):
        window_bounds = sam["bounds"]
        window_bounds["confidence"] = sam.get("confidence", 0.0)
        window_mask_b64 = sam.get("mask_b64")
    elif _sam2_analysis_enabled():
        logger.warning("SAM2 detection failed: %s", sam.get("error", "unknown"))

    result = {
        "qualityFailed":       False,
        "style":               "",
        "styleSummary":        "",
        "roomMood":            "",
        "lightingConditions":  lighting.get("lightingConditions", ""),
        "colour_palette":      colors.get("colour_palette", []),
        "windowCheck":         window_check,
        "windowBounds":        window_bounds,
        "windowMask":          window_mask_b64,
        "materialSuggestions": catalog_match.get("materialSuggestions", []),
        "suggestions":         catalog_match.get("suggestions", []),
    }

    return result
