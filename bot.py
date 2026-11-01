"""
bot.py — AppX Downloader Bot (pure Pyrogram, no pyromod)
"""
import os
import uuid
import asyncio
import logging
from datetime import datetime

from pyrogram import Client, filters, idle
from pyrogram.types import Message

from vars import API_ID, API_HASH, BOT_TOKEN, OWNER_ID, SUPERGROUP_ID, CREDIT, DOWNLOAD_DIR
from db import database as db
import appx_api as api
from downloader import download
from uploader import upload_file, send_text_to_topic
from utils import extract_topic_name, clean_title, make_progress_text, remove_file, ensure_dir
from auth import register_auth_handlers

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger(__name__)

app = Client("appx_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

_cancel: dict[int, bool] = {}
_active: dict[int, bool] = {}

# ── Pure Pyrogram listener (replaces pyromod) ─────────────────────────────────
_listeners: dict[int, asyncio.Future] = {}

@app.on_message(filters.private & ~filters.command(
    ["start", "stop", "status", "appx", "id", "addauth", "rmauth", "users", "plan"]
))
async def _resolve_listener(client: Client, m: Message):
    chat_id = m.chat.id
    if chat_id in _listeners and not _listeners[chat_id].done():
        _listeners[chat_id].set_result(m)

async def _ask(chat_id: int, timeout: int = 60) -> Message:
    """Wait for the next message from a chat."""
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    _listeners[chat_id] = fut
    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        raise asyncio.TimeoutError
    finally:
        _listeners.pop(chat_id, None)

# ─────────────────────────────────────────────────────────────────────────────

def is_auth(user_id: int) -> bool:
    return user_id == OWNER_ID or db.is_authorized(user_id)


@app.on_message(filters.command("start") & filters.private)
async def cmd_start(client: Client, m: Message):
    uid = m.from_user.id if m.from_user else 0
    auth_status = "✅ Authorized" if is_auth(uid) else "❌ Not authorized"
    await m.reply_text(
        f"**👋 AppX Downloader Bot**\n\n"
        f"Download full AppX courses and upload them to your Telegram channel.\n\n"
        f"**Your status:** {auth_status}\n\nUse /appx to start.\nBot by **{CREDIT}**"
    )


@app.on_message(filters.command("id"))
async def cmd_id(client: Client, m: Message):
    uid = m.from_user.id if m.from_user else "unknown"
    await m.reply_text(f"👤 **Your ID:** `{uid}`\n💬 **Chat ID:** `{m.chat.id}`")


@app.on_message(filters.command("stop") & filters.private)
async def cmd_stop(client: Client, m: Message):
    uid = m.from_user.id if m.from_user else 0
    if _active.get(uid):
        _cancel[uid] = True
        for job in db.get_pending_jobs():
            if job.get("user_id") == uid:
                db.delete_job(job["job_id"])
        await m.reply_text("🛑 **Stop requested.** Job removed.")
    else:
        await m.reply_text("No active session to stop.")


@app.on_message(filters.command("status") & filters.private)
async def cmd_status(client: Client, m: Message):
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


@app.on_message(filters.command("appx") & filters.private)
async def cmd_appx(client: Client, m: Message):
    uid = m.from_user.id if m.from_user else 0
    if not is_auth(uid):
        await m.reply_text("⛔ Not authorized. Ask the owner to add you with /addauth.")
        return
    if _active.get(uid):
        await m.reply_text("⚠️ You already have an active session. Send /stop first.")
        return

    chat_id = m.chat.id

    # Step 1: Domain
    editable = await m.reply_text(
        "**Step 1/7 — AppX Domain**\n\nSend the API domain.\nExample: `parmaracademyapi.teachx.in`"
    )
    try:
        reply = await _ask(chat_id, timeout=60)
        await reply.delete(); await editable.delete()
        domain_raw = reply.text.strip() if reply.text else None
    except asyncio.TimeoutError:
        await editable.edit_text("⌛ Timed out. Restart with /appx.")
        return
    if not domain_raw:
        await m.reply_text("❌ No domain received.")
        return
    domain = domain_raw.lstrip("https://").rstrip("/")

    # Step 2: Login
    editable = await m.reply_text(
        "**Step 2/7 — Login Method**\n\n`1` → JWT token\n`2` → Email + password"
    )
    try:
        reply = await _ask(chat_id, timeout=60)
        await reply.delete(); await editable.delete()
        login_choice = reply.text.strip() if reply.text else "2"
    except asyncio.TimeoutError:
        await editable.edit_text("⌛ Timed out.")
        return

    if login_choice == "1":
        editable = await m.reply_text("**Send your JWT token:**")
        try:
            reply = await _ask(chat_id, timeout=120)
            await reply.delete(); await editable.delete()
            token_text = reply.text.strip() if reply.text else None
        except asyncio.TimeoutError:
            await editable.edit_text("⌛ Timed out.")
            return
        if not token_text:
            await m.reply_text("❌ No token received.")
            return
        try:
            profile = api.login_with_token(domain, token_text)
        except Exception as e:
            await m.reply_text(f"❌ Error: {e}")
            return
    else:
        editable = await m.reply_text("**Send your email:**")
        try:
            reply = await _ask(chat_id, timeout=60)
            await reply.delete(); await editable.delete()
            email = reply.text.strip() if reply.text else None
        except asyncio.TimeoutError:
            await editable.edit_text("⌛ Timed out.")
            return

        editable = await m.reply_text("**Send your password:**")
        try:
            reply = await _ask(chat_id, timeout=60)
            await reply.delete(); await editable.delete()
            password = reply.text.strip() if reply.text else None
        except asyncio.TimeoutError:
            await editable.edit_text("⌛ Timed out.")
            return

        if not email or not password:
            await m.reply_text("❌ Email or password missing.")
            return
        try:
            profile = api.login_with_credentials(domain, email, password)
        except Exception as e:
            await m.reply_text(f"❌ Login failed: {e}")
            return

    token = profile["token"]
    await m.reply_text(f"✅ Logged in as **{profile.get('name', 'User')}**")

    # Step 3: Course
    status_msg = await m.reply_text("⌛ Fetching courses...")
    try:
        courses = api.get_courses(domain, token)
    except Exception as e:
        await status_msg.edit_text(f"❌ Failed: {e}")
        return
    if not courses:
        await status_msg.edit_text("❌ No courses found.")
        return

    course_list = "\n".join(
        f"`{i+1}` — {c.get('courseName', c.get('name', f'Course {i+1}'))}"
        for i, c in enumerate(courses)
    )
    await status_msg.edit_text(f"**Available Courses:**\n{course_list}")

    editable = await m.reply_text("Send course number:")
    try:
        reply = await _ask(chat_id, timeout=60)
        await reply.delete(); await editable.delete()
        course_idx_raw = reply.text.strip() if reply.text else None
    except asyncio.TimeoutError:
        await editable.edit_text("⌛ Timed out.")
        return

    if not course_idx_raw or not course_idx_raw.isdigit():
        await m.reply_text("❌ Invalid selection.")
        return
    course_idx = int(course_idx_raw) - 1
    if not (0 <= course_idx < len(courses)):
        await m.reply_text("❌ Out of range.")
        return
    course      = courses[course_idx]
    course_id   = str(course.get("id") or course.get("courseId") or course.get("_id", ""))
    course_name = course.get("courseName") or course.get("name", f"Course {course_idx+1}")

    # Step 4: Subjects
    loading = await m.reply_text("⌛ Fetching subjects...")
    try:
        subjects = api.get_subjects(domain, token, course_id)
    except Exception as e:
        await loading.edit_text(f"❌ Failed: {e}")
        return
    await loading.delete()
    if not subjects:
        await m.reply_text("❌ No subjects found.")
        return

    subj_list = "\n".join(
        f"`{i+1}` — {s.get('subjectName', s.get('name', f'Subject {i+1}'))}"
        for i, s in enumerate(subjects)
    )
    editable = await m.reply_text(
        f"**Subjects in {course_name}:**\n{subj_list}\n\nSend number(s) separated by commas, or `all`:"
    )
    try:
        reply = await _ask(chat_id, timeout=60)
        await reply.delete(); await editable.delete()
        subj_choice = reply.text.strip() if reply.text else None
    except asyncio.TimeoutError:
        await editable.edit_text("⌛ Timed out.")
        return

    if not subj_choice:
        await m.reply_text("❌ No selection received.")
        return
    if subj_choice.lower() == "all":
        sel_subjects = subjects
    else:
        idxs = [int(x.strip()) - 1 for x in subj_choice.split(",") if x.strip().isdigit()]
        sel_subjects = [subjects[i] for i in idxs if 0 <= i < len(subjects)]
    if not sel_subjects:
        await m.reply_text("❌ No valid subjects selected.")
        return

    # Step 5: Batch name
    editable = await m.reply_text("**Step 5/7 — Batch Name**\n\nEnter batch name or /d for default:")
    try:
        reply = await _ask(chat_id, timeout=60)
        await reply.delete(); await editable.delete()
        b_name_raw = reply.text.strip() if reply.text else "/d"
    except asyncio.TimeoutError:
        b_name_raw = "/d"
    b_name = course_name if b_name_raw == "/d" else b_name_raw

    # Step 6: Quality + topic-wise
    editable = await m.reply_text(
        "**Step 6/7 — Video Quality**\n\n`144` / `240` / `360` / `480` / `720` / `1080`\nor /d for 720:"
    )
    try:
        reply = await _ask(chat_id, timeout=60)
        await reply.delete(); await editable.delete()
        quality_raw = reply.text.strip() if reply.text else "/d"
    except asyncio.TimeoutError:
        quality_raw = "/d"
    quality = 720
    if quality_raw and quality_raw != "/d" and quality_raw.isdigit():
        quality = int(quality_raw)

    editable = await m.reply_text(
        "**Topic-wise upload?**\n\nSend `yes` to create forum topics.\nSend `no` to upload to General."
    )
    try:
        reply = await _ask(chat_id, timeout=60)
        await reply.delete(); await editable.delete()
        topic_wise_raw = reply.text.strip() if reply.text else "no"
    except asyncio.TimeoutError:
        topic_wise_raw = "no"
    topic_wise = topic_wise_raw.lower() in ("yes", "y", "1")

    # Step 7: Target channel
    default_hint = f"\n\nDefault (/d): `{SUPERGROUP_ID}`" if SUPERGROUP_ID else f"\n\nDefault (/d): this chat (`{chat_id}`)"
    editable = await m.reply_text(
        "**Step 7/7 — Target Channel / Group**\n\n"
        "<blockquote>🔹 Make me an admin in the target channel.\n"
        "🔸 Send /id inside that chat to get its ID.\n\n"
        "Example: `-100XXXXXXXXXXX`</blockquote>"
        f"{default_hint}"
    )
    try:
        reply = await _ask(chat_id, timeout=60)
        await reply.delete(); await editable.delete()
        channel_raw = reply.text.strip() if reply.text else "/d"
    except asyncio.TimeoutError:
        channel_raw = "/d"

    if not channel_raw or channel_raw == "/d":
        target_channel = SUPERGROUP_ID if SUPERGROUP_ID else chat_id
    else:
        if channel_raw.lstrip("-").isdigit():
            target_channel = int(channel_raw)
        else:
            await m.reply_text("❌ Invalid channel ID.")
            return

    try:
        chat_info = await client.get_chat(target_channel)
        chat_title = chat_info.title or str(target_channel)
    except Exception:
        chat_title = str(target_channel)

    await m.reply_text(f"✅ **Target:** `{chat_title}` (`{target_channel}`)\n\n⌛ Building content list...")

    # Build content list
    items_all = []
    for subj in sel_subjects:
        subj_id   = str(subj.get("id") or subj.get("subjectId") or subj.get("_id", ""))
        subj_name = subj.get("subjectName") or subj.get("name", "")
        try:
            topics = api.get_topics(domain, token, course_id, subj_id)
        except Exception as e:
            log.warning("get_topics failed: %s", e)
            continue
        for topic in topics:
            t_id   = str(topic.get("id") or topic.get("topicId") or topic.get("_id", ""))
            t_name = topic.get("topicName") or topic.get("name", "Topic")
            try:
                lessons = api.get_lessons(domain, token, course_id, subj_id, t_id)
            except Exception as e:
                log.warning("get_lessons failed: %s", e)
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
        f"📋 **Summary:**\n• Total: **{total}**\n• Batch: **{b_name}**\n"
        f"• Quality: **{quality}p**\n• Topic-wise: **{'Yes' if topic_wise else 'No'}**\n"
        f"• Channel: **{chat_title}**\n\n⌛ Starting..."
    )

    job_id = str(uuid.uuid4())[:8]
    job = {
        "job_id": job_id, "user_id": uid, "chat_id": chat_id,
        "target_channel": target_channel, "batch_name": b_name,
        "course_name": course_name, "total_items": total, "status": "running",
        "quality": quality, "topic_wise": topic_wise, "progress": {},
        "created_at": datetime.utcnow(), "domain": domain, "token": token, "items": items_all,
    }
    db.save_job(job)

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

        raw_title = (lesson.get("title") or lesson.get("videoTitle")
                     or lesson.get("name") or f"Item {idx+1}")
        topic_name_from_api = lesson.get("_topic_name", "General")
        url = api.resolve_video_url(lesson)

        if topic_wise:
            t_name_bracket, vid_title = extract_topic_name(raw_title)
            topic_label     = t_name_bracket if t_name_bracket != "General" else topic_name_from_api
            vid_title_clean = clean_title(vid_title)
        else:
            topic_label     = None
            vid_title_clean = clean_title(raw_title)

        try:
            prog_msg = await client.send_message(
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
            log.error("Download failed item %d: %s", idx, e)
            failed += 1
            if prog_msg:
                await prog_msg.edit_text(f"⚠️ **Download failed** ({count+1}/{total})\n`{vid_title_clean}`\n`{e}`")
            db.mark_item_done(job_id, idx)
            count += 1
            continue

        caption = (
            f"[📁] `{count+1:03d}` **{vid_title_clean}**\n"
            f"<blockquote><b>Batch: {b_name}"
            + (f"\nTopic: {topic_label}" if topic_label else "")
            + f"</b></blockquote>\n\n**by {CREDIT}**"
        )

        ok = await upload_file(client, job_id, target_channel, topic_label, local_file, caption, prog_msg)
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

    _active[uid] = False
    stopped_early = _cancel.get(uid, False)
    _cancel[uid]  = False

    if stopped_early:
        db.delete_job(job_id)
        await m.reply_text(f"🛑 **Stopped.**\nUploaded **{count - failed}** items.\n\n**{CREDIT}**")
    else:
        db.delete_job(job_id)
        success = total - failed
        final = (
            f"**✅ Batch Complete!**\n"
            f"<blockquote><b>🎯 Batch: {b_name}</b></blockquote>\n"
            f"📊 Total: {total} | ✅ OK: {success} | ❌ Failed: {failed}\n\n**{CREDIT}**"
        )
        await m.reply_text(final)
        try:
            await send_text_to_topic(client, target_channel, job_id, None, final)
        except Exception:
            pass


async def _run_job(job: dict):
    uid            = job["user_id"]
    chat_id        = job.get("chat_id", OWNER_ID)
    job_id         = job["job_id"]
    b_name         = job["batch_name"]
    quality        = job.get("quality", 720)
    topic_wise     = job.get("topic_wise", False)
    items_all      = job.get("items", [])
    total          = len(items_all)
    target_channel = job.get("target_channel") or SUPERGROUP_ID or chat_id

    if not items_all:
        db.delete_job(job_id)
        return

    _active[uid] = True
    _cancel[uid] = False
    ensure_dir(DOWNLOAD_DIR)

    try:
        await app.send_message(chat_id, f"🔄 **Resuming job after restart**\nBatch: **{b_name}**...")
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

        raw_title = (lesson.get("title") or lesson.get("videoTitle")
                     or lesson.get("name") or f"Item {idx+1}")
        url = api.resolve_video_url(lesson)

        if topic_wise:
            t_name_bracket, vid_title = extract_topic_name(raw_title)
            topic_label     = t_name_bracket if t_name_bracket != "General" else lesson.get("_topic_name", "General")
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
            failed += 1
            if prog_msg:
                await prog_msg.edit_text(f"⚠️ Download failed ({count+1}/{total})\n`{vid_title_clean}`\n`{e}`")
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
    _cancel[uid] = False
    db.delete_job(job_id)

    msg = (
        f"**✅ Batch Complete!**\n"
        f"<blockquote><b>🎯 Batch: {b_name}</b></blockquote>\n"
        f"📊 Total: {total} | ✅ OK: {total - failed} | ❌ Failed: {failed}\n\n**{CREDIT}**"
    )
    try:
        await app.send_message(chat_id, msg)
    except Exception:
        pass


async def resume_pending_jobs():
    pending = db.get_pending_jobs()
    if not pending:
        return
    log.info("Resuming %d interrupted job(s)...", len(pending))
    for job in pending:
        fresh = db.get_job(job["job_id"])
        if fresh:
            asyncio.ensure_future(_run_job(fresh))


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
    await idle()
    await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
