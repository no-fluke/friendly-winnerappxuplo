"""
downloader.py — Download engine, mirrors termux script exactly.

Priority per type (same as termux):
  Direct MP4:    aria2c(16x) → yt-dlp+aria2c → yt-dlp → aiohttp → wget → curl
  HLS m3u8:      yt-dlp → ffmpeg → hls_fragment_download
  Direct stream: aiohttp → yt-dlp → ffmpeg
  YouTube:       yt-dlp
  PDF:           aiohttp → aria2c → curl
  XOR MKV:       aiohttp/yt-dlp → xor_decrypt_file (first 28 bytes)
"""

import os
import re
import mmap
import struct
import asyncio
import logging
import tempfile
import shutil
import aiohttp
import aiofiles

log = logging.getLogger(__name__)

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

# ── Tool detection (identical to termux) ─────────────────────────────────────

def _find_tool(*candidates) -> str | None:
    for name in candidates:
        found = shutil.which(name)
        if found:
            return found
    return None

_YTDLP  = _find_tool("yt-dlp", "yt_dlp")
_FFMPEG = _find_tool("ffmpeg")
_ARIA2C = _find_tool("aria2c")


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _run_cmd(*args) -> int:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return await proc.wait()
    except FileNotFoundError:
        return -1


def xor_decrypt_file(path: str, key: str) -> bool:
    """XOR-decrypt first 28 bytes — AppX encrypted MKV only."""
    try:
        size = os.path.getsize(path)
        if size == 0:
            return False
        n = min(28, size)
        with open(path, "r+b") as f:
            with mmap.mmap(f.fileno(), length=n, access=mmap.ACCESS_WRITE) as mm:
                for i in range(n):
                    mm[i] ^= ord(key[i % len(key)])
        return True
    except Exception as e:
        log.error("XOR error: %s", e)
        return False


def _is_mp4_url(url: str) -> bool:
    lower = url.lower()
    if "static-db.appx.co.in" in lower:         return True
    if "appx-content-v2.classx.co.in" in lower: return True
    return lower.split("?")[0].endswith(".mp4")


def _mp4_headers(url: str) -> dict:
    from appx_api import APPX_HEADERS
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


def is_direct_video_url(url: str) -> bool:
    lower = url.lower()
    return (
        ".m3u8" not in lower and (
            "encrypted-" in lower or "/encrypted" in lower
            or any(h in lower for h in [
                "static-trans-v2.appx.co.in",
                "static-trans.appx.co.in",
                "cdn.appx.co.in",
                "akamai.net.in",
                "static-wsb.appx.co.in",
                "static-db-v2.appx.co.in",
            ])
        )
    )


# ── Individual download methods ───────────────────────────────────────────────

async def _aria2c_download(url: str, out_path: str,
                            extra_headers: dict = None) -> bool:
    if not _ARIA2C:
        return False
    hdr_args = []
    for k, v in (extra_headers or {}).items():
        hdr_args += ["--header", f"{k}: {v}"]
    rc = await _run_cmd(
        _ARIA2C,
        "--no-conf", "--allow-overwrite=true", "--auto-file-renaming=false",
        "-x", "16", "-j", "1", "-k", "1M",
        "--connect-timeout=10", "--timeout=60", "--retry-wait=3", "-m", "5",
        "--console-log-level=warn", "--summary-interval=5",
        *hdr_args,
        "-o", os.path.basename(out_path),
        "-d", os.path.dirname(out_path) or ".",
        url,
    )
    return rc == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 1024


async def _ytdlp_download(url: str, out_path: str,
                           quality: str = "720p",
                           extra_headers: dict = None) -> bool:
    if not _YTDLP:
        return False
    hdr_args = []
    for k, v in (extra_headers or {}).items():
        hdr_args += ["--add-header", f"{k}:{v}"]

    base_args = [
        _YTDLP,
        "--no-check-certificate",
        "-R", "5",
        "--fragment-retries", "10",
        "--hls-use-mpegts",
        "-o", f"{out_path}.%(ext)s",
        *hdr_args,
    ]

    # Try with aria2c first
    if _ARIA2C:
        rc = await _run_cmd(
            *base_args,
            "--external-downloader", "aria2c",
            "--downloader-args", "aria2c:-x 16 -j 32 -k 1M",
            url,
        )
        for ext in ("mp4", "mkv", "webm", "ts", "m4v"):
            c = f"{out_path}.{ext}"
            if os.path.exists(c) and os.path.getsize(c) > 1024:
                if c != out_path:
                    os.rename(c, out_path)
                return True

    # Plain yt-dlp
    rc = await _run_cmd(*base_args, url)
    for ext in ("mp4", "mkv", "webm", "ts", "m4v"):
        c = f"{out_path}.{ext}"
        if os.path.exists(c) and os.path.getsize(c) > 1024:
            if c != out_path:
                os.rename(c, out_path)
            return True
    return False


