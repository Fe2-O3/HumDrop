@echo off
cd /d "%~dp0"
echo Building HumDrop for Windows...

REM Generate icons if missing
if not exist HumDrop.ico (
    echo Generating app icons...
    pip install Pillow
    python generate_icons.py
)

pip install -r requirements.txt pyinstaller
pyinstaller --onefile --windowed --name "HumDrop" --icon "HumDrop.ico" humdrop.py
echo.
echo Done! Executable is at: dist\HumDrop.exe
pause
