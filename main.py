import asyncio
import html
import logging
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction, ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    ChatMemberUpdated,
    ChosenInlineResult,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultCachedVideo,
    InlineQueryResultVideo,
    InlineQueryResultsButton,
    InputMediaPhoto,
    InputMediaVideo,
    InputTextMessageContent,
    Message,
)

from config import BOT_TOKEN, COOKIES_FILE
from downloader import (
    AudioData,
    DownloadError,
    VideoData,
    build_caption,
    cleanup_old_downloads,
    create_video_note,
    detect_platform_name,
    download_audio,
    download_video,
    extract_all_video_urls,
    extract_video_url,
    get_fast_meta,
    is_regular_youtube_url,
    recognize_song_from_url,
    render_slideshow_to_video,
    update_ytdlp_package,
)
from storage import CachedMedia, ChatSettings, storage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("video_bot")

dp = Dispatcher()
_BOT_USERNAME: str = "bot"
_CAN_READ_ALL_GROUP_MESSAGES: bool = False


def _main_menu_keyboard(bot_username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Try Inline Mode & History",
                    switch_inline_query="",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Add Bot to a Group",
                    url=f"https://t.me/{bot_username}?startgroup=true&admin=delete_messages",
                )
            ],
            [
                InlineKeyboardButton(text="Chat Settings", callback_data="menu:settings"),
                InlineKeyboardButton(text="Platforms", callback_data="menu:platforms"),
            ],
            [
                InlineKeyboardButton(text="Stats & Status", callback_data="menu:stats"),
                InlineKeyboardButton(text="How to Use", callback_data="menu:help"),
            ],
        ]
    )


def _back_to_menu_keyboard(extra_buttons: list[list[InlineKeyboardButton]] | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if extra_buttons:
        rows.extend(extra_buttons)
    rows.append([InlineKeyboardButton(text="Back to Main Menu", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _settings_keyboard(cfg: ChatSettings, is_group: bool) -> InlineKeyboardMarkup:
    caption_state = "ON" if cfg.show_caption else "OFF"
    mp3_state = "ON" if cfg.show_mp3_button else "OFF"
    del_state = "ON" if cfg.delete_source_msg else "OFF"

    rows = [
        [
            InlineKeyboardButton(
                text=f"Video Caption: {caption_state}",
                callback_data="toggle:show_caption",
            )
        ],
        [
            InlineKeyboardButton(
                text=f"Action Buttons Under Video: {mp3_state}",
                callback_data="toggle:show_mp3_button",
            )
        ],
    ]
    if is_group:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"Delete Original Link Message: {del_state}",
                    callback_data="toggle:delete_source_msg",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="Back to Main Menu", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _loading_keyboard(result_id: str, platform: str = "Video") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Tap to Load Video ({platform})",
                    callback_data=f"inline_dl:{result_id}",
                )
            ]
        ]
    )


def _youtube_quality_keyboard(result_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="1080p Full HD", callback_data=f"ytq:1080:{result_id}"),
                InlineKeyboardButton(text="720p HD", callback_data=f"ytq:720:{result_id}"),
            ],
            [
                InlineKeyboardButton(text="480p", callback_data=f"ytq:480:{result_id}"),
                InlineKeyboardButton(text="360p", callback_data=f"ytq:360:{result_id}"),
            ],
            [
                InlineKeyboardButton(text="Audio Only (MP3)", callback_data=f"audio:{result_id}"),
            ],
        ]
    )


def _video_keyboard(
    result_id: str,
    url: str,
    show_actions: bool = True,
    mp3_done: bool = False,
) -> InlineKeyboardMarkup:
    if not show_actions:
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Original", url=url)]]
        )

    mp3_btn = (
        InlineKeyboardButton(text="MP3 Sent", callback_data="mp3_already_sent")
        if mp3_done
        else InlineKeyboardButton(text="Audio (MP3)", callback_data=f"audio:{result_id}")
    )
    shazam_btn = InlineKeyboardButton(text="Find Song", callback_data=f"shazam:{result_id}")
    vnote_btn = InlineKeyboardButton(text="Video Note", callback_data=f"vnote:{result_id}")
    orig_btn = InlineKeyboardButton(text="Original", url=url)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [mp3_btn, shazam_btn],
            [vnote_btn, orig_btn],
        ]
    )


def _build_home_text(bot_username: str) -> str:
    return (
        f"<b>Welcome! I download videos without watermarks.</b>\n\n"
        f"Send me one or multiple links in a single message, and I will reply with the video files directly in the chat.\n\n"
        f"<b>Three ways to use:</b>\n"
        f"1. <b>Direct Message.</b> Paste any video link right here.\n"
        f"2. <b>Inline Mode (@) in any chat.</b> Type in the message input:\n"
        f"<code>@{bot_username} link</code>, wait 2-3 seconds for the video card to appear, and tap it to send the video directly.\n"
        f"3. <b>Group Chats.</b> Add me to a group and I will automatically reply with the video file whenever someone shares a link."
    )


def _build_platforms_text() -> str:
    return (
        "<b>Supported Platforms & Features:</b>\n\n"
        "• <b>TikTok</b>: watermark-free videos & photo slideshows with music\n"
        "• <b>Instagram</b>: Reels, video posts, and carousels\n"
        "• <b>YouTube</b>: Shorts & regular videos with quality selection (1080p, 720p, 480p, 360p, MP3)\n"
        "• <b>Twitter (X)</b>: video posts\n"
        "• <b>Reddit</b>: video posts (reddit.com / v.redd.it)\n"
        "• <b>Threads</b>: video posts\n"
        "• <b>Facebook</b>: Reels and Watch videos\n"
        "• <b>Pinterest</b>: video pins\n"
        "• <b>Twitch</b>: clips\n\n"
        "<b>Built-in Tools Under Every Video:</b>\n"
        "• Extract audio as MP3\n"
        "• Identify songs via Shazam and download the full track\n"
        "• Convert any video into a round Telegram Video Note"
    )


