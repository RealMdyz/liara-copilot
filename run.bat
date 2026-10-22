@echo off
REM Liara Copilot launcher (Windows)
cd /d %~dp0
if not exist .env copy .env.example .env
if not exist .venv python -m venv .venv
call .venv\Scripts\activate
pip install -q -r requirements.txt
python -m copilot.server
