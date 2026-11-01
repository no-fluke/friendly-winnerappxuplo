"""
bot.py — AppX Downloader Bot
Architecture: Pyrogram (api_id + api_hash + bot_token)
              pyromod for interactive listen()
              aria2c 16 connections for downloads
              MongoDB for job persistence + resume-on-restart
              Dynamic channel ID — user provides it during /appx session
              Bot must be admin in the target channel/supergroup

Commands:
  /start   — welcome
  /appx    — start an AppX course download session
  /stop    — cancel current session
  /status  — show active jobs
  /id      — show your Telegram ID (also works in channels/groups)
  /addauth — (owner) add authorized user
  /rmauth  — (owner) remove authorized user
  /users   — (owner) list authorized users
"""

import os
import re
import uuid
import asyncio
import logging
import traceback
from datetime import datetime

from pyrogram import Client, filters, idle
from pyrogram.types import Message
import pyromod  # patches Client with .listen()

from vars import API_ID, API_HASH, BOT_TOKEN, OWNER_ID, SUPERGROUP_ID, CREDIT, DOWNLOAD_DIR
from db import db
import appx_api as api
from downloader import download
from uploader import upload_file, send_text_to_topic
from utils import extract_topic_name, clean_title, make_progress_text, remove_file, ensure_dir
from auth import register_auth_handlers

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

