# Telegram Video Downloader Bot

Downloads videos and photo slideshows without watermarks from TikTok, Instagram, YouTube, Twitter (X), Reddit, Threads, Facebook, Pinterest, and Twitch, sending the actual media file directly to the chat.

## Key Features

1. **Inline Mode & History (`@bot_username `).**
   * Type `@bot_username link` in any chat to download and send the video directly.
   * Type `@bot_username ` without a link (or with a search keyword) to pick from the 10 most recently downloaded videos.
2. **Group Chats & Direct Messages.** Automatically detects up to 5 video links in a single message and replies with the video files. Includes an automatic welcome message when added to a new group.
3. **YouTube Quality Selector.** For standard YouTube videos (`youtube.com/watch?v=...`), prompts the user to choose `1080p`, `720p`, `480p`, `360p`, or `Audio Only (MP3)`.
4. **Song Recognition (Shazam) & MP3 Extraction.** Every video includes buttons to extract the MP3 audio track or identify the song via Shazam and download the full track.
5. **Telegram Video Notes.** Converts any video into a centered `384x384` round Video Note (up to 60s) with one tap.
6. **TikTok Photo Slideshows.** Sends photo albums with the original audio track in chats/groups, and stitches slides with music into an `.mp4` video in inline mode.
7. **Linux VM Production Protections.**
   * `asyncio.Semaphore` (`MAX_CONCURRENT_JOBS=2`) prevents CPU overload from concurrent `ffmpeg` jobs.
   * Automatic hourly cleanup of stale files in `downloads/` and LRU cache trimming (`MAX_CACHE_ENTRIES=2000`).
   * Upload `cookies.txt` directly in a private chat with the bot to bypass Instagram/YouTube server IP blocks without SSH.

## BotFather Configuration

1. Create a bot via `/newbot` in `@BotFather` and paste the token into [.env](file:///c:/Users/PinchukRO/Videos/Kalyan/tg_bot/.env) (`BOT_TOKEN=...`).
2. Enable inline mode: `/setinline` -> select bot -> enter placeholder (`Paste a video link or search...`).
3. Enable inline feedback (required for replacing inline placeholders with video files): `/setinlinefeedback` -> select bot -> `Enable` (`100%`).
4. Allow the bot to read group messages: `/setprivacy` -> select bot -> `Disable`.

## Running on a Linux VM (Ubuntu / Debian)

Copy the `tg_bot` folder to your Linux server (excluding `.venv`) and run:

```bash
cd tg_bot
chmod +x setup_linux.sh
./setup_linux.sh
./.venv/bin/python main.py
```

To run the bot 24/7 in the background via `systemd`, create `/etc/systemd/system/tg-video-bot.service`:

```ini
[Unit]
Description=Telegram Video Downloader Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/tg_bot
ExecStart=/path/to/tg_bot/.venv/bin/python /path/to/tg_bot/main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tg-video-bot
```
