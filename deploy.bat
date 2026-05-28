@echo off
setlocal

echo.
echo  ==========================================
echo   Miss Vision Meets Mister Jealousy
echo   Deploy naar Cloud Run — windofy-core
echo  ==========================================
echo.
echo  Project : windofy-core
echo  Service : mrj526
echo  Regio   : europe-west4
echo  Bron    : %~dp0
echo.
echo  Stap 1: Image bouwen via Cloud Build... (duurt 3-5 min)
echo  Sluit dit venster NIET.
echo  ------------------------------------------
echo.

gcloud builds submit "%~dp0" ^
  --config "%~dp0cloudbuild.yaml" ^
  --project windofy-core ^
  --region europe-west4 ^
  --timeout 900s

if %errorlevel% neq 0 (
    echo.
    echo  Fout bij bouwen. Zie output hierboven.
    pause
    exit /b 1
)

echo.
echo  Stap 2: Image digest ophalen...
for /f "delims=" %%i in ('gcloud artifacts docker images describe europe-west4-docker.pkg.dev/windofy-core/mrj-builds/mrj526:latest --project windofy-core --format="value(image_summary.digest)" 2^>^&1') do set IMAGE_DIGEST=%%i

echo  Image: europe-west4-docker.pkg.dev/windofy-core/mrj-builds/mrj526@%IMAGE_DIGEST%
echo.
echo  Stap 3: Deployen naar Cloud Run...
echo  ------------------------------------------

gcloud run deploy mrj526 ^
  --image "europe-west4-docker.pkg.dev/windofy-core/mrj-builds/mrj526@%IMAGE_DIGEST%" ^
  --project windofy-core ^
  --region europe-west4 ^
  --allow-unauthenticated ^
  --set-env-vars "FLASK_ENV=production" ^
  --set-secrets "ANTHROPIC_API_KEY=ANTHROPIC_API_KEY:latest,GOOGLE_API_KEY=GOOGLE_API_KEY:latest,FAL_KEY=FAL_KEY:latest,SUPABASE_URL=SUPABASE_URL:latest,SUPABASE_KEY=SUPABASE_KEY:latest,NEXT_PUBLIC_SUPABASE_URL=NEXT_PUBLIC_SUPABASE_URL:latest,NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY=NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY:latest,SUPABASE_SERVICE_KEY=SUPABASE_SERVICE_KEY:latest"

echo.
echo  ------------------------------------------
if %errorlevel% equ 0 (
    echo  KLAAR! Live op:
    echo  https://mrsvision.nl
    echo  https://mrj526-lacsawy5va-ez.a.run.app
    echo.
    start https://mrsvision.nl
) else (
    echo  Fout bij deployen. Zie output hierboven.
)
echo.
pause
