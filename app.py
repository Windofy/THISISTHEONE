"""
app.py — MRJ4.15 Flask Application
Mr. Jealousy Interior Intelligence Tool

Routes:
  GET  /           → serve static/index.html
  POST /analyze    → run phases 1-8 (Claude vision + SAM2 segmentation)
  POST /render     → full SDXL inpaint render
  POST /preview    → fast SDXL inpaint preview
  GET  /agents     → describe AI agents (pipeline phases) and their models
  GET  /skills     → describe high-level skills this application exposes
"""

import os
import sys
from pathlib import Path

import io
from flask import Flask, request, jsonify, send_from_directory, send_file, render_template_string
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv

load_dotenv(".env.local", override=True)
load_dotenv()  # fallback to .env if .env.local is absent

# Alias: .env.local uses GOOGLE_API_KEY; render_engine expects GEMINI_API_KEY
import os as _os
if not _os.getenv("GEMINI_API_KEY") and _os.getenv("GOOGLE_API_KEY"):
    _os.environ["GEMINI_API_KEY"] = _os.environ["GOOGLE_API_KEY"]

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import core
from src.AI.analyse_claude import run_analysis_pipeline
from src.AI.utils import save_upload_locally, upload_to_supabase
from src.AI.sam2_segment import detect_window_bounds
from src.AI.render_engine import generate_decor
from src.AI.render_blind import render_blind_panel
from src.AI.warp_blind import (
    find_window_corners, warp_blind_to_window,
    composite_over_photo, b64_to_pil, mask_b64_to_array,
    pil_to_b64_jpeg, draw_corner_debug,
    clean_mask, apply_lighting,
    apply_watermark,
)


# ── APP SETUP ───────────────────────────────────────────────────

app = Flask(__name__, static_folder="static", static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20 MB max upload
CORS(app)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)


# ── STATIC / INDEX ───────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/favicon.ico")
def favicon():
    return send_from_directory("static", "favicon.ico") if \
        (ROOT / "static" / "favicon.ico").exists() else ("", 204)


# ── PHASE 1-8: ANALYZE ──────────────────────────────────────────

