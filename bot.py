"""
bot.py — AppX Downloader Bot (fixed)

Bugs fixed vs original:
  1. domain.lstrip("https://") → was stripping chars not prefix,
     eating "p" from "parmaracademyapi...". Fixed with re.sub.
  2. All api.* calls were sync (requests) inside async bot — now all async.
  3. api.get_courses/subjects/topics/lessons called without user_id arg.
  4. api.resolve_video_url() didn't exist — was a stub; replaced with
     full resolve_video() that decrypts encrypted_links, handles PDFs,
     class-note PDFs, YouTube, XOR MKV, HLS fallback.
  5. Download type menu added: Videos / PDFs / Everything / Quizzes / Test Series.
  6. Quiz detection + download (same logic as termux).
  7. class-note PDFs downloaded alongside videos (same as termux).
  8. user_id now stored in job and passed to all API calls.
  9. uploader.get_topic_id/set_topic_id added to db (was missing).
"""

import os
import re
import uuid
import asyncio
import logging
from datetime import datetime

from pyrogram import Client, filters, idle
from pyrogram.types import Message
import pyromod

from vars import API_ID, API_HASH, BOT_TOKEN, OWNER_ID, SUPERGROUP_ID, CREDIT, DOWNLOAD_DIR
from db import database as db
import appx_api as api
from downloader import download_video, download_pdf, download_item
from uploader import upload_file, send_text_to_topic
from utils import extract_topic_name, clean_title, make_progress_text, remove_file, ensure_dir
from auth import register_auth_handlers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

