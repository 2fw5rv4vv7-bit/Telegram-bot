import asyncio
import os
from pathlib import Path

from telegram import InputMediaDocument, InputMediaPhoto, InputMediaVideo

from ig_caption import meta_caption
from ig_core import PHOTO_EXT, PHOTO_LIMIT, SEND_AS, call, is_video
from ig_video import prep, probe


def _is_photo(path):
    return (path.lower().endswith(PHOTO_EXT)
            and os.path.getsize(path) < PHOTO_LIMIT)


async def send_single(bot, chat_id, path, caption):
    p = Path(path)
    try:
        if SEND_AS == "document":
            await call(bot.send_document, chat_id, p, caption=caption)
        elif is_video(path):
            m = await asyncio.to_thread(probe, path)
            await call(bot.send_video, chat_id, p, caption=caption,
                       supports_streaming=True, width=m.get("w"),
                       height=m.get("h"), duration=m.get("dur"))
        elif _is_photo(path):
            await call(bot.send_photo, chat_id, p, caption=caption)
        else:
            await call(bot.send_document, chat_id, p, caption=caption)
        return 1
    except Exception:
        try:
            await call(bot.send_document, chat_id, p, caption=caption)
            return 1
        except Exception:
            return 0


async def media_for(path, caption):
    p = Path(path)
    if SEND_AS == "document":
        return InputMediaDocument(p, caption=caption)
    if is_video(path):
        m = await asyncio.to_thread(probe, path)
        return InputMediaVideo(p, caption=caption, supports_streaming=True,
                               width=m.get("w"), height=m.get("h"),
                               duration=m.get("dur"))
    if _is_photo(path):
        return InputMediaPhoto(p, caption=caption)
    return InputMediaDocument(p, caption=caption)


async def send_album(bot, chat_id, files, caption):
    if len(files) == 1:
        return await send_single(bot, chat_id, files[0], caption)
    sent = 0
    for i in range(0, len(files), 10):
        batch = files[i:i + 10]
        cap = caption if i == 0 else None
        try:
            media = [await media_for(f, cap if j == 0 else None)
                     for j, f in enumerate(batch)]
            await call(bot.send_media_group, chat_id, media)
            sent += len(batch)
        except Exception:
            for j, f in enumerate(batch):
                sent += await send_single(bot, chat_id, f, cap if j == 0 else None)
        await asyncio.sleep(2)
    return sent


async def notes(bot, chat_id, shrunk, skipped):
    out = []
    if shrunk:
        out.append(f"{shrunk} video(s) were over 50 MB and got compressed.")
    if skipped:
        out.append(f"{skipped} file(s) were too large to send.")
    if out:
        await bot.send_message(chat_id, " ".join(out))


async def send_post(bot, chat_id, files, caption):
    ready, shrunk, skipped = [], 0, 0
    for f in files:
        path, st = await prep(f)
        if path:
            ready.append(path)
        shrunk += st == "shrunk"
        skipped += st == "skipped"
    sent = await send_album(bot, chat_id, ready, caption) if ready else 0
    await notes(bot, chat_id, shrunk, skipped)
    return sent


async def send_profile(bot, chat_id, files, who):
    sent = shrunk = skipped = 0
    seen = set()
    for f in files:
        cap, code = meta_caption(f, who=who)
        if code:
            if code in seen:
                cap = None
            seen.add(code)
        path, st = await prep(f)
        shrunk += st == "shrunk"
        skipped += st == "skipped"
        if path:
            sent += await send_single(bot, chat_id, path, cap)
            await asyncio.sleep(1.5)
    await notes(bot, chat_id, shrunk, skipped)
    return sent
