@echo off
cd /d "%~dp0"
echo Building HumDrop for Windows...
pip install -r requirements.txt pyinstaller
pyinstaller --onefile --windowed --name "HumDrop" --icon=NUL humdrop.py
echo.
echo Done! Executable is at: dist\HumDrop.exe
pause
