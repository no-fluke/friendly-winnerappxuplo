"""
uploader.py — Upload files to Telegram supergroup (forum topics) via Pyrogram.
Bot must be admin in the supergroup with 'manage_topics' permission.
Uses MTProto (Pyrogram) so files up to 2 GB are supported — no 50 MB limit.
"""

import os
import logging
from pyrogram import Client
from pyrogram.errors import FloodWait, RPCError
import asyncio

from db import db
from vars import SUPERGROUP_ID

log = logging.getLogger(__name__)

# In-process topic cache (MongoDB is the durable store)
_topic_cache: dict[str, int] = {}


async def get_or_create_topic(app: Client, job_id: str, topic_name: str) -> int:
    """Return forum thread_id for topic_name; create if not exists."""
    cache_key = f"{job_id}::{topic_name}"
    if cache_key in _topic_cache:
        return _topic_cache[cache_key]

    stored = db.get_topic_id(job_id, topic_name)
    if stored:
        _topic_cache[cache_key] = stored
        return stored

    # Create via Pyrogram
    try:
        topic = await app.create_forum_topic(SUPERGROUP_ID, topic_name[:128])
        topic_id = topic.id
        _topic_cache[cache_key] = topic_id
        db.set_topic_id(job_id, topic_name, topic_id)
        log.info("Created topic '%s' → thread_id=%d", topic_name, topic_id)
        return topic_id
    except RPCError as e:
        log.error("create_forum_topic error: %s", e)
        return None  # fall back to no topic (General)


async def upload_file(
    app: Client,
    job_id: str,
    topic_name: str | None,
    file_path: str,
    caption: str = "",
    progress_msg=None,  # Pyrogram Message to edit with progress
) -> bool:
    """
    Upload file_path to the supergroup. If topic_name is set, uploads to
    that forum topic. Returns True on success.
    """
    if not os.path.exists(file_path):
        log.error("File not found: %s", file_path)
        return False

    ext     = os.path.splitext(file_path)[1].lower()
    fname   = os.path.basename(file_path)
    size_mb = os.path.getsize(file_path) / (1024 * 1024)

    # Resolve topic
    thread_id = None
    if topic_name:
        thread_id = await get_or_create_topic(app, job_id, topic_name)

    kw = {}
    if thread_id:
        kw["message_thread_id"] = thread_id

    cap = (caption or fname)[:1024]

    # Progress callback
    last_pct = [0]
    async def _progress(current, total):
        if total and progress_msg:
            pct = int(current / total * 100)
            if pct - last_pct[0] >= 20:
                last_pct[0] = pct
                try:
                    await progress_msg.edit_text(
                        f"⬆️ Uploading `{fname[:40]}`\n"
                        f"Progress: **{pct}%** ({current/1024/1024:.1f}/{total/1024/1024:.1f} MB)"
                    )
                except Exception:
                    pass

    is_video = ext in (".mp4", ".mkv", ".webm", ".ts", ".m4v", ".avi", ".mov")

    try:
        if is_video:
            await app.send_video(
                chat_id=SUPERGROUP_ID,
                video=file_path,
                caption=cap,
                supports_streaming=True,
                progress=_progress,
                **kw,
            )
        elif ext == ".pdf":
            await app.send_document(
                chat_id=SUPERGROUP_ID,
                document=file_path,
                caption=cap,
                progress=_progress,
                **kw,
            )
        elif ext in (".jpg", ".jpeg", ".png", ".webp"):
            await app.send_photo(
                chat_id=SUPERGROUP_ID,
                photo=file_path,
                caption=cap,
                **kw,
            )
        elif ext in (".mp3", ".wav", ".m4a", ".ogg"):
            await app.send_audio(
                chat_id=SUPERGROUP_ID,
                audio=file_path,
                caption=cap,
                progress=_progress,
                **kw,
            )
        else:
            await app.send_document(
                chat_id=SUPERGROUP_ID,
                document=file_path,
                caption=cap,
                progress=_progress,
                **kw,
            )
        log.info("Uploaded %s (%.1f MB) → topic=%s", fname, size_mb, topic_name)
        return True

    except FloodWait as e:
        log.warning("FloodWait %ds", e.value)
        await asyncio.sleep(e.value)
        return await upload_file(app, job_id, topic_name, file_path, caption, progress_msg)
    except RPCError as e:
        log.error("Upload RPCError for %s: %s", fname, e)
        return False
    except Exception as e:
        log.error("Upload error for %s: %s", fname, e)
        return False


async def send_text_to_topic(
    app: Client, job_id: str, topic_name: str | None, text: str
) -> bool:
    """Send a plain text message into a forum topic."""
    thread_id = None
    if topic_name:
        thread_id = await get_or_create_topic(app, job_id, topic_name)
    kw = {}
    if thread_id:
        kw["message_thread_id"] = thread_id
    try:
        await app.send_message(SUPERGROUP_ID, text, **kw)
        return True
    except Exception as e:
        log.error("send_text_to_topic: %s", e)
        return False
