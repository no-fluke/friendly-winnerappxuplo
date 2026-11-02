"""
appx_api.py — AppX / Parmar Academy API client.
Exact port of termux script v3.8. All endpoints, headers,
decrypt logic, field names are identical to the working termux version.

Fixes vs old broken bot version:
  - All API calls are now async (aiohttp), not blocking (requests)
  - Correct endpoints: /get/... and /post/... NOT /api/v1/get/...
  - Correct headers: client-service, auth-key, device-type, okhttp UA
  - Correct login endpoint: /post/userLogin NOT /api/v2/auth/login
  - Correct decrypt_url: base64(ct):base64(iv) format NOT iv-prepended
  - Correct field names: Title, subjectid, topicid (not camelCase)
  - user_id extracted from JWT automatically
  - Quiz, Test Series, PDF, XOR MKV all supported
"""

import re
import json
import base64
import asyncio
import logging
import aiohttp

log = logging.getLogger(__name__)

# ── Constants (identical to termux script) ────────────────────────────────────
AES_KEY     = b'638udh3829162018'
FALLBACK_IV = b'fedcba9876543210'

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
    AES_OK = True
except ImportError:
    AES_OK = False
    log.warning("pycryptodome not installed — encrypted URLs will not decrypt")

# Exact APPX headers from termux HAR capture
APPX_HEADERS = {
    "client-service":       "Appx",
    "auth-key":             "appxapi",
    "language":             "en",
    "device-type":          "ANDROID",
    "x-tenant-app-version": "420",
    "user-agent":           "okhttp/5.3.2",
}

CDN_HEADERS = {
    "Referer":         "https://player-strict.akamai.net.in/",
    "User-Agent":      "okhttp/5.3.2",
    "Accept-Encoding": "gzip",
}

PDF_HEADERS = {
    "Referer":         "https://player-strict.akamai.net.in/",
    "User-Agent":      "Dalvik/2.1.0 (Linux; U; Android 11; RMX2001 Build/RP1A.200720.011)",
    "Accept":          "application/pdf,*/*",
    "Accept-Encoding": "gzip",
    "Connection":      "Keep-Alive",
}

_CDN_MIGRATIONS = {
    "appxcontent.kaxa.in":          "appx-content-v2.classx.co.in",
    "appx-content.classx.co.in":    "appx-content-v2.classx.co.in",
    "appxcontentcdn.classx.co.in":  "appx-content-v2.classx.co.in",
}


# ── Decrypt (identical to termux script) ──────────────────────────────────────

def decrypt_url(enc_path: str) -> str:
    """
    Decrypt AES-CBC encrypted AppX URL.
    Format: base64(ciphertext):base64(iv)
    Falls back to FALLBACK_IV when no colon present.
    """
    if not enc_path or not AES_OK:
        return ""
    try:
        parts  = enc_path.split(":")
        ct_b64 = parts[0]
        iv     = base64.b64decode(parts[1]) if len(parts) > 1 else FALLBACK_IV
        ct     = base64.b64decode(ct_b64)
        cipher = AES.new(AES_KEY, AES.MODE_CBC, iv)
        try:
            raw  = unpad(cipher.decrypt(ct), AES.block_size)
            text = raw.decode("utf-8", errors="ignore")
        except Exception:
            cipher2 = AES.new(AES_KEY, AES.MODE_CBC, iv)
            text    = cipher2.decrypt(ct).decode("latin-1")
        m = re.search(r'https?://[\x20-\x7e]+', text)
        if not m:
            return ""
        return m.group(0).rstrip(
            "\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f\x10"
        ).strip()
    except Exception:
        return ""


def decode_b64(s: str) -> str:
    try:
        return base64.b64decode(s).decode("utf-8")
    except Exception:
        return ""


def _migrate_cdn(url: str) -> str:
    if not url:
        return url
    for old, new in _CDN_MIGRATIONS.items():
        if old in url:
            return url.replace(old, new)
    return url


def _is_drm_url(url: str) -> bool:
    if not url:
        return False
    lower = url.lower()
    return "hls-drm" in lower or (
        "transcoded-recordings.classx.co.in" in lower and "drm-hls" in lower
    )


def _is_mp4_url(url: str) -> bool:
    if not url:
        return False
    lower = url.lower()
    if "static-db.appx.co.in" in lower:         return True
    if "appx-content-v2.classx.co.in" in lower: return True
    return lower.split("?")[0].endswith(".mp4")