def _build_help_text(bot_username: str) -> str:
    return (
        "<b>How to Use the Bot:</b>\n\n"
        "<b>Inline Mode & History (@):</b>\n"
        f"In any chat or channel, type <code>@{bot_username} link</code> and wait 2-3 seconds. "
        "The bot will prepare the video right in the popup list so when you tap it, the video file is sent immediately.\n\n"
        "<b>Batch Downloading:</b>\n"
        "You can send up to 5 links in one message, and the bot will download each of them in order.\n\n"
        "<b>Updating cookies.txt (Admin):</b>\n"
        "To bypass login walls on Instagram or YouTube from a server IP, simply send a <code>cookies.txt</code> file directly to this chat."
    )


def _build_settings_text(cfg: ChatSettings, is_group: bool) -> str:
    chat_label = "this group" if is_group else "your chat"
    text = (
        f"<b>Settings for {chat_label}:</b>\n\n"
        f"• <b>Video Caption</b>: show author and video title under the video.\n"
        f"• <b>Action Buttons</b>: show Audio (MP3), Find Song, and Video Note buttons.\n"
    )
    if is_group:
        text += (
            "• <b>Delete Original Link Message</b>: if the bot has admin rights in the group, "
            "it will delete the user's raw link message and keep only the clean video.\n"
        )
    text += "\nTap a button below to toggle a setting:"
    return text


def _build_stats_text(chat_id: int) -> str:
    storage_chat = storage.get_storage_chat_id()
    inline_ready = f"Ready (Chat ID: {storage_chat})" if storage_chat else "Send /start in a private chat with the bot first"
    cookies_ready = "Active (cookies.txt loaded)" if COOKIES_FILE.exists() else "Not set (send cookies.txt to DM if needed)"
    return (
        "<b>Bot Statistics & Status:</b>\n\n"
        f"• <b>Total Videos Downloaded</b>: {storage.total_downloads}\n"
        f"• <b>Videos in Instant Cache</b>: {len(storage.media_cache)}\n"
        f"• <b>Audio Tracks in Cache</b>: {len(storage.audio_only_cache)}\n"
        f"• <b>Recognized Songs (Shazam)</b>: {len(storage.song_cache)}\n"
        f"• <b>Inline Mode Status</b>: {inline_ready}\n"
        f"• <b>Cookies Status</b>: {cookies_ready}\n"
        f"• <b>Current Chat ID</b>: <code>{chat_id}</code>"
    )


async def _delete_msg_quietly(msg: Message) -> None:
    try:
        await msg.delete()
    except Exception:
        pass


async def _prepare_and_cache_video_for_inline(
    url: str,
    bot: Bot,
    user_id: int,
) -> tuple[CachedMedia | None, VideoData | None]:
    async with storage.get_lock(url):
        cached = storage.get_cached_media(url)
        if cached:
            return cached, None

        max_h = 720 if is_regular_youtube_url(url) else None
        video_data = await download_video(url, max_height=max_h)
        rendered_slideshow = None

        try:
            upload_path = video_data.file_path
            if video_data.is_slideshow and not upload_path:
                try:
                    rendered_path = await render_slideshow_to_video(video_data)
                    upload_path = rendered_path
                    rendered_slideshow = rendered_path
                except Exception as exc:
                    logger.warning("Slideshow render failed for %s: %s", url, exc)

            if not upload_path or not upload_path.exists():
                return None, video_data

            candidate_chats: list[int] = []
            saved_chat = storage.get_storage_chat_id()
            if saved_chat:
                candidate_chats.append(saved_chat)
            if user_id and user_id not in candidate_chats:
                candidate_chats.append(user_id)
            for known_chat_str in list(storage.chat_settings.keys()):
                try:
                    known_chat_id = int(known_chat_str)
                    if known_chat_id not in candidate_chats:
                        candidate_chats.append(known_chat_id)
                except ValueError:
                    continue

            for target_chat in candidate_chats:
                try:
                    if target_chat != saved_chat:
                        await bot.send_chat_action(chat_id=target_chat, action=ChatAction.UPLOAD_VIDEO)
                    temp_msg = await bot.send_video(
                        chat_id=target_chat,
                        video=FSInputFile(upload_path),
                        width=video_data.width,
                        height=video_data.height,
                        duration=video_data.duration,
                        supports_streaming=True,
                        disable_notification=True,
                    )
                    if temp_msg.video:
                        file_id = temp_msg.video.file_id
                        caption = video_data.caption or build_caption(url, video_data.title, video_data.author)
                        storage.save_media(
                            url,
                            file_id,
                            caption=caption,
                            title=video_data.title,
                            author=video_data.author,
                        )
                        if not saved_chat or target_chat != saved_chat:
                            storage.set_fallback_storage_chat_id(target_chat)
                        asyncio.create_task(_delete_msg_quietly(temp_msg))
                        return storage.get_cached_media(url), video_data
                except Exception as exc:
                    logger.warning("Could not upload temp inline video to chat %s: %s", target_chat, exc)
                    continue
            return None, video_data
        finally:
            video_data.cleanup()
            if rendered_slideshow and hasattr(rendered_slideshow, "unlink"):
                rendered_slideshow.unlink(missing_ok=True)


