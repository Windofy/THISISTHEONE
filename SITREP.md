# SITREP — MRJ / Miss Vision × Mr. Jealousy
**Date:** 2026-05-25  
**Branch:** `main` — 1 commit ahead of nothing (only 1 commit total: "Initial commit")  
**Local server:** running at http://localhost:5000 (started manually this session)  
**Production:** mrsvision.nl → Google Cloud Run (Docker) — **NOT yet redeployed with today's changes**

---

## 1. What the app is

A Flask AI tool (`app.py`) for Mr. Jealousy / Miss Vision:
- User uploads a room photo
- 8-phase Claude vision pipeline analyses it (style, colours, window architecture, lighting, catalog match)
- SAM2 segments the window
- Gemini renders a photorealistic visualisation with the selected jaloezie placed in the window
- Frontend: `static/index.html` + `static/script.js` + `static/style.css`
- Backend models: Claude Opus 4.6 (analysis), Claude Sonnet 4.6 (fallback/Phase 2), Gemini (render)
- Catalog: Aluminium Jaloezieën + Houten Jaloezieën (defined in `core.py`)

Key files:
```
app.py                      ← Flask routes, rate limiter
core.py                     ← ALL laws, catalog, model constants, phase prompts
src/AI/analyse_claude.py    ← Phase 1-8 pipeline
src/AI/render_engine.py     ← Gemini render (ALPHA primary + BETA fal.ai fallback)
src/AI/render_gemini.py     ← Alternative render path
src/AI/sam2_segment.py      ← SAM2 window segmentation
src/AI/warp_blind.py        ← Perspective warp + composite
src/AI/clean_window.py      ← NEW: Gemini inpainting helper (created, not yet called)
static/style.css            ← All UI styles
static/script.js            ← All frontend logic + catalog mirror
```

---

## 2. Changes made this session (ALL uncommitted)

### A. `core.py` — Phase 2 quality gate fixed
**Problem:** When a photo had existing window treatments (blinds, curtains) visible, Phase 2 returned `passed=false` and blocked the entire pipeline with a Dutch error message shown to the user.  
**Fix:** Rewrote the Phase 2 "Window accessibility" law to explicitly say existing treatments must NEVER cause `passed=false`. The render engine already has a built-in "VIRTUAL DEMOLITION" step (in `render_engine.py:165` and `render_gemini.py:181`) that removes them automatically before placing the new jaloezie.

```python
# OLD law (caused the error):
"Check: Window accessibility — if substantially covered ... the image FAILS this check."

# NEW law:
"NEVER fail (passed=false) because existing window treatments are visible in the photo.
 Existing blinds, curtains, shutters, or other window coverings are fully acceptable —
 the render step removes them automatically. Only set passed=false for real image quality
 problems (extreme blur, total darkness, wrong format, explicit content, rotated 90°+).
 Always set 'existing_treatment_detected': true when treatments are visible."
```

### B. `src/AI/analyse_claude.py` — Phase 2 user prompt hardened
Added a Dutch `KRITIEKE REGEL` directly in the user message for Phase 2, so Claude can't miss it even without reading all system laws. Also updated the JSON response schema to include `existing_treatment_detected: bool`.

### C. `static/style.css` — Flyout thumbnails fixed
**Problem:** Color swatch thumbnails in the flyout panel were not square and images were showing incorrectly.  
**Two-part fix:**
1. Replaced broken `padding-bottom: 100%` + `position: absolute` trick with `aspect-ratio: 1 / 1` (more reliable in flex/grid contexts)
2. Changed `object-fit: cover` (was clipping images) back to `object-fit: contain` (shows full swatch)
3. Added `display: flex; align-items: center; justify-content: center` to the thumb-wrap for proper centering

### D. `app.py` — Rate limiter wired up (was already in git diff, pre-existing)
`flask-limiter` import + `Limiter` instance + `@limiter.limit()` decorators on `/analyze` (15/hr) and `/render`+`/preview` (30/hr). This was already in the working tree from a prior session.

### E. `requirements.txt` — `flask-limiter>=3.5.0` added
Was missing, causing server startup crash. Installed manually into venv this session.

