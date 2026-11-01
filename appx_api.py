"""
appx_api.py — AppX / ClassX API client.
Ported from 34568.py v3.8 — all endpoints, headers and decryption logic
are HAR-verified and match the working Termux downloader exactly.
"""

import os
import re
import json
import time
import base64
import logging
import requests

log = logging.getLogger(__name__)

# ── AES constants (HAR-verified) ─────────────────────────────────────────────
AES_KEY     = b'638udh3829162018'
FALLBACK_IV = b'fedcba9876543210'

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
    AES_OK = True
except ImportError:
    AES_OK = False
    log.warning("pycryptodome not installed — encrypted URLs will not be decrypted")

# ── AppX request headers (HAR-verified) ──────────────────────────────────────
APPX_HEADERS = {
    "client-service":       "Appx",
    "auth-key":             "appxapi",
    "language":             "en",
    "device-type":          "ANDROID",
    "x-tenant-app-version": "420",
    "user-agent":           "okhttp/5.3.2",
}

# ── Old CDN → current CDN migration map ──────────────────────────────────────
_CDN_MIGRATIONS = {
    "appxcontent.kaxa.in":         "appx-content-v2.classx.co.in",
    "appx-content.classx.co.in":   "appx-content-v2.classx.co.in",
    "appxcontentcdn.classx.co.in": "appx-content-v2.classx.co.in",
}


# ══════════════════════════════════════════════════════════════════════════════
#  AES / URL DECRYPTION
# ══════════════════════════════════════════════════════════════════════════════

def decrypt_url(enc_path: str) -> str:
    """
    Decrypt an AppX AES-CBC encrypted URL.

    Format: base64(ciphertext):base64(iv)
    Falls back to FALLBACK_IV when no colon separator is present.
    """
    if not AES_OK or not enc_path:
        return enc_path or ""
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