@dp.my_chat_member()
async def handle_bot_added_to_group(event: ChatMemberUpdated, bot: Bot) -> None:
    if event.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    new_status = event.new_chat_member.status
    old_status = event.old_chat_member.status
    if new_status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR) and old_status in (
        ChatMemberStatus.LEFT,
        ChatMemberStatus.KICKED,
        ChatMemberStatus.RESTRICTED,
    ):
        if not storage.get_storage_chat_id():
            storage.set_fallback_storage_chat_id(event.chat.id)
        privacy_hint = ""
        if not _CAN_READ_ALL_GROUP_MESSAGES and new_status != ChatMemberStatus.ADMINISTRATOR:
            privacy_hint = (
                "\n\n<b>Important:</b> Give the bot <b>Admin rights</b> in this group (or disable "
                "<i>Group Privacy</i> in @BotFather) so Telegram allows it to see links posted in the chat."
            )
        await bot.send_message(
            chat_id=event.chat.id,
            text=(
                f"<b>@{_BOT_USERNAME} is active!</b>\n"
                "Send any video or Reel link from TikTok, Instagram, YouTube, Twitter (X), "
                "Reddit, Threads, Facebook, Pinterest, or Twitch, and I will automatically reply with the video."
                f"{privacy_hint}"
            ),
            parse_mode=ParseMode.HTML,
        )


@dp.message(CommandStart())
async def handle_start(message: Message, bot: Bot) -> None:
    if message.chat.type == ChatType.PRIVATE:
        storage.set_fallback_storage_chat_id(message.chat.id)

    await message.answer(
        _build_home_text(_BOT_USERNAME),
        parse_mode=ParseMode.HTML,
        reply_markup=_main_menu_keyboard(_BOT_USERNAME),
    )


@dp.message(Command("help"))
async def handle_help_command(message: Message) -> None:
    if message.chat.type == ChatType.PRIVATE:
        storage.set_fallback_storage_chat_id(message.chat.id)
    await message.answer(
        _build_help_text(_BOT_USERNAME),
        parse_mode=ParseMode.HTML,
        reply_markup=_back_to_menu_keyboard(),
    )


@dp.message(Command("platforms"))
async def handle_platforms_command(message: Message) -> None:
    await message.answer(
        _build_platforms_text(),
        parse_mode=ParseMode.HTML,
        reply_markup=_back_to_menu_keyboard(),
    )


@dp.message(Command("settings"))
async def handle_settings_command(message: Message) -> None:
    is_group = message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)
    cfg = storage.get_chat_settings(message.chat.id)
    await message.answer(
        _build_settings_text(cfg, is_group),
        parse_mode=ParseMode.HTML,
        reply_markup=_settings_keyboard(cfg, is_group),
    )


@dp.message(Command("stats"))
async def handle_stats_command(message: Message) -> None:
    extra = []
    if message.from_user and storage.is_admin(message.from_user.id):
        extra.append([InlineKeyboardButton(text="Update yt-dlp Module", callback_data="action:update_ytdlp")])
    await message.answer(
        _build_stats_text(message.chat.id),
        parse_mode=ParseMode.HTML,
        reply_markup=_back_to_menu_keyboard(extra),
    )


@dp.message(Command("update"))
async def handle_update_command(message: Message) -> None:
    if not message.from_user or not storage.is_admin(message.from_user.id):
        await message.reply("This command is only available to the bot administrator.")
        return

    status = await message.reply("Checking and installing yt-dlp updates...")
    success, output = await update_ytdlp_package()
    tail = html.escape(output[-500:] if len(output) > 500 else output)
    if success:
        await status.edit_text(
            f"<b>yt-dlp updated successfully.</b>\n\n<code>{tail}</code>",
            parse_mode=ParseMode.HTML,
        )
    else:
        await status.edit_text(
            f"<b>Error updating yt-dlp:</b>\n\n<code>{tail}</code>",
            parse_mode=ParseMode.HTML,
        )


@dp.message(F.document)
async def handle_cookies_upload(message: Message, bot: Bot) -> None:
    if message.chat.type != ChatType.PRIVATE or not message.document:
        return
    if not message.from_user or not storage.is_admin(message.from_user.id):
        return

    file_name = (message.document.file_name or "").lower()
    if not file_name.endswith(".txt"):
        return

    if message.document.file_size and message.document.file_size > 2 * 1024 * 1024:
        await message.reply("File is too large for a cookies.txt file.")
        return

    try:
        tg_file = await bot.get_file(message.document.file_id)
        if not tg_file.file_path:
            raise ValueError("Empty file path")
        await bot.download_file(tg_file.file_path, destination=COOKIES_FILE)
        await message.reply(
            "<b>cookies.txt has been saved and activated!</b>\n"
            "All future Instagram, YouTube, and TikTok downloads will use these cookies.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        logger.warning("Failed to save cookies.txt: %s", exc)
        await message.reply("Failed to save cookies.txt.")


@dp.callback_query(F.data.startswith("menu:"))
async def handle_menu_navigation(callback: CallbackQuery) -> None:
    if not callback.message:
        await callback.answer()
        return

    section = (callback.data or "").split(":", 1)[1]
    chat_id = callback.message.chat.id
    is_group = callback.message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)

    try:
        if section == "home":
            await callback.message.edit_text(
                _build_home_text(_BOT_USERNAME),
                parse_mode=ParseMode.HTML,
                reply_markup=_main_menu_keyboard(_BOT_USERNAME),
            )
        elif section == "platforms":
            await callback.message.edit_text(
                _build_platforms_text(),
                parse_mode=ParseMode.HTML,
                reply_markup=_back_to_menu_keyboard(),
            )
        elif section == "help":
            await callback.message.edit_text(
                _build_help_text(_BOT_USERNAME),
                parse_mode=ParseMode.HTML,
                reply_markup=_back_to_menu_keyboard(),
            )
        elif section == "settings":
            cfg = storage.get_chat_settings(chat_id)
            await callback.message.edit_text(
                _build_settings_text(cfg, is_group),
                parse_mode=ParseMode.HTML,
                reply_markup=_settings_keyboard(cfg, is_group),
            )
        elif section == "stats":
            extra = []
            if callback.from_user and storage.is_admin(callback.from_user.id):
                extra.append([InlineKeyboardButton(text="Update yt-dlp Module", callback_data="action:update_ytdlp")])
            await callback.message.edit_text(
                _build_stats_text(chat_id),
                parse_mode=ParseMode.HTML,
                reply_markup=_back_to_menu_keyboard(extra),
            )
    except TelegramBadRequest:
        pass
    await callback.answer()


