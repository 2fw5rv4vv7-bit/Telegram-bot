import io
import logging
import os
import re

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

BOT_TOKEN = os.environ["BOT_TOKEN"].strip()

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


async def send_media(update, client, item):
    url = item["url"]
    kind = item["type"]
    msg = update.effective_message

    try:
        if kind == "photo":
            await msg.reply_photo(url)
        else:
            await msg.reply_video(url, supports_streaming=True)
        return
    except Exception as e:
        log.info("Direct send failed (%s), falling back to upload", e)

    r = await client.get(url)
    r.raise_for_status()
    if len(r.content) > MAX_UPLOAD:
        await msg.reply_text(f"File too large for Telegram. Direct link:\n{url}")
        return
    bio = io.BytesIO(r.content)
    bio.name = url.split("?")[0].rsplit("/", 1)[-1] or "media"
    if kind == "photo":
        await msg.reply_document(bio)
    else:
        await msg.reply_video(bio, supports_streaming=True)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.effective_message.text or ""
    matches = TWEET_RE.findall(text)
    if not matches:
        return

    await context.bot.send_chat_action(update.effective_chat.id, "upload_video")

    async with httpx.AsyncClient(
        timeout=60, headers=HEADERS, follow_redirects=True
    ) as client:
        for user, tid in matches:
            media = await get_media(client, user, tid)
            if media is None:
                await update.effective_message.reply_text("Couldn't fetch that tweet.")
                continue
            if not media:
                await update.effective_message.reply_text("No photos or videos in that tweet.")
                continue

            for item in media:
                try:
                    await send_media(update, client, item)
                except Exception:
                    log.exception("Failed sending media")
                    await update.effective_message.reply_text(
                        f"Couldn't send one item. Direct link:\n{item['url']}"
                    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me a link to an X/Twitter post and I'll send back its photos and videos."
    )


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()


if __name__ == "__main__":
    main()
