import asyncio
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yt_dlp
from telegram import InputMediaDocument, InputMediaPhoto, InputMediaVideo
from telegram.error import RetryAfter

# ---- Settings (optional Railway variables) ----
DEFAULT_LIMIT = int(os.environ.get("PROFILE_LIMIT", "20"))
SEND_AS = os.environ.get("SEND_AS", "media").lower()  # media | document
SHRINK = os.environ.get("SHRINK", "1") == "1"
CAPTIONS = os.environ.get("CAPTIONS", "1") == "1"
MAX_LIMIT = 200
MAX_UPLOAD = 50 * 1024 * 1024
PHOTO_LIMIT = 10 * 1024 * 1024

IG_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:[A-Za-z0-9._]+/)?"
    r"(?:p|reel|reels|tv|stories|s|share)/[^\s]+"
)
PROFILE_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9._]+)/?(?:\?\S*)?"
    r"((?:[ \t]+(?:\d+|posts|reels|stories|highlights|avatar|all))*)",
    re.IGNORECASE,
)
RESERVED = {"p", "reel", "reels", "tv", "stories", "explore", "s",
            "share", "accounts", "direct"}
INCLUDE = {
    "posts": "posts", "reels": "reels", "stories": "stories",
    "highlights": "highlights", "avatar": "avatar",
    "all": "posts,reels,stories,highlights",
}
LOGIN_MODES = {"stories", "highlights", "all"}
VIDEO_EXT = (".mp4", ".mov", ".webm", ".mkv")
PHOTO_EXT = (".jpg", ".jpeg", ".png", ".webp")
SKIP_SUFFIX = (".json", ".part", ".ytdl", ".txt")

JOB_LOCK = asyncio.Semaphore(2)
NULL = subprocess.DEVNULL


# ---------------- helpers ----------------
def _cookie_file(outdir):
    raw = os.environ.get("IG_COOKIES")
    sid = os.environ.get("IG_SESSIONID", "").strip().strip('"')
    if raw:
        content = raw
    elif sid:
        sid = sid.replace(":", "%3A")
        rows = [("sessionid", sid), ("ds_user_id", sid.split("%3A")[0])]
        csrf = os.environ.get("IG_CSRF", "").strip()
        if csrf:
            rows.append(("csrftoken", csrf))
        content = "# Netscape HTTP Cookie File\n" + "".join(
            "\t".join([".instagram.com", "TRUE", "/", "TRUE", "0", n, v]) + "\n"
            for n, v in rows
        )
    else:
        return None
    path = os.path.join(outdir, "cookies.txt")
    with open(path, "w") as f:
        f.write(content)
    return path


def _list(folder):
    return sorted(
        f for f in glob.glob(os.path.join(folder, "*"))
        if os.path.isfile(f) and not f.endswith(SKIP_SUFFIX)
    )


def _is_video(path):
    return path.lower().endswith(VIDEO_EXT)


def _hint(cookies):
    if cookies:
        return ("Couldn't download that. It may be private, expired, or "
                "Instagram blocked the server. If your session expired, "
                "set a fresh IG_SESSIONID.")
    return ("Couldn't download that. Photo posts, private posts and stories "
            "need an Instagram login (set IG_SESSIONID in Railway).")


async def _call(fn, *a, **kw):
    for _ in range(4):
        try:
            return await fn(*a, **kw)
        except RetryAfter as e:
            wait = e.retry_after
            wait = wait.total_seconds() if hasattr(wait, "total_seconds") else wait
            await asyncio.sleep(wait + 1)
    return await fn(*a, **kw)


# ---------------- captions ----------------
def _build_caption(who, text, link):
    if not CAPTIONS:
        return None
    head = f"@{who}" if who else ""
    body = (text or "").strip()
    cap = "\n".join(p for p in (head, body) if p)[:900]
    if link:
        cap = f"{cap}\n{link}" if cap else link
    return cap[:1024] or None


def _info_caption(info, url):
    src = info or {}
    if src.get("entries"):
        first = next((e for e in src["entries"] if e), {})
        src = {**first, **{k: v for k, v in src.items() if v and k != "entries"}}
    who = src.get("channel") or src.get("uploader") or ""
    return _build_caption(who, src.get("description") or "", url.split("?")[0])


def _meta_caption(path, link=None, who=""):
    # Reads gallery-dl's .json metadata. Returns (caption, post_code).
    m = {}
    for jp in (path + ".json", os.path.splitext(path)[0] + ".json"):
        if os.path.exists(jp):
            try:
                with open(jp, encoding="utf-8") as fh:
                    m = json.load(fh)
                break
            except Exception:
                pass
    who = m.get("username") or who
    code = m.get("post_shortcode") or m.get("shortcode")
    link = m.get("post_url") or (
        f"https://www.instagram.com/p/{code}/" if code else link)
    return _build_caption(who, m.get("description") or "", link), code


# ---------------- video fixing ----------------
def _probe(path):
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_streams", "-show_format", path], timeout=60)
        data = json.loads(out)
    except Exception:
        return {}
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    return {
        "w": v.get("width"),
        "h": v.get("height"),
        "dur": int(float(data.get("format", {}).get("duration") or 0)),
        "audio": any(s.get("codec_type") == "audio" for s in streams),
    }


def _add_silent_audio(path):
    # Videos with no audio track are shown by Telegram as GIFs.
    out = path + ".a.mp4"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", path, "-f", "lavfi",
             "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
             "-c:v", "copy", "-c:a", "aac", "-shortest",
             "-movflags", "+faststart", out],
            stdout=NULL, stderr=NULL, timeout=300, check=True)
        if os.path.exists(out) and os.path.getsize(
