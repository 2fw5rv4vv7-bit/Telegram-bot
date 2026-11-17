import asyncio
import glob
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yt_dlp
from telegram import InputMediaDocument
from telegram.error import RetryAfter

# ---- Settings (all optional, set in Railway Variables) ----
DEFAULT_LIMIT = int(os.environ.get("PROFILE_LIMIT", "20"))
SEND_AS = os.environ.get("SEND_AS", "document").lower()  # document | media
SHRINK = os.environ.get("SHRINK", "1") == "1"
CAPTIONS = os.environ.get("CAPTIONS", "1") == "1"
MAX_LIMIT = 200
MAX_UPLOAD = 50 * 1024 * 1024

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


# ---------------- helpers ----------------
def _cookie_file(outdir):
    raw = os.environ.get("IG_COOKIES")
    sid = os.environ.get("IG_SESSIONID", "").strip()
    if raw:
        content = raw
    elif sid:
        content = "# Netscape HTTP Cookie File\n" + "\t".join(
            [".instagram.com", "TRUE", "/", "TRUE", "0", "sessionid", sid]
        ) + "\n"
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
        return ("Couldn't download that. It may be private, deleted, or "
                "Instagram blocked the server. If your session expired, "
                "set a fresh IG_SESSIONID.")
    return ("Couldn't download that. Photo posts, private posts and stories "
            "need an Instagram login (set IG_SESSIONID in Railway).")


def _caption(info, url):
    clean = url.split("?")[0]
    if not CAPTIONS or not info:
        return clean
    src = info
    if info.get("entries"):
        first = next((e for e in info["entries"] if e), {})
        src = {**first, **{k: v for k, v in info.items() if v and k != "entries"}}
    who = src.get("uploader") or src.get("channel") or ""
    text = (src.get("description") or "").strip()
    cap = (f"@{who}\n" if who else "") + text
    return (cap[:850] + "\n" + clean)[:1024]


async def _call(fn, *a, **kw):
    for _ in range(4):
        try:
            return await fn(*a, **kw)
        except RetryAfter as e:
            wait = e.retry_after
            wait = wait.total_seconds() if hasattr(wait, "total_seconds") else wait
            await asyncio.sleep(wait + 1)
    return await fn(*a, **kw)


# ---------------- downloading ----------------
def _ytdlp(url, outdir, cookies):
    opts = {
        "outtmpl": os.path.join(outdir, "%(autonumber)s_%(id)s.%(ext)s"),
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "socket_timeout": 30,
    }
    if cookies:
        opts["cookiefile"] = cookies
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=True)


def _gallery_post(url, outdir, cookies):
    cmd = [sys.executable, "-m", "gallery_dl", "-q", "-D", outdir]
    if cookies:
        cmd += ["--cookies", cookies]
    cmd.append(url)
    try:
        subprocess.run(cmd, timeout=600, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        pass


def _gallery_profile(username, outdir, cookies, limit, mode):
    cmd = [
        sys.executable, "-m", "gallery_dl", "-q", "-D", outdir,
        "-o", f"include={INCLUDE[mode]}",
        "--range", f"1-{limit}",
        "--sleep", "2-4", "--sleep-request", "1-2",
    ]
    if cookies:
        cmd += ["--cookies", cookies]
    cmd.append(f"https://www.instagram.com/{username}/")
    try:
        subprocess.run(cmd, timeout=1500, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        pass


def _shrink(path):
    """Compress a video under Telegram's 50 MB bot limit."""
    try:
        dur = float(subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path], timeout=60).strip())
        vbr = int(46 * 1024 * 1024 * 8 / dur) - 96_000
        if vbr < 150_000:
            return None
        out = path + ".small.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-i", path, "-c:v", "libx264", "-preset", "veryfast",
             "-b:v", str(vbr), "-maxrate", str(int(vbr * 1.2)),
             "-bufsize", str(vbr * 2), "-c:a", "aac", "-b:a", "96k",
             "-movflags", "+faststart", out],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=900, check=True)
        if os.path.exists(out) and os.path.getsize(out) <= MAX_UPLOAD:
            return out
    except Exception:
        pass
    return None


# ---------------- sending ----------------
def _batches(files, n=10, cap=100 * 1024 * 1024):
    batch, size = [], 0
    for f in files:
        s = os.path.getsize(f)
        if batch and (len(batch) >= n or size + s > cap):
            yield batch
            batch, size = [], 0
        batch.append(f)
        size += s
    if batch:
        yield batch


async def _send_one_doc(bot, chat_id, path, caption):
    try:
        await _call(bot.send_document, chat_id, Path(path), caption=caption)
        return 1
    except Exception:
        return 0


