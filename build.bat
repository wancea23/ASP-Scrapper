@echo off
title ASP Exam Checker - Build EXE
echo ============================================
echo   Construiesc ASP Exam Checker.exe
echo ============================================
echo.

echo [1/3] Instalez dependente...
pip install pyinstaller customtkinter playwright aiohttp --quiet

echo.
echo [2/3] Instalez Chromium browser...
python -m playwright install chromium --quiet

echo.
echo [3/3] Creez .exe-ul...
python -m PyInstaller --onefile --windowed --icon=asp_logo.ico --name="ASP Exam Checker" ^
    --hidden-import=aiohttp ^
    --hidden-import=playwright ^
    --hidden-import=playwright.async_api ^
    --collect-all=playwright ^
    app.py

echo.
echo [4/4] Copiez scrapper.py langa .exe...
if exist "dist\ASP Exam Checker.exe" (
    copy "dist\scrapper.py" "dist\scrapper.py.exe.backup" >nul 2>&1
    REM scrapper.py va ramane in dist folder
)

echo.
echo ============================================
if exist "dist\ASP Exam Checker.exe" (
    echo   SUCCES! Fisierul .exe a fost creat!
    echo.
    echo   Locatie: %~dp0dist\ASP Exam Checker.exe
    echo.
    echo   IMPORTANT: Asigura-te ca dist\scrapper.py e in ACELASI folder cu .exe
    echo   Poti copia .exe pe Desktop dar scrapper.py trebuie sa fie in acelasi folder.
) else (
    echo   EROARE: .exe-ul nu a fost creat.
)
echo ============================================
pause