app = Client("appx_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

_cancel: dict[int, bool] = {}
_active: dict[int, bool] = {}


def is_auth(user_id: int) -> bool:
    return user_id == OWNER_ID or db.is_authorized(user_id)


def _clean_domain(raw: str) -> str:
    """
    Strip protocol prefix safely.
    NEVER use lstrip("https://") — it strips individual chars,
    eating letters like 'p' from domain names.
    """
    raw = raw.strip()
    raw = re.sub(r'^https?://', '', raw)
    return raw.rstrip("/")


# ── Commands ──────────────────────────────────────────────────────────────────

@app.on_message(filters.command("start") & filters.private)
async def cmd_start(client: Client, m: Message):
    uid         = m.from_user.id if m.from_user else 0
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
        await m.reply_text("🛑 **Stop requested.** Job will end after current item.")
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

    # ── Step 1: Domain ────────────────────────────────────────────────────────
    editable = await m.reply_text(
        "**Step 1/7 — AppX Domain**\n\n"
        "Send the API domain.\nExample: `parmaracademyapi.teachx.in`\n\n"
        "⚠️ Do NOT include `https://` — just the domain."
    )
    try:
        reply: Message = await client.listen(chat_id, timeout=120)
        await reply.delete(); await editable.delete()
        domain_raw = reply.text.strip() if reply.text else None
    except asyncio.TimeoutError:
        await editable.edit_text("⌛ Timed out. Restart with /appx.")
        return
    if not domain_raw:
        await m.reply_text("❌ No domain received.")
        return

    # THE FIX: regex strip, not lstrip()
    domain = _clean_domain(domain_raw)
    await m.reply_text(f"✅ Domain set: `{domain}`")

    # ── Step 2: Login ─────────────────────────────────────────────────────────
    editable = await m.reply_text(
        "**Step 2/7 — Login Method**\n\n`1` → JWT token\n`2` → Email + password"
    )
    try:
        reply: Message = await client.listen(chat_id, timeout=60)
        await reply.delete(); await editable.delete()
        login_choice = reply.text.strip() if reply.text else "2"
    except asyncio.TimeoutError:
        await editable.edit_text("⌛ Timed out.")
        return

    if login_choice == "1":
        editable = await m.reply_text("**Send your JWT token:**")
        try:
            reply: Message = await client.listen(chat_id, timeout=120)
            await reply.delete(); await editable.delete()
            token_text = reply.text.strip() if reply.text else None
        except asyncio.TimeoutError:
            await editable.edit_text("⌛ Timed out.")
            return
        if not token_text:
            await m.reply_text("❌ No token received.")
            return
        profile = api.login_with_token(domain, token_text)
    else:
        editable = await m.reply_text("**Send your email / mobile:**")
        try:
            reply: Message = await client.listen(chat_id, timeout=60)
            await reply.delete(); await editable.delete()
            email = reply.text.strip() if reply.text else None
        except asyncio.TimeoutError:
            await editable.edit_text("⌛ Timed out.")
            return

        editable = await m.reply_text("**Send your password:**")
        try:
            reply: Message = await client.listen(chat_id, timeout=60)
            await reply.delete(); await editable.delete()
            password = reply.text.strip() if reply.text else None
        except asyncio.TimeoutError:
            await editable.edit_text("⌛ Timed out.")
            return

        if not email or not password:
            await m.reply_text("❌ Email or password missing.")
            return
        loading = await m.reply_text("⌛ Logging in...")
        try:
            profile = await api.login_with_credentials(domain, email, password)
            await loading.delete()
        except Exception as e:
            await loading.edit_text(f"❌ Login failed: {e}")
            return

    token   = profile["token"]
    user_id = profile.get("user_id", "")
    await m.reply_text(
        f"✅ Logged in as **{profile.get('name', 'User')}**\n`user_id: {user_id}`"
    )

    # ── Step 3: Courses ───────────────────────────────────────────────────────
    status_msg = await m.reply_text("⌛ Fetching courses...")
    try:
        # user_id is required — was missing in original bot
        courses = await api.get_courses(domain, token, user_id)
    except Exception as e:
        await status_msg.edit_text(f"❌ Failed to fetch courses: {e}")
        return
    if not courses:
        await status_msg.edit_text("❌ No courses found.")
        return

    course_list = "\n".join(
        f"`{i+1}` — {c.get('course_name') or c.get('courseName') or c.get('name', f'Course {i+1}')}"
        for i, c in enumerate(courses)
    )
    await status_msg.edit_text(f"**Your Courses:**\n{course_list}")

    editable = await m.reply_text("Send course number:")
    try:
        reply: Message = await client.listen(chat_id, timeout=60)
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
    course_name = (course.get("course_name") or course.get("courseName")
                   or course.get("name", f"Course {course_idx+1}"))

    # ── Step 4: Subjects ──────────────────────────────────────────────────────
    loading = await m.reply_text("⌛ Fetching subjects...")
    try:
        subjects = await api.get_subjects(domain, token, user_id, course_id)
    except Exception as e:
        await loading.edit_text(f"❌ Failed: {e}")
        return
    await loading.delete()
    if not subjects:
        await m.reply_text("❌ No subjects found.")
        return

    subj_list = "\n".join(
        f"`{i+1}` — {s.get('subject_name') or s.get('subjectName') or s.get('name', f'Subject {i+1}')}"
        for i, s in enumerate(subjects)
    )
    editable = await m.reply_text(
        f"**Subjects in {course_name}:**\n{subj_list}\n\n"
        "Send number(s) separated by commas, or `all`:"
    )
    try:
        reply: Message = await client.listen(chat_id, timeout=60)
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

    # ── Step 5: Batch name ────────────────────────────────────────────────────
    editable = await m.reply_text(
        "**Step 5/7 — Batch Name**\n\nEnter batch name or /d for default:"
    )
    try:
        reply: Message = await client.listen(chat_id, timeout=60)
        await reply.delete(); await editable.delete()
        b_name_raw = reply.text.strip() if reply.text else "/d"
    except asyncio.TimeoutError:
        b_name_raw = "/d"
    b_name = course_name if b_name_raw == "/d" else b_name_raw

    # ── Step 6: Download type ─────────────────────────────────────────────────
    editable = await m.reply_text(
        "**Step 6/7 — What to download?**\n\n"
        "`1` — Videos only\n"
        "`2` — PDFs only (class notes + standalone PDFs)\n"
        "`3` — Everything (videos + PDFs + quizzes)\n"
        "`4` — Quizzes only (lecture-attached)\n"
        "`5` — Test Series (Monthly Blitz / Weekly Test / etc.)\n"
        "or /d for Everything:"
    )
    try:
        reply: Message = await client.listen(chat_id, timeout=60)
        await reply.delete(); await editable.delete()
        dl_choice = reply.text.strip() if reply.text else "/d"
    except asyncio.TimeoutError:
        dl_choice = "/d"
    if dl_choice == "/d":
        dl_choice = "3"

    want_video = dl_choice in ("1", "3")
    want_pdf   = dl_choice in ("2", "3")
    want_quiz  = dl_choice in ("3", "4")

    # ── Test Series (option 5) — separate flow like termux ────────────────────
    if dl_choice == "5":
        await _test_series_flow(
            client, m, chat_id, domain, token, user_id,
            course_id, course_name, b_name
        )
        return

    # ── Quality (only when downloading videos) ────────────────────────────────
    prefer_quality = "720p"
    if want_video:
        editable = await m.reply_text(
            "**Video Quality?**\n\n`144` / `240` / `360` / `480` / `720` / `1080`\nor /d for 720:"
        )
        try:
            reply: Message = await client.listen(chat_id, timeout=60)
            await reply.delete(); await editable.delete()
            quality_raw = reply.text.strip() if reply.text else "/d"
        except asyncio.TimeoutError:
            quality_raw = "/d"
        if quality_raw and quality_raw != "/d" and quality_raw.isdigit():
            prefer_quality = f"{quality_raw}p"

    # ── Topic-wise ────────────────────────────────────────────────────────────
    editable = await m.reply_text(
        "**Topic-wise upload?**\n\nSend `yes` to create forum topics.\nSend `no` to upload to General."
    )
    try:
        reply: Message = await client.listen(chat_id, timeout=60)
        await reply.delete(); await editable.delete()
        topic_wise_raw = reply.text.strip() if reply.text else "no"
    except asyncio.TimeoutError:
        topic_wise_raw = "no"
    topic_wise = topic_wise_raw.lower() in ("yes", "y", "1")

    # ── Step 7: Target channel ────────────────────────────────────────────────
    default_hint = (
        f"\n\nDefault (/d): `{SUPERGROUP_ID}`" if SUPERGROUP_ID
        else f"\n\nDefault (/d): this chat (`{chat_id}`)"
    )
    editable = await m.reply_text(
        "**Step 7/7 — Target Channel / Group**\n\n"
        "<blockquote>🔹 Make me an admin in the target channel.\n"
        "🔸 Send /id in that chat to get its ID.\n\n"
        "Example: `-100XXXXXXXXXXX`</blockquote>"
        f"{default_hint}"
    )
    try:
        reply: Message = await client.listen(chat_id, timeout=60)
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
        chat_info  = await client.get_chat(target_channel)
        chat_title = chat_info.title or str(target_channel)
    except Exception:
        chat_title = str(target_channel)

    await m.reply_text(
        f"✅ **Target:** `{chat_title}` (`{target_channel}`)\n\n⌛ Building content list..."
    )

    # ── Build content list ────────────────────────────────────────────────────
    loading = await m.reply_text("⌛ Fetching all subjects → topics → lectures...")
    try:
        items_all = await api.collect_all_lectures(
            domain, token, user_id, course_id, sel_subjects
        )
    except Exception as e:
        await loading.edit_text(f"❌ Failed to fetch content: {e}")
        return
    await loading.delete()

    if not items_all:
        await m.reply_text("❌ No content found.")
        return

    total = len(items_all)
    await m.reply_text(
        f"📋 **Summary:**\n• Total: **{total}**\n• Batch: **{b_name}**\n"
        f"• Quality: **{prefer_quality}**\n"
        f"• Downloads: Videos={'✅' if want_video else '❌'} "
        f"PDFs={'✅' if want_pdf else '❌'} Quizzes={'✅' if want_quiz else '❌'}\n"
        f"• Topic-wise: **{'Yes' if topic_wise else 'No'}**\n"
        f"• Channel: **{chat_title}**\n\n⌛ Starting..."
    )

    job_id = str(uuid.uuid4())[:8]
    job = {
        "job_id": job_id, "user_id": uid, "chat_id": chat_id,
        "target_channel": target_channel, "batch_name": b_name,
        "course_name": course_name, "course_id": course_id,
        "total_items": total, "status": "running",
        "prefer_quality": prefer_quality, "topic_wise": topic_wise,
        "want_video": want_video, "want_pdf": want_pdf, "want_quiz": want_quiz,
        "progress": {}, "created_at": datetime.utcnow(),
        "domain": domain, "token": token, "user_id_appx": user_id,
        "items": items_all,
    }
    db.save_job(job)

    _active[uid] = True
    _cancel[uid] = False
    ensure_dir(DOWNLOAD_DIR)

    await _run_download_loop(client, m, job, uid, chat_id, target_channel, chat_title)


# ── Download loop (mirrors termux main download loop exactly) ─────────────────

QUIZ_TYPES = {"QUIZ", "TEST", "ATTEMPT", "EXERCISE", "ASSIGNMENT"}


async def _run_download_loop(client, m, job: dict, uid: int, chat_id: int,
                              target_channel: int, chat_title: str):
    job_id         = job["job_id"]
    b_name         = job["batch_name"]
    prefer_quality = job.get("prefer_quality", "720p")
    topic_wise     = job.get("topic_wise", False)
    want_video     = job.get("want_video", True)
    want_pdf       = job.get("want_pdf", True)
    want_quiz      = job.get("want_quiz", False)
    items_all      = job.get("items", [])
    domain         = job.get("domain", "")
    token          = job.get("token", "")
    user_id        = job.get("user_id_appx", "")
    course_id      = job.get("course_id", "")
    total          = len(items_all)
    failed         = 0
    count          = 0

    for idx, lec in enumerate(items_all):
        if _cancel.get(uid):
            await m.reply_text("🛑 **Stopped by user.**")
            break

        if job.get("progress", {}).get(str(idx)) == "done":
            count += 1
            continue

        # Use correct field name: "Title" not "title" (termux uses Title)
        raw_title = (lec.get("Title") or lec.get("title") or lec.get("videoTitle")
                     or lec.get("name") or f"Item {idx+1}")
        topic_name_from_api = lec.get("_topic_name", "General")
        mat    = lec.get("material_type", "")
        lec_id = lec.get("id")

        if topic_wise:
            t_name_bracket, vid_title = extract_topic_name(raw_title)
            topic_label     = t_name_bracket if t_name_bracket != "General" else topic_name_from_api
            vid_title_clean = clean_title(vid_title)
        else:
            topic_label     = None
            vid_title_clean = clean_title(raw_title)

        sub_dir = os.path.join(DOWNLOAD_DIR, clean_title(b_name))
        if lec.get("_subject"):
            sub_dir = os.path.join(sub_dir, clean_title(lec["_subject"]))
        ensure_dir(sub_dir)

        out_path = os.path.join(sub_dir, f"{str(idx+1).zfill(4)}_{clean_title(vid_title_clean)}")

        try:
            prog_msg = await client.send_message(
                chat_id,
                make_progress_text(count + 1, total, vid_title_clean, b_name, topic_label, CREDIT),
            )
        except Exception:
            prog_msg = None

        # ── Quiz detection (identical to termux) ──────────────────────────────
        _raw_qid  = str(lec.get("quiz_title_id") or lec.get("quiz_id") or "")
        _has_quiz = (
            mat.upper() in QUIZ_TYPES
            or "quiz" in mat.lower()
            or (_raw_qid not in ("", "0", "-1", "None"))
        )

        if _has_quiz and want_quiz:
            quiz_id = _raw_qid or str(lec_id or "")
            if quiz_id:
                if prog_msg:
                    try:
                        await prog_msg.edit_text(
                            f"⌛ Fetching quiz `{vid_title_clean[:50]}`..."
                        )
                    except Exception:
                        pass
                questions = await api.fetch_quiz(domain, token, user_id, quiz_id)
                if questions:
                    qfile = api.save_quiz_txt(questions, out_path, vid_title_clean)
                    if qfile:
                        cap = (
                            f"[📝] `{count+1:03d}` **{vid_title_clean}**\n"
                            f"<blockquote><b>Batch: {b_name}"
                            + (f"\nTopic: {topic_label}" if topic_label else "")
                            + f"</b></blockquote>\n\n**by {CREDIT}**"
                        )
                        ok = await upload_file(
                            client, job_id, target_channel, topic_label,
                            qfile, cap, prog_msg,
                        )
                        remove_file(qfile)
                        if ok:
                            success_count = 1
                        else:
                            failed += 1
                    else:
                        failed += 1
                else:
                    failed += 1

        # Pure quiz/test items have no video — skip to next
        if mat.upper() in QUIZ_TYPES or (
                "quiz" in mat.lower() and mat.upper() != "VIDEO"):
            db.mark_item_done(job_id, idx)
            count += 1
            if prog_msg:
                try: await prog_msg.delete()
                except Exception: pass
            await asyncio.sleep(0.5)
            continue

        # ── Resolve video/PDF URL ─────────────────────────────────────────────
        if prog_msg:
            try:
                await prog_msg.edit_text(f"⌛ Resolving `{vid_title_clean[:50]}`...")
            except Exception:
                pass

        try:
            vtitle, url, xor_key, vtype, class_pdfs, all_candidates = \
                await api.resolve_video(
                    domain, token, user_id, course_id, str(lec_id),
                    prefer_quality=prefer_quality,
                )
        except Exception as e:
            log.error("resolve_video failed for %s: %s", lec_id, e)
            url = None; vtype = None; class_pdfs = []; all_candidates = []

        if not url and not class_pdfs:
            log.warning("No URL for lesson %s (%s)", lec_id, vid_title_clean)
            failed += 1
            db.mark_item_done(job_id, idx)
            count += 1
            if prog_msg:
                try:
                    await prog_msg.edit_text(f"⚠️ No URL for `{vid_title_clean[:50]}`")
                except Exception:
                    pass
            await asyncio.sleep(0.5)
            continue

        # Skip unwanted content types
        if vtype == "pdf" and not want_pdf:
            db.mark_item_done(job_id, idx); count += 1
            if prog_msg:
                try: await prog_msg.delete()
                except Exception: pass
            continue

        if vtype not in (None, "pdf") and not want_video:
            # Still grab class-note PDFs if wanted
            if want_pdf and class_pdfs:
                for suffix, purl in class_pdfs:
                    if not purl: continue
                    pout   = f"{out_path}{suffix}.pdf"
                    pfile  = await download_pdf(purl, pout)
                    if pfile:
                        cap = f"[📄] **{vid_title_clean}**\n\n**by {CREDIT}**"
                        await upload_file(
                            client, job_id, target_channel, topic_label, pfile, cap, None
                        )
                        remove_file(pfile)
            db.mark_item_done(job_id, idx); count += 1
            if prog_msg:
                try: await prog_msg.delete()
                except Exception: pass
            continue

        # ── Download ──────────────────────────────────────────────────────────
        if prog_msg:
            try:
                await prog_msg.edit_text(
                    make_progress_text(count + 1, total, vid_title_clean, b_name, topic_label, CREDIT)
                )
            except Exception:
                pass

        ok = await download_item(
            vtitle or vid_title_clean,
            url, xor_key, vtype,
            out_path,
            class_pdfs=class_pdfs,
            want_pdf=want_pdf,
            all_candidates=all_candidates,
            quality=prefer_quality,
        )

        # Find what file was saved
        local_file = None
        for ext in ("mp4", "mkv", "webm", "ts", "m4v", "pdf"):
            candidate = f"{out_path}.{ext}"
            if os.path.exists(candidate):
                local_file = candidate
                break

        if local_file:
            caption = (
                f"[📁] `{count+1:03d}` **{vid_title_clean}**\n"
                f"<blockquote><b>Batch: {b_name}"
                + (f"\nTopic: {topic_label}" if topic_label else "")
                + f"</b></blockquote>\n\n**by {CREDIT}**"
            )
            uploaded = await upload_file(
                client, job_id, target_channel, topic_label,
                local_file, caption, prog_msg,
            )
            remove_file(local_file)
            if uploaded:
                db.mark_item_done(job_id, idx)
            else:
                failed += 1
                if prog_msg:
                    try:
                        await prog_msg.edit_text(f"⚠️ Upload failed for `{vid_title_clean}`")
                    except Exception:
                        pass
        else:
            failed += 1
            if prog_msg:
                try:
                    await prog_msg.edit_text(
                        f"⚠️ **Download failed** ({count+1}/{total})\n`{vid_title_clean}`"
                    )
                except Exception:
                    pass
            db.mark_item_done(job_id, idx)

        # Upload class-note PDFs that download_item saved alongside
        if want_pdf and class_pdfs:
            for suffix, purl in class_pdfs:
                pfile = f"{out_path}{suffix}.pdf"
                if os.path.exists(pfile):
                    pcap = (f"[📄] **{vid_title_clean}** — Notes{suffix}\n\n**by {CREDIT}**")
                    await upload_file(
                        client, job_id, target_channel, topic_label, pfile, pcap, None
                    )
                    remove_file(pfile)

        if prog_msg:
            try: await prog_msg.delete()
            except Exception: pass

        count += 1
        await asyncio.sleep(1)

    _active[uid] = False
    stopped_early = _cancel.get(uid, False)
    _cancel[uid]  = False

    if stopped_early:
        db.delete_job(job_id)
        await m.reply_text(
            f"🛑 **Stopped.**\nUploaded **{count - failed}** items.\n\n**{CREDIT}**"
        )
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


# ── Test Series flow (mirrors termux download_test_series exactly) ────────────

async def _test_series_flow(client, m, chat_id: int,
                             domain: str, token: str, user_id: str,
                             course_id: str, course_name: str, b_name: str):
    """Option 5 — Test Series download, same flow as termux."""
    loading = await m.reply_text("⌛ Fetching test series list...")
    try:
        series_list = await api.get_test_series_list(domain, token, user_id, course_id)
    except Exception as e:
        await loading.edit_text(f"❌ Failed: {e}")
        return
    await loading.delete()

    if not series_list:
        await m.reply_text("❌ No test series found inside this course.")
        return

    series_text = "\n".join(
        f"`{i+1}` — {s.get('title') or s.get('name') or f'Series {i+1}'}"
        f"  [{s.get('totaltesttitle', '')} tests]"
        for i, s in enumerate(series_list)
    )
    editable = await m.reply_text(
        f"**Test Series in {course_name}:**\n{series_text}\n\n"
        "Send number(s) or `all`:"
    )
    try:
        reply: Message = await client.listen(chat_id, timeout=60)
        await reply.delete(); await editable.delete()
        choice = reply.text.strip() if reply.text else "all"
    except asyncio.TimeoutError:
        choice = "all"

    if choice.lower() == "all":
        chosen_series = series_list
    else:
        idxs = [int(x.strip()) - 1 for x in choice.split(",") if x.strip().isdigit()]
        chosen_series = [series_list[i] for i in idxs if 0 <= i < len(series_list)]

    if not chosen_series:
        await m.reply_text("❌ No series selected.")
        return

    # Target channel
    editable = await m.reply_text(
        "**Target channel for quiz files:**\n\n"
        f"Default (/d): `{SUPERGROUP_ID or chat_id}`"
    )
    try:
        reply: Message = await client.listen(chat_id, timeout=60)
        await reply.delete(); await editable.delete()
        ch_raw = reply.text.strip() if reply.text else "/d"
    except asyncio.TimeoutError:
        ch_raw = "/d"

    if ch_raw == "/d" or not ch_raw:
        target_channel = SUPERGROUP_ID if SUPERGROUP_ID else chat_id
    elif ch_raw.lstrip("-").isdigit():
        target_channel = int(ch_raw)
    else:
        await m.reply_text("❌ Invalid channel ID.")
        return

    job_id = str(uuid.uuid4())[:8]
    ensure_dir(DOWNLOAD_DIR)

    for series in chosen_series:
        sid   = str(series.get("id") or series.get("series_id") or "")
        sname = series.get("title") or series.get("name") or f"Series_{sid}"

        await m.reply_text(f"📚 **Series:** {sname}")

        # Subjects
        try:
            subjects = await api.get_testseries_subjects(domain, token, user_id, sid)
        except Exception:
            subjects = []
        if not subjects:
            subjects = [{"subjectid": 0, "subject_name": "All Tests", "id": 0}]

        subj_text = "\n".join(
            f"`{i+1}` — {s.get('subject_name') or s.get('name') or f'Subject {i+1}'}"
            for i, s in enumerate(subjects)
        )
        editable = await m.reply_text(
            f"**Subjects in '{sname}':**\n{subj_text}\n\nSend number(s) or `all`:"
        )
        try:
            reply: Message = await client.listen(chat_id, timeout=60)
            await reply.delete(); await editable.delete()
            subj_choice = reply.text.strip() if reply.text else "all"
        except asyncio.TimeoutError:
            subj_choice = "all"

        if subj_choice.lower() == "all":
            chosen_subjects = subjects
        else:
            idxs = [int(x.strip()) - 1 for x in subj_choice.split(",") if x.strip().isdigit()]
            chosen_subjects = [subjects[i] for i in idxs if 0 <= i < len(subjects)]

        # Collect tests
        all_tests = []
        for subj in chosen_subjects:
            subj_id   = (subj.get("subjectid") or subj.get("id") or subj.get("subject_id") or 0)
            subj_name = subj.get("subject_name") or subj.get("name") or "Subject"
            try:
                tests = await api.get_testseries_tests(domain, token, user_id, sid, subj_id)
            except Exception:
                tests = []
            for t in tests:
                t["_subject_name"] = subj_name
                t["_series_name"]  = sname
            all_tests.extend(tests)

        if not all_tests:
            await m.reply_text(f"⚠️ No tests found in '{sname}'.")
            continue

        await m.reply_text(f"✅ {len(all_tests)} tests found in '{sname}'. Starting download...")

        success = failed = 0
        total   = len(all_tests)

        for i, test in enumerate(all_tests, 1):
            tname = test.get("title") or test.get("name") or f"Test_{test.get('id','?')}"
            subj  = test.get("_subject_name", "")

            try:
                prog_msg = await client.send_message(
                    chat_id,
                    make_progress_text(i, total, tname, sname, subj or None, CREDIT)
                )
            except Exception:
                prog_msg = None

            out_dir  = os.path.join(DOWNLOAD_DIR, clean_title(sname), clean_title(subj or ""))
            ensure_dir(out_dir)
            out_path = os.path.join(out_dir, f"{str(i).zfill(4)}_{clean_title(tname)}")

            try:
                questions = await api.fetch_testseries_questions(domain, token, user_id, test)
            except Exception:
                questions = []

            if not questions:
                failed += 1
                if prog_msg:
                    try: await prog_msg.edit_text(f"⚠️ No questions for `{tname[:50]}`")
                    except Exception: pass
                await asyncio.sleep(0.5)
                continue

            qfile = api.save_quiz_txt(questions, out_path, tname)
            if qfile:
                cap = (
                    f"[📝] `{i:03d}` **{tname}**\n"
                    f"<blockquote><b>Series: {sname}"
                    + (f"\nSubject: {subj}" if subj else "")
                    + f"</b></blockquote>\n\n**by {CREDIT}**"
                )
                ok = await upload_file(
                    client, job_id, target_channel, subj or None, qfile, cap, prog_msg
                )
                remove_file(qfile)
                if ok:
                    success += 1
                else:
                    failed += 1
            else:
                failed += 1

            if prog_msg:
                try: await prog_msg.delete()
                except Exception: pass

            await asyncio.sleep(0.5)

        await m.reply_text(
            f"**✅ Series Done: {sname}**\n"
            f"✅ Saved: {success} | ❌ Failed: {failed}\n\n**{CREDIT}**"
        )


# ── Job resume on restart ─────────────────────────────────────────────────────

async def _run_job(job: dict):
    """Resume an interrupted job after bot restart."""
    uid            = job["user_id"]
    chat_id        = job.get("chat_id", OWNER_ID)
    b_name         = job["batch_name"]
    target_channel = job.get("target_channel") or SUPERGROUP_ID or chat_id

    if not job.get("items"):
        db.delete_job(job["job_id"])
        return

    _active[uid] = True
    _cancel[uid] = False
    ensure_dir(DOWNLOAD_DIR)
    db.update_job_status(job["job_id"], "running")

    try:
        await app.send_message(chat_id, f"🔄 **Resuming job**\nBatch: **{b_name}**...")
    except Exception:
        pass

    class _FakeM:
        async def reply_text(self, text):
            try:
                await app.send_message(chat_id, text)
            except Exception:
                pass

    await _run_download_loop(
        app, _FakeM(), job, uid, chat_id, target_channel, str(target_channel)
    )


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