def xor_decrypt(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def _migrate_cdn(url: str) -> str:
    """Replace deprecated CDN hostnames with the current one."""
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
    if "hls-drm" in lower:
        return True
    if "transcoded-recordings.classx.co.in" in lower and "drm-hls" in lower:
        return True
    return False


def _is_mp4_url(url: str) -> bool:
    if not url:
        return False
    lower = url.lower()
    if "static-db.appx.co.in" in lower:
        return True
    if "appx-content-v2.classx.co.in" in lower:
        return True
    path = lower.split("?")[0]
    if path.endswith(".mp4"):
        return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
#  PDF URL RESOLVER
# ══════════════════════════════════════════════════════════════════════════════

def _resolve_pdf_url(pdf_link: str, pdf_key: str) -> str:
    """
    Resolve a PDF URL from its encrypted link + encryption key.

    Variant A — pdf_link already has IV suffix  → decrypt directly.
    Variant B — pdf_link has no IV              → borrow IV from pdf_key.
    Variant C — pdf_link is already a plain URL → return as-is.
    """
    if not pdf_link:
        return ""
    if pdf_link.startswith("http"):
        return _migrate_cdn(pdf_link)

    url = decrypt_url(pdf_link)
    if url:
        return _migrate_cdn(url)

    if pdf_key and ":" in pdf_key:
        iv_b64   = pdf_key.split(":", 1)[1]
        ct_part  = pdf_link.split(":", 1)[0]
        combined = ct_part + ":" + iv_b64
        url      = decrypt_url(combined)
        if url:
            return _migrate_cdn(url)

    if pdf_key:
        url = decrypt_url(pdf_key)
        if url and url.startswith("http"):
            return _migrate_cdn(url)

    return ""


def extract_class_pdfs(d: dict) -> list:
    """
    Return list of (suffix, url) for class-note PDFs attached to a lecture item.
    Both VIDEO and PDF material_type items can carry pdf_link / pdf_link2.

    Returns: [("", url1), ("_2", url2)]
    """
    pdfs = []
    p1 = _resolve_pdf_url(d.get("pdf_link",  ""), d.get("pdf_encryption_key",  ""))
    p2 = _resolve_pdf_url(d.get("pdf_link2", ""), d.get("pdf2_encryption_key", ""))
    if p1:
        pdfs.append(("", p1))
    if p2:
        pdfs.append(("_2", p2))
    return pdfs


# ══════════════════════════════════════════════════════════════════════════════
#  decrypt_all_links — quality-ranked URL list from one lecture item
# ══════════════════════════════════════════════════════════════════════════════

def decrypt_all_links(d: dict) -> list:
    """
    Decrypt EVERY encrypted video link in an API response dict and return a
    de-duplicated, quality-ranked list of (quality_label, url) tuples.

    Sources checked in order:
      1. encrypted_links[]  — per-quality HLS/MP4 paths  (primary CDN)
      2. download_links[]   — per-quality download paths  (may overlap)
      3. file_link          — single fallback link
      4. download_link      — single download fallback

    DRM URLs are intentionally kept so the caller can log/skip them.
    """
    quality_rank = {
        "1080p": 0, "720p": 1, "480p": 2,
        "360p": 3, "240p": 4, "144p": 5,
    }
    seen: set     = set()
    results: list = []

    def _add(quality: str, url: str):
        if url and url not in seen:
            seen.add(url)
            results.append((quality or "?", url))

    # 1. encrypted_links[]
    for link in d.get("encrypted_links", []):
        q = str(link.get("quality") or "?")
        for field in ("path", "backup_url", "backup_url2"):
            raw = link.get(field, "")
            if raw:
                url = _migrate_cdn(decrypt_url(raw))
                if url:
                    _add(q, url)

    # 2. download_links[]
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
                if url:
                    _add(q, url)

    # 3. file_link
    fl = _migrate_cdn(decrypt_url(d.get("file_link", "")))
    if fl:
        _add("file_link", fl)

    # 4. download_link
    dl = _migrate_cdn(decrypt_url(d.get("download_link", "")))
    if dl:
        _add("download_link", dl)

    results.sort(key=lambda x: quality_rank.get(x[0], 99))
    return results


def resolve_video_url(item: dict, prefer_quality: str = "720p") -> str:
    """
    Quick helper — pick the best non-DRM video URL from a lecture item dict.
    Use resolve_lecture() when you also need vtype / xor_key / class_pdfs.
    """
    all_candidates = decrypt_all_links(item)
    prefer_order   = [prefer_quality, "1080p", "720p", "480p",
                      "360p", "240p", "144p", "file_link", "download_link"]
    by_quality: dict = {}
    for qlabel, url in all_candidates:
        if url and ".pdf" not in url and not _is_drm_url(url):
            if qlabel not in by_quality:
                by_quality[qlabel] = url

    for q in prefer_order:
        if q in by_quality:
            return by_quality[q]

    return next(iter(by_quality.values()), "")


# ══════════════════════════════════════════════════════════════════════════════
#  QUIZ DETECTION HELPER
# ══════════════════════════════════════════════════════════════════════════════

# material_type values that are pure quiz items (no video to download)
QUIZ_MATERIAL_TYPES = {"QUIZ", "TEST", "ATTEMPT", "EXERCISE", "ASSIGNMENT"}


def has_quiz(lec: dict) -> tuple:
    """
    Return (has_quiz: bool, quiz_id: str).

    A lecture item may be a quiz even when material_type is "VIDEO" —
    the reliable signal is quiz_title_id being set and not "-1"/"0".
    """
    mat     = (lec.get("material_type") or "").upper()
    raw_qid = str(lec.get("quiz_title_id") or lec.get("quiz_id") or "")
    is_quiz = (
        mat in QUIZ_MATERIAL_TYPES
        or "quiz" in mat.lower()
        or raw_qid not in ("", "0", "-1", "None")
    )
    quiz_id = raw_qid if raw_qid not in ("0", "-1", "None") else str(lec.get("id", ""))
    return is_quiz, quiz_id


def is_pure_quiz(lec: dict) -> bool:
    """True for items that have no video — quiz/test/exercise only."""
    mat = (lec.get("material_type") or "").upper()
    return mat in QUIZ_MATERIAL_TYPES or ("quiz" in mat.lower() and mat != "VIDEO")


# ══════════════════════════════════════════════════════════════════════════════
#  QUIZ TEXT HELPERS  (strip_html + save_quiz_txt)
# ══════════════════════════════════════════════════════════════════════════════

def strip_html(raw: str) -> str:
    """
    Strip HTML tags and decode entities from question / option text.
    Removes embedded <style>…</style> blocks entirely.
    Returns clean plain text with collapsed whitespace.
    """
    if not raw:
        return ""
    text = re.sub(r'<style[^>]*>.*?</style>', '', raw,
                  flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = (text
            .replace('&nbsp;',  ' ')
            .replace('&amp;',   '&')
            .replace('&lt;',    '<')
            .replace('&gt;',    '>')
            .replace('&quot;',  '"')
            .replace('&#39;',   "'")
            .replace('&ldquo;', '"')
            .replace('&rdquo;', '"'))
    return re.sub(r'\s+', ' ', text).strip()


def save_quiz_txt(questions: list, out_path: str, title: str) -> str | None:
    """
    Save quiz questions to <out_path>_quiz.txt.

    Expected question dict shape (from /get/quizquestion):
      {
        "id", "quiz_id",
        "question":  "<HTML>",
        "option_1" … "option_5": "<HTML>",
        "answer":    "3",          ← 1-based option number as string
        "solution_text": "<HTML>",
        "correct_score", "negative_score",
      }

    Returns the saved file path, or None on failure.
    """
    if not questions:
        return None
    out = f"{out_path}_quiz.txt"
    try:
        opt_labels = ["A", "B", "C", "D", "E"]
        with open(out, "w", encoding="utf-8") as f:
            f.write(f"Quiz: {title}\n")
            f.write("═" * 60 + "\n\n")

            for idx, q in enumerate(questions, 1):
                # ── Question ──────────────────────────────────────────────
                q_text = strip_html(
                    q.get("question") or q.get("question_en")
                    or q.get("que")   or q.get("title") or ""
                )
                f.write(f"Q{idx}. {q_text}\n")

                # ── Options (option_1 … option_5, skip empty) ─────────────
                options = []
                for num in range(1, 6):
                    clean = strip_html(q.get(f"option_{num}", ""))
                    if clean:
                        label = (opt_labels[num - 1]
                                 if num - 1 < len(opt_labels) else str(num))
                        options.append((label, clean))

                # Fallback: options as list/dict
                if not options:
                    for oi, opt in enumerate(q.get("options") or []):
                        t = (strip_html(opt.get("text") or opt.get("option") or "")
                             if isinstance(opt, dict) else strip_html(str(opt)))
                        if t:
                            label = (opt_labels[oi]
                                     if oi < len(opt_labels) else str(oi + 1))
                            options.append((label, t))

                # ── Resolve correct answer ────────────────────────────────
                answer_raw = str(
                    q.get("answer") or q.get("correct_answer")
                    or q.get("correct_option") or q.get("correct") or ""
                ).strip()

                correct_label = correct_text = ""
                try:
                    ans_idx = int(answer_raw) - 1   # "3" → index 2
                    if 0 <= ans_idx < len(options):
                        correct_label, correct_text = options[ans_idx]
                except (ValueError, TypeError):
                    for lbl, txt in options:
                        if lbl == answer_raw.upper():
                            correct_label, correct_text = lbl, txt
                            break

                # ── Write options ─────────────────────────────────────────
                for lbl, txt in options:
                    marker = " ✓" if lbl == correct_label else ""
                    f.write(f"   {lbl}) {txt}{marker}\n")

                if correct_text:
                    f.write(f"\n   ✓ Answer: {correct_label}) {correct_text}\n")
                elif answer_raw:
                    f.write(f"\n   ✓ Answer: Option {answer_raw}\n")

                # ── Explanation ───────────────────────────────────────────
                exp = strip_html(
                    q.get("solution_text") or q.get("explanation")
                    or q.get("solution")   or ""
                )
                if exp:
                    f.write(f"   💡 {exp}\n")

                # ── Scoring ───────────────────────────────────────────────
                cs = q.get("correct_score", "")
                ns = q.get("negative_score", "")
                if cs or ns:
                    f.write(f"   [+{cs} / -{ns}]\n")

                f.write("\n")

        return out
    except Exception as e:
        log.error("Quiz save error: %s", e)
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  HTTP SESSION
# ══════════════════════════════════════════════════════════════════════════════

def _session(token: str = None, user_id: str = None) -> requests.Session:
    """Build a requests.Session with all AppX headers pre-loaded."""
    s = requests.Session()
    h = dict(APPX_HEADERS)
    if token:   h["authorization"] = token      # raw JWT, no "Bearer" prefix
    if user_id: h["user-id"]       = str(user_id)
    s.headers.update(h)
    return s


def _uid(token: str, user_id: str = None) -> str:
    """Return user_id; auto-extract from JWT when not supplied."""
    if user_id:
        return str(user_id)
    return extract_user_id_from_jwt(token)


def _api_get(s: requests.Session, url: str, params: dict = None,
             retries: int = 3) -> dict:
    """
    GET with retry + 429 back-off, mirrors api_get() from 34568.py.
    Returns parsed JSON dict or {} on failure.
    """
    for attempt in range(retries):
        time.sleep(0.3)                 # same polite delay as 34568.py
        try:
            r = s.get(url, params=params, timeout=30)
            if r.status_code == 429:
                wait = 15 * (attempt + 1)
                log.warning("Rate limited — waiting %ds", wait)
                time.sleep(wait)
                continue
            if r.status_code != 200:
                log.debug("HTTP %d for %s", r.status_code, url)
                return {}
            return r.json()
        except Exception as e:
            if attempt == retries - 1:
                log.error("Request error (%s): %s", url, e)
            time.sleep(2)
    return {}


# ══════════════════════════════════════════════════════════════════════════════
#  JWT HELPER
# ══════════════════════════════════════════════════════════════════════════════

def extract_user_id_from_jwt(token: str) -> str:
    """Decode the JWT payload and return the user ID field."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return ""
        payload_b64  = parts[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode(payload_b64).decode("utf-8"))
        uid = (payload.get("id") or payload.get("userid")
               or payload.get("user_id") or "")
        return str(uid)
    except Exception:
        return ""


# ══════════════════════════════════════════════════════════════════════════════
#  LOGIN
# ══════════════════════════════════════════════════════════════════════════════

def login_with_token(domain: str, jwt_token: str) -> dict:
    """Return a profile dict when the caller supplies their own JWT."""
    user_id = extract_user_id_from_jwt(jwt_token)
    return {
        "token":   jwt_token,
        "user_id": user_id,
        "domain":  domain,
        "name":    f"User #{user_id}" if user_id else "User",
    }


def login_with_credentials(domain: str, email: str, password: str) -> dict:
    """
    Login with email/phone + password.
    Uses /post/userLogin with form-encoded body — HAR-verified endpoint.
    Tries two variants for cross-platform support.

    Returns: { token, user_id, domain, name }
    Raises ValueError on failure.
    """
    base = f"https://{domain}" if not domain.startswith("http") else domain

    login_headers = {
        **APPX_HEADERS,
        "content-type": "application/x-www-form-urlencoded",
    }
    payload = {
        "email":       email,
        "phone":       email,           # some platforms key on "phone"
        "password":    password,
        "devicetoken": f"python_{int(time.time())}",
        "mydeviceid":  "python_client",
        "source":      "website",
        "extra_details": "1",
    }

    endpoints = [
        f"{base}/post/userLogin",
        f"{base}/post/userLogin?extra_details=0",
    ]

    last_msg = "Unknown error"
    for url in endpoints:
        try:
            resp = requests.post(url, data=payload,
                                 headers=login_headers, timeout=30)
            resp.raise_for_status()
            r = resp.json()
            if r.get("status") == 200:
                d = r["data"]
                return {
                    "token":   d["token"],
                    "user_id": str(d.get("userid", "")),
                    "domain":  domain,
                    "name":    d.get("name", email),
                }
            last_msg = r.get("message") or last_msg
        except requests.HTTPError as e:
            last_msg = str(e)
        except Exception as e:
            last_msg = str(e)

    raise ValueError(f"Login failed: {last_msg}")


# ══════════════════════════════════════════════════════════════════════════════
#  COURSE / CONTENT API
# ══════════════════════════════════════════════════════════════════════════════

def get_courses(domain: str, token: str, user_id: str = None) -> list:
    """
    Purchased courses for this user.
    Tries /get/mycoursev2 first, falls back to /get/get_all_purchases.
    user_id is optional — auto-extracted from the JWT when omitted.
    """
    base    = f"https://{domain}" if not domain.startswith("http") else domain
    user_id = _uid(token, user_id)
    s       = _session(token, user_id)

    r = _api_get(s, f"{base}/get/mycoursev2",
                 params={"userid": user_id, "type": "all"})
    if r.get("data"):
        return r["data"]

    r2 = _api_get(s, f"{base}/get/get_all_purchases",
                  params={"userid": user_id, "item_type": "10"})
    courses = []
    for item in r2.get("data", []):
        for ct in item.get("coursedt", []):
            courses.append(ct)
    return courses


def get_subjects(domain: str, token: str, course_id: str,
                 user_id: str = None) -> list:
    base    = f"https://{domain}" if not domain.startswith("http") else domain
    user_id = _uid(token, user_id)
    s       = _session(token, user_id)
    r    = _api_get(s, f"{base}/get/allsubjectfrmlivecourseclass",
                    params={"courseid": course_id, "start": -1})
    return r.get("data", [])


def get_topics(domain: str, token: str, course_id: str, subject_id: str,
               user_id: str = None) -> list:
    base    = f"https://{domain}" if not domain.startswith("http") else domain
    user_id = _uid(token, user_id)
    s       = _session(token, user_id)
    r     = _api_get(s, f"{base}/get/alltopicfrmlivecourseclass",
                     params={"courseid": course_id, "subjectid": subject_id,
                             "start": -1})
    items = r.get("data", [])
    return sorted(items, key=lambda x: x.get("topicid", 0))


def get_lectures(domain: str, token: str, course_id: str,
                 subject_id: str, topic_id: str,
                 user_id: str = None) -> list:
    base    = f"https://{domain}" if not domain.startswith("http") else domain
    user_id = _uid(token, user_id)
    s       = _session(token, user_id)
    r     = _api_get(s,
                     f"{base}/get/livecourseclassbycoursesubtopconceptapiv3",
                     params={"courseid":  course_id,
                             "subjectid": subject_id,
                             "topicid":   topic_id,
                             "conceptid": "",
                             "start":     -1})
    items = r.get("data", [])
    return sorted(items, key=lambda x: x.get("id", 0))


def get_folder_contents(domain: str, token: str, course_id: str,
                        parent_id, user_id: str = None) -> list:
    base    = f"https://{domain}" if not domain.startswith("http") else domain
    user_id = _uid(token, user_id)
    s       = _session(token, user_id)
    r    = _api_get(s, f"{base}/get/folder_contentsv2",
                    params={"course_id": course_id, "parent_id": parent_id})
    return r.get("data", [])


def get_video_details(domain: str, token: str, course_id: str,
                      video_id: str, user_id: str = None) -> dict:
    """
    Full video detail: encrypted_links[], download_links[], pdf_link, etc.
    Endpoint: /get/fetchVideoDetailsById  (HAR-verified from 34568.py)
    """
    base    = f"https://{domain}" if not domain.startswith("http") else domain
    user_id = _uid(token, user_id)
    s       = _session(token, user_id)
    r    = _api_get(s, f"{base}/get/fetchVideoDetailsById",
                    params={"course_id":        course_id,
                            "video_id":         video_id,
                            "ytflag":           0,
                            "folder_wise_course": 0})
    return r.get("data", {})


# ══════════════════════════════════════════════════════════════════════════════
#  QUIZ API
# ══════════════════════════════════════════════════════════════════════════════

def get_quiz_questions(domain: str, token: str, quiz_id: str,
                       user_id: str = None) -> list:
    """
    Fetch quiz questions for a lecture quiz.
    Tries 4 endpoint variants — same fallback chain as fetch_quiz() in 34568.py.

    Primary (HAR-confirmed): /get/quizquestion?start=-1&quizid=<id>
    """
    base    = f"https://{domain}" if not domain.startswith("http") else domain
    user_id = _uid(token, user_id)
    s       = _session(token, user_id)

    endpoints = [
        f"{base}/get/quizquestion?start=-1&quizid={quiz_id}",
        f"{base}/get/quiztitlebyid?quiz_title_id={quiz_id}&userid={user_id}",
        f"{base}/get/quiztitlebyid?quiztitleid={quiz_id}&userid={user_id}",
        f"{base}/get/quizbyid?quiz_id={quiz_id}&userid={user_id}",
    ]
    for url in endpoints:
        try:
            r    = _api_get(s, url)
            data = r.get("data") or []
            if isinstance(data, list) and data:
                return data
            if isinstance(data, dict):
                for key in ("questions", "question_list",
                            "quiz_questions", "data"):
                    nested = data.get(key)
                    if isinstance(nested, list) and nested:
                        return nested
        except Exception as e:
            log.debug("Quiz endpoint %s failed: %s", url, e)

    return []


# ══════════════════════════════════════════════════════════════════════════════
#  TEST SERIES API
# ══════════════════════════════════════════════════════════════════════════════

def get_test_series(domain: str, token: str, course_id: str,
                    user_id: str = None) -> list:
    """
    /get/test_seriesbycourseid — all test series inside a batch/course.
    """
    base    = f"https://{domain}" if not domain.startswith("http") else domain
    user_id = _uid(token, user_id)
    s       = _session(token, user_id)
    r       = _api_get(s, f"{base}/get/test_seriesbycourseid",
                    params={"start":              -1,
                            "userid":             user_id,
                            "courseid":           course_id,
                            "folder_wise_course": 0})
    data = r.get("data") or r.get("result") or []
    return data if isinstance(data, list) else []


def get_testseries_subjects(domain: str, token: str, series_id: str,
                             user_id: str = None) -> list:
    """Subjects/topics inside a test series."""
    base    = f"https://{domain}" if not domain.startswith("http") else domain
    user_id = _uid(token, user_id)
    s       = _session(token, user_id)
    r       = _api_get(s, f"{base}/get/testseries_subjects",
                    params={"testseries_id": series_id})
    data = r.get("data") or r.get("subjects") or r.get("result") or []
    return data if isinstance(data, list) else []


def get_testseries_tests(domain: str, token: str, series_id: str,
                          subject_id: str, user_id: str = None) -> list:
    """
    All test titles inside one subject of a test series.
    Auto-paginates using the same logic as 34568.py.
    """
    base      = f"https://{domain}" if not domain.startswith("http") else domain
    user_id   = _uid(token, user_id)
    s         = _session(token, user_id)
    all_tests = []
    start     = 0

    while True:
        r     = _api_get(s, f"{base}/get/test_titlev2",
                         params={"testseriesid": series_id,
                                 "subject_id":   subject_id,
                                 "chapter_id":   -1,
                                 "userid":       0,
                                 "search":       "",
                                 "start":        start})
        items = (r.get("data") or r.get("tests")
                 or r.get("test_titles") or r.get("result") or [])
        if not isinstance(items, list) or not items:
            break
        all_tests.extend(items)
        if len(items) < 10:
            break
        start += len(items)
        time.sleep(0.3)

    return all_tests


def get_testseries_questions(domain: str, token: str, test: dict,
                              user_id: str = None) -> list:
    """
    Fetch questions for one test-series test.

    Mirrors fetch_testseries_questions() from 34568.py:
      1. Try test_questions_url (CDN JSON) — fast path.
      2. Fallback to /get/quizquestion with the test id.

    `test` is a raw test dict from get_testseries_tests().
    """
    # 1. CDN path — test_questions_url points to a pre-built JSON file
    cdn_url = test.get("test_questions_url") or ""
    if cdn_url:
        try:
            resp = requests.get(cdn_url, timeout=30,
                                headers={"User-Agent": "okhttp/5.3.2"})
            if resp.status_code == 200:
                data = resp.json()
                qs   = (data if isinstance(data, list)
                        else data.get("questions") or data.get("data") or [])
                if qs:
                    return qs
        except Exception as e:
            log.debug("CDN questions fetch failed: %s", e)

    # 2. Fallback: same /get/quizquestion endpoint used by lecture quizzes
    test_id = str(test.get("id") or test.get("test_id") or "")
    if test_id:
        return get_quiz_questions(domain, token, test_id, user_id)

    return []


# ══════════════════════════════════════════════════════════════════════════════
#  COLLECT ALL LECTURES  (folder-based + subject/topic-based detection)
# ══════════════════════════════════════════════════════════════════════════════

def _collect_folder_recursive(domain: str, token: str, course_id: str,
                               parent_id, out: list,
                               user_id: str = None) -> None:
    """Recursively walk folder_contentsv2 and collect non-folder items."""
    items = get_folder_contents(domain, token, course_id, parent_id, user_id)
    for item in items:
        if (item.get("material_type") or "").upper() == "FOLDER":
            _collect_folder_recursive(
                domain, token, course_id, item["id"], out, user_id)
        else:
            out.append(item)
        time.sleep(0.1)


def collect_all_lectures(domain: str, token: str, course_id: str,
                          user_id: str = None,
                          subject_ids: list = None,
                          topic_ids: list = None) -> list:
    """
    Collect all lecture items for a course.

    Mirrors collect_all_lectures() logic from 34568.py:
      - If /get/folder_contentsv2 returns data → folder-based course,
        walk recursively.
      - Otherwise → subject → topic → lectures hierarchy.

    subject_ids / topic_ids: optional filter lists (by id string/int).
    If None, all subjects/topics are included.

    Returns a flat list of lecture item dicts, each annotated with
    "_subject" and "_topic" keys.
    """
    uid        = _uid(token, user_id)
    lectures: list = []

    # ── Folder-based course detection ──────────────────────────────────
    folder_data = get_folder_contents(domain, token, course_id, -1, uid)
    if folder_data:
        log.info("Folder-based course detected — walking recursively")
        _collect_folder_recursive(domain, token, course_id, -1, lectures, uid)
        return lectures

    # ── Subject → Topic → Lectures hierarchy ───────────────────────────
    subjects = get_subjects(domain, token, course_id, uid)
    if subject_ids is not None:
        sid_set  = {str(x) for x in subject_ids}
        subjects = [s for s in subjects
                    if str(s.get("subjectid", "")) in sid_set]

    for sub in subjects:
        si    = sub.get("subjectid")
        sname = sub.get("subject_name", "")
        time.sleep(0.3)

        topics = get_topics(domain, token, course_id, si, uid)
        if topic_ids is not None:
            tid_set = {str(x) for x in topic_ids}
            topics  = [t for t in topics
                       if str(t.get("topicid", "")) in tid_set]

        for topic in topics:
            ti   = topic.get("topicid")
            tn   = topic.get("topic_name", "")
            time.sleep(0.3)

            lecs = get_lectures(domain, token, course_id, si, ti, uid)
            log.info("  %s / %s: %d lectures", sname, tn, len(lecs))
            for lec in lecs:
                lec["_subject"] = sname
                lec["_topic"]   = tn
            lectures.extend(lecs)

    return lectures


# ══════════════════════════════════════════════════════════════════════════════
#  HIGH-LEVEL RESOLVER
# ══════════════════════════════════════════════════════════════════════════════

def resolve_lecture(domain: str, token: str, course_id: str, video_id: str,
                    user_id: str = None,
                    prefer_quality: str = "720p") -> dict:
    """
    Fetch full video details and return everything the downloader needs.

    Return dict keys:
        title          : str
        url            : str   — primary video URL (non-DRM, best quality)
        vtype          : str   — "mp4" | "hls" | "youtube" | "pdf" | None
        xor_key        : str | None
        class_pdfs     : [(suffix, url), …]
        all_candidates : [(quality_label, url), …]   — ordered fallback list
        drm            : bool  — True if only DRM URLs found
        raw            : dict  — raw API data{} for debugging
    """
    d = get_video_details(domain, token, user_id, course_id, video_id)
    if not d:
        return {"title": None, "url": None, "vtype": None, "xor_key": None,
                "class_pdfs": [], "all_candidates": [], "drm": False, "raw": {}}

    title          = d.get("Title") or f"video_{video_id}"
    class_pdfs     = extract_class_pdfs(d)
    all_candidates = decrypt_all_links(d)

    # 1. YouTube encrypted video_id
    yt_enc = d.get("video_id", "")
    if yt_enc:
        yt_id = decrypt_url(yt_enc)
        if yt_id and not yt_id.startswith("http"):
            return {"title": title,
                    "url": f"https://youtu.be/{yt_id}",
                    "vtype": "youtube", "xor_key": None,
                    "class_pdfs": class_pdfs,
                    "all_candidates": all_candidates,
                    "drm": False, "raw": d}

    # 2. YouTube embed URL (ytFlag == "1")
    if str(d.get("ytFlag", "")) == "1" and d.get("embed_url"):
        return {"title": title,
                "url": d["embed_url"],
                "vtype": "youtube", "xor_key": None,
                "class_pdfs": class_pdfs,
                "all_candidates": all_candidates,
                "drm": False, "raw": d}

    # 3. Best non-DRM quality from encrypted_links[] / download_links[]
    prefer_order = [prefer_quality, "1080p", "720p", "480p",
                    "360p", "240p", "144p", "file_link", "download_link"]
    by_quality: dict = {}
    all_drm = True
    for qlabel, url in all_candidates:
        if url and ".pdf" not in url:
            if _is_drm_url(url):
                continue
            all_drm = False
            if qlabel not in by_quality:
                by_quality[qlabel] = url

    chosen_url = None
    for q in prefer_order:
        if q in by_quality:
            chosen_url = by_quality[q]
            break
    if not chosen_url and by_quality:
        chosen_url = next(iter(by_quality.values()))

    if chosen_url:
        vtype = "mp4" if _is_mp4_url(chosen_url) else "hls"
        return {"title": title, "url": chosen_url,
                "vtype": vtype, "xor_key": None,
                "class_pdfs": class_pdfs,
                "all_candidates": all_candidates,
                "drm": False, "raw": d}

    # 4. Legacy encrypted MKV with xor_key
    quality_rank = {"1080p": 0, "720p": 1, "480p": 2, "360p": 3, "240p": 4}
    enc_links = sorted(
        d.get("encrypted_links", []),
        key=lambda x: quality_rank.get(str(x.get("quality", "")), 99)
    )
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
        return {"title": title, "url": video_url,
                "vtype": vtype, "xor_key": xor_key or None,
                "class_pdfs": class_pdfs,
                "all_candidates": all_candidates,
                "drm": False, "raw": d}

    # 5. PDF-only item
    if class_pdfs:
        primary_pdf = class_pdfs[0][1]
        extra_pdfs  = class_pdfs[1:]
        return {"title": title, "url": primary_pdf,
                "vtype": "pdf", "xor_key": None,
                "class_pdfs": extra_pdfs,
                "all_candidates": all_candidates,
                "drm": all_drm and bool(all_candidates), "raw": d}

    return {"title": title, "url": None, "vtype": None, "xor_key": None,
            "class_pdfs": [], "all_candidates": all_candidates,
            "drm": all_drm and bool(all_candidates), "raw": d}