### F. `src/AI/clean_window.py` — NEW file (created, currently unused)
A Gemini-based inpainting helper that can remove existing window treatments from an image. Created during an earlier approach that was abandoned in favour of the simpler prompt-fix. The file exists but is never imported or called — it's dead code. Either wire it in or delete it next session.

---

## 3. What still needs doing

### PRIORITY 1 — Deploy to production
All changes are local only. mrsvision.nl still runs the old code.  
To deploy: rebuild + push Docker image to Google Cloud Run.  
Need: GCP project ID + Cloud Run service name (or a deploy script).

### PRIORITY 2 — Verify the existing-treatment fix works end-to-end
The Phase 2 fix has been coded but not tested with a real photo that has existing blinds. Test by uploading a photo with visible venetian blinds — the pipeline should now run through to a visualisation instead of showing the error.

### PRIORITY 3 — Flyout thumbnail visual QA
The CSS fix is live locally (just refresh browser). Verify the swatches look correct on both desktop (380px panel) and mobile (100vw bottom sheet). The texture images (`textureUrl`) may look better with `object-fit: cover` than `contain` depending on the actual image dimensions — check visually.

### PRIORITY 4 — `clean_window.py` decision
Either:
- **Wire it in** to `run_analysis_pipeline` (after Phase 2, if `existing_treatment_detected=true`) for cleaner analysis results on photos with existing treatments, OR
- **Delete it** if the render engine's VIRTUAL DEMOLITION is sufficient

### PRIORITY 5 — Supabase upload broken
`upload_to_supabase()` fails silently because the `supabase` Python package can't install (its transitive dep `pyiceberg` fails to build wheels on this machine). The app continues without it (wrapped in try/except). Fix options: pin `supabase<2.10` or find a compatible version, or bypass by using the supabase REST API directly.

### PRIORITY 6 — Commit & tag current state
No commits since initial. Everything is uncommitted working-tree changes.  
Suggested commit message:  
`fix: bypass Phase 2 quality gate for existing window treatments; fix flyout thumbnail squares`

---

## 4. Local dev environment

- **Python venv:** `venv/Scripts/python.exe` — activate with `venv\Scripts\activate.bat`
- **Start server:** `venv\Scripts\python.exe app.py` (logs → `server-current.out.log` / `server-current.err.log`)
- **Full start (with SAM2 setup):** `start.bat` — slower, runs `setup_sam2.py` first
- **Installed in venv:** Flask, anthropic, google-genai, Pillow, python-dotenv, flask-limiter
- **NOT installed:** supabase (pyiceberg build fails), sam2, torch (heavy, GPU optional)
- **Port:** 5000 (local) / 8080 (Docker/Cloud Run)
- **Env vars needed:** `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` (or `GOOGLE_API_KEY`), optionally `RENDER_KEY_ALPHA`, `RENDER_KEY_BETA`, `SUPABASE_URL`, `SUPABASE_KEY`

---

## 5. Architecture snapshot

```
User upload (base64)
       │
       ▼
  /analyze (POST)
       │
  Phase 2: Quality gate ← FIXED: no longer blocks on existing treatments
       │
  ┌────┴────────────────────────────────┐
  │  Concurrent (ThreadPoolExecutor)    │
  │  Phase 3: Style                     │
  │  Phase 4: Color DNA                 │
  │  Phase 5: Window architecture       │
  │  Phase 7: Lighting                  │
  │  SAM2: Window segmentation          │
  └────┬────────────────────────────────┘
       │
  Phase 6: Mounting strategy (pure logic, no API)
  Phase 8: Catalog match
       │
       ▼
  AnalysisResult JSON → frontend
       │
       ▼
  /render (POST)
       │
  Gemini ALPHA (gemini-3-pro-image-preview)
    Step 1: VIRTUAL DEMOLITION (removes existing treatments)
    Step 2: Place new jaloezie
       │ (fallback on overload)
  Gemini BETA (fal.ai flux-kontext)
       │
       ▼
  apply_watermark → base64 JPEG → frontend
```
