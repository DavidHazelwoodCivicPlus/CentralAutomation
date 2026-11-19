@echo off
rem Opens the AutoCentral window. scan.bat runs a scan from the command line instead.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Setting up for first use...
  python -m venv .venv || goto :nopython
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || goto :fail
  copy /y requirements.txt ".venv\requirements.installed" >nul
)
rem A cut-off install (or security software removing files afterwards) leaves
rem libraries half there. Check they import, and reinstall them over the top if not.
".venv\Scripts\python.exe" -c "import selenium.webdriver.common.by, selenium.webdriver.remote.webelement, selenium.webdriver.chrome.options, openpyxl, bs4" >nul 2>&1 || (
  echo Some libraries are missing or incomplete. Reinstalling them...
  ".venv\Scripts\python.exe" -m pip install --quiet --ignore-installed -r requirements.txt || goto :fail
  ".venv\Scripts\python.exe" -c "import selenium.webdriver.common.by, selenium.webdriver.remote.webelement, selenium.webdriver.chrome.options, openpyxl, bs4" || goto :broken
  copy /y requirements.txt ".venv\requirements.installed" >nul
)
rem Install anything new when requirements.txt has changed since the last install.
fc /b requirements.txt ".venv\requirements.installed" >nul 2>&1 || (
  echo Installing updated requirements...
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt && (
    copy /y requirements.txt ".venv\requirements.installed" >nul
  ) || (
    echo Could not install the new requirements; starting anyway. It will try again next time.
  )
)
".venv\Scripts\python.exe" -m autocentral --gui
if errorlevel 1 pause
exit /b
:nopython
echo Python 3.10+ is needed. Install it from the Software Center, then run this again.
pause
exit /b 1
:fail
echo Installing requirements failed. See the message above.
pause
exit /b 1
:broken
echo The libraries still don't import after reinstalling. Delete the .venv folder and run this again;
echo if that fails too, security software may be removing files (selenium includes selenium-manager.exe).
pause
exit /b 1
