@echo off
setlocal EnableExtensions

rem ASCII-only prompts: cmd.exe parses .cmd as system ANSI (e.g. CP936).
rem A UTF-8 file without BOM mis-decodes CJK; some bytes become & and the
rem remainder (e.g. REVIEW_REQUIRED / pending-review fragments) is executed
rem as commands. Business Chinese stays in the Python output, not here.

rem Project root = directory containing this .cmd (works from any CWD / double-click).
set "ROOT=%~dp0"

set "PY=%ROOT%.venv\Scripts\python.exe"
set "SCRIPT=%ROOT%scripts\p4d_run_product.py"
set "CONFIG=%ROOT%config\products\b913a.json"
set "EXITCODE=1"

echo === PriceRadar one-click launcher (B913A) ===
echo Project root: %ROOT%
echo.
echo Reminder: close other Chrome/automation sessions using browser_profiles\jd_persistent.
echo Note: process finished does NOT mean price capture OK. REVIEW_REQUIRED is not NOT_BREAK.
echo.

if not exist "%PY%" (
  echo [ERROR] Python interpreter not found:
  echo   %PY%
  echo Create project .venv first per README. This launcher does not install deps.
  set "EXITCODE=2"
  goto :finish
)

if not exist "%SCRIPT%" (
  echo [ERROR] Script not found:
  echo   %SCRIPT%
  set "EXITCODE=2"
  goto :finish
)

if not exist "%CONFIG%" (
  echo [ERROR] Product config not found:
  echo   %CONFIG%
  set "EXITCODE=2"
  goto :finish
)

echo Running: "%PY%" "%SCRIPT%" --product-config "%CONFIG%"
echo.

"%PY%" "%SCRIPT%" --product-config "%CONFIG%"
set "EXITCODE=%ERRORLEVEL%"

echo.
echo === Process finished ===
echo Exit code: %EXITCODE%
echo.
echo Copy the HTML report absolute path from the Python summary above to open it.
echo This launcher does not open a browser, retry, clean Profile, or kill other Chrome.
echo REVIEW_REQUIRED means insufficient evidence this run - not NOT_BREAK, not capture OK.

:finish
echo.
pause
exit /b %EXITCODE%
