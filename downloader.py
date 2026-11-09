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
    r"(https?://(?:"
    r"(?:(?:vm|vt)\.tiktok\.com/[\w-]+|(?:[\w-]+\.)?tiktok\.com/(?:@[\w.-]+/(?:video|photo)/\d+|t/[\w-]+|v/\d+|video/\d+))"
    r"|(?:[\w-]+\.)?(?:instagram\.com|instagr\.am)/(?:(?:(?!share/)[\w.-]+/)?(?:reels?|p|tv)/[\w-]+|share/(?:(?:reels?|p)/)?[\w-]+)"
    r"|(?:(?:[\w-]+\.)?youtube\.com/(?:shorts/[\w-]+|watch\?[^\s<>\"']*v=[\w-]+|clip/[\w-]+)|youtu\.be/[\w-]+)"
    r"|(?:(?:[\w-]+\.)?(?:twitter\.com|x\.com)/[\w.-]+/status/\d+|t\.co/[\w-]+)"
    r"|(?:(?:[\w-]+\.)?reddit\.com/r/[\w.-]+/(?:comments|s)/[\w-]+|v\.redd\.it/[\w-]+|redd\.it/[\w-]+)"
    r"|(?:[\w-]+\.)?threads\.(?:net|com)/(?:@[\w.-]+/post/|t/)[\w-]+"
    r"|(?:(?:[\w-]+\.)?pinterest\.[\w.]+/pin/[\w-]+|pin\.it/[\w-]+)"
    r"|(?:(?:[\w-]+\.)?facebook\.com/(?:reel/|watch/?\?v=|share/[rv]/|[\w.-]+/videos/)[\w-]+|fb\.watch/[\w-]+)"
    r"|(?:(?:[\w-]+\.)?twitch\.tv/[\w.-]+/clip/[\w-]+|clips\.twitch\.tv/[\w-]+)"
    r")[^\s<>\"']*)",
    re.IGNORECASE,
)

ANY_URL_PATTERN = re.compile(
    r"(https?://[^\s<>\"']+)",
    re.IGNORECASE,
)

REDIRECT_NEEDED_PATTERN = re.compile(
    r"https?://(?:(?:www\.)?instagram\.com/share/|instagr\.am/|vm\.tiktok\.com/|vt\.tiktok\.com/|(?:www\.)?tiktok\.com/t/|pin\.it/|fb\.watch/|t\.co/)",
    re.IGNORECASE,
)

INSTAGRAM_SHORTCODE_PATTERN = re.compile(
    r"(?:instagram\.com|instagr\.am)/(?:(?!share/)[^/?#]+/)?(?:p|tv|reels?)/([^/?#&]+)",
    re.IGNORECASE,
)