def _mp4_headers(url: str) -> dict:
    lower = url.lower()
    if "appx-content-v2.classx.co.in" in lower:
        return {
            **APPX_HEADERS,
            "Referer":    "https://player-strict.akamai.net.in/",
            "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 11; RMX2001 Build/RP1A.200720.011)",
        }
    return {
        "Referer":    "https://player-strict.akamai.net.in/",
        "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 11; RMX2001 Build/RP1A.200720.011)",
    }


def _resolve_pdf_url(pdf_link: str, pdf_key: str) -> str:
    """Handle all 3 PDF encoding variants (identical to termux)."""
    if not pdf_link:
        return ""
    if pdf_link.startswith("http"):
        return _migrate_cdn(pdf_link)
    url = decrypt_url(pdf_link)
    if url:
        return _migrate_cdn(url)
    if pdf_key and ":" in pdf_key:
        iv_b64  = pdf_key.split(":", 1)[1]
        ct_part = pdf_link.split(":", 1)[0]
        url     = decrypt_url(ct_part + ":" + iv_b64)
        if url:
            return _migrate_cdn(url)
    if pdf_key:
        url = decrypt_url(pdf_key)
        if url and url.startswith("http"):
            return _migrate_cdn(url)
    return ""


def _extract_class_pdfs(d: dict) -> list:
    pdfs = []
    p1 = _resolve_pdf_url(d.get("pdf_link",  ""), d.get("pdf_encryption_key",  ""))
    p2 = _resolve_pdf_url(d.get("pdf_link2", ""), d.get("pdf2_encryption_key", ""))
    if p1: pdfs.append(("",   p1))
    if p2: pdfs.append(("_2", p2))
    return pdfs


def decrypt_all_links(d: dict) -> list:
    """Quality-ranked (quality_label, url) from all encrypted link fields."""
    quality_rank = {"1080p": 0, "720p": 1, "480p": 2, "360p": 3, "240p": 4, "144p": 5}
    seen: set    = set()
    results: list = []

    def _add(quality: str, url: str):
        if url and url not in seen:
            seen.add(url)
            results.append((quality or "?", url))

    for link in d.get("encrypted_links", []):
        q = str(link.get("quality") or "?")
        for field in ("path", "backup_url", "backup_url2"):
            raw = link.get(field, "")
            if raw:
                url = _migrate_cdn(decrypt_url(raw))
                if url: _add(q, url)

    raw_dl  = d.get("download_links", "")
    dl_list = raw_dl if isinstance(raw_dl, list) else []
    if isinstance(raw_dl, str) and raw_dl.strip() not in ("", "[]"):
        try:
            dl_list = json.loads(raw_dl.replace("'", '"'))
        except Exception:
            pass
    for lnk in dl_list:
        q = str(lnk.get("bitrate") or lnk.get("quality") or "?")
        for field in ("path", "backup_url", "backup_url2"):
            raw = lnk.get(field, "")
            if raw:
                url = _migrate_cdn(decrypt_url(raw))
                if url: _add(q, url)

    fl = _migrate_cdn(decrypt_url(d.get("file_link", "")))
    if fl: _add("file_link", fl)
    dl = _migrate_cdn(decrypt_url(d.get("download_link", "")))
    if dl: _add("download_link", dl)

    results.sort(key=lambda x: quality_rank.get(x[0], 99))
    return results


# ── JWT helper ────────────────────────────────────────────────────────────────

def extract_user_id_from_jwt(token: str) -> str:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return ""
        p = parts[1]
        p += "=" * (-len(p) % 4)
        payload = json.loads(base64.urlsafe_b64decode(p).decode("utf-8"))
        uid = payload.get("id") or payload.get("userid") or payload.get("user_id") or ""
        return str(uid)
    except Exception:
        return ""


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _make_headers(token: str = None, user_id: str = None) -> dict:
    h = dict(APPX_HEADERS)
    if token:   h["authorization"] = token
    if user_id: h["user-id"]       = str(user_id)
    return h