@app.route("/analyze", methods=["POST"])
@limiter.limit("15 per hour")
def analyze():
    """
    Receive a base64 image, upload it, and run the 9-phase analysis pipeline.
    Returns an AnalysisResult JSON object.
    """
    data = request.get_json(silent=True) or {}
    image_b64 = data.get("image")

    if not image_b64:
        return jsonify({"error": "Geen afbeelding ontvangen."}), 400

    try:
        upload_to_supabase(image_b64)
    except Exception as exc:
        app.logger.warning("Supabase upload failed (non-critical): %s", exc)

    try:
        save_upload_locally(image_b64)
    except Exception as exc:
        app.logger.warning("Local save failed (non-critical): %s", exc)

    try:
        result = run_analysis_pipeline(image_b64)
        return jsonify(result)
    except Exception as exc:
        app.logger.error("Pipeline error: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


# ── PHASE 9: RENDER (procedural blind + SAM2 mask + perspective warp) ──

@app.route("/render", methods=["POST"])
@limiter.limit("30 per hour")
def render():
    return _do_render()


@app.route("/preview", methods=["POST"])
@limiter.limit("30 per hour")
def preview():
    return _do_render()


def _do_render():
    """
    Gemini visualization pipeline (port of MRJ415 generateDecor).
    Sends the room photo + structured prompt to gemini-2.5-flash-image
    and returns the inpainted result.
    """
    data      = request.get_json(silent=True) or {}
    image_b64 = data.get("image")
    config    = data.get("config", {})
    state     = data.get("state", "Tot de helft")
    extra     = data.get("extraOptions", {})
    analysis  = data.get("analysis") or {}

    if not image_b64 or not config:
        return jsonify({"error": "Ontbrekende parameters."}), 400

    mounting = (analysis.get("windowCheck") or {}).get("recommendation") or "in de dag"

    try:
        image_url = generate_decor(
            image_b64=image_b64,
            config=config,
            state=state,
            mounting=mounting,
            extra_options=extra,
        )
        # ── Post-processing: watermark ──────────────────────────────────────
        try:
            rendered = b64_to_pil(image_url).convert("RGB")
            rendered = apply_watermark(rendered)
            image_url = pil_to_b64_jpeg(rendered, quality=92)
        except Exception as wm_exc:
            app.logger.warning("Post-processing mislukt (non-critical): %s", type(wm_exc).__name__)

        return jsonify({"image": image_url})

    except RuntimeError as exc:
        # RuntimeError from render_engine already has a neutral, client-safe message
        msg = str(exc)
        app.logger.warning("[RENDER] %s", msg)
        # Determine if this is an overload vs hard failure
        overload_keywords = ("tijdelijk", "later", "opnieuw")
        status = 503 if any(k in msg for k in overload_keywords) else 500
        return jsonify({"error": msg}), status

    except Exception as exc:
        # Unexpected errors: log with type but return generic message to client
        app.logger.error("[RENDER] Onverwachte fout: %s", type(exc).__name__, exc_info=True)
        return jsonify({"error": "Visualisatie tijdelijk niet beschikbaar."}), 503


# ── AGENTS ──────────────────────────────────────────────────────

@app.route("/agents", methods=["GET"])
def agents():
    """
    Return a machine-readable description of every AI agent (pipeline phase)
    in the analysis pipeline, including which model it uses and what it does.
    Useful for Claude Code skill discovery and pipeline debugging.
    """
    return jsonify({
        "agents": [
            {
                "id":          "quality_check",
                "phase":       2,
                "name":        "Image Quality Agent",
                "model":       core.FALLBACK_MODEL,
                "endpoint":    "/analyze",
                "description": "Checks image quality, framing, lighting, and compliance before the pipeline proceeds.",
                "output":      ["passed", "feedback"],
            },
            {
                "id":          "style_analysis",
                "phase":       3,
                "name":        "Interior Style Agent",
                "model":       core.ANALYSIS_MODEL,
                "endpoint":    "/analyze",
                "description": "Extracts interior design thesis: style label, room mood, luxury level, material language.",
                "output":      ["style", "styleSummary", "roomMood"],
            },
            {
                "id":          "color_dna",
                "phase":       4,
                "name":        "Color DNA Agent",
                "model":       core.ANALYSIS_MODEL,
                "endpoint":    "/analyze",
                "description": "Extracts exactly 5 dominant room colours and maps each to the Mr. Jealousy catalog.",
                "output":      ["colour_palette"],
            },
            {
                "id":          "window_architecture",
                "phase":       5,
                "name":        "Window Architecture Agent",
                "model":       core.ANALYSIS_MODEL,
                "endpoint":    "/analyze",
                "description": "Forensic window analysis: frame type, recess depth, handle/vent, opening mechanism, collision risks.",
                "output":      ["windowType", "recessDepth", "handlePresent", "ventPresent", "openingMechanism", "stackHeightClearance"],
            },
            {
                "id":          "mounting_strategy",
                "phase":       6,
                "name":        "Mounting Strategy Agent",
                "model":       None,
                "endpoint":    "/analyze",
                "description": "Pure-logic rule engine (no API call): applies 5 mounting rules to determine inside vs outside mount.",
                "output":      ["recommendation", "rule", "reasoning"],
            },
            {
                "id":          "lighting_analysis",
                "phase":       7,
                "name":        "Lighting Analysis Agent",
                "model":       core.ANALYSIS_MODEL,
                "endpoint":    "/analyze",
                "description": "Analyses natural/artificial light contribution, direction, temperature, and recommends material.",
                "output":      ["lightDirection", "lightIntensity", "lightTemperature", "recommendedMaterial", "lightingConditions"],
            },
            {
                "id":          "catalog_match",
                "phase":       8,
                "name":        "Catalog Match Agent",
                "model":       core.ANALYSIS_MODEL,
                "endpoint":    "/analyze",
                "description": "Selects the 3 best-matching products from the Mr. Jealousy catalog using style, mood, palette, and light context.",
                "output":      ["materialSuggestions", "suggestions"],
            },
            {
                "id":          "window_segmentation",
                "phase":       "sam2",
                "name":        "SAM2 Segmentation Agent",
                "model":       "sam2",
                "endpoint":    "/analyze",
                "description": "GPU/CPU segmentation: produces a binary window mask and bounding box for perspective warp.",
                "output":      ["windowBounds", "windowMask"],
            },
            {
                "id":          "visualization_render",
                "phase":       9,
                "name":        "Visualization Render Agent",
                "model":       core.RENDER_MODEL,
                "endpoint":    "/render",
                "description": "Generates a photorealistic room visualization. CONSTRAINT: only Houten Jaloezieën or Aluminium Jaloezieën, always fully rolled out (Geheel uitgerold). Default lighting: Zonsondergang (Warm) / Golden hour.",
                "output":      ["image"],
                "constraints": {
                    "allowed_products": ["Houten Jaloezieën", "Aluminium Jaloezieën"],
                    "state":            "Geheel uitgerold",
                    "default_lighting": "Zonsondergang (Warm)",
                },
            },
        ]
    })


# ── SKILLS ──────────────────────────────────────────────────────

@app.route("/skills", methods=["GET"])
def skills():
    """
    Return the high-level skills this application exposes.
    Designed for Claude Code skill discovery and external orchestration.
    """
    return jsonify({
        "skills": [
            {
                "id":          "analyze_room",
                "name":        "Analyse Room",
                "method":      "POST",
                "endpoint":    "/analyze",
                "description": "Upload a room photo to run the full 8-phase Claude vision pipeline: quality check, style extraction, colour DNA, window architecture, mounting strategy, lighting analysis, SAM2 segmentation, and catalog match.",
                "input":       {"image": "base64 data-URL (image/png, image/jpeg, image/webp, max 4 MB)"},
                "output":      "AnalysisResult JSON object",
                "agents":      ["quality_check", "style_analysis", "color_dna", "window_architecture",
                                "mounting_strategy", "lighting_analysis", "catalog_match", "window_segmentation"],
            },
            {
                "id":          "render_visualization",
                "name":        "Render Visualization",
                "method":      "POST",
                "endpoint":    "/render",
                "description": "Generate a photorealistic visualization of the selected blind. Only Houten Jaloezieën or Aluminium Jaloezieën. State is always Geheel uitgerold (enforced server-side). Default lighting: Zonsondergang (Warm).",
                "input":       {
                    "image":        "base64 data-URL of the room photo",
                    "config":       {"colorHex": "hex string", "productType": "Aluminium Jaloezieën | Houten Jaloezieën"},
                    "extraOptions": {"slatWidth": "50mm | 25mm", "ladderTape": "boolean", "lighting": "Zonsondergang (Warm) | Ochtend (Koel) | Middag (Helder) | Avond (Sfeervol)"},
                    "analysis":     "AnalysisResult from /analyze (optional but improves mounting accuracy)",
                },
                "constraints":  {"state": "Geheel uitgerold", "default_lighting": "Zonsondergang (Warm)"},
                "output":      {"image": "base64 JPEG of the rendered room"},
                "agents":      ["visualization_render"],
            },
            {
                "id":          "preview_visualization",
                "name":        "Preview Visualization",
                "method":      "POST",
                "endpoint":    "/preview",
                "description": "Same as render_visualization but uses the faster image model for quick previews.",
                "input":       "same as render_visualization",
                "constraints": {"state": "Geheel uitgerold", "default_lighting": "Zonsondergang (Warm)"},
                "output":      {"image": "base64 JPEG of the rendered room"},
                "agents":      ["visualization_render"],
            },
            {
                "id":          "warp_test",
                "name":        "SAM2 Warp Test",
                "method":      "POST",
                "endpoint":    "/test_warp",
                "description": "End-to-end test: SAM2 segmentation → perspective warp → blind composite → lighting integration.",
                "input":       {
                    "image":         "base64 data-URL",
                    "config":        {"colorHex": "hex", "productType": "product type"},
                    "state":         "blind state",
                    "extra":         {"slatWidth": "mm", "ladderTape": "boolean"},
                    "windowHeightMm": "number",
                },
                "output":      {"corners": "array", "debug": "base64 JPEG", "warped": "base64 JPEG", "final": "base64 JPEG"},
                "agents":      ["window_segmentation"],
            },
        ],
        "models": {
            "analysis":  core.ANALYSIS_MODEL,
            "fallback":  core.FALLBACK_MODEL,
            "render":    core.RENDER_MODEL,
            "render_fast": core.RENDER_MODEL_FAST,
        },
        "catalog_summary": {
            product: len(colors)
            for product, colors in core.MR_JEALOUSY_CATALOG.items()
        },
    })


# ── SAM2 CONFIGURATOR (warp + composite test) ──────────────────

_TEST_WARP_HTML = """<!doctype html>
<html lang="nl"><head><meta charset="utf-8"><title>Warp + Composite — Test</title>
<style>
  body { font-family:-apple-system,sans-serif; background:#222; color:#eee;
         margin:0; padding:20px; }
  .row { display:flex; gap:20px; align-items:flex-start; }
  .col { flex:1; }
  .panel { background:#333; padding:16px; border-radius:8px; }
  label { display:block; margin:8px 0 4px; font-size:12px; color:#aaa; }
  input, select { width:100%; padding:6px; background:#1a1a1a; color:#eee;
                  border:1px solid #555; border-radius:4px; box-sizing:border-box; }
  button { margin-top:12px; padding:10px 16px; background:#4a7; color:#fff;
           border:0; border-radius:4px; cursor:pointer; font-weight:600; }
  button:disabled { background:#555; cursor:wait; }
  img { max-width:100%; display:block; border:1px solid #444; border-radius:4px; }
  .imgs { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-top:12px; }
  .imgs figure { margin:0; }
  .imgs figcaption { font-size:12px; color:#888; padding:4px 0; }
  h1 { margin:0 0 12px; font-size:18px; }
  #status { font-size:13px; color:#fc6; margin-top:8px; min-height:18px; }
</style></head><body>
<div class="row">
  <div class="col panel" style="max-width:340px;">
    <h1>Warp + Composite</h1>
    <label>Foto (jpg/png)</label>
    <input id="file" type="file" accept="image/*">

    <label>Color hex</label>
    <input id="colorHex" value="#9c8b7a">

    <label>Product type</label>
    <select id="productType">
      <option>Houten Jaloezieën</option>
      <option>Aluminium Jaloezieën</option>
    </select>

    <label>Slat width</label>
    <select id="slatWidth"><option>50mm</option><option>25mm</option></select>

    <label>State</label>
    <select id="state">
      <option>Tot de helft</option>
      <option>Geheel uitgerold</option>
    </select>

    <label>Ladder</label>
    <select id="ladderTape"><option value="true">Ladderband</option><option value="false">Ladderkoord</option></select>

    <label>Window height (mm) — voor slat-density</label>
    <input id="windowHeightMm" type="number" value="1400">

    <button id="go">Run SAM2 + Render + Warp</button>
    <div id="status"></div>
  </div>

  <div class="col">
    <div class="imgs">
      <figure><figcaption>Origineel</figcaption><img id="orig"></figure>
      <figure><figcaption>SAM2 mask + 4 corners</figcaption><img id="dbg"></figure>
      <figure><figcaption>Warped blind (alleen jaloezie)</figcaption><img id="warped"></figure>
      <figure><figcaption>Final composite</figcaption><img id="final"></figure>
    </div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);

function fileToB64(f) {
  return new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(r.result);
    r.onerror = rej;
    r.readAsDataURL(f);
  });
}

$('go').onclick = async () => {
  const f = $('file').files[0];
  if (!f) { $('status').textContent = 'Kies eerst een foto.'; return; }
  $('go').disabled = true;
  $('status').textContent = 'Bezig… (SAM2 ~5s eerste keer)';

  const image = await fileToB64(f);
  $('orig').src = image;

  const body = {
    image,
    config: { colorHex: $('colorHex').value, productType: $('productType').value },
    state:  $('state').value,
    extra:  { slatWidth: $('slatWidth').value, ladderTape: $('ladderTape').value === 'true' },
    windowHeightMm: parseFloat($('windowHeightMm').value) || 1400,
  };

  try {
    const r = await fetch('/test_warp', {
      method: 'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body),
    });
    const data = await r.json();
    if (data.error) { $('status').textContent = 'FOUT: ' + data.error; return; }
    $('dbg').src    = data.debug;
    $('warped').src = data.warped;
    $('final').src  = data.final;
    $('status').textContent = 'Klaar. Corners: ' + JSON.stringify(data.corners);
  } catch (e) {
    $('status').textContent = 'FOUT: ' + e.message;
  } finally {
    $('go').disabled = false;
  }
};
</script>
</body></html>"""


@app.route("/test_warp_page")
def test_warp_page():
    return render_template_string(_TEST_WARP_HTML)


@app.route("/test_warp", methods=["POST"])
def test_warp():
    """End-to-end: photo → SAM2 → corners → procedural blind → warp → composite."""
    data = request.get_json(silent=True) or {}
    image_b64 = data.get("image")
    if not image_b64:
        return jsonify({"error": "Geen afbeelding ontvangen."}), 400

    config   = data.get("config", {})
    state    = data.get("state", "Tot de helft")
    extra    = data.get("extra", {})
    win_mm   = float(data.get("windowHeightMm", 1400))

    try:
        # 1. SAM2
        sam = detect_window_bounds(image_b64)
        if not sam.get("success"):
            return jsonify({"error": f"SAM2 mislukt: {sam.get('error')}"}), 500

        mask_arr = mask_b64_to_array(sam["mask_b64"])

        # 1b. Clean the mask: opening (erode→dilate) drops protrusions like
        #     windowsill bumps and mullion stubs that would inflate the bbox,
        #     then a final dilate extends onto the kozijn edge.
        mask_arr = clean_mask(mask_arr, open_px=18, dilate_px=14)

        corners  = find_window_corners(mask_arr)

        photo   = b64_to_pil(image_b64).convert("RGB")
        photo_w, photo_h = photo.size

        # 2. Pick a render size = bbox of the corners (preserves pixel density)
        xs = [c[0] for c in corners]; ys = [c[1] for c in corners]
        target_w = max(64, max(xs) - min(xs))
        target_h = max(64, max(ys) - min(ys))

        # 3. Render front-on blind
        blind = render_blind_panel(
            width_px=target_w, height_px=target_h,
            config=config, state=state, extra=extra,
            window_height_mm=win_mm,
        )

        # 4. Warp to the window quad
        warped = warp_blind_to_window(blind, corners, (photo_w, photo_h))

        # 5. Light integration: modulate blind by photo brightness behind it
        warped_lit = apply_lighting(warped, photo, blur_px=25, strength=0.55)

        # 6. Composite
        final = composite_over_photo(photo, warped_lit)

        # 6b. Watermark (post-processing, non-destructive)
        final = apply_watermark(final)

        # 7. Debug overlay
        dbg = draw_corner_debug(photo, corners)

        return jsonify({
            "corners":  corners,
            "debug":    pil_to_b64_jpeg(dbg, quality=85),
            "warped":   pil_to_b64_jpeg(warped_lit, quality=88),
            "final":    pil_to_b64_jpeg(final, quality=92),
        })
    except Exception as exc:
        app.logger.error("test_warp error: %s", exc, exc_info=True)
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500


# ── ENTRYPOINT ───────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
