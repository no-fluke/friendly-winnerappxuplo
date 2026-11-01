"""
downloader.py — Download video/pdf/image content using aria2c + yt-dlp.
Primary: aria2c with 16 parallel connections.
Fallback: yt-dlp (for HLS / encrypted streams).
"""

import os
import re
import asyncio
import logging
import subprocess

log = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _safe_name(title: str, max_len: int = 60) -> str:
    """Sanitize a filename from a content title."""
    name = re.sub(r'[\\/:*?"<>|]', '', title)
    name = name.replace('(', '[').replace(')', ']')
    name = name.strip()
    return name[:max_len]


async def _run(cmd: str, cwd: str | None = None) -> tuple[int, str]:
    """Run a shell command asynchronously; return (returncode, combined output)."""
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
    )
    out, _ = await proc.communicate()
    return proc.returncode, out.decode(errors="replace")


# ── aria2c downloader (primary, for direct MP4/m3u8/PDF/images) ─────────────

async def aria2c_download(url: str, out_path: str) -> bool:
    """
    Download url to out_path using aria2c with 16 parallel connections.
    Returns True on success.
    """
    cmd = (
        f'aria2c -x 16 -j 1 -k 1M --allow-overwrite=true '
        f'--auto-file-renaming=false '
        f'--out="{os.path.basename(out_path)}" '
        f'--dir="{os.path.dirname(out_path) or "."}" '
        f'"{url}"'
    )
    log.info("aria2c: %s", url[:80])
    rc, output = await _run(cmd)
    if rc != 0:
        log.warning("aria2c failed (rc=%d): %s", rc, output[-300:])
        return False
    return os.path.exists(out_path)


# ── yt-dlp downloader (for HLS / YouTube / encrypted streams) ───────────────

async def ytdlp_download(url: str, out_path: str, quality: int = 720) -> bool:
    """
    Download url using yt-dlp. Tries to get up to `quality`p.
    For HLS uses --concurrent-fragments 16.
    Returns True on success.
    """
    base = os.path.splitext(out_path)[0]
    ext  = os.path.splitext(out_path)[1] or ".mp4"

    if "youtu" in url or "youtube.com" in url:
        fmt = f"bv*[height<={quality}][ext=mp4]+ba[ext=m4a]/b[height<=?{quality}]"
        cmd = (
            f'yt-dlp --extractor-args "youtube:player_client=android" '
            f'-f "{fmt}" -R 25 --fragment-retries 25 '
            f'-o "{base}{ext}" "{url}"'
        )
    elif ".m3u8" in url or "akamaized" in url or "fastly" in url:
        fmt = f"b[height<={quality}]/bv[height<={quality}]+ba/b/bv+ba"
        cmd = (
            f'yt-dlp -f "{fmt}" --concurrent-fragments 16 --no-part '
            f'-o "{base}{ext}" "{url}"'
        )
    else:
        fmt = f"b[height<={quality}]/bv[height<={quality}]+ba/b/bv+ba"
        cmd = f'yt-dlp -f "{fmt}" -R 25 --fragment-retries 25 -o "{base}{ext}" "{url}"'

    log.info("yt-dlp: %s", url[:80])
    rc, output = await _run(cmd)
    # yt-dlp may rename the output; search for it
    if rc != 0:
        log.warning("yt-dlp failed (rc=%d): %s", rc, output[-300:])
        return False
    # Find actual output file
    for candidate in [out_path, f"{base}.mp4", f"{base}.mkv", f"{base}.webm"]:
        if os.path.exists(candidate):
            if candidate != out_path:
                os.rename(candidate, out_path)
            return True
    return False


# ── Encrypted AppX stream (XOR/AES segments) ────────────────────────────────

async def download_encrypted(url: str, out_path: str, aes_key: str, quality: int = 720) -> bool:
    """
    Download an AppX encrypted HLS stream.
    url format: https://...encrypted.m3u8*<aes_key>
    """
    # Strip key from URL if bundled
    if '*' in url:
        url, aes_key = url.split('*', 1)

    # Use yt-dlp with the key passed via --key (N_m3u8DL-RE style not available on Heroku)
    # Fallback: download segments and decrypt with ffmpeg
    fmt = f"b[height<={quality}]/bv[height<={quality}]+ba/b/bv+ba"
    cmd = (
        f'yt-dlp -f "{fmt}" --concurrent-fragments 16 --no-part '
        f'--add-header "Authorization:Bearer {aes_key}" '
        f'-o "{out_path}" "{url}"'
    )
    rc, output = await _run(cmd)
    if rc == 0 and os.path.exists(out_path):
        return True

    # Direct ffmpeg attempt
    cmd2 = (
        f'ffmpeg -y -allowed_extensions ALL '
        f'-headers "Authorization: Bearer {aes_key}\\r\\n" '
        f'-i "{url}" -c copy "{out_path}"'
    )
    rc2, _ = await _run(cmd2)
    return rc2 == 0 and os.path.exists(out_path)


# ── Public dispatcher ────────────────────────────────────────────────────────

async def download(url: str, title: str, out_dir: str, quality: int = 720) -> str | None:
    """
    Main entry point. Returns the local file path on success, None on failure.
    Picks the best downloader automatically.
    """
    os.makedirs(out_dir, exist_ok=True)
    name = _safe_name(title)

    # Encrypted AppX
    if 'encrypted.m' in url or ('*' in url and '.m3u8' in url.split('*')[0]):
        aes_key = url.split('*')[1] if '*' in url else ""
        out = os.path.join(out_dir, f"{name}.mp4")
        ok  = await download_encrypted(url, out, aes_key, quality)
        return out if ok else None

    # PDF
    if url.endswith('.pdf') or '.pdf?' in url:
        out = os.path.join(out_dir, f"{name}.pdf")
        ok  = await aria2c_download(url, out)
        if not ok:
            rc, _ = await _run(f'yt-dlp -o "{out}" "{url}"')
            ok = os.path.exists(out)
        return out if ok else None

    # Images
    for ext in ('.jpg', '.jpeg', '.png', '.webp'):
        if ext in url.lower():
            out = os.path.join(out_dir, f"{name}{ext}")
            ok  = await aria2c_download(url, out)
            return out if ok else None

    # HLS / YouTube / generic video
    out = os.path.join(out_dir, f"{name}.mp4")

    # Try aria2c first for direct MP4 links
    if url.endswith('.mp4') and 'youtu' not in url:
        ok = await aria2c_download(url, out)
        if ok:
            return out

    # Fall back to yt-dlp for everything else
    ok = await ytdlp_download(url, out, quality)
    return out if ok else None