@dp.callback_query(F.data.startswith("toggle:"))
async def handle_toggle_setting(callback: CallbackQuery) -> None:
    if not callback.message:
        await callback.answer()
        return

    field_name = (callback.data or "").split(":", 1)[1]
    chat_id = callback.message.chat.id
    is_group = callback.message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)

    cfg = storage.toggle_chat_setting(chat_id, field_name)
    try:
        await callback.message.edit_text(
            _build_settings_text(cfg, is_group),
            parse_mode=ParseMode.HTML,
            reply_markup=_settings_keyboard(cfg, is_group),
        )
    except TelegramBadRequest:
        pass
    await callback.answer("Setting saved")


@dp.callback_query(F.data == "action:update_ytdlp")
async def handle_inline_update_ytdlp(callback: CallbackQuery) -> None:
    if not callback.from_user or not storage.is_admin(callback.from_user.id):
        await callback.answer("Administrator only.", show_alert=True)
        return

    await callback.answer("Updating yt-dlp, please wait a few seconds...")
    success, output = await update_ytdlp_package()
    tail = html.escape(output[-400:] if len(output) > 400 else output)
    if callback.message:
        status_title = "yt-dlp updated successfully!" if success else "Error updating yt-dlp:"
        try:
            await callback.message.edit_text(
                f"<b>{status_title}</b>\n\n<code>{tail}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=_back_to_menu_keyboard(),
            )
        except TelegramBadRequest:
            pass


@dp.callback_query(F.data == "downloading")
async def handle_downloading_callback(callback: CallbackQuery) -> None:
    await callback.answer("Video is currently downloading, please wait a few seconds.", show_alert=False)


@dp.callback_query(F.data.startswith("inline_dl:"))
async def handle_inline_download_button(callback: CallbackQuery, bot: Bot) -> None:
    if not callback.inline_message_id:
        await callback.answer()
        return

    result_id = (callback.data or "").split(":", 1)[1]
    url = storage.get_url(result_id)
    if not url:
        await callback.answer("Link expired, please resend.", show_alert=True)
        return

    await callback.answer("Loading video into chat...")
    await _replace_inline_message_with_video(
        inline_message_id=callback.inline_message_id,
        url=url,
        user_id=callback.from_user.id,
        bot=bot,
    )


@dp.callback_query(F.data == "mp3_already_sent")
async def handle_mp3_already_sent_callback(callback: CallbackQuery) -> None:
    await callback.answer("The audio track for this video has already been sent.", show_alert=False)


@dp.callback_query(F.data.startswith("audio:"))
async def handle_audio_callback(callback: CallbackQuery, bot: Bot) -> None:
    result_id = (callback.data or "").split(":", 1)[1]
    url = storage.get_url(result_id)
    if not url:
        await callback.answer("Link expired, please resend the video link.", show_alert=True)
        return

    is_inline = callback.message is None
    if is_inline:
        await callback.answer("Preparing MP3! It will be sent to your private chat with the bot.", show_alert=True)
    else:
        await callback.answer("Extracting MP3 audio...")

    target_chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    reply_to_id = callback.message.message_id if callback.message else None

    async with storage.get_lock(f"audio:{url}"):
        cached_audio_id = storage.get_audio_file_id(url)
        if cached_audio_id:
            try:
                await bot.send_audio(
                    chat_id=target_chat_id,
                    audio=cached_audio_id,
                    reply_to_message_id=reply_to_id,
                )
                if callback.message:
                    try:
                        await callback.message.edit_reply_markup(
                            reply_markup=_video_keyboard(result_id, url, show_actions=True, mp3_done=True)
                        )
                    except TelegramBadRequest:
                        pass
                return
            except TelegramBadRequest:
                pass

        audio_data: AudioData | None = None
        try:
            await bot.send_chat_action(chat_id=target_chat_id, action=ChatAction.UPLOAD_VOICE)
            audio_data = await download_audio(url)
            sent_audio = await bot.send_audio(
                chat_id=target_chat_id,
                audio=FSInputFile(audio_data.file_path),
                title=audio_data.title,
                performer=audio_data.performer or None,
                duration=audio_data.duration,
                reply_to_message_id=reply_to_id,
            )
            if sent_audio.audio:
                storage.save_audio_file_id(url, sent_audio.audio.file_id)

            if callback.message:
                try:
                    await callback.message.edit_reply_markup(
                        reply_markup=_video_keyboard(result_id, url, show_actions=True, mp3_done=True)
                    )
                except TelegramBadRequest:
                    pass
        except Exception as exc:
            logger.warning("Failed to send MP3 (%s): %s", url, exc)
            if callback.message:
                await callback.message.reply("Could not extract the audio track from this video.")
        finally:
            if audio_data:
                audio_data.cleanup()


