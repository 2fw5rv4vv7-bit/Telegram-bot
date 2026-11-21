import asyncio
import os
import tempfile

from ig_caption import info_caption, meta_caption
from ig_core import (DEFAULT_LIMIT, IG_RE, INCLUDE, JOB_LOCK, LOGIN_MODES,
                     MAX_LIMIT, PROFILE_RE, RESERVED, cookie_file, hint,
                     list_files)
from ig_download import gallery_post, gallery_profile, ytdlp
from ig_send import send_post, send_profile

__all__ = ["IG_RE", "process_instagram", "find_profiles", "process_profile"]


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
        cookies = cookie_file(d)
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
            err = await asyncio.to_thread(gallery_post, url, media, cookies)
            files = list_files(media)
        if not files:
            try:
                info = await asyncio.to_thread(ytdlp, url, media, cookies)
            except Exception as e:
                err = err or str(e)[-250:]
            files = list_files(media)
        if not files and not is_story:
            err = await asyncio.to_thread(gallery_post, url, media, cookies) or err
            files = list_files(media)
        if not files:
            extra = f"\n\nReason: {err.strip()}" if err.strip() else ""
            await bot.send_message(chat_id, hint(cookies) + extra)
            return
        clean = url.split("?")[0]
        if info:
            caption = info_caption(info, url)
        else:
            caption = meta_caption(files[0], link=clean)[0] or clean
        await send_post(bot, chat_id, files, caption)


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
        cookies = cookie_file(d)
        if mode in LOGIN_MODES and not cookies:
            await bot.send_message(
                chat_id, f"'{mode}' needs an Instagram login (set IG_SESSIONID in Railway).")
            return
        media = os.path.join(d, "m")
        os.makedirs(media)
        await bot.send_message(chat_id, f"Fetching {mode} from @{username} (up to {limit})...")
        err = await asyncio.to_thread(
            gallery_profile, username, media, cookies, limit, mode)
        files = list_files(media)
        if not files:
            extra = f"\n\nReason: {err.strip()}" if err.strip() else ""
            await bot.send_message(chat_id, hint(cookies) + extra)
            return
        await bot.send_message(chat_id, f"Got {len(files)} files, sending...")
        sent = await send_profile(bot, chat_id, files, username)
        await bot.send_message(chat_id, f"Done: sent {sent} of {len(files)} from @{username}.")


async def process_profile(bot, chat_id, profiles):
    for username, limit, mode in profiles:
        async with JOB_LOCK:
            try:
                await _do_profile(bot, chat_id, username, limit, mode)
            except Exception as e:
                await bot.send_message(chat_id, f"Error: {str(e)[:200]}")
