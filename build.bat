@echo off
REM Run this on Windows with Python 3.10+ installed (python.org)
python -m venv venv
call venv\Scripts\activate
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --onedir --name MemeBot --collect-submodules websockets --add-data "index.html;." memebot.py
echo.
echo Done: dist\MemeBot\MemeBot.exe works already.
echo For the installer: install Inno Setup (jrsoftware.org), open installer.iss, press Compile.
pause
