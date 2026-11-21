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
                if os.path.exists(out) and os.path.getsize(out) > 0:
            return out
    except Exception:
        pass
    return path


def _shrink(path):
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
            stdout=NULL, stderr=NULL, timeout=900, check=True)
        if os.path.exists(out) and os.path.getsize(out) <= MAX_UPLOAD:
            return out
    except Exception:
        pass
    return None


async def _prep(path):
    # Returns (ready_path or None, status)
    if _is_video(path) and SEND_AS != "document":
        m = await asyncio.to_thread(_probe, path)
        if m and not m.get("audio"):
            path = await asyncio.to_thread(_add_silent_audio, path)
    if os.path.getsize(path) > MAX_UPLOAD:
        if SHRINK and _is_video(path):
            small = await asyncio.to_thread(_shrink, path)
            if small:
                return small, "shrunk"
        return None, "skipped"
    return path, "ok"


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
    cmd = [sys.executable, "-m", "gallery_dl", "-q", "--write-metadata", "-D", outdir]
    if cookies:
        cmd += ["--cookies", cookies]
    cmd.append(url)
    try:
        r = subprocess.run(cmd, timeout=600, check=False,
                           capture_output=True, text=True)
        return (r.stderr or "")[-300:]
    except subprocess.TimeoutExpired:
        return "timeout"


def _gallery_profile(username, outdir, cookies, limit, mode):
    cmd = [
        sys.executable, "-m", "gallery_dl", "-q", "--write-metadata",
        "-D", outdir,
        "-o", f"include={INCLUDE[mode]}",
        "--range", f"1-{limit}",
        "--sleep", "2-4", "--sleep-request", "1-2",
    ]
    if cookies:
        cmd += ["--cookies", cookies]
    cmd.append(f"https://www.instagram.com/{username}/")
    try:
        r = subprocess.run(cmd, timeout=1500, check=False,
                           capture_output=True, text=True)
        return (r.stderr or "")[-300:]
    except subprocess.TimeoutExpired:
        return "timeout"
# ---------------- sending ----------------
async def _send_single(bot, chat_id, path, caption):
    p = Path(path)
    try:
        if SEND_AS == "document":
            await _call(bot.send_document, chat_id, p, caption=caption)
        elif _is_video(path):
            m = await asyncio.to_thread(_probe, path)
            await _call(bot.send_video, chat_id, p, caption=caption,
                        supports_streaming=True, width=m.get("w"),
                        height=m.get("h"), duration=m.get("dur"))
        elif path.lower().endswith(PHOTO_EXT) and os.path.getsize(path) < PHOTO_LIMIT:
            await _call(bot.send_photo, chat_id, p, caption=caption)
        else:
            await _call(bot.send_document, chat_id, p, caption=caption)
        return 1
    except Exception:
        try:
            await _call(bot.send_document, chat_id, p, caption=caption)
            return 1
        except Exception:
            return 0


async def _media_for(path, caption):
    p = Path(path)
    if SEND_AS == "document":
        return InputMediaDocument(p, caption=caption)
    if _is_video(path):
        m = await asyncio.to_thread(_probe, path)
        return InputMediaVideo(p, caption=caption, supports_streaming=True,
                               width=m.get("w"), height=m.get("h"),
                               duration=m.get("dur"))
    if path.lower().endswith(PHOTO_EXT) and os.path.getsize(path) < PHOTO_LIMIT:
        return InputMediaPhoto(p, caption=caption)
    return InputMediaDocument(p, caption=caption)


async def _send_album(bot, chat_id, files, caption):
    if len(files) == 1:
        return await _send_single(bot, chat_id, files[0], caption)
    sent = 0
    for i in range(0, len(files), 10):
        batch = files[i:i + 10]
        cap = caption if i == 0 else None
        try:
            media = [await _media_for(f, cap if j == 0 else None)
                     for j, f in enumerate(batch)]
            await _call(bot.send_media_group, chat_id, media)
            sent += len(batch)
        except Exception:
            for j, f in enumerate(batch):
                sent += await _send_single(bot, chat_id, f, cap if j == 0 else None)
        await asyncio.sleep(2)
    return sent


async def _notes(bot, chat_id, shrunk, skipped):
    notes = []
    if shrunk:
        notes.append(f"{shrunk} video(s) were over 50 MB and got compressed to fit Telegram.")
    if skipped:
        notes.append(f"{skipped} file(s) were too large to send.")
    if notes:
        await bot.send_message(chat_id, " ".join(notes))


async def _send_post(bot, chat_id, files, caption):
    ready, shrunk, skipped = [], 0, 0
    for f in files:
        path, st = await _prep(f)
        if path:
            ready.append(path)
        shrunk += st == "shrunk"
        skipped += st == "skipped"
    sent = await _send_album(bot, chat_id, ready, caption) if ready else 0
    await _notes(bot, chat_id, shrunk, skipped)
    return sent


async def _send_profile(bot, chat_id, files, who):
    sent = shrunk = skipped = 0
    seen = set()
    for f in files:
        cap, code = _meta_caption(f, who=who)
        if code:
            if code in seen:
                cap = None
            seen.add(code)
        path, st = await _prep(f)
        shrunk += st == "shrunk"
        skipped += st == "skipped"
        if path:
            sent += await _send_single(bot, chat_id, path, cap)
            await asyncio.sleep(1.5)
    await _notes(bot, chat_id, shrunk, skipped)
    return sent