@dp.callback_query(F.data.startswith("shazam:"))
async def handle_shazam_callback(callback: CallbackQuery, bot: Bot) -> None:
    result_id = (callback.data or "").split(":", 1)[1]
    url = storage.get_url(result_id)
    if not url:
        await callback.answer("Link expired, please resend the video link.", show_alert=True)
        return

    is_inline = callback.message is None
    if is_inline:
        await callback.answer("Identifying track via Shazam! Result will be sent to your private chat.", show_alert=True)
    else:
        await callback.answer("Identifying song via Shazam...")

    target_chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    reply_to_id = callback.message.message_id if callback.message else None

    async with storage.get_lock(f"shazam:{url}"):
        cached_song = storage.get_recognized_song(url)
        if not cached_song:
            try:
                song = await recognize_song_from_url(url)
                cached_song = {
                    "title": song.title,
                    "artist": song.artist,
                    "full_track_url": song.full_track_url,
                }
                storage.save_recognized_song(url, cached_song)
            except DownloadError as exc:
                await bot.send_message(
                    chat_id=target_chat_id,
                    text=str(exc),
                    reply_to_message_id=reply_to_id,
                )
                return
            except Exception as exc:
                logger.warning("Shazam error (%s): %s", url, exc)
                await bot.send_message(
                    chat_id=target_chat_id,
                    text="Could not identify the song in this video.",
                    reply_to_message_id=reply_to_id,
                )
                return

        title = cached_song.get("title", "Unknown Track")
        artist = cached_song.get("artist", "")
        full_url = cached_song.get("full_track_url", "")

        song_label = f"<b>{html.escape(artist)}</b> - {html.escape(title)}" if artist else f"<b>{html.escape(title)}</b>"
        text = f"Identified Track:\n{song_label}"

        buttons: list[list[InlineKeyboardButton]] = []
        if full_url:
            track_res_id = storage.register_url(full_url)
            buttons.append(
                [
                    InlineKeyboardButton(
                        text="Download Full Song (MP3)",
                        callback_data=f"audio:{track_res_id}",
                    )
                ]
            )
            buttons.append([InlineKeyboardButton(text="Open on YouTube", url=full_url)])

        markup = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
        await bot.send_message(
            chat_id=target_chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            reply_to_message_id=reply_to_id,
        )


@dp.callback_query(F.data.startswith("vnote:"))
async def handle_video_note_callback(callback: CallbackQuery, bot: Bot) -> None:
    result_id = (callback.data or "").split(":", 1)[1]
    url = storage.get_url(result_id)
    if not url:
        await callback.answer("Link expired, please resend the video link.", show_alert=True)
        return

    is_inline = callback.message is None
    if is_inline:
        await callback.answer("Creating Video Note! It will be sent to your private chat.", show_alert=True)
    else:
        await callback.answer("Formatting as a round Video Note (up to 60s)...")

    target_chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    reply_to_id = callback.message.message_id if callback.message else None

    async with storage.get_lock(f"vnote:{url}"):
        cached_vnote_id = storage.get_video_note_file_id(url)
        if cached_vnote_id:
            try:
                await bot.send_video_note(
                    chat_id=target_chat_id,
                    video_note=cached_vnote_id,
                    reply_to_message_id=reply_to_id,
                )
                return
            except TelegramBadRequest:
                pass

        note_path: Path | None = None
        try:
            await bot.send_chat_action(chat_id=target_chat_id, action=ChatAction.RECORD_VIDEO_NOTE)
            note_path = await create_video_note(url)
            await bot.send_chat_action(chat_id=target_chat_id, action=ChatAction.UPLOAD_VIDEO_NOTE)
            sent_note = await bot.send_video_note(
                chat_id=target_chat_id,
                video_note=FSInputFile(note_path),
                length=384,
                reply_to_message_id=reply_to_id,
            )
            if sent_note.video_note:
                storage.save_video_note_file_id(url, sent_note.video_note.file_id)
        except Exception as exc:
            logger.warning("Video Note error (%s): %s", url, exc)
            await bot.send_message(
                chat_id=target_chat_id,
                text="Could not convert this video into a Video Note.",
                reply_to_message_id=reply_to_id,
            )
        finally:
            if note_path and note_path.exists():
                note_path.unlink(missing_ok=True)


@dp.callback_query(F.data.startswith("ytq:"))
async def handle_youtube_quality_callback(callback: CallbackQuery, bot: Bot) -> None:
    if not callback.message:
        await callback.answer()
        return

    parts = (callback.data or "").split(":", 2)
    if len(parts) < 3:
        await callback.answer()
        return

    height = int(parts[1])
    result_id = parts[2]
    url = storage.get_url(result_id)
    if not url:
        await callback.answer("Link expired, please send the link again.", show_alert=True)
        return

    await callback.answer(f"Downloading YouTube video in {height}p...")
    chat_id = callback.message.chat.id
    cfg = storage.get_chat_settings(chat_id)

    try:
        await callback.message.edit_text(
            f"<b>Downloading from YouTube ({height}p)...</b>",
            parse_mode=ParseMode.HTML,
        )
    except TelegramBadRequest:
        pass

    video_data: VideoData | None = None
    try:
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)
        video_data = await download_video(url, max_height=height)
        if not video_data.file_path or not video_data.file_path.exists():
            raise DownloadError("Video file was not found.")

        full_caption = video_data.caption or build_caption(url, video_data.title, video_data.author)
        display_caption = (
            full_caption
            if cfg.show_caption
            else f'<a href="{html.escape(url, quote=True)}">Original</a>'
        )

        await callback.message.edit_text("<b>Sending video to chat...</b>", parse_mode=ParseMode.HTML)
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

        reply_target = callback.message.reply_to_message or callback.message
        sent_msg = await reply_target.reply_video(
            video=FSInputFile(video_data.file_path),
            caption=display_caption,
            parse_mode=ParseMode.HTML,
            reply_markup=_video_keyboard(result_id, url, show_actions=cfg.show_mp3_button),
            width=video_data.width,
            height=video_data.height,
            duration=video_data.duration,
            supports_streaming=True,
        )
        if sent_msg.video:
            storage.save_media(
                url,
                sent_msg.video.file_id,
                caption=full_caption,
                title=video_data.title,
                author=video_data.author,
            )
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            pass
    except Exception as exc:
        logger.warning("YouTube error (%s, %sp): %s", url, height, exc)
        try:
            await callback.message.edit_text(
                f"<b>Failed to download YouTube video ({height}p).</b>\n{html.escape(str(exc))}",
                parse_mode=ParseMode.HTML,
            )
        except TelegramBadRequest:
            pass
    finally:
        if video_data:
            video_data.cleanup()


