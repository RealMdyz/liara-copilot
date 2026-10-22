#!/usr/bin/env bash
# Liara Copilot launcher (macOS / Linux)
set -e
cd "$(dirname "$0")"
[ -f .env ] || cp .env.example .env
if [ ! -d .venv ]; then python3 -m venv .venv; fi
source .venv/bin/activate
pip install -q -r requirements.txt
python -m copilot.server
