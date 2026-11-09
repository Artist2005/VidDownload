import asyncio
import html
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
import yt_dlp

from config import (
    COOKIES_FILE,
    DOWNLOAD_DIR,
    DOWNLOAD_FILE_TTL_SECONDS,
    MAX_CONCURRENT_JOBS,
    MAX_FILESIZE_BYTES,
    MAX_RAW_DOWNLOAD_BYTES,
)

SUPPORTED_DOMAIN_PATTERN = re.compile(
    r"(https?://(?:[\w-]+\.)?(?:"
    r"tiktok\.com"
    r"|instagram\.com|instagr\.am"
    r"|youtube\.com|youtu\.be"
    r"|twitter\.com|x\.com|t\.co"
    r"|reddit\.com|redd\.it"
    r"|threads\.net|threads\.com"
    r"|pinterest\.com|pin\.it"
    r"|facebook\.com|fb\.watch|fb\.gg"
    r"|twitch\.tv"
    r")/[^\s<>\"']+)",
    re.IGNORECASE,
)

ANY_URL_PATTERN = re.compile(
    r"(https?://[^\s<>\"']+)",
    re.IGNORECASE,
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

JOB_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_JOBS)


@dataclass
class VideoData:
    file_path: Path | None
    title: str
    author: str = ""
    caption: str = ""
    width: int | None = None
    height: int | None = None
    duration: int | None = None
    is_slideshow: bool = False
    image_paths: list[Path] = field(default_factory=list)
    audio_path: Path | None = None
    direct_video_url: str | None = None
    thumbnail_url: str | None = None

    def cleanup(self) -> None:
        if self.file_path and self.file_path.exists():
            self.file_path.unlink(missing_ok=True)
        for img in self.image_paths:
            if img.exists():
                img.unlink(missing_ok=True)
        if self.audio_path and self.audio_path.exists():
            self.audio_path.unlink(missing_ok=True)


@dataclass
class AudioData:
    file_path: Path
    title: str
    performer: str = ""
    duration: int | None = None

    def cleanup(self) -> None:
        if self.file_path.exists():
            self.file_path.unlink(missing_ok=True)


@dataclass
class RecognizedSong:
    title: str
    artist: str
    full_track_url: str = ""


class DownloadError(Exception):
    pass


def extract_video_url(text: str | None, allow_any_http: bool = False) -> str | None:
    if not text:
        return None
    match = SUPPORTED_DOMAIN_PATTERN.search(text)
    if match:
        return match.group(1).rstrip(".,;!)>\"'")
    if allow_any_http:
        any_match = ANY_URL_PATTERN.search(text)
        if any_match:
            return any_match.group(1).rstrip(".,;!)>\"'")
    return None


def extract_all_video_urls(
    text: str | None,
    max_urls: int = 5,
    allow_any_http: bool = False,
) -> list[str]:
    if not text:
        return []
    seen: list[str] = []
    for match in SUPPORTED_DOMAIN_PATTERN.finditer(text):
        clean = match.group(1).rstrip(".,;!)>\"'")
        if clean not in seen:
            seen.append(clean)
        if len(seen) >= max_urls:
            return seen
    if allow_any_http and not seen:
        for match in ANY_URL_PATTERN.finditer(text):
            clean = match.group(1).rstrip(".,;!)>\"'")
            if clean not in seen:
                seen.append(clean)
            if len(seen) >= max_urls:
                break
    return seen


def is_regular_youtube_url(url: str) -> bool:
    lower = url.lower()
    if "youtube.com/shorts/" in lower:
        return False
    return "youtube.com/watch" in lower or "youtu.be/" in lower


def detect_platform_name(url: str) -> str:
    lower = url.lower()
    if "tiktok.com" in lower:
        return "TikTok"
    if "instagram.com" in lower or "instagr.am" in lower:
        return "Instagram"
    if "youtube.com/shorts/" in lower:
        return "YouTube Shorts"
    if "youtube.com" in lower or "youtu.be" in lower:
        return "YouTube"
    if "twitter.com" in lower or "x.com" in lower or "t.co/" in lower:
        return "Twitter (X)"
    if "reddit.com" in lower or "redd.it" in lower:
        return "Reddit"
    if "threads." in lower:
        return "Threads"
    if "pinterest.com" in lower or "pin.it" in lower:
        return "Pinterest"
    if "facebook.com" in lower or "fb.watch" in lower or "fb.gg" in lower:
        return "Facebook"
    if "twitch.tv" in lower:
        return "Twitch"
    return "Video"