# ---------------- sending ----------------
async def _send_single(bot, chat_id, path, caption):
    p = Path(path)
    try:
        if SEND_AS == "document":
            await _call(bot.send_document, chat_id, p, caption=caption)
        elif _is_video(path):
            m = await asyncio.to_thread(_probe, path)
            await _call(bot.send_video, chat_id, p, caption=caption,
                        supports_streaming=True, width=m.get("w"),
                        height=m.get("h"), duration=m.get("dur"))
        elif path.lower().endswith(PHOTO_EXT) and os.path.getsize(path) < PHOTO_LIMIT:
            await _call(bot.send_photo, chat_id, p, caption=caption)
        else:
            await _call(bot.send_document, chat_id, p, caption=caption)
        return 1
    except Exception:
        try:
            await _call(bot.send_document, chat_id, p, caption=caption)
            return 1
        except Exception:
            return 0


async def _media_for(path, caption):
    p = Path(path)
    if SEND_AS == "document":
        return InputMediaDocument(p, caption=caption)
    if _is_video(path):
        m = await asyncio.to_thread(_probe, path)
        return InputMediaVideo(p, caption=caption, supports_streaming=True,
                               width=m.get("w"), height=m.get("h"),
                               duration=m.get("dur"))
    if path.lower().endswith(PHOTO_EXT) and os.path.getsize(path) < PHOTO_LIMIT:
        return InputMediaPhoto(p, caption=caption)
    return InputMediaDocument(p, caption=caption)


async def _send_album(bot, chat_id, files, caption):
    if len(files) == 1:
        return await _send_single(bot, chat_id, files[0], caption)
    sent = 0
    for i in range(0, len(files), 10):
        batch = files[i:i + 10]
        cap = caption if i == 0 else None
        try:
            media = [await _media_for(f, cap if j == 0 else None)
                     for j, f in enumerate(batch)]
            await _call(bot.send_media_group, chat_id, media)
            sent += len(batch)
        except Exception:
            for j, f in enumerate(batch):
                sent += await _send_single(bot, chat_id, f, cap if j == 0 else None)
        await asyncio.sleep(2)
    return sent


async def _notes(bot, chat_id, shrunk, skipped):
    notes = []
    if shrunk:
        notes.append(f"{shrunk} video(s) were over 50 MB and got compressed to fit Telegram.")
    if skipped:
        notes.append(f"{skipped} file(s) were too large to send.")
    if notes:
        await bot.send_message(chat_id, " ".join(notes))


async def _send_post(bot, chat_id, files, caption):
    ready, shrunk, skipped = [], 0, 0
    for f in files:
        path, st = await _prep(f)
        if path:
            ready.append(path)
        shrunk += st == "shrunk"
        skipped += st == "skipped"
    sent = await _send_album(bot, chat_id, ready, caption) if ready else 0
    await _notes(bot, chat_id, shrunk, skipped)
    return sent


async def _send_profile(bot, chat_id, files, who):
    sent = shrunk = skipped = 0
    seen = set()
    for f in files:
        cap, code = _meta_caption(f, who=who)
        if code:
            if code in seen:
                cap = None
            seen.add(code)
        path, st = await _prep(f)
        shrunk += st == "shrunk"
        skipped += st == "skipped"
        if path:
            sent += await _send_single(bot, chat_id, path, cap)
            await asyncio.sleep(1.5)
    await _notes(bot, chat_id, shrunk, skipped)
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
        is_story = "/stories/" in url
        if is_story and not cookies:
            await bot.send_message(
                chat_id, "Stories need an Instagram login (set IG_SESSIONID in Railway).")
            return
        media = os.path.join(d, "m")
        os.makedirs(media)
        info = None
        err = ""
        files = []

        if is_story:
            err = await asyncio.to_thread(_gallery_post, url, media, cookies)
            files = _list(media)
        if not files:
            try:
                info = await asyncio.to_thread(_ytdlp, url, media, cookies)
            except Exception as e:
                err = err or str(e)[-250:]
            files = _list(media)
        if not files and not is_story:
            err = await asyncio.to_thread(_gallery_post, url, media, cookies) or err
            files = _list(media)

        if not files:
            extra = f"\n\nReason: {err.strip()}" if err.strip() else ""
            await bot.send_message(chat_id, _hint(cookies) + extra)
            return

        clean = url.split("?")[0]
        if info:
            caption = _info_caption(info, url)
        else:
            caption = _meta_caption(files[0], link=clean)[0] or clean
        await _send_post(bot, chat_id, files, caption)


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
            await bot.send_message(
                chat_id, f"'{mode}' needs an Instagram login (set IG_SESSIONID in Railway).")
            return
        media = os.path.join(d, "m")
        os.makedirs(media)
        await bot.send_message(chat_id, f"Fetching {mode} from @{username} (up to {limit})...")
        err = await asyncio.to_thread(_gallery_profile, username, media, cookies, limit, mode)
        files = _list(media)
        if not files:
            extra = f"\n\nReason: {err.strip()}" if err.strip() else ""
            await bot.send_message(chat_id, _hint(cookies) + extra)
            return
        await bot.send_message(chat_id, f"Got {len(files)} files, sending...")
        sent = await _send_profile(bot, chat_id, files, username)
        await bot.send_message(chat_id, f"Done: sent {sent} of {len(files)} from @{username}.")


async def process_profile(bot, chat_id, profiles):
    for username, limit, mode in profiles:
        async with JOB_LOCK:
            try:
                await _do_profile(bot, chat_id, username, limit, mode)
            except Exception as e:
                await bot.send_message(chat_id, f"Error: {str(e)[:200]}")


