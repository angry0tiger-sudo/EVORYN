@echo off
setlocal
cd /d "%~dp0.."

py -3.12 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>&1
if errorlevel 1 (
  echo ERROR: Python 3.12.x is required. No files were removed.
  exit /b 2
)

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>&1
  if errorlevel 1 goto :venv_error
) else (
  py -3.12 -m venv .venv
  if errorlevel 1 goto :error
)

if not exist ".env" (
  copy ".env.example" ".env" >nul
  if errorlevel 1 goto :error
  echo Created .env from .env.example; no credentials were added.
) else (
  echo Existing .env preserved; it was not overwritten.
)

if not exist "data" mkdir "data"
if errorlevel 1 goto :error
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :error
".venv\Scripts\python.exe" -m pip install -e ".[dev]"
if errorlevel 1 goto :error
".venv\Scripts\python.exe" -m compileall -q src tests scripts
if errorlevel 1 goto :error
".venv\Scripts\python.exe" -m pytest -q
if errorlevel 1 goto :error

echo.
echo EVORYN local environment setup and offline verification completed.
echo No exchange request was made and no trading process was started.
echo Read README.md before configuring any credential.
exit /b 0

:error
echo.
echo ERROR: setup or offline verification failed. Review the messages above.
exit /b 1

:venv_error
echo ERROR: Existing .venv does not use Python 3.12.x; it was preserved.
echo Rename or remove that environment manually, then run setup again.
exit /b 2