def build_caption(url: str, title: str = "", author: str = "") -> str:
    parts: list[str] = []
    clean_author = author.strip()
    clean_title = title.strip()

    if clean_author and clean_author.lower() not in ("unknown", "none"):
        parts.append(f"<b>{html.escape(clean_author[:60])}</b>")

    if clean_title and clean_title.lower() not in ("video", "відео", clean_author.lower()):
        short_title = clean_title[:160] + ("..." if len(clean_title) > 160 else "")
        parts.append(html.escape(short_title))

    safe_url = html.escape(url, quote=True)
    parts.append(f'<a href="{safe_url}">Original</a>')
    return "\n".join(parts)


def cleanup_old_downloads(max_age_seconds: int = DOWNLOAD_FILE_TTL_SECONDS) -> int:
    removed = 0
    now = time.time()
    for path in DOWNLOAD_DIR.iterdir():
        if not path.is_file():
            continue
        try:
            if now - path.stat().st_mtime > max_age_seconds:
                path.unlink(missing_ok=True)
                removed += 1
        except Exception:
            continue
    return removed


def _resolve_ffmpeg_path() -> str | None:
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _compress_video_if_needed(
    file_path: Path,
    duration: int | None,
    ffmpeg_path: str | None,
) -> Path:
    if not file_path.exists() or file_path.stat().st_size <= MAX_FILESIZE_BYTES:
        return file_path

    if not ffmpeg_path:
        file_path.unlink(missing_ok=True)
        raise DownloadError("Video exceeds 50 MB and ffmpeg is unavailable for compression.")

    dur = max(duration or 60, 5)
    target_total_kbps = max(250, int((44 * 8192) / dur))
    audio_kbps = 96
    video_kbps = max(150, target_total_kbps - audio_kbps)

    compressed_path = file_path.with_name(f"{file_path.stem}_compressed.mp4")
    cmd = [
        ffmpeg_path,
        "-y",
        "-i",
        str(file_path),
        "-vf",
        "scale='min(720,iw)':-2",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-b:v",
        f"{video_kbps}k",
        "-maxrate",
        f"{int(video_kbps * 1.2)}k",
        "-bufsize",
        f"{video_kbps * 2}k",
        "-c:a",
        "aac",
        "-b:a",
        f"{audio_kbps}k",
        "-movflags",
        "+faststart",
        str(compressed_path),
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
    except Exception as exc:
        compressed_path.unlink(missing_ok=True)
        file_path.unlink(missing_ok=True)
        raise DownloadError("Video exceeds 50 MB and automatic compression failed.") from exc

    file_path.unlink(missing_ok=True)
    if not compressed_path.exists() or compressed_path.stat().st_size > MAX_FILESIZE_BYTES:
        compressed_path.unlink(missing_ok=True)
        raise DownloadError("Video still exceeds the 50 MB Telegram limit after compression.")

    return compressed_path


def _download_file(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        dest.write_bytes(resp.read())


def _try_tikwm_fallback(url: str, unique_id: str, ffmpeg_path: str | None) -> VideoData | None:
    if "tiktok.com" not in url.lower():
        return None

    api_url = f"https://www.tikwm.com/api/?url={urllib.parse.quote(url)}"
    req = urllib.request.Request(api_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None

    if payload.get("code") != 0 or not payload.get("data"):
        return None

    data = payload["data"]
    title = (data.get("title") or "Video").strip()
    author_info = data.get("author") or {}
    author = (author_info.get("nickname") or author_info.get("unique_id") or "").strip()
    duration = data.get("duration")
    cover_url = data.get("cover") or data.get("origin_cover")
    caption = build_caption(url, title=title, author=author)

    images = data.get("images")
    if isinstance(images, list) and images:
        image_paths: list[Path] = []
        for idx, img_url in enumerate(images[:20]):
            img_dest = DOWNLOAD_DIR / f"{unique_id}_slide_{idx}.jpg"
            try:
                _download_file(img_url, img_dest)
                image_paths.append(img_dest)
            except Exception:
                continue

        audio_path: Path | None = None
        music_url = data.get("music") or (data.get("music_info") or {}).get("play")
        if music_url:
            candidate_audio = DOWNLOAD_DIR / f"{unique_id}_music.mp3"
            try:
                _download_file(music_url, candidate_audio)
                audio_path = candidate_audio
            except Exception:
                audio_path = None

        if image_paths:
            return VideoData(
                file_path=None,
                title=title[:200],
                author=author,
                caption=caption,
                is_slideshow=True,
                image_paths=image_paths,
                audio_path=audio_path,
                thumbnail_url=cover_url,
            )

    play_url = data.get("play") or data.get("wmplay")
    if play_url:
        video_dest = DOWNLOAD_DIR / f"{unique_id}.mp4"
        try:
            _download_file(play_url, video_dest)
            video_dest = _compress_video_if_needed(
                video_dest,
                int(duration) if isinstance(duration, (int, float)) else None,
                ffmpeg_path,
            )
            return VideoData(
                file_path=video_dest,
                title=title[:200],
                author=author,
                caption=caption,
                duration=int(duration) if isinstance(duration, (int, float)) else None,
                direct_video_url=play_url if str(play_url).startswith("http") else None,
                thumbnail_url=cover_url if str(cover_url).startswith("http") else None,
            )
        except Exception:
            video_dest.unlink(missing_ok=True)

    return None


def render_slideshow_to_video_sync(video_data: VideoData) -> Path | None:
    ffmpeg_path = _resolve_ffmpeg_path()
    if not ffmpeg_path or not video_data.image_paths:
        return None

    unique_id = uuid.uuid4().hex[:10]
    list_file = DOWNLOAD_DIR / f"{unique_id}_slides.txt"
    out_video = DOWNLOAD_DIR / f"{unique_id}_slideshow.mp4"

    lines: list[str] = []
    for img in video_data.image_paths:
        safe_path = str(img.resolve()).replace("\\", "/")
        lines.append(f"file '{safe_path}'")
        lines.append("duration 3")
    last_path = str(video_data.image_paths[-1].resolve()).replace("\\", "/")
    lines.append(f"file '{last_path}'")
    list_file.write_text("\n".join(lines), encoding="utf-8")

    cmd = [
        ffmpeg_path,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
    ]
    if video_data.audio_path and video_data.audio_path.exists():
        cmd += ["-i", str(video_data.audio_path), "-shortest", "-c:a", "aac", "-b:a", "128k"]

    cmd += [
        "-vf",
        "scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
        "-r",
        "25",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-movflags",
        "+faststart",
        str(out_video),
    ]

    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        return out_video if out_video.exists() else None
    except Exception:
        out_video.unlink(missing_ok=True)
        return None
    finally:
        list_file.unlink(missing_ok=True)


def _download_sync(url: str, max_height: int | None = None) -> VideoData:
    unique_id = uuid.uuid4().hex[:12]
    ffmpeg_path = _resolve_ffmpeg_path()

    if "tiktok.com" in url.lower() and "/photo/" in url.lower():
        tikwm_res = _try_tikwm_fallback(url, unique_id, ffmpeg_path)
        if tikwm_res:
            return tikwm_res

    output_template = str(DOWNLOAD_DIR / f"{unique_id}.%(ext)s")
    target_res = max_height or 1080
    height_filter = f"[height<={target_res}]" if max_height else ""

    ydl_opts: dict = {
        "outtmpl": output_template,
        "format": (
            f"bestvideo{height_filter}[ext=mp4][filesize<45M]+bestaudio[ext=m4a]/"
            f"best{height_filter}[ext=mp4][filesize<49M]/"
            f"bestvideo[height<={min(target_res, 720)}][ext=mp4]+bestaudio[ext=m4a]/"
            f"best{height_filter}[ext=mp4]/best"
        ),
        "format_sort": ["vcodec:h264", "acodec:m4a", "ext:mp4:m4a", f"res:{target_res}"],
        "merge_output_format": "mp4",
        "max_filesize": MAX_RAW_DOWNLOAD_BYTES,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "geo_bypass": True,
        "http_headers": {"User-Agent": USER_AGENT},
    }

    if ffmpeg_path:
        ydl_opts["ffmpeg_location"] = ffmpeg_path
    if COOKIES_FILE.exists():
        ydl_opts["cookiefile"] = str(COOKIES_FILE)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                raise DownloadError("Could not extract video information.")

            if "entries" in info and info["entries"]:
                info = info["entries"][0]

            if info.get("vcodec") == "none" and "tiktok.com" in url.lower():
                for leftover in DOWNLOAD_DIR.glob(f"{unique_id}.*"):
                    leftover.unlink(missing_ok=True)
                tikwm_res = _try_tikwm_fallback(url, unique_id, ffmpeg_path)
                if tikwm_res:
                    return tikwm_res

            downloaded_str = ydl.prepare_filename(info)
            downloaded_path = Path(downloaded_str)
            mp4_candidate = downloaded_path.with_suffix(".mp4")
            if mp4_candidate.exists():
                downloaded_path = mp4_candidate

            if not downloaded_path.exists():
                matches = list(DOWNLOAD_DIR.glob(f"{unique_id}.*"))
                if not matches:
                    raise DownloadError("Downloaded file was not found on disk.")
                downloaded_path = matches[0]

            title = (info.get("title") or info.get("description") or "Video").strip()
            author = (info.get("uploader") or info.get("channel") or info.get("creator") or "").strip()
            width = info.get("width")
            height = info.get("height")
            duration = info.get("duration")
            dur_int = int(round(duration)) if isinstance(duration, (int, float)) else None
            thumb = info.get("thumbnail")
            direct_url = info.get("url") if info.get("ext") == "mp4" else None

            downloaded_path = _compress_video_if_needed(downloaded_path, dur_int, ffmpeg_path)

            return VideoData(
                file_path=downloaded_path,
                title=title[:200],
                author=author[:100],
                caption=build_caption(url, title=title, author=author),
                width=int(width) if isinstance(width, (int, float)) else None,
                height=int(height) if isinstance(height, (int, float)) else None,
                duration=dur_int,
                direct_video_url=direct_url if isinstance(direct_url, str) and direct_url.startswith("http") else None,
                thumbnail_url=thumb if isinstance(thumb, str) and thumb.startswith("http") else None,
            )
    except Exception as exc:
        for leftover in DOWNLOAD_DIR.glob(f"{unique_id}.*"):
            leftover.unlink(missing_ok=True)

        tikwm_res = _try_tikwm_fallback(url, unique_id, ffmpeg_path)
        if tikwm_res:
            return tikwm_res

        if isinstance(exc, DownloadError):
            raise
        raise DownloadError(f"Download failed: {exc}") from exc


def _download_audio_sync(url: str) -> AudioData:
    unique_id = uuid.uuid4().hex[:12]
    ffmpeg_path = _resolve_ffmpeg_path()
    output_template = str(DOWNLOAD_DIR / f"{unique_id}_audio.%(ext)s")

    ydl_opts: dict = {
        "outtmpl": output_template,
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "http_headers": {"User-Agent": USER_AGENT},
    }

    if ffmpeg_path:
        ydl_opts["ffmpeg_location"] = ffmpeg_path
        ydl_opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]

    if COOKIES_FILE.exists():
        ydl_opts["cookiefile"] = str(COOKIES_FILE)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                raise DownloadError("Could not extract audio track.")
            if "entries" in info and info["entries"]:
                info = info["entries"][0]

            mp3_candidate = DOWNLOAD_DIR / f"{unique_id}_audio.mp3"
            if mp3_candidate.exists():
                audio_path = mp3_candidate
            else:
                matches = list(DOWNLOAD_DIR.glob(f"{unique_id}_audio.*"))
                if not matches:
                    raise DownloadError("Audio file was not found after conversion.")
                audio_path = matches[0]

            title = (info.get("track") or info.get("title") or "Audio").strip()[:100]
            performer = (info.get("artist") or info.get("uploader") or "").strip()[:80]
            duration = info.get("duration")

            return AudioData(
                file_path=audio_path,
                title=title,
                performer=performer,
                duration=int(round(duration)) if isinstance(duration, (int, float)) else None,
            )
    except Exception as exc:
        for leftover in DOWNLOAD_DIR.glob(f"{unique_id}_audio.*"):
            leftover.unlink(missing_ok=True)

        if "tiktok.com" in url.lower():
            tikwm = _try_tikwm_fallback(url, unique_id, ffmpeg_path)
            if tikwm:
                if tikwm.audio_path and tikwm.audio_path.exists():
                    for img in tikwm.image_paths:
                        img.unlink(missing_ok=True)
                    return AudioData(
                        file_path=tikwm.audio_path,
                        title=tikwm.title[:100],
                        performer=tikwm.author[:80],
                    )
                if tikwm.file_path and tikwm.file_path.exists() and ffmpeg_path:
                    mp3_out = DOWNLOAD_DIR / f"{unique_id}_extracted.mp3"
                    try:
                        subprocess.run(
                            [ffmpeg_path, "-y", "-i", str(tikwm.file_path), "-vn", "-c:a", "libmp3lame", "-b:a", "192k", str(mp3_out)],
                            check=True,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=60,
                        )
                        tikwm.cleanup()
                        return AudioData(
                            file_path=mp3_out,
                            title=tikwm.title[:100],
                            performer=tikwm.author[:80],
                        )
                    except Exception:
                        mp3_out.unlink(missing_ok=True)
                tikwm.cleanup()

        raise DownloadError(f"Could not extract audio: {exc}") from exc


def _create_video_note_sync(url: str) -> Path:
    ffmpeg_path = _resolve_ffmpeg_path()
    if not ffmpeg_path:
        raise DownloadError("ffmpeg is required to create a video note.")

    video_data = _download_sync(url, max_height=480)
    source_path = video_data.file_path
    rendered_temp: Path | None = None

    if video_data.is_slideshow and not source_path:
        rendered_temp = render_slideshow_to_video_sync(video_data)
        source_path = rendered_temp

    if not source_path or not source_path.exists():
        video_data.cleanup()
        raise DownloadError("Could not prepare video for video note.")

    unique_id = uuid.uuid4().hex[:10]
    note_path = DOWNLOAD_DIR / f"{unique_id}_vnote.mp4"

    cmd = [
        ffmpeg_path,
        "-y",
        "-i",
        str(source_path),
        "-t",
        "60",
        "-vf",
        "crop='min(iw,ih):min(iw,ih)',scale=384:384,format=yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-b:v",
        "600k",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-movflags",
        "+faststart",
        str(note_path),
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)
        if not note_path.exists():
            raise DownloadError("Failed to convert video note.")
        return note_path
    except Exception as exc:
        note_path.unlink(missing_ok=True)
        raise DownloadError("Error while converting to video note.") from exc
    finally:
        video_data.cleanup()
        if rendered_temp and rendered_temp.exists():
            rendered_temp.unlink(missing_ok=True)


def _search_youtube_track_sync(query: str) -> str:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            res = ydl.extract_info(f"ytsearch1:{query}", download=False)
            entries = (res or {}).get("entries") or []
            if entries:
                first = entries[0]
                vid_url = first.get("url") or first.get("webpage_url") or ""
                if vid_url and not vid_url.startswith("http"):
                    vid_url = f"https://www.youtube.com/watch?v={vid_url}"
                return vid_url
    except Exception:
        pass
    return ""


async def recognize_song_from_url(url: str) -> RecognizedSong:
    audio_data = await download_audio(url)
    title = ""
    artist = ""

    try:
        from shazamio import Shazam

        shazam = Shazam()
        out = await shazam.recognize(str(audio_data.file_path))
        track_info = (out or {}).get("track") or {}
        if track_info:
            title = (track_info.get("title") or "").strip()
            artist = (track_info.get("subtitle") or "").strip()
    except Exception:
        pass
    finally:
        if not title and audio_data.title and audio_data.title.lower() not in (
            "audio",
            "video",
            "відео",
            "original sound",
            "оригінальний звук",
        ):
            title = audio_data.title
            artist = audio_data.performer
        audio_data.cleanup()

    if not title:
        raise DownloadError("Could not recognize the song in this video.")

    search_q = f"{artist} - {title}" if artist else title
    full_track_url = await asyncio.to_thread(_search_youtube_track_sync, search_q)
    return RecognizedSong(title=title, artist=artist, full_track_url=full_track_url)


async def download_video(url: str, max_height: int | None = None) -> VideoData:
    async with JOB_SEMAPHORE:
        return await asyncio.to_thread(_download_sync, url, max_height)


async def download_audio(url: str) -> AudioData:
    async with JOB_SEMAPHORE:
        return await asyncio.to_thread(_download_audio_sync, url)


async def create_video_note(url: str) -> Path:
    async with JOB_SEMAPHORE:
        return await asyncio.to_thread(_create_video_note_sync, url)


async def render_slideshow_to_video(video_data: VideoData) -> Path | None:
    async with JOB_SEMAPHORE:
        return await asyncio.to_thread(render_slideshow_to_video_sync, video_data)


async def update_ytdlp_package() -> tuple[bool, str]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "yt-dlp",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    output = stdout.decode("utf-8", errors="replace").strip()
    success = proc.returncode == 0
    return success, output