async def _api_get(session: aiohttp.ClientSession, url: str,
                   headers: dict, retries: int = 3) -> dict:
    for attempt in range(retries):
        try:
            await asyncio.sleep(0.3)
            async with session.get(url, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 429:
                    wait = 15 * (attempt + 1)
                    log.warning("Rate limited — waiting %ds", wait)
                    await asyncio.sleep(wait)
                    continue
                if resp.status != 200:
                    log.warning("HTTP %d — %s", resp.status, url[:80])
                    return {}
                return await resp.json(content_type=None)
        except Exception as e:
            if attempt == retries - 1:
                log.error("Request error (%s): %s", url[:80], e)
            await asyncio.sleep(2)
    return {}


async def _api_post(session: aiohttp.ClientSession, url: str,
                    headers: dict, data: dict) -> dict:
    try:
        async with session.post(url, headers=headers, data=data,
                                timeout=aiohttp.ClientTimeout(total=30)) as resp:
            return await resp.json(content_type=None)
    except Exception as e:
        log.error("POST error (%s): %s", url[:80], e)
        return {}


# ── Login ─────────────────────────────────────────────────────────────────────

def login_with_token(domain: str, jwt_token: str) -> dict:
    """JWT bypass — auto-extract user_id from token payload."""
    user_id = extract_user_id_from_jwt(jwt_token)
    return {
        "token":   jwt_token,
        "user_id": user_id,
        "name":    f"User #{user_id}",
        "domain":  domain,
    }


async def login_with_credentials(domain: str, email: str, password: str) -> dict:
    """
    POST /post/userLogin — exact same logic as termux script.
    NOT /api/v2/auth/login (that doesn't exist).
    """
    base         = f"https://{domain}"
    post_headers = {
        **APPX_HEADERS,
        "content-type": "application/x-www-form-urlencoded",
    }
    async with aiohttp.ClientSession() as session:
        # Primary endpoint
        r = await _api_post(session, f"{base}/post/userLogin", post_headers, {
            "email":       email,
            "password":    password,
            "devicetoken": "tgbot_appx",
            "mydeviceid":  "tgbot",
        })
        if r.get("status") == 200:
            d = r["data"]
            return {
                "token":   d["token"],
                "user_id": str(d.get("userid") or d.get("user_id", "")),
                "name":    d.get("name", email),
                "domain":  domain,
            }
        # Fallback (some platforms)
        r2 = await _api_post(session,
            f"{base}/post/userLogin?extra_details=0",
            {**post_headers, "source": "website"}, {
                "email":         email,
                "phone":         email,
                "password":      password,
                "source":        "website",
                "extra_details": "1",
            })
        if r2.get("status") == 200:
            d = r2["data"]
            return {
                "token":   d["token"],
                "user_id": str(d.get("userid") or d.get("user_id", "")),
                "name":    d.get("name", email),
                "domain":  domain,
            }
        msg = r.get("message") or r2.get("message") or "Unknown error"
        raise ValueError(f"Login failed: {msg}")


# ── Course / content (exact URLs and params from termux) ─────────────────────

async def get_courses(domain: str, token: str, user_id: str) -> list:
    """/get/mycoursev2?userid=<uid>&type=all"""
    hdr = _make_headers(token, user_id)
    async with aiohttp.ClientSession() as session:
        r = await _api_get(session,
            f"https://{domain}/get/mycoursev2?userid={user_id}&type=all", hdr)
        if r.get("data"):
            return r["data"]
        # Fallback purchases endpoint
        r2 = await _api_get(session,
            f"https://{domain}/get/get_all_purchases?userid={user_id}&item_type=10", hdr)
        courses = []
        for item in r2.get("data", []):
            for ct in item.get("coursedt", []):
                courses.append(ct)
        return courses


async def get_subjects(domain: str, token: str, user_id: str,
                       course_id: str) -> list:
    """/get/allsubjectfrmlivecourseclass?courseid=<cid>&start=-1"""
    hdr = _make_headers(token, user_id)
    async with aiohttp.ClientSession() as session:
        r = await _api_get(session,
            f"https://{domain}/get/allsubjectfrmlivecourseclass"
            f"?courseid={course_id}&start=-1", hdr)
        return r.get("data", [])


async def get_topics(domain: str, token: str, user_id: str,
                     course_id: str, subject_id: str) -> list:
    """/get/alltopicfrmlivecourseclass — sorted by topicid"""
    hdr = _make_headers(token, user_id)
    async with aiohttp.ClientSession() as session:
        r = await _api_get(session,
            f"https://{domain}/get/alltopicfrmlivecourseclass"
            f"?courseid={course_id}&subjectid={subject_id}&start=-1", hdr)
        return sorted(r.get("data", []), key=lambda x: x.get("topicid", 0))


async def get_lectures(domain: str, token: str, user_id: str,
                       course_id: str, subject_id: str, topic_id: str) -> list:
    """/get/livecourseclassbycoursesubtopconceptapiv3 — sorted by id"""
    hdr = _make_headers(token, user_id)
    async with aiohttp.ClientSession() as session:
        r = await _api_get(session,
            f"https://{domain}/get/livecourseclassbycoursesubtopconceptapiv3"
            f"?courseid={course_id}&subjectid={subject_id}"
            f"&topicid={topic_id}&conceptid=&start=-1", hdr)
        return sorted(r.get("data", []), key=lambda x: x.get("id", 0))


async def get_folder_contents(domain: str, token: str, user_id: str,
                               course_id: str, parent_id) -> list:
    """/get/folder_contentsv2"""
    hdr = _make_headers(token, user_id)
    async with aiohttp.ClientSession() as session:
        r = await _api_get(session,
            f"https://{domain}/get/folder_contentsv2"
            f"?course_id={course_id}&parent_id={parent_id}", hdr)
        return r.get("data", [])


async def resolve_video(domain: str, token: str, user_id: str,
                        course_id: str, video_id: str,
                        prefer_quality: str = "720p") -> tuple:
    """
    Exact port of termux resolve_video().
    Returns (title, url, xor_key, vtype, class_pdfs, all_candidates).
    vtype: 'youtube' | 'mp4' | 'hls' | 'pdf' | None
    class_pdfs: list of (suffix, pdf_url)
    all_candidates: list of (quality_label, url) for fallback
    """
    hdr = _make_headers(token, user_id)
    async with aiohttp.ClientSession() as session:
        r = await _api_get(session,
            f"https://{domain}/get/fetchVideoDetailsById"
            f"?course_id={course_id}&video_id={video_id}"
            f"&ytflag=0&folder_wise_course=0", hdr)
    d = r.get("data", {})
    if not d:
        return None, None, None, None, [], []

    title          = d.get("Title") or f"video_{video_id}"
    class_pdfs     = _extract_class_pdfs(d)
    all_candidates = decrypt_all_links(d)

    # 1. YouTube encrypted video_id
    yt_enc = d.get("video_id", "")
    if yt_enc:
        yt_id = decrypt_url(yt_enc)
        if yt_id and not yt_id.startswith("http"):
            return (title, f"https://youtu.be/{yt_id}",
                    None, "youtube", class_pdfs, all_candidates)

    # 2. YouTube embed flag
    if str(d.get("ytFlag", "")) == "1" and d.get("embed_url"):
        return (title, d["embed_url"], None, "youtube", class_pdfs, all_candidates)

    # 3. Best non-DRM link at preferred quality
    prefer_order = [prefer_quality, "1080p", "720p", "480p", "360p",
                    "240p", "144p", "file_link", "download_link"]
    by_quality: dict = {}
    for qlabel, cand_url in all_candidates:
        if cand_url and ".pdf" not in cand_url and not _is_drm_url(cand_url):
            if qlabel not in by_quality:
                by_quality[qlabel] = cand_url

    chosen_url = None
    for q in prefer_order:
        if q in by_quality:
            chosen_url = by_quality[q]
            break
    if not chosen_url and by_quality:
        chosen_url = next(iter(by_quality.values()))

    if chosen_url:
        vtype = "mp4" if _is_mp4_url(chosen_url) else "hls"
        return title, chosen_url, None, vtype, class_pdfs, all_candidates

    # 4. Legacy encrypted_links[] with xor_key
    quality_rank = {"1080p": 0, "720p": 1, "480p": 2, "360p": 3, "240p": 4}
    enc_links = sorted(d.get("encrypted_links", []),
                       key=lambda x: quality_rank.get(str(x.get("quality", "")), 99))
    for link in enc_links:
        raw_path = link.get("path", "")
        raw_key  = link.get("key",  "")
        if not raw_path:
            continue
        video_url = _migrate_cdn(decrypt_url(raw_path))
        if not video_url or _is_drm_url(video_url):
            continue
        xor_key = decode_b64(decrypt_url(raw_key)) if raw_key else ""
        vtype   = "mp4" if _is_mp4_url(video_url) else "hls"
        return title, video_url, xor_key or None, vtype, class_pdfs, all_candidates

    # 5. PDF-only item
    if class_pdfs:
        primary_pdf = class_pdfs[0][1]
        extra_pdfs  = class_pdfs[1:]
        return title, primary_pdf, None, "pdf", extra_pdfs, all_candidates

    return title, None, None, None, [], all_candidates


# ── Quiz (exact from termux) ──────────────────────────────────────────────────

async def fetch_quiz(domain: str, token: str, user_id: str,
                     quiz_title_id: str) -> list:
    """/get/quizquestion?start=-1&quizid=<id> with fallbacks."""
    hdr = _make_headers(token, user_id)
    endpoints = [
        f"https://{domain}/get/quizquestion?start=-1&quizid={quiz_title_id}",
        f"https://{domain}/get/quiztitlebyid?quiz_title_id={quiz_title_id}&userid={user_id}",
        f"https://{domain}/get/quiztitlebyid?quiztitleid={quiz_title_id}&userid={user_id}",
        f"https://{domain}/get/quizbyid?quiz_id={quiz_title_id}&userid={user_id}",
    ]
    async with aiohttp.ClientSession() as session:
        for url in endpoints:
            r    = await _api_get(session, url, hdr)
            data = r.get("data") or []
            if isinstance(data, list) and data:
                return data
            if isinstance(data, dict):
                for key in ("questions", "question_list", "quiz_questions", "data"):
                    nested = data.get(key)
                    if isinstance(nested, list) and nested:
                        return nested
    return []


# ── Test Series (exact from termux) ──────────────────────────────────────────

async def get_test_series_list(domain: str, token: str, user_id: str,
                                course_id: str) -> list:
    hdr = _make_headers(token, user_id)
    async with aiohttp.ClientSession() as session:
        r = await _api_get(session,
            f"https://{domain}/get/test_seriesbycourseid"
            f"?start=-1&userid={user_id}&courseid={course_id}&folder_wise_course=0", hdr)
    data = r.get("data") or r.get("result") or []
    return data if isinstance(data, list) else []


async def get_testseries_subjects(domain: str, token: str, user_id: str,
                                   series_id: str) -> list:
    hdr = _make_headers(token, user_id)
    async with aiohttp.ClientSession() as session:
        r = await _api_get(session,
            f"https://{domain}/get/testseries_subjects?testseries_id={series_id}", hdr)
    data = r.get("data") or r.get("subjects") or r.get("result") or []
    return data if isinstance(data, list) else []


async def get_testseries_tests(domain: str, token: str, user_id: str,
                                series_id: str, subject_id) -> list:
    hdr       = _make_headers(token, user_id)
    all_tests = []
    start     = 0
    async with aiohttp.ClientSession() as session:
        while True:
            r = await _api_get(session,
                f"https://{domain}/get/test_titlev2"
                f"?testseriesid={series_id}&subject_id={subject_id}"
                f"&chapter_id=-1&userid=0&search=&start={start}", hdr)
            items = (r.get("data") or r.get("tests") or
                     r.get("test_titles") or r.get("result") or [])
            if not isinstance(items, list) or not items:
                break
            all_tests.extend(items)
            if len(items) < 10:
                break
            start += len(items)
            await asyncio.sleep(0.3)
    return all_tests


async def fetch_testseries_questions(domain: str, token: str, user_id: str,
                                      test: dict) -> list:
    """CDN JSON first, fallback to /get/quizquestion — exact from termux."""
    cdn_url = test.get("test_questions_url") or ""
    if cdn_url:
        try:
            async with aiohttp.ClientSession() as cdn_sess:
                async with cdn_sess.get(cdn_url,
                        timeout=aiohttp.ClientTimeout(total=30),
                        headers={"User-Agent": "okhttp/5.3.2"}) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        qs   = (data if isinstance(data, list)
                                else data.get("questions") or data.get("data") or [])
                        if qs:
                            return qs
        except Exception:
            pass
    test_id = str(test.get("id") or test.get("test_id") or "")
    if test_id:
        hdr = _make_headers(token, user_id)
        async with aiohttp.ClientSession() as session:
            r  = await _api_get(session,
                f"https://{domain}/get/quizquestion?start=-1&quizid={test_id}", hdr)
            qs = r.get("data") or []
            if isinstance(qs, list) and qs:
                return qs
    return []


# ── Collect all lectures (folder-aware, exact from termux) ────────────────────

async def collect_all_lectures(domain: str, token: str, user_id: str,
                                course_id: str, sel_subjects: list) -> list:
    """
    Returns flat list of lecture dicts with _subject and _topic keys injected.
    Handles both folder-based and subject/topic-based courses.
    """
    # Try folder-based first
    folder_data = await get_folder_contents(domain, token, user_id, course_id, -1)
    if folder_data:
        lectures: list = []
        await _collect_folder_recursive(domain, token, user_id, course_id, -1, lectures)
        return lectures

    lectures: list = []
    for subj in sel_subjects:
        si = str(subj.get("subjectid") or subj.get("id") or "")
        sn = subj.get("subject_name") or subj.get("name", "")
        topics = await get_topics(domain, token, user_id, course_id, si)
        for topic in topics:
            ti   = str(topic.get("topicid") or topic.get("id") or "")
            tn   = topic.get("topic_name") or topic.get("name", "")
            lecs = await get_lectures(domain, token, user_id, course_id, si, ti)
            for lec in lecs:
                lec["_subject"] = sn
                lec["_topic"]   = tn
            lectures.extend(lecs)
            await asyncio.sleep(0.3)
    return lectures


async def _collect_folder_recursive(domain: str, token: str, user_id: str,
                                     course_id: str, parent_id, out: list):
    items = await get_folder_contents(domain, token, user_id, course_id, parent_id)
    for item in items:
        if item.get("material_type") == "FOLDER":
            await _collect_folder_recursive(
                domain, token, user_id, course_id, item["id"], out)
        else:
            out.append(item)


# ── Quiz text saver (identical to termux) ────────────────────────────────────

def _strip_html(raw: str) -> str:
    if not raw:
        return ""
    text = re.sub(r'<style[^>]*>.*?</style>', '', raw,
                  flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = (text
            .replace('&nbsp;', ' ').replace('&amp;', '&')
            .replace('&lt;', '<').replace('&gt;', '>')
            .replace('&quot;', '"').replace('&#39;', "'")
            .replace('&ldquo;', '"').replace('&rdquo;', '"'))
    return re.sub(r'\s+', ' ', text).strip()


def save_quiz_txt(questions: list, out_path: str, title: str) -> str | None:
    """Save quiz questions to .txt. Returns path or None."""
    if not questions:
        return None
    out = f"{out_path}_quiz.txt"
    try:
        with open(out, "w", encoding="utf-8") as f:
            f.write(f"Quiz: {title}\n")
            f.write("═" * 60 + "\n\n")
            opt_labels = ["A", "B", "C", "D", "E"]

            for idx, q in enumerate(questions, 1):
                q_text = _strip_html(
                    q.get("question") or q.get("question_en")
                    or q.get("que")   or q.get("title") or "")
                f.write(f"Q{idx}. {q_text}\n")

                options = []
                for num in range(1, 6):
                    clean = _strip_html(q.get(f"option_{num}", ""))
                    if clean:
                        label = opt_labels[num - 1] if num - 1 < 5 else str(num)
                        options.append((label, clean))

                if not options:
                    raw_list = q.get("options") or q.get("option") or []
                    if isinstance(raw_list, list):
                        for oi, opt in enumerate(raw_list):
                            t = (_strip_html(opt.get("text") or opt.get("option") or "")
                                 if isinstance(opt, dict) else _strip_html(str(opt)))
                            if t:
                                label = opt_labels[oi] if oi < 5 else str(oi + 1)
                                options.append((label, t))

                answer_raw = str(
                    q.get("answer") or q.get("correct_answer")
                    or q.get("correct_option") or q.get("correct") or "").strip()

                correct_label = correct_text = ""
                try:
                    ans_idx = int(answer_raw) - 1
                    if 0 <= ans_idx < len(options):
                        correct_label, correct_text = options[ans_idx]
                except (ValueError, TypeError):
                    for lbl, txt in options:
                        if lbl == answer_raw.upper():
                            correct_label, correct_text = lbl, txt
                            break

                for lbl, txt in options:
                    marker = " ✓" if lbl == correct_label else ""
                    f.write(f"   {lbl}) {txt}{marker}\n")

                if correct_text:
                    f.write(f"\n   ✓ Answer: {correct_label}) {correct_text}\n")
                elif answer_raw:
                    f.write(f"\n   ✓ Answer: Option {answer_raw}\n")

                exp = _strip_html(
                    q.get("solution_text") or q.get("explanation")
                    or q.get("solution") or "")
                if exp:
                    f.write(f"   💡 {exp}\n")

                cs = q.get("correct_score",  "")
                ns = q.get("negative_score", "")
                if cs or ns:
                    f.write(f"   [+{cs} / -{ns}]\n")

                f.write("\n")
        return out
    except Exception as e:
        log.error("save_quiz_txt: %s", e)
        return None
