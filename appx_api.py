"""
appx_api.py — AppX / Parmar Academy API client.
Handles login, course listing, content fetching, URL decryption.
Ported from 34568.py v3.8.
"""

import os
import re
import json
import base64
import struct
import hashlib
import asyncio
import logging
import requests

log = logging.getLogger(__name__)

# ── AES / XOR constants ───────────────────────────────────────────────────────
AES_KEY     = b'638udh3829162018'
FALLBACK_IV = b'fedcba9876543210'

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
    AES_OK = True
except ImportError:
    AES_OK = False
    log.warning("pycryptodome not installed — encrypted URLs will not be decrypted")


def decrypt_url(encrypted_text: str) -> str:
    """Decrypt an AES-CBC encrypted AppX URL."""
    if not AES_OK:
        return encrypted_text
    try:
        data = base64.b64decode(encrypted_text)
        if len(data) < 16:
            return encrypted_text
        iv        = data[:16]
        ct        = data[16:]
        cipher    = AES.new(AES_KEY, AES.MODE_CBC, iv)
        plaintext = unpad(cipher.decrypt(ct), AES.block_size)
        return plaintext.decode("utf-8")
    except Exception:
        try:
            iv        = FALLBACK_IV
            ct        = base64.b64decode(encrypted_text)
            cipher    = AES.new(AES_KEY, AES.MODE_CBC, iv)
            plaintext = unpad(cipher.decrypt(ct), AES.block_size)
            return plaintext.decode("utf-8")
        except Exception:
            return encrypted_text


def xor_decrypt(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def decrypt_all_links(items: list) -> list:
    """Walk a list of content dicts and decrypt any encrypted video URLs."""
    for item in items:
        for field in ("videoUrl", "videoUrl1", "pdfUrl", "url"):
            val = item.get(field, "")
            if val and not val.startswith("http"):
                item[field] = decrypt_url(val)
    return items


# ── HTTP session ─────────────────────────────────────────────────────────────

def _session(token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "okhttp/4.9.1",
    })
    return s


# ── Login ─────────────────────────────────────────────────────────────────────

def login_with_token(domain: str, jwt_token: str) -> dict:
    """Return a minimal profile dict when user supplies their own JWT."""
    return {"token": jwt_token, "domain": domain, "name": "User"}


def login_with_credentials(domain: str, email: str, password: str) -> dict:
    url  = f"https://{domain}/api/v2/auth/login"
    resp = requests.post(url, json={"email": email, "password": password}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    token = data.get("data", {}).get("token") or data.get("token")
    if not token:
        raise ValueError(f"Login failed: {data.get('message', 'unknown error')}")
    return {"token": token, "domain": domain, "name": data.get("data", {}).get("name", "User")}


# ── Course / content API ──────────────────────────────────────────────────────

def get_courses(domain: str, token: str) -> list:
    url  = f"https://{domain}/api/v1/get/mycoursev2"
    resp = _session(token).get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", [])


def get_subjects(domain: str, token: str, course_id: str) -> list:
    url  = f"https://{domain}/api/v1/get/allsubjectfrmlivecourseclass"
    resp = _session(token).get(url, params={"courseId": course_id}, timeout=30)
    resp.raise_for_status()
    return resp.json().get("data", [])


def get_topics(domain: str, token: str, course_id: str, subject_id: str) -> list:
    url  = f"https://{domain}/api/v1/get/alltopicfrmlivecourseclass"
    resp = _session(token).get(url, params={"courseId": course_id, "subjectId": subject_id}, timeout=30)
    resp.raise_for_status()
    return resp.json().get("data", [])


def get_lessons(domain: str, token: str, course_id: str, subject_id: str, topic_id: str) -> list:
    url  = f"https://{domain}/api/v1/get/livecourseclassbycoursesubtopconceptapiv3"
    resp = _session(token).get(
        url,
        params={"courseId": course_id, "subjectId": subject_id, "topicId": topic_id},
        timeout=30,
    )
    resp.raise_for_status()
    items = resp.json().get("data", [])
    return decrypt_all_links(items)


def get_video_details(domain: str, token: str, video_id: str) -> dict:
    url  = f"https://{domain}/api/v1/get/fetchVideoDetailsById"
    resp = _session(token).get(url, params={"videoId": video_id}, timeout=30)
    resp.raise_for_status()
    data = resp.json().get("data", {})
    for field in ("videoUrl", "videoUrl1", "url"):
        if data.get(field) and not data[field].startswith("http"):
            data[field] = decrypt_url(data[field])
    return data


def get_quiz(domain: str, token: str, topic_id: str) -> list:
    url  = f"https://{domain}/api/v1/get/quizquestion"
    resp = _session(token).get(url, params={"topicId": topic_id}, timeout=30)
    resp.raise_for_status()
    return resp.json().get("data", [])


def get_test_series(domain: str, token: str, course_id: str) -> list:
    url  = f"https://{domain}/api/v1/get/test_seriesbycourseid"
    resp = _session(token).get(url, params={"courseId": course_id}, timeout=30)
    resp.raise_for_status()
    return resp.json().get("data", [])


def resolve_video_url(item: dict) -> str:
    """Pick the best video URL from a lesson item dict."""
    for field in ("videoUrl", "videoUrl1", "url"):
        val = item.get(field, "")
        if val and val.startswith("http"):
            return val
    return ""