async def _send_doc_batch(bot, chat_id, batch, caption):
    if len(batch) == 1:
        return await _send_one_doc(bot, chat_id, batch[0], caption)
    try:
        media = [
            InputMediaDocument(Path(p), caption=caption if j == 0 else None)
            for j, p in enumerate(batch)
        ]
        await _call(bot.send_media_group, chat_id, media)
        return len(batch)
    except Exception:
        pass
    sent = 0
    for j, p in enumerate(batch):
        sent += await _send_one_doc(bot, chat_id, p, caption if j == 0 else None)
    return sent


async def _send_media_one(bot, chat_id, path, caption):
    p = Path(path)
    try:
        if _is_video(path):
            await _call(bot.send_video, chat_id, p, caption=caption,
                        supports_streaming=True)
        elif path.lower().endswith(PHOTO_EXT) and os.path.getsize(path) < 10 * 1024 * 1024:
            await _call(bot.send_photo, chat_id, p, caption=caption)
        else:
            await _call(bot.send_document, chat_id, p, caption=caption)
        return 1
    except Exception:
        return await _send_one_doc(bot, chat_id, path, caption)


async def _send_files(bot, chat_id, files, caption=None):
    ready, skipped, shrunk = [], 0, 0
    for f in files:
        if os.path.getsize(f) <= MAX_UPLOAD:
            ready.append(f)
            continue
        small = await asyncio.to_thread(_shrink, f) if (SHRINK and _is_video(f)) else None
        if small:
            ready.append(small)
            shrunk += 1
        else:
            skipped += 1

    sent = 0
    if SEND_AS == "media":
        for i, f in enumerate(ready):
            sent += await _send_media_one(bot, chat_id, f, caption if i == 0 else None)
            await asyncio.sleep(1)
    else:
        for i, batch in enumerate(_batches(ready)):
            sent += await _send_doc_batch(bot, chat_id, batch, caption if i == 0 else None)
            await asyncio.sleep(2)

    notes = []
    if shrunk:
        notes.append(f"{shrunk} video(s) were over 50 MB and got compressed to fit Telegram.")
    if skipped:
        notes.append(f"{skipped} file(s) were too large to send.")
    if notes:
        await bot.send_message(chat_id, " ".join(notes))
    return sent


# ---------------- public API (used by twitter_bot.py) ----------------
def find_profiles(text):
    out = []
    for name, rest in PROFILE_RE.findall(text):
        if name.lower() in RESERVED:
            continue
        limit, mode = DEFAULT_LIMIT, "posts"
        for tok in rest.lower().split():
            if tok.isdigit():
                limit = int(tok)
            elif tok in INCLUDE:
                mode = tok
        out.append((name, limit, mode))
    return out


async def _do_post(bot, chat_id, url):
    with tempfile.TemporaryDirectory() as d:
        cookies = _cookie_file(d)
        if "/stories/" in url and not cookies:
            await bot.send_message(chat_id, "Stories need an Instagram login (set IG_SESSIONID in Railway).")
            return
        media = os.path.join(d, "m")
        os.makedirs(media)
        info = None
        try:
            info = await asyncio.to_thread(_ytdlp, url, media, cookies)
        except Exception:
            pass
        files = _list(media)
        if not files:
            await asyncio.to_thread(_gallery_post, url, media, cookies)
            files = _list(media)
        if not files:
            await bot.send_message(chat_id, _hint(cookies))
            return
        await _send_files(bot, chat_id, files, _caption(info, url))


async def process_instagram(bot, chat_id, urls):
    for raw in urls:
        url = raw.rstrip(".,;)")
        async with JOB_LOCK:
            try:
                await _do_post(bot, chat_id, url)
            except Exception as e:
                await bot.send_message(chat_id, f"Error: {str(e)[:200]}")


async def _do_profile(bot, chat_id, username, limit, mode):
    limit = max(1, min(limit, MAX_LIMIT))
    with tempfile.TemporaryDirectory() as d:
        cookies = _cookie_file(d)
        if mode in LOGIN_MODES and not cookies:
            await bot.send_message(chat_id, f"'{mode}' needs an Instagram login (set IG_SESSIONID in Railway).")
            return
        media = os.path.join(d, "m")
        os.makedirs(media)
        await bot.send_message(chat_id, f"Fetching {mode} from @{username} (up to {limit})...")
        await asyncio.to_thread(_gallery_profile, username, media, cookies, limit, mode)
        files = _list(media)
        if not files:
            await bot.send_message(chat_id, _hint(cookies))
            return
        await bot.send_message(chat_id, f"Got {len(files)} files, sending...")
        sent = await _send_files(bot, chat_id, files)
        await bot.send_message(chat_id, f"Done: sent {sent} of {len(files)} from @{username}.")


async def process_profile(bot, chat_id, profiles):
    for username, limit, mode in profiles:
        async with JOB_LOCK:
            try:
                await _do_profile(bot, chat_id, username, limit, mode)
            except Exception as e:
                await bot.send_message(chat_id, f"Error: {str(e)[:200]}")