@dp.inline_query()
async def handle_inline_query(inline_query: InlineQuery, bot: Bot) -> None:
    query_text = inline_query.query.strip()
    url = extract_video_url(query_text, allow_any_http=True)

    if not url:
        recent_items = storage.get_recent_media(query=query_text, limit=10)
        if recent_items:
            results = []
            for idx, (cached_url, media) in enumerate(recent_items):
                res_id = storage.register_url(cached_url)
                platform = detect_platform_name(cached_url)
                title_label = media.title or cached_url
                caption = media.caption or build_caption(cached_url, media.title, media.author)
                results.append(
                    InlineQueryResultCachedVideo(
                        id=f"hist_v{storage.SCHEMA_VERSION}_{idx}_{res_id[:12]}",
                        video_file_id=media.file_id,
                        title=f"[{platform}] {title_label[:60]}",
                        description=media.author or cached_url,
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                        reply_markup=_video_keyboard(res_id, cached_url),
                    )
                )
            await inline_query.answer(
                results,
                cache_time=2,
                is_personal=True,
                button=InlineQueryResultsButton(
                    text="Paste a video link or pick from history below",
                    start_parameter="inline_help",
                ),
            )
            return

        help_article = InlineQueryResultArticle(
            id="help_prompt",
            title="Paste a video link after the bot username",
            description="Supports TikTok, Instagram, YouTube, Twitter (X), Reddit, Threads, Facebook, Pinterest, Twitch",
            input_message_content=InputTextMessageContent(
                message_text=_build_home_text(_BOT_USERNAME),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            ),
        )
        await inline_query.answer(
            [help_article],
            cache_time=1,
            is_personal=True,
            button=InlineQueryResultsButton(
                text="Open Bot Main Menu",
                start_parameter="inline_help",
            ),
        )
        return

    platform = detect_platform_name(url)
    result_id = storage.register_url(url)
    cached_media = storage.get_cached_media(url)

    if not cached_media:
        try:
            prep_task = asyncio.create_task(
                _prepare_and_cache_video_for_inline(
                    url,
                    bot,
                    inline_query.from_user.id,
                )
            )
            cached_media, _ = await asyncio.wait_for(
                asyncio.shield(prep_task),
                timeout=9.0,
            )
        except asyncio.TimeoutError:
            logger.info("Inline download exceeded 9.0s timeout for %s, falling back to placeholder card", url)
        except Exception as exc:
            logger.warning("Inline direct download failed for %s: %s", url, exc)

    if cached_media:
        caption = cached_media.caption or build_caption(url, cached_media.title, cached_media.author)
        cached_video = InlineQueryResultCachedVideo(
            id=f"cached_v{storage.SCHEMA_VERSION}_{result_id}",
            video_file_id=cached_media.file_id,
            title=f"Send Video from {platform}",
            description=cached_media.title or url,
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=_video_keyboard(result_id, url),
        )
        try:
            await inline_query.answer([cached_video], cache_time=5, is_personal=False)
            return
        except TelegramBadRequest as exc:
            logger.warning("Inline answer with cached_video failed for %s: %s", url, exc)

    download_article = InlineQueryResultArticle(
        id=f"load_{result_id}",
        title=f"Send Video from {platform}",
        description=f"Tap here to send the video to chat ({url[:60]})",
        input_message_content=InputTextMessageContent(
            message_text=f"<b>Loading video from {platform}...</b>",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        ),
        reply_markup=_loading_keyboard(result_id, platform),
    )
    try:
        await inline_query.answer([download_article], cache_time=1, is_personal=False)
    except TelegramBadRequest as exc:
        logger.warning("Inline answer with download_article failed for %s: %s", url, exc)


async def _replace_inline_message_with_video(
    inline_message_id: str,
    url: str,
    user_id: int,
    bot: Bot,
    is_placeholder: bool = True,
) -> None:
    platform = detect_platform_name(url)
    result_id = storage.register_url(url)

    try:
        cached_media, _ = await _prepare_and_cache_video_for_inline(url, bot, user_id)
        if not cached_media:
            if is_placeholder:
                await bot.edit_message_text(
                    inline_message_id=inline_message_id,
                    text=(
                        f"<b>One-time activation needed:</b> open @{_BOT_USERNAME} and press /start "
                        f"so the bot can upload inline video files."
                    ),
                    parse_mode=ParseMode.HTML,
                )
            return

        caption = cached_media.caption or build_caption(url, cached_media.title, cached_media.author)
        await bot.edit_message_media(
            inline_message_id=inline_message_id,
            media=InputMediaVideo(
                media=cached_media.file_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
                supports_streaming=True,
            ),
            reply_markup=_video_keyboard(result_id, url),
        )
    except DownloadError as exc:
        logger.warning("Download error (%s): %s", url, exc)
        if is_placeholder:
            try:
                await bot.edit_message_text(
                    inline_message_id=inline_message_id,
                    text=f"<b>Failed to download video from {platform}.</b>\nMake sure the link is public.",
                    parse_mode=ParseMode.HTML,
                )
            except TelegramBadRequest:
                pass
    except Exception as exc:
        logger.exception("Unexpected error in inline mode: %s", exc)
        if is_placeholder:
            try:
                await bot.edit_message_text(
                    inline_message_id=inline_message_id,
                    text=f"<b>An error occurred while processing the video.</b>\n{html.escape(url)}",
                    parse_mode=ParseMode.HTML,
                )
            except TelegramBadRequest:
                pass


