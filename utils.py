"""
utils.py — Shared helpers.
"""

import re
import os
import shutil
import logging

log = logging.getLogger(__name__)


def progress_bar(current: int, total: int, width: int = 10) -> str:
    """Return a text progress bar string."""
    if total == 0:
        return "░" * width
    filled = int(width * current / total)
    bar    = "█" * filled + "░" * (width - filled)
    pct    = current / total * 100
    return f"{bar} {pct:.1f}%"


def clean_title(title: str) -> str:
    """Strip forbidden filename characters from a title."""
    return re.sub(r'[\\/:*?"<>|\t]+', '', title).strip()


def extract_topic_name(title: str) -> tuple[str, str]:
    """
    Extract topic name from title like '(TopicName) Video Title'.
    Returns (topic_name, clean_video_title).
    If no brackets, returns ('General', title).
    """
    m = re.search(r'^[\(\[\{]([^\)\]\}]+)[\)\]\}]\s*', title)
    if m:
        topic    = m.group(1).strip()
        vid_title = title[m.end():].strip()
        # Remove any trailing bracket groups too
        vid_title = re.sub(r'\s*[\(\[\{][^\)\]\}]+[\)\]\}]', '', vid_title).strip()
        if not vid_title:
            vid_title = topic
        return topic, vid_title
    return "General", title


def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)


def remove_file(path: str):
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:
        log.warning("remove_file %s: %s", path, e)


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def make_progress_text(
    count: int, total: int, title: str, b_name: str,
    topic: str | None, credit: str
) -> str:
    remaining = total - count
    pct       = count / total * 100 if total else 0
    bar       = progress_bar(count, total)
    t_line    = f"\n┣🏷️ **Topic »** `{topic}`" if topic else ""
    return (
        f"<blockquote>🚀 **Progress »** {pct:.1f}%\n{bar}</blockquote>\n"
        f"┣🔗 **Index »** {count}/{total}\n"
        f"┣⏳ **Remain »** {remaining}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<blockquote>📚 **Title »** `{title[:60]}`</blockquote>\n"
        f"┣📦 **Batch »** {b_name}"
        f"{t_line}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🛑 Send /stop to cancel\n"
        f"✦ **{credit}**"
    )
