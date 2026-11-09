import asyncio
import hashlib
import json
import os
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from config import ADMIN_IDS, CACHE_FILE, MAX_CACHE_ENTRIES, STORAGE_CHAT_ID

TRACKING_QUERY_PARAMS = {
    "igsh",
    "igshid",
    "si",
    "feature",
    "is_from_webapp",
    "sender_device",
    "_r",
    "_t",
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "s",
    "t",
}


def normalize_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw.startswith("http"):
        return raw
    try:
        parsed = urllib.parse.urlsplit(raw)
        path = parsed.path.replace("/reels/", "/reel/")
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/")
        if parsed.query:
            pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=False)
            filtered = [(k, v) for k, v in pairs if k.lower() not in TRACKING_QUERY_PARAMS]
            clean_query = urllib.parse.urlencode(filtered)
        else:
            clean_query = ""
        return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, clean_query, ""))
    except Exception:
        return raw


@dataclass
class CachedMedia:
    file_id: str
    caption: str = ""
    title: str = ""
    author: str = ""
    audio_file_id: str | None = None
    video_note_file_id: str | None = None


@dataclass
class ChatSettings:
    show_caption: bool = True
    show_mp3_button: bool = True
    delete_source_msg: bool = False


class BotStorage:
    def __init__(self, cache_path: Path = CACHE_FILE) -> None:
        self.cache_path = cache_path
        self.media_cache: dict[str, CachedMedia] = {}
        self.audio_only_cache: dict[str, str] = {}
        self.video_note_cache: dict[str, str] = {}
        self.song_cache: dict[str, dict[str, str]] = {}
        self.url_by_id: dict[str, str] = {}
        self.chat_settings: dict[str, ChatSettings] = {}
        self.fallback_storage_chat_id: int | None = None
        self.total_downloads: int = 0
        self._locks: dict[str, asyncio.Lock] = {}
        self._load()

    def _load(self) -> None:
        if not self.cache_path.exists():
            return
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            raw_files = data.get("file_cache", {})
            for url, val in raw_files.items():
                if isinstance(val, str):
                    self.media_cache[url] = CachedMedia(file_id=val)
                elif isinstance(val, dict) and "file_id" in val:
                    self.media_cache[url] = CachedMedia(
                        file_id=val["file_id"],
                        caption=val.get("caption", ""),
                        title=val.get("title", ""),
                        author=val.get("author", ""),
                        audio_file_id=val.get("audio_file_id"),
                        video_note_file_id=val.get("video_note_file_id"),
                    )
            self.audio_only_cache = data.get("audio_only_cache", {})
            self.video_note_cache = data.get("video_note_cache", {})
            self.song_cache = data.get("song_cache", {})
            self.url_by_id = data.get("url_by_id", {})

            raw_settings = data.get("chat_settings", {})
            for chat_key, cfg in raw_settings.items():
                if isinstance(cfg, dict):
                    self.chat_settings[str(chat_key)] = ChatSettings(
                        show_caption=bool(cfg.get("show_caption", True)),
                        show_mp3_button=bool(cfg.get("show_mp3_button", True)),
                        delete_source_msg=bool(cfg.get("delete_source_msg", False)),
                    )

            raw_chat_id = data.get("fallback_storage_chat_id")
            if isinstance(raw_chat_id, int):
                self.fallback_storage_chat_id = raw_chat_id
            self.total_downloads = int(data.get("total_downloads", len(self.media_cache)))
        except Exception:
            self.media_cache = {}
            self.audio_only_cache = {}
            self.video_note_cache = {}
            self.song_cache = {}
            self.url_by_id = {}
            self.chat_settings = {}

    @staticmethod
    def _trim_dict(d: dict, max_size: int = MAX_CACHE_ENTRIES) -> None:
        while len(d) > max_size:
            oldest_key = next(iter(d))
            del d[oldest_key]

    def _save(self) -> None:
        self._trim_dict(self.media_cache, MAX_CACHE_ENTRIES)
        self._trim_dict(self.audio_only_cache, MAX_CACHE_ENTRIES)
        self._trim_dict(self.video_note_cache, MAX_CACHE_ENTRIES)
        self._trim_dict(self.song_cache, MAX_CACHE_ENTRIES)
        self._trim_dict(self.url_by_id, MAX_CACHE_ENTRIES * 2)

        serializable_cache = {
            url: asdict(item)
            for url, item in self.media_cache.items()
        }
        serializable_settings = {
            chat_key: asdict(cfg)
            for chat_key, cfg in self.chat_settings.items()
        }
        data = {
            "file_cache": serializable_cache,
            "audio_only_cache": self.audio_only_cache,
            "video_note_cache": self.video_note_cache,
            "song_cache": self.song_cache,
            "url_by_id": self.url_by_id,
            "chat_settings": serializable_settings,
            "fallback_storage_chat_id": self.fallback_storage_chat_id,
            "total_downloads": self.total_downloads,
        }
        tmp_path = self.cache_path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, self.cache_path)

    @staticmethod
    def make_result_id(url: str) -> str:
        clean = normalize_url(url)
        return hashlib.md5(clean.encode("utf-8")).hexdigest()[:20]

    def get_lock(self, key: str) -> asyncio.Lock:
        lock_id = self.make_result_id(key)
        if len(self._locks) > MAX_CACHE_ENTRIES:
            self._locks = {k: v for k, v in self._locks.items() if v.locked()}
        if lock_id not in self._locks:
            self._locks[lock_id] = asyncio.Lock()
        return self._locks[lock_id]

    def get_chat_settings(self, chat_id: int) -> ChatSettings:
        key = str(chat_id)
        if key not in self.chat_settings:
            self.chat_settings[key] = ChatSettings(
                show_caption=True,
                show_mp3_button=(chat_id > 0),
                delete_source_msg=False,
            )
        return self.chat_settings[key]

    def toggle_chat_setting(self, chat_id: int, field_name: str) -> ChatSettings:
        cfg = self.get_chat_settings(chat_id)
        if hasattr(cfg, field_name):
            current = getattr(cfg, field_name)
            setattr(cfg, field_name, not current)
            self._save()
        return cfg

    def register_url(self, url: str) -> str:
        clean = normalize_url(url)
        result_id = self.make_result_id(clean)
        if self.url_by_id.get(result_id) != clean:
            self.url_by_id[result_id] = clean
            self._save()
        return result_id

    def get_url(self, result_id: str) -> str | None:
        clean_id = (result_id or "").strip()
        for prefix in ("cached_", "direct_", "load_", "hist_"):
            if clean_id.startswith(prefix):
                clean_id = clean_id[len(prefix):]
                break
        return self.url_by_id.get(clean_id) or self.url_by_id.get(result_id)

    def get_cached_media(self, url: str) -> CachedMedia | None:
        clean = normalize_url(url)
        return self.media_cache.get(clean) or self.media_cache.get(url)

    def get_recent_media(
        self,
        query: str = "",
        limit: int = 10,
    ) -> list[tuple[str, CachedMedia]]:
        q = query.strip().lower()
        items = list(self.media_cache.items())
        items.reverse()
        if not q:
            return items[:limit]
        filtered = [
            (url, media)
            for url, media in items
            if q in url.lower()
            or q in (media.title or "").lower()
            or q in (media.author or "").lower()
        ]
        return filtered[:limit]

    def get_file_id(self, url: str) -> str | None:
        item = self.get_cached_media(url)
        return item.file_id if item else None

    def save_media(
        self,
        url: str,
        file_id: str,
        caption: str = "",
        title: str = "",
        author: str = "",
    ) -> None:
        clean = normalize_url(url)
        existing = self.media_cache.get(clean) or self.media_cache.get(url)
        audio_id = existing.audio_file_id if existing else (self.audio_only_cache.get(clean) or self.audio_only_cache.get(url))
        vnote_id = existing.video_note_file_id if existing else (self.video_note_cache.get(clean) or self.video_note_cache.get(url))
        if clean in self.media_cache:
            del self.media_cache[clean]
        elif url in self.media_cache:
            del self.media_cache[url]
        else:
            self.total_downloads += 1
        self.media_cache[clean] = CachedMedia(
            file_id=file_id,
            caption=caption,
            title=title,
            author=author,
            audio_file_id=audio_id,
            video_note_file_id=vnote_id,
        )
        result_id = self.make_result_id(clean)
        self.url_by_id[result_id] = clean
        self._save()

    def get_audio_file_id(self, url: str) -> str | None:
        clean = normalize_url(url)
        item = self.get_cached_media(clean)
        if item and item.audio_file_id:
            return item.audio_file_id
        return self.audio_only_cache.get(clean) or self.audio_only_cache.get(url)

    def save_audio_file_id(self, url: str, audio_file_id: str) -> None:
        clean = normalize_url(url)
        self.audio_only_cache[clean] = audio_file_id
        if clean in self.media_cache:
            self.media_cache[clean].audio_file_id = audio_file_id
        self._save()

    def get_video_note_file_id(self, url: str) -> str | None:
        clean = normalize_url(url)
        item = self.get_cached_media(clean)
        if item and item.video_note_file_id:
            return item.video_note_file_id
        return self.video_note_cache.get(clean) or self.video_note_cache.get(url)

    def save_video_note_file_id(self, url: str, file_id: str) -> None:
        clean = normalize_url(url)
        self.video_note_cache[clean] = file_id
        if clean in self.media_cache:
            self.media_cache[clean].video_note_file_id = file_id
        self._save()

    def get_recognized_song(self, url: str) -> dict[str, str] | None:
        clean = normalize_url(url)
        return self.song_cache.get(clean) or self.song_cache.get(url)

    def save_recognized_song(self, url: str, song_info: dict[str, str]) -> None:
        clean = normalize_url(url)
        self.song_cache[clean] = song_info
        self._save()

    def get_storage_chat_id(self) -> int | None:
        return STORAGE_CHAT_ID or self.fallback_storage_chat_id

    def set_fallback_storage_chat_id(self, chat_id: int) -> None:
        if self.fallback_storage_chat_id != chat_id:
            self.fallback_storage_chat_id = chat_id
            self._save()

    def is_admin(self, user_id: int) -> bool:
        if ADMIN_IDS:
            return user_id in ADMIN_IDS
        if STORAGE_CHAT_ID and user_id == STORAGE_CHAT_ID:
            return True
        if self.fallback_storage_chat_id and user_id == self.fallback_storage_chat_id:
            return True
        return True


storage = BotStorage()
