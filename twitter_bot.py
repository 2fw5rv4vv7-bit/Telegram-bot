import asyncio
import io
import logging
import os
import re
import threading

import httpx
from flask import Flask, request
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from instagram import IG_RE, process_instagram, find_profiles, process_profile


BOT_TOKEN = os.environ["BOT_TOKEN"].strip()
CHAT_ID = int(os.environ["CHAT_ID"])
SECRET = os.environ["SECRET"]

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("twitter_bot")

TWEET_RE = re.compile(
    r"https?://(?:www\.|mobile\.)?(?:twitter\.com|x\.com)/(\w+)/status/(\d+)"
)
MAX_UPLOAD = 50 * 1024 * 1024
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}


async def from_fx(client, user, tid):
    r = await client.get(f"https://api.fxtwitter.com/{user}/status/{tid}")
    log.info("fxtwitter status %s", r.status_code)
    if r.status_code != 200:
        return None
    tweet = r.json().get("tweet") or {}
    items = (tweet.get("media") or {}).get("all") or []
    return [
        {"url": i["url"], "type": "photo" if i.get("type") == "photo" else "video"}
        for i in items
        if i.get("url")
    ]


async def from_vx(client, user, tid):
    r = await client.get(f"https://api.vxtwitter.com/{user}/status/{tid}")
    log.info("vxtwitter status %s", r.status_code)
    if r.status_code != 200:
        return None
    items = r.json().get("media_extended") or []
    return [
        {"url": i["url"], "type": "photo" if i.get("type") == "image" else "video"}
        for i in items
        if i.get("url")
    ]


async def get_media(client, user, tid):
    for fn in (from_fx, from_vx):
        try:
            result = await fn(client, user, tid)
        except Exception:
            log.exception("%s failed", fn.__name__)
            result = None
        if result is not None:
            return result
    return None


async def send_media(bot, chat_id, client, item):
    url = item["url"]
    kind = item["type"]

    try:
        if kind == "photo":
            await bot.send_photo(chat_id, url)
        else:
            await bot.send_video(chat_id, url, supports_streaming=True)
        return
    except Exception as e:
        log.info("Direct send failed (%s), falling back to upload", e)

    r = await client.get(url)
    r.raise_for_status()
    if len(r.content) > MAX_UPLOAD:
        await bot.send_message(chat_id, f"File too large for Telegram. Direct link:\n{url}")
        return
    bio = io.BytesIO(r.content)
    bio.name = url.split("?")[0].rsplit("/", 1)[-1] or "media"
    if kind == "photo":
        await bot.send_document(chat_id, bio)
    else:
        await bot.send_video(chat_id, bio, supports_streaming=True)


async def process_links(bot, chat_id, matches):
    async with httpx.AsyncClient(
        timeout=60, headers=HEADERS, follow_redirects=True
    ) as client:
        for user, tid in matches:
            media = await get_media(client, user, tid)
            if media is None:
                await bot.send_message(chat_id, "Couldn't fetch that tweet.")
                continue
            if not media:
                await bot.send_message(chat_id, "No photos or videos in that tweet.")
                continue

            for item in media:
                try:
                    await send_media(bot, chat_id, client, item)
                except Exception:
                    log.exception("Failed sending media")
                    await bot.send_message(
                        chat_id, f"Couldn't send one item. Direct link:\n{item['url']}"
                    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.effective_message.text or ""
    chat_id = update.effective_chat.id
    tw = TWEET_RE.findall(text)
    ig = IG_RE.findall(text)
    prof = [] if (tw or ig) else find_profiles(text)
    if not (tw or ig or prof):
        return
    await context.bot.send_chat_action(chat_id, "upload_document")
    if tw:
        await process_links(context.bot, chat_id, tw)
    if ig:
        await process_instagram(context.bot, chat_id, ig)
    if prof:
        await process_profile(context.bot, chat_id, prof)
    if update.effective_chat.id != CHAT_ID:
        return



async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me a link to an X/Twitter post and I'll send back its photos and videos."
    )


# ---------- Bookmark endpoint ----------
web = Flask(__name__)


async def push(matches):
    async with Bot(BOT_TOKEN) as bot:
        await process_links(bot, CHAT_ID, matches)


def run_push(matches):
    try:
        asyncio.run(push(matches))
    except Exception:
        log.exception("push failed")


@web.post("/bookmark")
def bookmark():
    if request.headers.get("X-Secret") != SECRET:
        return "no", 403
    url = (request.get_json(silent=True) or {}).get("url", "")
    matches = TWEET_RE.findall(url)
    if not matches:
        return "bad url", 400
    threading.Thread(target=run_push, args=(matches,), daemon=True).start()
    return "ok"


@web.get("/")
def home():
    return "running"


def start_web():
    port = int(os.environ.get("PORT", 5000))
    web.run(host="0.0.0.0", port=port)


def main():
    threading.Thread(target=start_web, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()


if __name__ == "__main__":
    main()
