import io
import logging
import os
import re

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

BOT_TOKEN = os.environ["BOT_TOKEN"]

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("twitter_bot")

TWEET_RE = re.compile(
    r"https?://(?:www\.|mobile\.)?(?:twitter\.com|x\.com)/(\w+)/status/(\d+)"
)
MAX_UPLOAD = 50 * 1024 * 1024


async def fetch_tweet(client, user, tweet_id):
    r = await client.get(f"https://api.fxtwitter.com/{user}/status/{tweet_id}")
    if r.status_code != 200:
        return None
    return r.json().get("tweet")


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

    r = await client.get(url, follow_redirects=True)
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

    async with httpx.AsyncClient(timeout=60) as client:
        for user, tweet_id in matches:
            tweet = await fetch_tweet(client, user, tweet_id)
            if not tweet:
                await update.effective_message.reply_text("Couldn't fetch that tweet.")
                continue

            media = (tweet.get("media") or {}).get("all") or []
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