@dp.chosen_inline_result()
async def handle_chosen_inline_result(chosen: ChosenInlineResult, bot: Bot) -> None:
    inline_message_id = chosen.inline_message_id
    if not inline_message_id:
        return

    res_id = chosen.result_id or ""
    if res_id.startswith(("cached_", "hist_")):
        return

    clean_res_id = res_id.removeprefix("load_").removeprefix("direct_")
    url = extract_video_url(chosen.query, allow_any_http=True) or storage.get_url(clean_res_id) or storage.get_url(res_id)
    if not url:
        return

    await _replace_inline_message_with_video(
        inline_message_id=inline_message_id,
        url=url,
        user_id=chosen.from_user.id,
        bot=bot,
        is_placeholder=not res_id.startswith("direct_"),
    )


async def _process_single_url_in_chat(
    message: Message,
    bot: Bot,
    url: str,
    prefix: str = "",
    allow_delete_source: bool = True,
) -> None:
    cfg = storage.get_chat_settings(message.chat.id)
    is_group = message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)
    platform = detect_platform_name(url)
    result_id = storage.register_url(url)

    sender_note = ""
    if is_group and cfg.delete_source_msg and allow_delete_source and message.from_user:
        user_name = html.escape(message.from_user.full_name)
        sender_note = f"\nShared by: <b>{user_name}</b>"

    async with storage.get_lock(url):
        cached_media = storage.get_cached_media(url)
        if cached_media:
            base_caption = (
                (cached_media.caption or build_caption(url, cached_media.title, cached_media.author))
                if cfg.show_caption
                else f'<a href="{html.escape(url, quote=True)}">Original</a>'
            )
            final_caption = base_caption + sender_note
            try:
                if is_group and cfg.delete_source_msg and allow_delete_source:
                    await message.answer_video(
                        video=cached_media.file_id,
                        caption=final_caption,
                        parse_mode=ParseMode.HTML,
                        reply_markup=_video_keyboard(result_id, url, show_actions=cfg.show_mp3_button),
                        supports_streaming=True,
                        message_thread_id=message.message_thread_id,
                    )
                    try:
                        await message.delete()
                    except TelegramBadRequest:
                        pass
                else:
                    await message.reply_video(
                        video=cached_media.file_id,
                        caption=final_caption,
                        parse_mode=ParseMode.HTML,
                        reply_markup=_video_keyboard(result_id, url, show_actions=cfg.show_mp3_button),
                        supports_streaming=True,
                    )
                return
            except TelegramBadRequest:
                pass

        status_msg: Message | None = None
        if not is_group:
            status_msg = await message.reply(
                f"{prefix}<b>Downloading from {platform}...</b>",
                parse_mode=ParseMode.HTML,
            )
        video_data: VideoData | None = None

        try:
            await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.UPLOAD_VIDEO)
            max_h = 720 if is_regular_youtube_url(url) else None
            video_data = await download_video(url, max_height=max_h)
            full_caption = video_data.caption or build_caption(url, video_data.title, video_data.author)
            display_caption = (
                full_caption
                if cfg.show_caption
                else f'<a href="{html.escape(url, quote=True)}">Original</a>'
            ) + sender_note

            if video_data.is_slideshow and video_data.image_paths:
                if status_msg:
                    await status_msg.edit_text(
                        f"{prefix}<b>Sending photo slideshow ({len(video_data.image_paths)} photos)...</b>",
                        parse_mode=ParseMode.HTML,
                    )
                await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.UPLOAD_PHOTO)
                for chunk_start in range(0, len(video_data.image_paths), 10):
                    chunk = video_data.image_paths[chunk_start : chunk_start + 10]
                    media_group = [
                        InputMediaPhoto(
                            media=FSInputFile(img_path),
                            caption=display_caption if (chunk_start == 0 and idx == 0) else None,
                            parse_mode=ParseMode.HTML if (chunk_start == 0 and idx == 0) else None,
                        )
                        for idx, img_path in enumerate(chunk)
                    ]
                    await message.reply_media_group(media=media_group)

                if video_data.audio_path and video_data.audio_path.exists():
                    sent_audio = await message.reply_audio(
                        audio=FSInputFile(video_data.audio_path),
                        title=video_data.title[:100],
                        performer=video_data.author[:80] or None,
                    )
                    if sent_audio.audio:
                        storage.save_audio_file_id(url, sent_audio.audio.file_id)
            elif len(video_data.video_paths) > 1 or (video_data.video_paths and video_data.image_paths):
                if status_msg:
                    await status_msg.edit_text(
                        f"{prefix}<b>Sending media album...</b>",
                        parse_mode=ParseMode.HTML,
                    )
                await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.UPLOAD_VIDEO)
                combined_items: list[tuple[str, Path]] = (
                    [("video", p) for p in video_data.video_paths]
                    + [("photo", p) for p in video_data.image_paths]
                )
                for chunk_start in range(0, len(combined_items), 10):
                    chunk = combined_items[chunk_start : chunk_start + 10]
                    media_group = []
                    for idx, (kind, media_path) in enumerate(chunk):
                        cap = display_caption if (chunk_start == 0 and idx == 0) else None
                        pm = ParseMode.HTML if (chunk_start == 0 and idx == 0) else None
                        if kind == "video":
                            media_group.append(
                                InputMediaVideo(
                                    media=FSInputFile(media_path),
                                    caption=cap,
                                    parse_mode=pm,
                                    supports_streaming=True,
                                )
                            )
                        else:
                            media_group.append(
                                InputMediaPhoto(
                                    media=FSInputFile(media_path),
                                    caption=cap,
                                    parse_mode=pm,
                                )
                            )
                    sent_group = await message.reply_media_group(media=media_group)
                    if chunk_start == 0 and sent_group and sent_group[0].video:
                        storage.save_media(
                            url,
                            sent_group[0].video.file_id,
                            caption=full_caption,
                            title=video_data.title,
                            author=video_data.author,
                        )
            elif video_data.file_path and video_data.file_path.exists():
                if is_group and cfg.delete_source_msg and allow_delete_source:
                    sent_msg = await message.answer_video(
                        video=FSInputFile(video_data.file_path),
                        caption=display_caption,
                        parse_mode=ParseMode.HTML,
                        reply_markup=_video_keyboard(result_id, url, show_actions=cfg.show_mp3_button),
                        width=video_data.width,
                        height=video_data.height,
                        duration=video_data.duration,
                        supports_streaming=True,
                        message_thread_id=message.message_thread_id,
                    )
                    asyncio.create_task(_delete_msg_quietly(message))
                else:
                    sent_msg = await message.reply_video(
                        video=FSInputFile(video_data.file_path),
                        caption=display_caption,
                        parse_mode=ParseMode.HTML,
                        reply_markup=_video_keyboard(result_id, url, show_actions=cfg.show_mp3_button),
                        width=video_data.width,
                        height=video_data.height,
                        duration=video_data.duration,
                        supports_streaming=True,
                    )

                if sent_msg.video:
                    storage.save_media(
                        url,
                        sent_msg.video.file_id,
                        caption=full_caption,
                        title=video_data.title,
                        author=video_data.author,
                    )
            else:
                raise DownloadError("Media file was not found after download.")

            if status_msg:
                asyncio.create_task(_delete_msg_quietly(status_msg))

        except DownloadError as exc:
            logger.warning("Download error in chat %s (%s): %s", message.chat.id, url, exc)
            if status_msg:
                try:
                    await status_msg.edit_text(
                        f"{prefix}<b>Failed to download video from {platform}.</b>\nReason: {html.escape(str(exc))}",
                        parse_mode=ParseMode.HTML,
                    )
                except TelegramBadRequest:
                    pass
        except Exception as exc:
            logger.exception("Error sending video in chat %s: %s", message.chat.id, exc)
            if status_msg:
                try:
                    await status_msg.delete()
                except TelegramBadRequest:
                    pass
        finally:
            if video_data:
                video_data.cleanup()


