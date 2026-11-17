import asyncio
import glob
import os
import re
import subprocess
import sys
import tempfile

import yt_dlp

IG_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:p|reel|reels|tv|stories|s)/[^\s]+"
)
PROFILE_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9._]+)/?(?:\?\S*)?(?:[ \t]+(\d+))?"
)
RESERVED = {"p", "reel", "reels", "tv", "stories", "explore", "s", "accounts", "direct"}
MAX_UPLOAD = 50 * 1024 * 1024
DEFAULT_LIMIT = int(os.environ.get("PROFILE_LIMIT", "20"))
MAX_LIMIT = 200


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


def _download(url, outdir, cookies):
    opts = {
        "outtmpl": os.path.join(outdir, "%(autonumber)s_%(id)s.%(ext)s"),
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
    }
    if cookies:
        opts["cookiefile"] = cookies
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])


def _gallery(username, mediadir, cookies, limit):
    cmd = [
        sys.executable, "-m", "gallery_dl", "-q",
        "-D", mediadir,
        "-o", "include=posts",
        "--range", f"1-{limit}",
        "--sleep", "2-4",
    ]
    if cookies:
        cmd += ["--cookies", cookies]
    cmd.append(f"https://www.instagram.com/{username}/")
    subprocess.run(cmd, timeout=1500, check=False)


def find_profiles(text):
    out = []
    for name, num in PROFILE_RE.findall(text):
        if name.lower() not in RESERVED:
            out.append((name, int(num) if num else DEFAULT_LIMIT))
    return out


async def _send_files(bot, chat_id, files):
    sent = 0
    for f in files:
        if f.endswith("cookies.txt"):
            continue
        if os.path.getsize(f) > MAX_UPLOAD:
            await bot.send_message(chat_id, "A file is over Telegram's 50 MB bot limit, skipped.")
            continue
        try:
            with open(f, "rb") as fh:
                await bot.send_document(chat_id, fh)
            sent += 1
        except Exception:
            pass
        await asyncio.sleep(1.5)
    return sent


async def process_instagram(bot, chat_id, urls):
    for url in urls:
        with tempfile.TemporaryDirectory() as d:
            cookies = _cookie_file(d)
            if "/stories/" in url and not cookies:
                await bot.send_message(chat_id, "Stories need an Instagram login (IG_SESSIONID).")
                continue
            try:
                await asyncio.to_thread(_download, url, d, cookies)
            except Exception as e:
                await bot.send_message(chat_id, f"Couldn't download that post: {str(e)[:200]}")
                continue
            files = sorted(glob.glob(d + "/*"))
            if not [f for f in files if not f.endswith("cookies.txt")]:
                await bot.send_message(chat_id, "No media found.")
                continue
            await _send_files(bot, chat_id, files)


async def process_profile(bot, chat_id, profiles):
    for username, limit in profiles:
        limit = min(limit, MAX_LIMIT)
        with tempfile.TemporaryDirectory() as d:
            cookies = _cookie_file(d)
            media = os.path.join(d, "media")
            os.makedirs(media)
            await bot.send_message(chat_id, f"Fetching up to {limit} items from @{username}...")
            try:
                await asyncio.to_thread(_gallery, username, media, cookies, limit)
            except Exception as e:
                await bot.send_message(chat_id, f"Failed: {str(e)[:200]}")
                continue
            files = sorted(glob.glob(media + "/*"))
            if not files:
                await bot.send_message(
                    chat_id, "Nothing downloaded (private, wrong name, or Instagram blocked it)."
                )
                continue
            sent = await _send_files(bot, chat_id, files)
            await bot.send_message(chat_id, f"Done: sent {sent} of {len(files)} files from @{username}.")
