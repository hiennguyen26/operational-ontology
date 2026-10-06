@echo off
rem Windows double-click launcher: runs onto setup from this folder with py -3, else python.
setlocal
if not defined ONTO_SETUP_CWD set "ONTO_SETUP_CWD=%CD%"
cd /d "%~dp0"
set "ONTO=%~dp0plugins\general-ontology\bin\onto"
where py >nul 2>nul
if not errorlevel 1 (
  py -3 "%ONTO%" setup %*
) else (
  python "%ONTO%" setup %*
)
set "STATUS=%errorlevel%"
echo.
pause
exit /b %STATUS%
