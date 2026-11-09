#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

echo "[1/4] Checking system packages (python3-venv, ffmpeg, nodejs)..."
if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -y
    sudo apt-get install -y python3 python3-venv python3-pip ffmpeg nodejs
fi

echo "[2/4] Creating Python virtual environment (.venv)..."
python3 -m venv .venv

echo "[3/4] Installing Python dependencies..."
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt

if [ ! -f .env ]; then
    cp .env.example .env
    echo ""
    read -r -p "Paste your Telegram BOT_TOKEN from @BotFather: " user_token
    if [ -n "$user_token" ]; then
        sed -i "s|^BOT_TOKEN=.*|BOT_TOKEN=${user_token}|" .env
        echo "Saved BOT_TOKEN to .env!"
    else
        echo "Created .env from template. Please edit .env and set BOT_TOKEN."
    fi
fi

echo ""
echo "[4/4] Setup complete! Run the bot with:"
echo "  ./.venv/bin/python main.py"