TWITTER_STATUS_PATTERN = re.compile(
    r"(?:twitter\.com|x\.com)/([\w.-]+)/status/(\d+)",
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
    video_paths: list[Path] = field(default_factory=list)
    audio_path: Path | None = None
    direct_video_url: str | None = None
    thumbnail_url: str | None = None

    def cleanup(self) -> None:
        if self.file_path and self.file_path.exists():
            self.file_path.unlink(missing_ok=True)
        for vid in self.video_paths:
            if vid.exists():
                vid.unlink(missing_ok=True)
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


def _extract_clean_redirect_url(candidate: str | None) -> str | None:
    if not candidate:
        return None
    if candidate.startswith("/"):
        candidate = f"https://www.instagram.com{candidate}"
    if not candidate.startswith("http"):
        return None
    if "/accounts/login" in candidate:
        parsed = urllib.parse.urlparse(candidate)
        qs = urllib.parse.parse_qs(parsed.query)
        next_vals = qs.get("next")
        if next_vals and next_vals[0]:
            nxt = next_vals[0]
            if nxt.startswith("/"):
                nxt = f"https://www.instagram.com{nxt}"
            if nxt.startswith("http") and "/accounts/login" not in nxt:
                return nxt
        return None
    return candidate


def _resolve_share_redirects(url: str) -> str:
    if not REDIRECT_NEEDED_PATTERN.search(url):
        return url
    try:
        from curl_cffi import requests as cffi_requests

        # First try a single-hop check so Instagram /share/ links don't bounce twice to /accounts/login
        resp_hop = cffi_requests.get(
            url,
            impersonate="chrome",
            allow_redirects=False,
            timeout=10,
        )
        loc_hop = _extract_clean_redirect_url(resp_hop.headers.get("location"))
        if loc_hop and "/share/" not in loc_hop:
            return loc_hop

        resp = cffi_requests.get(
            url,
            impersonate="chrome",
            allow_redirects=True,
            timeout=10,
            stream=True,
        )
        final_url = _extract_clean_redirect_url(str(resp.url))
        resp.close()
        if final_url and "/share/" not in final_url:
            return final_url

        # Fallback with bot User-Agent which Instagram/TikTok often redirect cleanly for OG previews
        resp_bot = cffi_requests.get(
            url,
            headers={"User-Agent": "TelegramBot (like TwitterBot)"},
            allow_redirects=False,
            timeout=10,
        )
        loc_bot = _extract_clean_redirect_url(resp_bot.headers.get("location"))
        if loc_bot and "/share/" not in loc_bot:
            return loc_bot
    except Exception:
        pass

    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as resp:
            final_url = _extract_clean_redirect_url(resp.geturl())
            if final_url:
                return final_url
    except Exception:
        pass
    return url


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
        cl = resp.headers.get("Content-Length")
        if cl and cl.isdigit() and int(cl) > MAX_RAW_DOWNLOAD_BYTES:
            raise DownloadError("Video exceeds maximum allowed download size.")
        total = 0
        with dest.open("wb") as fh:
            while True:
                chunk = resp.read(262144)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_RAW_DOWNLOAD_BYTES:
                    fh.close()
                    dest.unlink(missing_ok=True)
                    raise DownloadError("Video exceeds maximum allowed download size.")
                fh.write(chunk)


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


def _download_cffi_or_urllib(url: str, dest: Path) -> None:
    try:
        from curl_cffi import requests as cffi_requests

        resp = cffi_requests.get(url, impersonate="chrome", timeout=35, stream=True)
        try:
            if resp.status_code == 200:
                cl = resp.headers.get("content-length")
                if cl and cl.isdigit() and int(cl) > MAX_RAW_DOWNLOAD_BYTES:
                    raise DownloadError("Video exceeds maximum allowed download size.")
                total = 0
                with dest.open("wb") as fh:
                    for chunk in resp.iter_content(chunk_size=262144):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > MAX_RAW_DOWNLOAD_BYTES:
                            fh.close()
                            dest.unlink(missing_ok=True)
                            raise DownloadError("Video exceeds maximum allowed download size.")
                        fh.write(chunk)
                if total > 1000:
                    return
                dest.unlink(missing_ok=True)
        finally:
            resp.close()
    except DownloadError:
        raise
    except Exception:
        dest.unlink(missing_ok=True)
    _download_file(url, dest)


def _try_instagram_fallback(url: str, unique_id: str, ffmpeg_path: str | None) -> VideoData | None:
    match = INSTAGRAM_SHORTCODE_PATTERN.search(url)
    if not match:
        return None
    shortcode = match.group(1)

    title = "Video"
    author = ""
    width: int | None = None
    height: int | None = None
    duration: float | int | None = None
    thumb_url: str | None = None
    video_url: str | None = None
    slide_video_urls: list[str] = []
    slide_image_urls: list[str] = []

    try:
        from curl_cffi import requests as cffi_requests

        # 1. Try Instagram Embed Captioned page (works without login even on datacenter IPs)
        try:
            emb_resp = cffi_requests.get(
                f"https://www.instagram.com/p/{shortcode}/embed/captioned/",
                impersonate="chrome",
                timeout=12,
            )
            if emb_resp.status_code == 200 and emb_resp.text:
                html_text = emb_resp.text
                ctx_match = re.search(r'"contextJSON"\s*:\s*"((?:[^"\\]|\\.)*)"', html_text)
                if ctx_match:
                    raw_ctx = json.loads(f'"{ctx_match.group(1)}"')
                    ctx_data = json.loads(raw_ctx)
                    gql = ctx_data.get("gql_data") or {}
                    media = gql.get("shortcode_media") or gql.get("xdt_shortcode_media") or {}
                    if media:
                        owner = media.get("owner") or {}
                        author = (owner.get("username") or owner.get("full_name") or "").strip()
                        cap_edges = ((media.get("edge_media_to_caption") or {}).get("edges")) or []
                        if cap_edges and isinstance(cap_edges[0], dict):
                            node_txt = ((cap_edges[0].get("node") or {}).get("text") or "").strip()
                            if node_txt:
                                title = node_txt
                        dims = media.get("dimensions") or {}
                        if isinstance(dims.get("width"), (int, float)):
                            width = int(dims["width"])
                        if isinstance(dims.get("height"), (int, float)):
                            height = int(dims["height"])
                        if isinstance(media.get("video_duration"), (int, float)):
                            duration = media["video_duration"]
                        thumb_url = media.get("display_url") or media.get("thumbnail_src")
                        video_url = media.get("video_url")

                        sidecar = ((media.get("edge_sidecar_to_children") or {}).get("edges")) or []
                        for edge in sidecar:
                            node = (edge or {}).get("node") or {}
                            if node.get("is_video") and node.get("video_url"):
                                v_candidate = node.get("video_url")
                                if v_candidate not in slide_video_urls:
                                    slide_video_urls.append(v_candidate)
                                if not video_url:
                                    video_url = v_candidate
                            else:
                                img_candidate = node.get("display_url")
                                if img_candidate and img_candidate not in slide_image_urls:
                                    slide_image_urls.append(img_candidate)

                if not video_url:
                    m_vid = re.search(r'\\"video_url\\"\s*:\s*\\"((?:[^"\\]|\\.)*)\\"', html_text)
                    if m_vid:
                        video_url = json.loads(f'"{m_vid.group(1)}"').replace(r"\/", "/")
        except Exception:
            pass

        # 2. Try kkinstagram redirect to direct Instagram CDN MP4
        if not video_url:
            try:
                kk_resp = cffi_requests.get(
                    f"https://www.kkinstagram.com/reel/{shortcode}/",
                    headers={"User-Agent": "TelegramBot (like TwitterBot)"},
                    allow_redirects=False,
                    timeout=10,
                )
                loc = kk_resp.headers.get("location")
                if loc and loc.startswith("http") and (".mp4" in loc or "fbcdn.net" in loc or "cdninstagram.com" in loc):
                    video_url = loc
                elif kk_resp.status_code == 200 and kk_resp.text:
                    og_m = re.search(
                        r'<meta[^>]+property=["\']og:video(?::secure_url)?["\'][^>]+content=["\']([^"\']+)["\']',
                        kk_resp.text,
                    )
                    if og_m:
                        video_url = html.unescape(og_m.group(1))
            except Exception:
                pass

        # 3. Try vxinstagram offload MP4
        if not video_url:
            try:
                vx_resp = cffi_requests.get(
                    f"https://www.vxinstagram.com/reel/{shortcode}/",
                    headers={"User-Agent": "TelegramBot (like TwitterBot)"},
                    allow_redirects=True,
                    timeout=10,
                )
                if vx_resp.status_code == 200 and vx_resp.text:
                    og_m = re.search(
                        r'<meta[^>]+property=["\']og:video(?::secure_url)?["\'][^>]+content=["\']([^"\']+)["\']',
                        vx_resp.text,
                    )
                    if og_m:
                        video_url = html.unescape(og_m.group(1))
                    if not thumb_url:
                        img_m = re.search(
                            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
                            vx_resp.text,
                        )
                        if img_m:
                            thumb_url = html.unescape(img_m.group(1))
            except Exception:
                pass
    except Exception:
        pass

    caption = build_caption(url, title=title, author=author)
    dur_int = int(round(duration)) if isinstance(duration, (int, float)) else None

    # Multi-video or mixed carousel post
    if len(slide_video_urls) > 1 or (slide_video_urls and slide_image_urls):
        video_paths: list[Path] = []
        image_paths: list[Path] = []
        for idx, v_url in enumerate(slide_video_urls[:10]):
            v_dest = DOWNLOAD_DIR / f"{unique_id}_igvid_{idx}.mp4"
            try:
                _download_cffi_or_urllib(v_url, v_dest)
                if v_dest.exists() and v_dest.stat().st_size > 1000:
                    v_dest = _compress_video_if_needed(v_dest, dur_int, ffmpeg_path)
                    video_paths.append(v_dest)
            except Exception:
                v_dest.unlink(missing_ok=True)
        for idx, img_url in enumerate(slide_image_urls[:10]):
            img_dest = DOWNLOAD_DIR / f"{unique_id}_igslide_{idx}.jpg"
            try:
                _download_cffi_or_urllib(img_url, img_dest)
                if img_dest.exists() and img_dest.stat().st_size > 500:
                    image_paths.append(img_dest)
            except Exception:
                img_dest.unlink(missing_ok=True)
        if video_paths:
            return VideoData(
                file_path=video_paths[0],
                title=title[:200],
                author=author[:100],
                caption=caption,
                width=width,
                height=height,
                duration=dur_int,
                video_paths=video_paths,
                image_paths=image_paths,
                direct_video_url=slide_video_urls[0],
                thumbnail_url=thumb_url if isinstance(thumb_url, str) and thumb_url.startswith("http") else None,
            )

    if video_url and video_url.startswith("http"):
        video_dest = DOWNLOAD_DIR / f"{unique_id}.mp4"
        try:
            _download_cffi_or_urllib(video_url, video_dest)
            if video_dest.exists() and video_dest.stat().st_size > 1000:
                video_dest = _compress_video_if_needed(video_dest, dur_int, ffmpeg_path)
                return VideoData(
                    file_path=video_dest,
                    title=title[:200],
                    author=author[:100],
                    caption=caption,
                    width=width,
                    height=height,
                    duration=dur_int,
                    direct_video_url=video_url,
                    thumbnail_url=thumb_url if isinstance(thumb_url, str) and thumb_url.startswith("http") else None,
                )
        except Exception:
            video_dest.unlink(missing_ok=True)

    if slide_image_urls:
        image_paths = []
        for idx, img_url in enumerate(slide_image_urls[:20]):
            img_dest = DOWNLOAD_DIR / f"{unique_id}_igslide_{idx}.jpg"
            try:
                _download_cffi_or_urllib(img_url, img_dest)
                if img_dest.exists() and img_dest.stat().st_size > 500:
                    image_paths.append(img_dest)
            except Exception:
                img_dest.unlink(missing_ok=True)
        if image_paths:
            return VideoData(
                file_path=None,
                title=title[:200],
                author=author[:100],
                caption=caption,
                is_slideshow=True,
                image_paths=image_paths,
                thumbnail_url=thumb_url if isinstance(thumb_url, str) and thumb_url.startswith("http") else None,
            )

    return None


def _try_twitter_fallback(url: str, unique_id: str, ffmpeg_path: str | None) -> VideoData | None:
    match = TWITTER_STATUS_PATTERN.search(url)
    if not match:
        return None
    username, status_id = match.group(1), match.group(2)
    api_url = f"https://api.vxtwitter.com/{username}/status/{status_id}"

    try:
        from curl_cffi import requests as cffi_requests

        resp = cffi_requests.get(api_url, impersonate="chrome", timeout=12)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception:
        return None

    media_list = data.get("media_extended") or []
    if not media_list:
        return None

    title = (data.get("text") or "Video").strip()
    author = (data.get("user_name") or data.get("user_screen_name") or username).strip()
    caption = build_caption(url, title=title, author=author)

    for item in media_list:
        if item.get("type") in ("video", "gif") and item.get("url"):
            v_url = item["url"]
            size_info = item.get("size") or {}
            width = size_info.get("width") if isinstance(size_info.get("width"), int) else None
            height = size_info.get("height") if isinstance(size_info.get("height"), int) else None
            dur_ms = item.get("duration_millis")
            dur_int = int(round(dur_ms / 1000)) if isinstance(dur_ms, (int, float)) else None
            thumb = item.get("thumbnail_url")

            video_dest = DOWNLOAD_DIR / f"{unique_id}.mp4"
            try:
                _download_cffi_or_urllib(v_url, video_dest)
                if video_dest.exists() and video_dest.stat().st_size > 1000:
                    video_dest = _compress_video_if_needed(video_dest, dur_int, ffmpeg_path)
                    return VideoData(
                        file_path=video_dest,
                        title=title[:200],
                        author=author[:100],
                        caption=caption,
                        width=width,
                        height=height,
                        duration=dur_int,
                        direct_video_url=v_url,
                        thumbnail_url=thumb if isinstance(thumb, str) and thumb.startswith("http") else None,
                    )
            except Exception:
                video_dest.unlink(missing_ok=True)
    return None


def _try_reddit_fallback(url: str, unique_id: str, ffmpeg_path: str | None) -> VideoData | None:
    if "reddit.com" not in url.lower() and "redd.it" not in url.lower():
        return None
    try:
        from curl_cffi import requests as cffi_requests

        rx_url = re.sub(r"https?://(?:[\w-]+\.)?reddit\.com", "https://rxddit.com", url, flags=re.IGNORECASE)
        resp = cffi_requests.get(
            rx_url,
            headers={"User-Agent": "TelegramBot (like TwitterBot)"},
            allow_redirects=True,
            timeout=12,
        )
        if resp.status_code == 200 and resp.text:
            og_v = re.search(
                r'<meta[^>]+property=["\']og:video(?::secure_url)?["\'][^>]+content=["\']([^"\']+)["\']',
                resp.text,
            )
            if og_v:
                v_url = html.unescape(og_v.group(1))
                t_m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', resp.text)
                img_m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', resp.text)
                title = html.unescape(t_m.group(1)).strip() if t_m else "Reddit Video"
                thumb = html.unescape(img_m.group(1)).strip() if img_m else None
                video_dest = DOWNLOAD_DIR / f"{unique_id}.mp4"
                try:
                    _download_cffi_or_urllib(v_url, video_dest)
                    if video_dest.exists() and video_dest.stat().st_size > 1000:
                        video_dest = _compress_video_if_needed(video_dest, None, ffmpeg_path)
                        return VideoData(
                            file_path=video_dest,
                            title=title[:200],
                            author="Reddit",
                            caption=build_caption(url, title=title, author="Reddit"),
                            direct_video_url=v_url,
                            thumbnail_url=thumb if isinstance(thumb, str) and thumb.startswith("http") else None,
                        )
                except Exception:
                    video_dest.unlink(missing_ok=True)
    except Exception:
        pass
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
    resolved_url = _resolve_share_redirects(url)

    if "tiktok.com" in resolved_url.lower():
        tikwm_res = _try_tikwm_fallback(resolved_url, unique_id, ffmpeg_path)
        if tikwm_res:
            return tikwm_res

    is_instagram = "instagram.com" in resolved_url.lower() or "instagr.am" in resolved_url.lower()
    if is_instagram and not COOKIES_FILE.exists():
        ig_res = _try_instagram_fallback(resolved_url, unique_id, ffmpeg_path)
        if ig_res:
            return ig_res

    is_twitter = "twitter.com" in resolved_url.lower() or "x.com" in resolved_url.lower()
    if is_twitter and not COOKIES_FILE.exists():
        tw_res = _try_twitter_fallback(resolved_url, unique_id, ffmpeg_path)
        if tw_res:
            return tw_res

    output_template = str(DOWNLOAD_DIR / f"{unique_id}.%(ext)s")
    target_res = max_height or 1080
    height_filter = f"[height<={target_res}]" if max_height else ""

    ydl_opts: dict = {
        "outtmpl": output_template,
        "format": f"bestvideo{height_filter}+bestaudio/best{height_filter}/best",
        "format_sort": ["vcodec:h264", "acodec:m4a", "ext:mp4:m4a", f"res:{target_res}"],
        "merge_output_format": "mp4",
        "max_filesize": MAX_RAW_DOWNLOAD_BYTES,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "geo_bypass": True,
        "js_runtimes": {"node": {}, "deno": {}},
    }

    if ffmpeg_path:
        ydl_opts["ffmpeg_location"] = ffmpeg_path
    if COOKIES_FILE.exists():
        ydl_opts["cookiefile"] = str(COOKIES_FILE)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(resolved_url, download=True)
            if not info:
                raise DownloadError("Could not extract video information.")

            if "entries" in info and info["entries"]:
                info = info["entries"][0]

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
            if not direct_url and isinstance(info.get("formats"), list):
                for fmt in reversed(info["formats"]):
                    if fmt.get("ext") == "mp4" and fmt.get("vcodec") != "none" and fmt.get("acodec") != "none":
                        direct_url = fmt.get("url")
                        break

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

        if is_instagram:
            ig_res = _try_instagram_fallback(resolved_url, unique_id, ffmpeg_path)
            if ig_res:
                return ig_res
        if is_twitter:
            tw_res = _try_twitter_fallback(resolved_url, unique_id, ffmpeg_path)
            if tw_res:
                return tw_res
        if "reddit.com" in resolved_url.lower() or "redd.it" in resolved_url.lower():
            rd_res = _try_reddit_fallback(resolved_url, unique_id, ffmpeg_path)
            if rd_res:
                return rd_res

        if isinstance(exc, DownloadError):
            raise
        raise DownloadError(f"Download failed: {exc}") from exc


def _download_audio_sync(url: str) -> AudioData:
    unique_id = uuid.uuid4().hex[:12]
    ffmpeg_path = _resolve_ffmpeg_path()
    resolved_url = _resolve_share_redirects(url)

    if "tiktok.com" in resolved_url.lower():
        tikwm = _try_tikwm_fallback(resolved_url, unique_id, ffmpeg_path)
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

    if ("instagram.com" in resolved_url.lower() or "instagr.am" in resolved_url.lower()) and not COOKIES_FILE.exists():
        ig_vid = _try_instagram_fallback(resolved_url, unique_id, ffmpeg_path)
        if ig_vid:
            if ig_vid.file_path and ig_vid.file_path.exists() and ffmpeg_path:
                mp3_out = DOWNLOAD_DIR / f"{unique_id}_ig_extracted.mp3"
                try:
                    subprocess.run(
                        [ffmpeg_path, "-y", "-i", str(ig_vid.file_path), "-vn", "-c:a", "libmp3lame", "-b:a", "192k", str(mp3_out)],
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=60,
                    )
                    ig_vid.cleanup()
                    return AudioData(
                        file_path=mp3_out,
                        title=ig_vid.title[:100],
                        performer=ig_vid.author[:80],
                        duration=ig_vid.duration,
                    )
                except Exception:
                    mp3_out.unlink(missing_ok=True)
            ig_vid.cleanup()

    output_template = str(DOWNLOAD_DIR / f"{unique_id}_audio.%(ext)s")
    ydl_opts: dict = {
        "outtmpl": output_template,
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "js_runtimes": {"node": {}, "deno": {}},
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
            info = ydl.extract_info(resolved_url, download=True)
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
        "yt-dlp[default]",
        "curl_cffi",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    output = stdout.decode("utf-8", errors="replace").strip()
    success = proc.returncode == 0
    return success, output
