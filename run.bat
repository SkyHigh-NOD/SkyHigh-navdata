@echo off
rem ============================================================================
rem  SkyHigh-navdata launcher
rem
rem  Builds the SkyRadar navdata release ZIP (dist\EG_<airac>.zip) from the
rem  current NATS UK eAIP and the X-Plane Gateway airport layouts.
rem
rem    run.bat                       full build (nav + airspace + layouts)
rem    run.bat --skip-layouts        skip the X-Plane Gateway layouts
rem    run.bat --skip-airspace       skip the CONFIG/bg airspace drawings
rem    run.bat --airac 2610          force the AIRAC cycle
rem    run.bat --help                show every option
rem
rem  Anything after "run.bat" is passed straight to build_navdata.py.
rem  Dependencies (beautifulsoup4, lxml) are installed on first run.
rem ============================================================================
setlocal
cd /d "%~dp0"

rem UTF-8 console so non-ASCII names render correctly.
chcp 65001 >nul 2>nul
set "PYTHONUTF8=1"

rem Prefer a local virtualenv, then the py launcher, then python on PATH.
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY (where py >nul 2>nul && set "PY=py -3")
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (
	echo Python 3 was not found on PATH.
	echo Install it from https://www.python.org/downloads/ and tick
	echo "Add python.exe to PATH", then run this again.
	pause
	endlocal & exit /b 1
)

rem Make sure the parsing dependencies are importable, installing them if not.
%PY% -c "import bs4, lxml" >nul 2>nul
if errorlevel 1 (
	echo Installing build dependencies ^(beautifulsoup4, lxml^) ...
	%PY% -m pip install --disable-pip-version-check -r requirements.txt
	if errorlevel 1 (
		echo.
		echo Could not install the dependencies. Install them manually:
		echo     %PY% -m pip install -r requirements.txt
		pause
		endlocal & exit /b 1
	)
)

echo SkyHigh-navdata: %PY% build_navdata.py %*
echo.
%PY% build_navdata.py %*
set "CODE=%ERRORLEVEL%"

if not "%CODE%"=="0" (
	echo.
	echo Build failed ^(exit code %CODE%^).
	pause
)

endlocal & exit /b %CODE%
