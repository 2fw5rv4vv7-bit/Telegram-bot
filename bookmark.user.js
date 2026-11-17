// ==UserScript==
// @name         X Bookmark to Telegram Bot
// @match        https://x.com/*
// @match        https://twitter.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @connect      telegram-bot-production-65e28b05.up.railway.app
// ==/UserScript==

const ENDPOINT = "https://telegram-bot-production-65e28b05.up.railway.app/bookmark";

document.addEventListener("click", async (e) => {
  const btn = e.target.closest('[data-testid="bookmark"]');
  if (!btn) return;

  let secret = await GM_getValue("secret", "");
  if (!secret) {
    secret = prompt("Enter your bot SECRET (saved on this device):") || "";
    if (!secret) return;
    await GM_setValue("secret", secret);
  }

  const article = btn.closest("article");
  let url = location.href;
  const link = article?.querySelector('a[href*="/status/"] time')?.closest("a");
  if (link) url = link.href.split("?")[0];

  GM_xmlhttpRequest({
    method: "POST",
    url: ENDPOINT,
    headers: { "Content-Type": "application/json", "X-Secret": secret },
    data: JSON.stringify({ url }),
  });
}, true);
