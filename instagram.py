import asyncio
import glob
import os
import re
import tempfile

import yt_dlp

IG_RE = re.compile(r"https?://(?:www\.)?instagram\.com/(?:p|reel|reels|tv)/[\w-]+\S*")
MAX_UPLOAD = 50 * 1024 * 1024
PHOTO_EXT = (".jpg", ".jpeg", ".png", ".webp")


def _download(url, outdir):
    opts = {
        "outtmpl": os.path.join(outdir, "%(id)s.%(ext)s"),
        "format": "best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
    }
    cookies = os.environ.get("IG_COOKIES")
    if cookies:
        path = os.path.join(outdir, "cookies.txt")
        with open(path, "w") as f:
            f.write(cookies)
        opts["cookiefile"] = path
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])


async def process_instagram(bot, chat_id, urls):
    for url in urls:
        with tempfile.TemporaryDirectory() as d:
            try:
                await asyncio.to_thread(_download, url, d)
            except Exception:
                await bot.send_message(
                    chat_id,
                    "Couldn't download that Instagram post (private, photo-only, or blocked).",
                )
                continue

            files = [f for f in sorted(glob.glob(d + "/*")) if not f.endswith("cookies.txt")]
            if not files:
                await bot.send_message(chat_id, "No media found in that post.")
                continue

            for f in files:
                if os.path.getsize(f) > MAX_UPLOAD:
                    await bot.send_message(chat_id, "File too large for Telegram.")
                    continue
                with open(f, "rb") as fh:
                    if f.lower().endswith(PHOTO_EXT):
                        await bot.send_photo(chat_id, fh)
                    else:
                        await bot.send_video(chat_id, fh, supports_streaming=True)
