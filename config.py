import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

raw_storage_chat = os.getenv("STORAGE_CHAT_ID", "").strip()
STORAGE_CHAT_ID: int | None = (
    int(raw_storage_chat) if raw_storage_chat.lstrip("-").isdigit() else None
)

raw_admin_ids = os.getenv("ADMIN_IDS", "").strip()
ADMIN_IDS: set[int] = {
    int(x.strip())
    for x in raw_admin_ids.split(",")
    if x.strip().lstrip("-").isdigit()
}

raw_max_jobs = os.getenv("MAX_CONCURRENT_JOBS", "2").strip()
MAX_CONCURRENT_JOBS: int = int(raw_max_jobs) if raw_max_jobs.isdigit() and int(raw_max_jobs) > 0 else 2

DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

CACHE_FILE = BASE_DIR / "cache.json"
COOKIES_FILE = BASE_DIR / "cookies.txt"

MAX_FILESIZE_BYTES = 49 * 1024 * 1024  # 49 MB Telegram Bot API limit
MAX_RAW_DOWNLOAD_BYTES = 180 * 1024 * 1024  # Up to 180 MB before ffmpeg auto-compression
MAX_CACHE_ENTRIES = 2000
DOWNLOAD_FILE_TTL_SECONDS = 900  # 15 minutes