@dp.message(F.text | F.caption)
async def handle_message_with_link(message: Message, bot: Bot) -> None:
    if not storage.get_storage_chat_id():
        storage.set_fallback_storage_chat_id(message.chat.id)
    elif message.chat.type == ChatType.PRIVATE:
        storage.set_fallback_storage_chat_id(message.chat.id)

    if message.via_bot:
        return

    text_parts: list[str] = []
    if message.text:
        text_parts.append(message.text)
    if message.caption:
        text_parts.append(message.caption)
    for ent in (message.entities or []) + (message.caption_entities or []):
        if getattr(ent, "url", None):
            text_parts.append(ent.url)
    text = "\n".join(text_parts)

    is_private_or_mentioned = (
        message.chat.type == ChatType.PRIVATE
        or f"@{_BOT_USERNAME.lower()}" in text.lower()
    )
    urls = extract_all_video_urls(text, max_urls=5, allow_any_http=is_private_or_mentioned)

    if not urls:
        if message.chat.type == ChatType.PRIVATE and not text.startswith("/"):
            await message.answer(
                "Send me a video link from TikTok, Instagram, YouTube, Twitter (X), Reddit, Threads, Facebook, Pinterest, or Twitch.",
                reply_markup=_main_menu_keyboard(_BOT_USERNAME),
            )
        return

    total = len(urls)
    for idx, url in enumerate(urls, start=1):
        prefix = f"[{idx}/{total}] " if total > 1 else ""
        is_last = idx == total
        await _process_single_url_in_chat(
            message,
            bot,
            url,
            prefix=prefix,
            allow_delete_source=(total == 1 or is_last),
        )


async def _setup_bot_commands(bot: Bot) -> None:
    commands = [
        BotCommand(command="start", description="Main menu and quick actions"),
        BotCommand(command="settings", description="Chat video display settings"),
        BotCommand(command="platforms", description="Supported platforms and formats"),
        BotCommand(command="stats", description="Bot statistics and status"),
        BotCommand(command="help", description="Detailed usage guide"),
        BotCommand(command="update", description="Update yt-dlp module"),
    ]
    await bot.set_my_commands(commands)


async def _maintenance_loop() -> None:
    hours_counter = 0
    while True:
        await asyncio.sleep(3600)
        removed = cleanup_old_downloads()
        if removed:
            logger.info("Cleaned up %d stale temporary files from downloads/", removed)
        hours_counter += 1
        if hours_counter >= 24:
            hours_counter = 0
            logger.info("Running scheduled 24h yt-dlp update...")
            success, _ = await update_ytdlp_package()
            logger.info("Scheduled yt-dlp update result: %s", success)


async def main() -> None:
    global _BOT_USERNAME, _CAN_READ_ALL_GROUP_MESSAGES
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is missing! Create tg_bot/.env and set BOT_TOKEN=your_token")
        sys.exit(1)

    cleanup_old_downloads()
    bot = Bot(token=BOT_TOKEN)
    me = await bot.get_me()
    _BOT_USERNAME = me.username or "bot"
    _CAN_READ_ALL_GROUP_MESSAGES = bool(me.can_read_all_group_messages)
    await _setup_bot_commands(bot)
    logger.info(
        "Bot @%s started successfully! (can_read_all_group_messages=%s)",
        _BOT_USERNAME,
        _CAN_READ_ALL_GROUP_MESSAGES,
    )
    asyncio.create_task(_maintenance_loop())
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