async def _ffmpeg_download(url: str, out_path: str) -> bool:
    if not _FFMPEG:
        return False
    referer = CDN_HEADERS["Referer"]
    rc = await _run_cmd(
        _FFMPEG, "-y",
        "-headers", f"Referer: {referer}\r\nOrigin: https://player-strict.akamai.net.in\r\n",
        "-allowed_extensions", "ALL",
        "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
        "-i", url,
        "-c", "copy",
        out_path,
    )
    return rc == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 1024


async def _aiohttp_download(url: str, out_path: str,
                             headers: dict = None,
                             progress_cb=None) -> bool:
    hdrs = {**CDN_HEADERS, **(headers or {})}
    try:
        connector = aiohttp.TCPConnector(ssl=False, limit=4,
                                          keepalive_timeout=30,
                                          enable_cleanup_closed=True)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(
                    url, headers=hdrs,
                    timeout=aiohttp.ClientTimeout(
                        total=3600, connect=15, sock_read=60)) as resp:
                if resp.status == 404:
                    log.warning("404 — expired URL: %s", url[:80])
                    return False
                if resp.status not in (200, 206):
                    log.warning("HTTP %d for %s", resp.status, url[:80])
                    return False
                total_size = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                async with aiofiles.open(out_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(65536):
                        await f.write(chunk)
                        downloaded += len(chunk)
                        if progress_cb:
                            await progress_cb(downloaded, total_size)
        return os.path.exists(out_path) and os.path.getsize(out_path) > 1024
    except Exception as e:
        log.warning("aiohttp download failed: %s", e)
        return False


# ── PDF download ──────────────────────────────────────────────────────────────

async def download_pdf(url: str, out_path: str) -> str | None:
    """Download a PDF. Returns local path or None."""
    if not url:
        return None
    # aiohttp
    if await _aiohttp_download(url, out_path, headers=PDF_HEADERS):
        if os.path.getsize(out_path) > 512:
            return out_path
    # aria2c
    if await _aria2c_download(url, out_path, extra_headers=PDF_HEADERS):
        return out_path
    # curl
    proc = await asyncio.create_subprocess_shell(
        f'curl -sL --max-time 120 --no-check-certificate '
        f'-H "Referer: {PDF_HEADERS["Referer"]}" '
        f'-H "User-Agent: {PDF_HEADERS["User-Agent"]}" '
        f'-H "Accept: application/pdf,*/*" '
        f'-o "{out_path}" "{url}"',
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    if os.path.exists(out_path) and os.path.getsize(out_path) > 512:
        return out_path
    return None


# ── Single URL download (mirrors termux _download_single_url) ─────────────────

async def download_video(url: str, out_path: str,
                         vtype: str = None,
                         xor_key: str = None,
                         quality: str = "720p",
                         progress_cb=None) -> str | None:
    """Download one video URL. Returns saved file path or None."""
    from appx_api import _is_drm_url

    if _is_drm_url(url):
        log.warning("Skipping DRM URL: %s", url[:80])
        return None

    # ── Direct MP4 (aria2c → yt-dlp → aiohttp → wget → curl) ────────────────
    if _is_mp4_url(url):
        mp4_hdrs = _mp4_headers(url)
        out      = f"{out_path}.mp4"

        if await _aria2c_download(url, out, extra_headers=mp4_hdrs):
            return out
        if await _ytdlp_download(url, out, quality, extra_headers=mp4_hdrs):
            return out
        if await _aiohttp_download(url, out, headers=mp4_hdrs, progress_cb=progress_cb):
            return out
        # wget
        hdr_str = " ".join(f'--header="{k}: {v}"' for k, v in mp4_hdrs.items())
        proc = await asyncio.create_subprocess_shell(
            f'wget --no-check-certificate {hdr_str} -O "{out}" "{url}"',
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
        if os.path.exists(out) and os.path.getsize(out) > 1024:
            return out
        # curl
        curl_hdr = " ".join(f'-H "{k}: {v}"' for k, v in mp4_hdrs.items())
        proc = await asyncio.create_subprocess_shell(
            f'curl -L --no-progress-meter --max-time 3600 '
            f'--no-check-certificate {curl_hdr} -o "{out}" "{url}"',
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
        if os.path.exists(out) and os.path.getsize(out) > 1024:
            return out
        return None

    # ── HLS / direct-stream ───────────────────────────────────────────────────
    is_m3u8    = ".m3u8" in url.lower()
    is_direct  = is_direct_video_url(url)
    is_enc_mkv = "encrypted.mkv" in url.lower()
    ext        = "mkv" if is_enc_mkv else "mp4"
    out        = f"{out_path}.{ext}"

    if is_direct:
        ok = await _aiohttp_download(url, out, progress_cb=progress_cb)
        if not ok:
            ok = await _ytdlp_download(url, out, quality, extra_headers={
                "Referer": CDN_HEADERS["Referer"],
                "Origin":  "https://player-strict.akamai.net.in",
            })
        if not ok:
            ok = await _ffmpeg_download(url, out)
        if ok and xor_key and is_enc_mkv:
            xor_decrypt_file(out, xor_key)
        return out if ok else None

    if is_m3u8:
        ok = await _ytdlp_download(url, out, quality, extra_headers={
            "Referer": CDN_HEADERS["Referer"],
            "Origin":  "https://player-strict.akamai.net.in",
        })
        if not ok:
            ok = await _ffmpeg_download(url, out)
        if not ok:
            result = await hls_fragment_download(url, out_path)
            return result
        return out if ok else None

    # Generic / YouTube
    ok = await _ytdlp_download(url, out, quality, extra_headers={
        "Referer": CDN_HEADERS["Referer"],
        "Origin":  "https://player-strict.akamai.net.in",
    })
    if not ok:
        ok = await _aiohttp_download(url, out, progress_cb=progress_cb)
    if not ok:
        ok = await _ffmpeg_download(url, out)
    if ok and xor_key and is_enc_mkv:
        xor_decrypt_file(out, xor_key)
    return out if ok else None


# ── download_item (mirrors termux download_item exactly) ─────────────────────

async def download_item(title: str, url: str, xor_key: str, vtype: str,
                        out_path: str,
                        class_pdfs: list = None,
                        want_pdf: bool = True,
                        all_candidates: list = None,
                        quality: str = "720p",
                        progress_cb=None) -> bool:
    """
    Download one lecture item + attached class-note PDFs.
    Mirrors termux download_item() exactly.
    """
    from appx_api import _is_drm_url

    class_pdfs     = class_pdfs or []
    all_candidates = all_candidates or []
    result         = None

    if vtype == "youtube":
        out    = f"{out_path}.mp4"
        ok     = await _ytdlp_download(url, out, quality)
        result = out if ok else None

    elif vtype == "pdf":
        out    = f"{out_path}.pdf"
        result = await download_pdf(url, out)

    else:
        tried: set = set()

        async def _attempt(cand_url: str, cand_xor: str) -> str | None:
            if not cand_url or cand_url in tried:
                return None
            tried.add(cand_url)
            if _is_drm_url(cand_url):
                log.warning("Skipping DRM: %s", cand_url[:80])
                return None
            return await download_video(
                cand_url, out_path,
                vtype=vtype, xor_key=cand_xor,
                quality=quality, progress_cb=progress_cb,
            )

        # Primary URL
        if url:
            result = await _attempt(url, xor_key or "")

        # Fallback: quality-ordered candidates
        if not result and all_candidates:
            for qlabel, cand_url in all_candidates:
                if cand_url in tried:
                    continue
                if _is_drm_url(cand_url):
                    tried.add(cand_url)
                    continue
                result = await _attempt(cand_url, "")
                if result:
                    break

    ok = bool(result and os.path.exists(result))

    # Attached class-note PDFs (pdf_link / pdf_link2)
    if want_pdf and class_pdfs:
        for suffix, pdf_url in class_pdfs:
            if not pdf_url:
                continue
            pdf_out    = f"{out_path}{suffix}.pdf"
            pdf_result = await download_pdf(pdf_url, pdf_out)
            if pdf_result:
                log.info("Class PDF%s saved: %s", suffix, os.path.basename(pdf_result))

    return ok


# ── HLS fragment downloader (last-resort, identical to termux) ────────────────

async def hls_fragment_download(url: str, out_path: str) -> str | None:
    """Plain AES-128 HLS fragment downloader. No DRM. Last resort."""
    from urllib.parse import urljoin

    try:
        from Crypto.Cipher import AES as _AES
        from Crypto.Util.Padding import unpad as _unpad
        _AES_OK = True
    except ImportError:
        _AES_OK = False

    out_mp4 = f"{out_path}.mp4"
    tmp_dir = tempfile.mkdtemp(prefix="hls_frags_")

    async def fetch_bytes(session, fetch_url, retries=5):
        for attempt in range(retries):
            try:
                async with session.get(fetch_url, headers=CDN_HEADERS,
                                       timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status == 200:
                        return await resp.read()
                    if resp.status == 404:
                        return None
            except Exception:
                pass
            await asyncio.sleep(1.5 * (attempt + 1))
        return None

    def abs_url(base, ref):
        return ref if ref.startswith("http") else urljoin(base, ref)

    async def parse_manifest(session, manifest_url):
        data = await fetch_bytes(session, manifest_url)
        if not data:
            return manifest_url, [], "Failed to fetch manifest"
        text = data.decode("utf-8", errors="replace")

        target_url = manifest_url
        if "#EXT-X-STREAM-INF" in text:
            for line in text.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    target_url = abs_url(manifest_url, line)
                    break
            if target_url != manifest_url:
                vdata = await fetch_bytes(session, target_url)
                if not vdata:
                    return target_url, [], "Failed to fetch variant"
                text = vdata.decode("utf-8", errors="replace")

        key_cache: dict = {}
        fragments = []
        cur_key = cur_iv = None
        seq = 0

        for line in text.splitlines():
            line = line.strip()
            if line.startswith("#EXT-X-MEDIA-SEQUENCE"):
                try:
                    seq = int(line.split(":")[1])
                except Exception:
                    pass
                continue
            if line.startswith("#EXT-X-KEY"):
                attrs  = dict(re.findall(r'(\w+)=(".*?"|[^,]+)', line))
                method = attrs.get("METHOD", "NONE").strip('"')
                if method == "NONE":
                    cur_key = cur_iv = None
                elif method == "AES-128" and _AES_OK:
                    raw_uri = attrs.get("URI", "").strip('"')
                    key_url = abs_url(target_url, raw_uri)
                    iv_str  = attrs.get("IV", "").strip('"')
                    cur_iv  = (bytes.fromhex(iv_str[2:].zfill(32))
                               if iv_str.startswith(("0x", "0X")) else None)
                    if key_url not in key_cache:
                        kd = await fetch_bytes(session, key_url, retries=5)
                        if not kd or len(kd) != 16:
                            return target_url, [], f"Bad AES key ({len(kd) if kd else 0}B)"
                        key_cache[key_url] = kd
                    cur_key = key_cache[key_url]
                continue
            if line and not line.startswith("#"):
                fragments.append((seq, abs_url(target_url, line), cur_key, cur_iv))
                seq += 1

        return target_url, fragments, None

    import re as _re_mod
    connector = aiohttp.TCPConnector(ssl=False, limit=4)
    async with aiohttp.ClientSession(connector=connector) as session:
        _, fragments, err = await parse_manifest(session, url)
        if err or not fragments:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return None

        cat_path     = os.path.join(tmp_dir, "concat.ts")
        failed_frags = 0
        total        = len(fragments)

        async with aiofiles.open(cat_path, "wb") as cat_f:
            for idx, (frag_seq, frag_url, frag_key, frag_iv) in enumerate(fragments, 1):
                ts_data = await fetch_bytes(session, frag_url, retries=2)
                if ts_data is None:
                    _, fresh_frags, rerr = await parse_manifest(session, url)
                    if not rerr and fresh_frags:
                        new_map = {f[0]: f[1] for f in fresh_frags}
                        for j in range(idx - 1, len(fragments)):
                            s = fragments[j][0]
                            if s in new_map:
                                fragments[j] = (s, new_map[s], fragments[j][2], fragments[j][3])
                        frag_url = fragments[idx - 1][1]
                        ts_data  = await fetch_bytes(session, frag_url, retries=5)
                if not ts_data:
                    failed_frags += 1
                    continue
                if frag_key and _AES_OK:
                    iv = frag_iv if frag_iv is not None else \
                         (b'\x00' * 12) + struct.pack(">I", frag_seq)
                    cipher  = _AES.new(frag_key, _AES.MODE_CBC, iv)
                    ts_data = cipher.decrypt(ts_data)
                    if idx == total:
                        try:
                            ts_data = _unpad(ts_data, 16)
                        except Exception:
                            pass
                await cat_f.write(ts_data)

    if failed_frags == total:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None

    # Remux TS → MP4
    if _FFMPEG:
        proc = await asyncio.create_subprocess_exec(
            _FFMPEG, "-y", "-i", cat_path, "-c", "copy",
            "-movflags", "+faststart", out_mp4,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if (await proc.wait() == 0
                and os.path.exists(out_mp4)
                and os.path.getsize(out_mp4) > 1024):
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return out_mp4

    # Keep raw TS if ffmpeg unavailable
    ts_out = f"{out_path}.ts"
    shutil.move(cat_path, ts_out)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    if os.path.exists(ts_out) and os.path.getsize(ts_out) > 1024:
        return ts_out
    return None
