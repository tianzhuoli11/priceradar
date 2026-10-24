@echo off
setlocal EnableExtensions

rem Isolated launcher self-test: same structure as run_b913a.cmd, mock Python only.
rem No JD navigation, no OCR, no product_history writes.

set "ROOT=%~dp0..\..\"
for %%I in ("%ROOT%") do set "ROOT=%%~fI\"

set "PY=%ROOT%.venv\Scripts\python.exe"
set "MOCK=%ROOT%scripts\fixtures\mock_launcher_entry.py"
set "CONFIG=%ROOT%config\products\b913a.json"
set "EXPECTED=%~1"
if "%EXPECTED%"=="" set "EXPECTED=0"
set "EXITCODE=1"
set "FAIL=0"

echo === Launcher self-test (mock entry, expected exit %EXPECTED%) ===
echo Project root: %ROOT%
echo Note: process finished does NOT mean price capture OK. REVIEW_REQUIRED is not NOT_BREAK.
echo.

if not exist "%PY%" (
  echo [ERROR] Python interpreter not found: %PY%
  set "EXITCODE=2"
  set "FAIL=1"
  goto :finish
)

if not exist "%MOCK%" (
  echo [ERROR] Mock entry not found: %MOCK%
  set "EXITCODE=2"
  set "FAIL=1"
  goto :finish
)

if not exist "%CONFIG%" (
  echo [ERROR] Product config not found: %CONFIG%
  set "EXITCODE=2"
  set "FAIL=1"
  goto :finish
)

echo Running: "%PY%" "%MOCK%" --exit-code %EXPECTED% --product-config "%CONFIG%"
echo.

"%PY%" "%MOCK%" --exit-code %EXPECTED% --product-config "%CONFIG%"
set "EXITCODE=%ERRORLEVEL%"

echo.
echo === Process finished ===
echo Exit code: %EXITCODE%
echo REVIEW_REQUIRED means insufficient evidence this run - not NOT_BREAK, not capture OK.

if not "%EXITCODE%"=="%EXPECTED%" (
  echo [FAIL] expected exit %EXPECTED% got %EXITCODE%
  set "FAIL=1"
) else (
  echo [OK] exit code matched expected %EXPECTED%
)

:finish
echo.
if "%PRICERADAR_SELFTEST_NOPAUSE%"=="1" (
  if "%FAIL%"=="1" exit /b 1
  exit /b 0
)
pause
if "%FAIL%"=="1" exit /b 1
exit /b 0
