import asyncio
import glob
import os
import re
import subprocess

from telegram.error import RetryAfter

DEFAULT_LIMIT = int(os.environ.get("PROFILE_LIMIT", "20"))
SEND_AS = os.environ.get("SEND_AS", "media").lower()
SHRINK = os.environ.get("SHRINK", "1") == "1"
CAPTIONS = os.environ.get("CAPTIONS", "1") == "1"
MAX_LIMIT = 200
MAX_UPLOAD = 50 * 1024 * 1024
PHOTO_LIMIT = 10 * 1024 * 1024
NULL = subprocess.DEVNULL
JOB_LOCK = asyncio.Semaphore(2)

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


def cookie_file(outdir):
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


def list_files(folder):
    return sorted(
        f for f in glob.glob(os.path.join(folder, "*"))
        if os.path.isfile(f) and not f.endswith(SKIP_SUFFIX)
    )


def is_video(path):
    return path.lower().endswith(VIDEO_EXT)


def hint(cookies):
    if cookies:
        return ("Couldn't download that. It may be private, expired, or "
                "Instagram blocked the server. If your session expired, "
                "set a fresh IG_SESSIONID.")
    return ("Couldn't download that. Photo posts, private posts and stories "
            "need an Instagram login (set IG_SESSIONID in Railway).")


async def call(fn, *a, **kw):
    for _ in range(4):
        try:
            return await fn(*a, **kw)
        except RetryAfter as e:
            wait = e.retry_after
            wait = wait.total_seconds() if hasattr(wait, "total_seconds") else wait
            await asyncio.sleep(wait + 1)
    return await fn(*a, **kw)
