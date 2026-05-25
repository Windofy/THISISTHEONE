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
echo  Uploaden gestart... (duurt 3-6 minuten)
echo  Sluit dit venster NIET.
echo  ------------------------------------------
echo.

gcloud run deploy mrj526 ^
  --source "%~dp0" ^
  --project windofy-core ^
  --region europe-west4 ^
  --allow-unauthenticated ^
  --set-env-vars "FLASK_ENV=production" ^
  --set-secrets "ANTHROPIC_API_KEY=ANTHROPIC_API_KEY:latest,GOOGLE_API_KEY=GOOGLE_API_KEY:latest,FAL_KEY=FAL_KEY:latest,SUPABASE_URL=SUPABASE_URL:latest,SUPABASE_KEY=SUPABASE_KEY:latest,NEXT_PUBLIC_SUPABASE_URL=NEXT_PUBLIC_SUPABASE_URL:latest,NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY=NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY:latest,SUPABASE_SERVICE_KEY=SUPABASE_SERVICE_KEY:latest" ^
  --verbosity=info 2>&1

echo.
echo  ------------------------------------------
if %errorlevel% equ 0 (
    echo  KLAAR! Live op:
    echo  https://mrj526-lacsawy5va-ez.a.run.app
    echo.
    echo  Volgende stap: DNS van mrsvision.nl instellen
    echo  Zie instructies in de terminal output hierboven.
    start https://mrj526-lacsawy5va-ez.a.run.app
) else (
    echo  Fout bij deployen. Zie output hierboven.
)
echo.
pause