# ── Pyrogram client ───────────────────────────────────────────────────────────
app = Client(
    "appx_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

# ── Per-user state ────────────────────────────────────────────────────────────
_cancel: dict[int, bool] = {}
_active: dict[int, bool] = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_auth(user_id: int) -> bool:
    return user_id == OWNER_ID or db.is_authorized(user_id)


async def ask(chat_id: int, text: str, timeout: int = 60) -> str | None:
    """Send a message then wait for a reply. Returns reply text or None on timeout."""
    sent = await app.send_message(chat_id, text)
    try:
        reply: Message = await app.listen(chat_id, timeout=timeout)
        await reply.delete()
        await sent.delete()
        return reply.text.strip() if reply.text else None
    except asyncio.TimeoutError:
        await sent.edit_text("⌛ Timed out. Please restart with /appx.")
        return None


# ── /start ────────────────────────────────────────────────────────────────────

@app.on_message(filters.command("start") & filters.private)
async def cmd_start(_, m: Message):
    uid = m.from_user.id if m.from_user else 0
    auth_status = "✅ Authorized" if is_auth(uid) else "❌ Not authorized"
    await m.reply_text(
        f"**👋 AppX Downloader Bot**\n\n"
        f"Download full AppX / Parmar Academy courses and upload them "
        f"topic-wise to your Telegram channel or supergroup.\n\n"
        f"**Your status:** {auth_status}\n\n"
        f"Use /appx to start a download session.\n"
        f"Bot by **{CREDIT}**",
    )


# ── /stop ─────────────────────────────────────────────────────────────────────

@app.on_message(filters.command("stop") & filters.private)
async def cmd_stop(_, m: Message):
    uid = m.from_user.id if m.from_user else 0
    if _active.get(uid):
        _cancel[uid] = True
        for job in db.get_pending_jobs():
            if job.get("user_id") == uid:
                db.delete_job(job["job_id"])
        await m.reply_text("🛑 **Stop requested.** Job removed from DB — won't resume on restart.")
    else:
        await m.reply_text("No active session to stop.")


# ── /status ───────────────────────────────────────────────────────────────────

@app.on_message(filters.command("status") & filters.private)
async def cmd_status(_, m: Message):
    uid = m.from_user.id if m.from_user else 0
    if not is_auth(uid):
        await m.reply_text("⛔ Not authorized.")
        return
    jobs = db.get_pending_jobs()
    if not jobs:
        await m.reply_text("No active jobs.")
        return
    lines = []
    for j in jobs:
        done  = sum(1 for v in j.get("progress", {}).values() if v == "done")
        total = j.get("total_items", "?")
        lines.append(f"• **{j.get('batch_name', j['job_id'])}** — {done}/{total}")
    await m.reply_text("**Active jobs:**\n" + "\n".join(lines))


# ── /id ───────────────────────────────────────────────────────────────────────

@app.on_message(filters.command("id"))
async def cmd_id(_, m: Message):
    uid = m.from_user.id if m.from_user else "unknown"
    cid = m.chat.id
    await m.reply_text(
        f"👤 **Your ID:** `{uid}`\n"
        f"💬 **Chat ID:** `{cid}`"
    )


# ── /appx ─────────────────────────────────────────────────────────────────────

@app.on_message(filters.command("appx") & filters.private)
async def cmd_appx(_, m: Message):
    uid = m.from_user.id if m.from_user else 0
    if not is_auth(uid):
        await m.reply_text("⛔ Not authorized. Ask the owner to add you with /addauth.")
        return
    if _active.get(uid):
        await m.reply_text("⚠️ You already have an active session. Send /stop first.")
        return

    chat_id = m.chat.id

    # ── Step 1: API domain ────────────────────────────────────────────────────
    domain_raw = await ask(
        chat_id,
        "**Step 1/7 — AppX Domain**\n\n"
        "Send the API domain of your AppX platform.\n"
        "Example: `parmaracademyapi.teachx.in`",
        timeout=60,
    )
    if not domain_raw:
        return
    domain = domain_raw.strip().lstrip("https://").rstrip("/")

    # ── Step 2: Login method ─────────────────────────────────────────────────
    login_choice = await ask(
        chat_id,
        "**Step 2/7 — Login Method**\n\n"
        "Send:\n`1` → I already have a JWT token\n`2` → Login with email + password",
        timeout=60,
    )
    if not login_choice:
        return

    if login_choice == "1":
        token_text = await ask(chat_id, "**Send your JWT token:**", timeout=120)
        if not token_text:
            return
        try:
            profile = api.login_with_token(domain, token_text)
        except Exception as e:
            await m.reply_text(f"❌ Error: {e}")
            return
    else:
        email = await ask(chat_id, "**Send your email:**", timeout=60)
        if not email:
            return
        password = await ask(chat_id, "**Send your password:**", timeout=60)
        if not password:
            return
        try:
            profile = api.login_with_credentials(domain, email, password)
        except Exception as e:
            await m.reply_text(f"❌ Login failed: {e}")
            return

    token = profile["token"]
    await m.reply_text(f"✅ Logged in as **{profile.get('name', 'User')}**")

    # ── Step 3: Choose course ─────────────────────────────────────────────────
    status_msg = await m.reply_text("⌛ Fetching courses...")
    try:
        courses = api.get_courses(domain, token)
    except Exception as e:
        await status_msg.edit(f"❌ Failed to fetch courses: {e}")
        return

    if not courses:
        await status_msg.edit("❌ No courses found.")
        return

    course_list = "\n".join(
        f"`{i+1}` — {c.get('courseName', c.get('name', f'Course {i+1}'))}"
        for i, c in enumerate(courses)
    )
    await status_msg.edit(f"**Available Courses:**\n{course_list}")

    course_idx_raw = await ask(chat_id, "Send course number:", timeout=60)
    if not course_idx_raw or not course_idx_raw.isdigit():
        await m.reply_text("❌ Invalid selection.")
        return
    course_idx = int(course_idx_raw) - 1
    if course_idx < 0 or course_idx >= len(courses):
        await m.reply_text("❌ Out of range.")
        return
    course      = courses[course_idx]
    course_id   = str(course.get("id") or course.get("courseId") or course.get("_id", ""))
    course_name = course.get("courseName") or course.get("name", f"Course {course_idx+1}")

    # ── Step 4: Choose subjects ───────────────────────────────────────────────
    loading = await m.reply_text("⌛ Fetching subjects...")
    try:
        subjects = api.get_subjects(domain, token, course_id)
    except Exception as e:
        await loading.edit(f"❌ Failed: {e}")
        return
    await loading.delete()

    if not subjects:
        await m.reply_text("❌ No subjects found for this course.")
        return

    subj_list = "\n".join(
        f"`{i+1}` — {s.get('subjectName', s.get('name', f'Subject {i+1}'))}"
        for i, s in enumerate(subjects)
    )
    subj_choice = await ask(
        chat_id,
        f"**Subjects in {course_name}:**\n{subj_list}\n\n"
        "Send number(s) separated by commas, or `all`:",
        timeout=60,
    )
    if not subj_choice:
        return

    if subj_choice.lower() == "all":
        sel_subjects = subjects
    else:
        idxs = [int(x.strip()) - 1 for x in subj_choice.split(",") if x.strip().isdigit()]
        sel_subjects = [subjects[i] for i in idxs if 0 <= i < len(subjects)]
    if not sel_subjects:
        await m.reply_text("❌ No valid subjects selected.")
        return

    # ── Step 5: Batch name ────────────────────────────────────────────────────
    b_name = await ask(
        chat_id,
        "**Step 5/7 — Batch Name**\n\nEnter a name for this batch (shown in captions):",
        timeout=60,
    )
    if not b_name:
        b_name = course_name

    # ── Step 6: Quality + topic-wise ─────────────────────────────────────────
    quality_raw = await ask(
        chat_id,
        "**Step 6/7 — Video Quality**\n\n"
        "Send resolution: `144` / `240` / `360` / `480` / `720` / `1080`\n"
        "Or /d for default (720):",
        timeout=60,
    )
    quality = 720
    if quality_raw and quality_raw != "/d" and quality_raw.isdigit():
        quality = int(quality_raw)

    topic_wise_raw = await ask(
        chat_id,
        "**Topic-wise upload?**\n\n"
        "Send `yes` to create forum topics from content topic names.\n"
        "Send `no` to upload everything to General.",
        timeout=60,
    )
    topic_wise = topic_wise_raw and topic_wise_raw.lower() in ("yes", "y", "1")

    # ── Step 7: Target channel/group ─────────────────────────────────────────
    default_hint = f"\n\nDefault (press /d): your current chat (`{chat_id}`)"
    if SUPERGROUP_ID:
        default_hint = f"\n\nDefault (press /d): `{SUPERGROUP_ID}` (from env)"

    channel_raw = await ask(
        chat_id,
        "**Step 7/7 — Target Channel / Group**\n\n"
        "<blockquote>"
        "🔹 Make me an admin in the target channel/group.\n"
        "🔸 Send /id inside that chat to get its ID.\n\n"
        "Example: `-100XXXXXXXXXXX`"
        "</blockquote>"
        f"{default_hint}",
        timeout=60,
    )

    if not channel_raw or channel_raw.strip() == "/d":
        # Use env SUPERGROUP_ID if set, else fall back to current chat
        target_channel = SUPERGROUP_ID if SUPERGROUP_ID else chat_id
    else:
        raw = channel_raw.strip()
        if raw.lstrip("-").isdigit():
            target_channel = int(raw)
        else:
            await m.reply_text("❌ Invalid channel ID. Must be a number like `-100XXXXXXXXXXX`.")
            return

    # Confirm the target
    try:
        chat_info = await app.get_chat(target_channel)
        chat_title = chat_info.title or str(target_channel)
    except Exception:
        chat_title = str(target_channel)

    await m.reply_text(
        f"✅ **Target set:** `{chat_title}` (`{target_channel}`)\n\n"
        f"⌛ Building content list..."
    )

    # ── Build content list ────────────────────────────────────────────────────
    items_all = []

    for subj in sel_subjects:
        subj_id   = str(subj.get("id") or subj.get("subjectId") or subj.get("_id", ""))
        subj_name = subj.get("subjectName") or subj.get("name", "")
        try:
            topics = api.get_topics(domain, token, course_id, subj_id)
        except Exception as e:
            log.warning("get_topics failed for subj %s: %s", subj_id, e)
            continue

        for topic in topics:
            t_id   = str(topic.get("id") or topic.get("topicId") or topic.get("_id", ""))
            t_name = topic.get("topicName") or topic.get("name", "Topic")
            try:
                lessons = api.get_lessons(domain, token, course_id, subj_id, t_id)
            except Exception as e:
                log.warning("get_lessons failed for topic %s: %s", t_id, e)
                continue
            for lesson in lessons:
                lesson["_topic_name"]   = t_name
                lesson["_subject_name"] = subj_name
                items_all.append(lesson)

    if not items_all:
        await m.reply_text("❌ No content found.")
        return

    total = len(items_all)
    await m.reply_text(
        f"📋 **Content summary:**\n"
        f"• Total items: **{total}**\n"
        f"• Batch: **{b_name}**\n"
        f"• Quality: **{quality}p**\n"
        f"• Topic-wise: **{'Yes' if topic_wise else 'No'}**\n"
        f"• Uploading to: **{chat_title}**\n\n"
        f"⌛ Starting download + upload..."
    )

    # ── Create / save job in MongoDB ──────────────────────────────────────────
    job_id = str(uuid.uuid4())[:8]
    job = {
        "job_id":         job_id,
        "user_id":        uid,
        "chat_id":        chat_id,
        "target_channel": target_channel,    # ← saved so resume works
        "batch_name":     b_name,
        "course_name":    course_name,
        "total_items":    total,
        "status":         "running",
        "quality":        quality,
        "topic_wise":     topic_wise,
        "progress":       {},
        "created_at":     datetime.utcnow(),
        "domain":         domain,
        "token":          token,
        "items":          items_all,
    }
    db.save_job(job)

    # ── Process items ─────────────────────────────────────────────────────────
    _active[uid] = True
    _cancel[uid] = False
    ensure_dir(DOWNLOAD_DIR)

    failed = 0
    count  = 0

    for idx, lesson in enumerate(items_all):
        if _cancel.get(uid):
            await m.reply_text("🛑 **Stopped by user.**")
            break

        if job.get("progress", {}).get(str(idx)) == "done":
            count += 1
            continue

        raw_title = (
            lesson.get("title")
            or lesson.get("videoTitle")
            or lesson.get("name")
            or f"Item {idx+1}"
        )
        topic_name_from_api = lesson.get("_topic_name", "General")
        url = api.resolve_video_url(lesson)

        if topic_wise:
            t_name_bracket, vid_title = extract_topic_name(raw_title)
            topic_label    = t_name_bracket if t_name_bracket != "General" else topic_name_from_api
            vid_title_clean = clean_title(vid_title)
        else:
            topic_label     = None
            vid_title_clean = clean_title(raw_title)

        try:
            prog_msg = await app.send_message(
                chat_id,
                make_progress_text(count + 1, total, vid_title_clean, b_name, topic_label, CREDIT),
            )
        except Exception:
            prog_msg = None

        # ── Download ──────────────────────────────────────────────────────────
        local_file = None
        try:
            if url:
                local_file = await download(url, vid_title_clean, DOWNLOAD_DIR, quality)
            if not local_file:
                raise FileNotFoundError(f"Download returned None for URL: {url}")
        except Exception as e:
            log.error("Download failed item %d: %s", idx, e)
            failed += 1
            if prog_msg:
                await prog_msg.edit_text(
                    f"⚠️ **Download failed** ({count+1}/{total})\n"
                    f"`{vid_title_clean}`\n`{e}`"
                )
            db.mark_item_done(job_id, idx)
            count += 1
            continue

        # ── Build caption ─────────────────────────────────────────────────────
        caption = (
            f"[📁] `{count+1:03d}` **{vid_title_clean}**\n"
            f"<blockquote><b>Batch: {b_name}"
            + (f"\nTopic: {topic_label}" if topic_label else "")
            + f"</b></blockquote>\n\n**by {CREDIT}**"
        )

        # ── Upload ────────────────────────────────────────────────────────────
        ok = await upload_file(
            app, job_id, target_channel, topic_label, local_file, caption, prog_msg
        )
        remove_file(local_file)

        if ok:
            db.mark_item_done(job_id, idx)
        else:
            failed += 1
            if prog_msg:
                await prog_msg.edit_text(f"⚠️ Upload failed for `{vid_title_clean}`")

        if prog_msg:
            try:
                await prog_msg.delete()
            except Exception:
                pass

        count += 1
        await asyncio.sleep(0.5)

    # ── Done ─────────────────────────────────────────────────────────────────
    _active[uid] = False
    stopped_early = _cancel.get(uid, False)
    _cancel[uid]  = False

    if stopped_early:
        db.delete_job(job_id)
        await m.reply_text(
            f"🛑 **Session stopped.**\n"
            f"Uploaded **{count - failed}** items before stopping.\n\n"
            f"**{CREDIT}**"
        )
    else:
        db.delete_job(job_id)
        success = total - failed
        final = (
            f"**✅ Batch Complete!**\n"
            f"<blockquote><b>🎯 Batch: {b_name}</b></blockquote>\n"
            f"📊 Total: {total} | ✅ OK: {success} | ❌ Failed: {failed}\n\n"
            f"**{CREDIT}**"
        )
        await m.reply_text(final)
        try:
            await send_text_to_topic(app, target_channel, job_id, None, final)
        except Exception:
            pass


# ── Resume pending jobs on startup ────────────────────────────────────────────

async def _run_job(job: dict):
    """Execute (or resume) a saved job on startup after a dyno restart."""
    uid            = job["user_id"]
    chat_id        = job.get("chat_id", OWNER_ID)
    job_id         = job["job_id"]
    b_name         = job["batch_name"]
    quality        = job.get("quality", 720)
    topic_wise     = job.get("topic_wise", False)
    items_all      = job.get("items", [])
    total          = len(items_all)
    # Use the saved channel; fall back to SUPERGROUP_ID or the original chat
    target_channel = job.get("target_channel") or SUPERGROUP_ID or chat_id

    if not items_all:
        db.delete_job(job_id)
        return

    _active[uid] = True
    _cancel[uid] = False
    ensure_dir(DOWNLOAD_DIR)

    try:
        await app.send_message(
            chat_id,
            f"🔄 **Resuming job after restart**\n"
            f"Batch: **{b_name}** — picking up from where we left off...",
        )
    except Exception:
        pass

    db.update_job_status(job_id, "running")
    failed = 0
    count  = 0

    for idx, lesson in enumerate(items_all):
        if _cancel.get(uid):
            break

        if job.get("progress", {}).get(str(idx)) == "done":
            count += 1
            continue

        raw_title = (
            lesson.get("title") or lesson.get("videoTitle")
            or lesson.get("name") or f"Item {idx+1}"
        )
        url = api.resolve_video_url(lesson)

        if topic_wise:
            t_name_bracket, vid_title = extract_topic_name(raw_title)
            topic_label    = t_name_bracket if t_name_bracket != "General" else lesson.get("_topic_name", "General")
            vid_title_clean = clean_title(vid_title)
        else:
            topic_label     = None
            vid_title_clean = clean_title(raw_title)

        try:
            prog_msg = await app.send_message(
                chat_id,
                make_progress_text(count + 1, total, vid_title_clean, b_name, topic_label, CREDIT),
            )
        except Exception:
            prog_msg = None

        local_file = None
        try:
            if url:
                local_file = await download(url, vid_title_clean, DOWNLOAD_DIR, quality)
            if not local_file:
                raise FileNotFoundError(f"Download returned None for: {url}")
        except Exception as e:
            log.error("Resume download failed item %d: %s", idx, e)
            failed += 1
            if prog_msg:
                await prog_msg.edit_text(
                    f"⚠️ Download failed ({count+1}/{total})\n`{vid_title_clean}`\n`{e}`"
                )
            db.mark_item_done(job_id, idx)
            count += 1
            continue

        caption = (
            f"[📁] `{count+1:03d}` **{vid_title_clean}**\n"
            f"<blockquote><b>Batch: {b_name}"
            + (f"\nTopic: {topic_label}" if topic_label else "")
            + f"</b></blockquote>\n\n**by {CREDIT}**"
        )

        ok = await upload_file(app, job_id, target_channel, topic_label, local_file, caption, prog_msg)
        remove_file(local_file)

        if ok:
            db.mark_item_done(job_id, idx)
        else:
            failed += 1

        if prog_msg:
            try:
                await prog_msg.delete()
            except Exception:
                pass

        count += 1
        await asyncio.sleep(0.5)

    _active[uid] = False
    stopped_early = _cancel.get(uid, False)
    _cancel[uid]  = False
    db.delete_job(job_id)

    msg = (
        f"🛑 Stopped. Uploaded {count - failed} items. Job removed."
        if stopped_early
        else (
            f"**✅ Batch Complete!**\n"
            f"<blockquote><b>🎯 Batch: {b_name}</b></blockquote>\n"
            f"📊 Total: {total} | ✅ OK: {total - failed} | ❌ Failed: {failed}\n\n**{CREDIT}**"
        )
    )
    try:
        await app.send_message(chat_id, msg)
    except Exception:
        pass


async def resume_pending_jobs():
    """On startup: re-run any jobs interrupted by a dyno restart."""
    pending = db.get_pending_jobs()
    if not pending:
        return
    log.info("Resuming %d interrupted job(s)...", len(pending))
    for job in pending:
        fresh = db.get_job(job["job_id"])
        if fresh:
            asyncio.ensure_future(_run_job(fresh))


# ── Main ─────────────────────────────────────────────────────────────────────

async def main():
    ensure_dir(DOWNLOAD_DIR)

    await app.start()
    register_auth_handlers(app)

    log.info("AppX Bot started. Owner: %d", OWNER_ID)

    try:
        await app.send_message(OWNER_ID, "🤖 **AppX Bot started!** Use /appx to begin.")
    except Exception:
        pass

    await resume_pending_jobs()
    await idle()          # ← correct Pyrogram way to keep bot running
    await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
