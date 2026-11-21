import json
import os

from ig_core import CAPTIONS


def build_caption(who, text, link):
    if not CAPTIONS:
        return None
    head = f"@{who}" if who else ""
    body = (text or "").strip()
    cap = "\n".join(p for p in (head, body) if p)[:900]
    if link:
        cap = f"{cap}\n{link}" if cap else link
    return cap[:1024] or None


def info_caption(info, url):
    src = info or {}
    if src.get("entries"):
        first = next((e for e in src["entries"] if e), {})
        src = {**first, **{k: v for k, v in src.items() if v and k != "entries"}}
    who = src.get("channel") or src.get("uploader") or ""
    return build_caption(who, src.get("description") or "", url.split("?")[0])


def meta_caption(path, link=None, who=""):
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
    return build_caption(who, m.get("description") or "", link), code
