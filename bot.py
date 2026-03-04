"""
Telegram Affiliate Deal Bot
Reads messages from source deal channels, replaces links with your affiliate
codes, and posts to your own Telegram channel.
"""

import os
import re
import json
import asyncio
import requests
from telethon import TelegramClient
from telethon.sessions import StringSession

# ── Config from GitHub Secrets ───────────────────────────────────────────────
API_ID             = int(os.environ["TELEGRAM_API_ID"])
API_HASH           = os.environ["TELEGRAM_API_HASH"]
BOT_TOKEN          = os.environ["TELEGRAM_BOT_TOKEN"]
YOUR_CHANNEL       = os.environ["YOUR_CHANNEL_USERNAME"]    # e.g. @mydealsChannel
SOURCE_CHANNELS    = os.environ["SOURCE_CHANNELS"].split(",")
AMAZON_AFFILIATE   = os.environ["AMAZON_AFFILIATE_ID"]      # e.g. yourname-21
FLIPKART_AFFILIATE = os.environ.get("FLIPKART_AFFILIATE_ID", "")
SESSION_STRING     = os.environ["TELEGRAM_SESSION_STRING"]
STATE_FILE         = "last_seen.json"
# ─────────────────────────────────────────────────────────────────────────────


# ── URL Helpers ───────────────────────────────────────────────────────────────

def expand_short_url(url: str) -> str:
    """Follow redirects to resolve short links like amzn.to"""
    try:
        r = requests.head(url, allow_redirects=True, timeout=10)
        return r.url
    except Exception:
        return url


def inject_amazon_tag(url: str, tag: str) -> str:
    """Replace or add Amazon affiliate tag= parameter"""
    url = re.sub(r"([&?])tag=[^&]*", "", url)
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}tag={tag}"


def process_amazon_url(url: str):
    """Returns affiliate URL if Amazon link, else None"""
    amazon_patterns = [
        r"https?://(?:www\.)?amazon\.in",
        r"https?://(?:www\.)?amazon\.com",
        r"https?://amzn\.to",
        r"https?://amzn\.in",
        r"https?://(?:www\.)?a\.co",
    ]
    if not any(re.match(p, url) for p in amazon_patterns):
        return None
    full_url = expand_short_url(url)
    full_url = re.sub(r"/ref=[^?&]*", "", full_url)
    return inject_amazon_tag(full_url, AMAZON_AFFILIATE)


def process_flipkart_url(url: str):
    """Returns affiliate URL if Flipkart link, else None"""
    if not FLIPKART_AFFILIATE:
        return None
    flipkart_patterns = [
        r"https?://(?:www\.)?flipkart\.com",
        r"https?://fkrt\.it",
        r"https?://dl\.flipkart\.com",
    ]
    if not any(re.match(p, url) for p in flipkart_patterns):
        return None
    full_url = expand_short_url(url)
    full_url = re.sub(r"([&?])affid=[^&]*", "", full_url)
    separator = "&" if "?" in full_url else "?"
    return f"{full_url}{separator}affid={FLIPKART_AFFILIATE}"


def extract_urls(text: str) -> list:
    return re.findall(r"https?://[^\s\)\]>\"']+", text or "")


def rewrite_message(text: str):
    """Replace all deal links with affiliate versions. Returns (new_text, was_modified)."""
    urls = extract_urls(text)
    modified = False
    new_text = text
    for url in urls:
        affiliate_url = process_amazon_url(url) or process_flipkart_url(url)
        if affiliate_url and affiliate_url != url:
            new_text = new_text.replace(url, affiliate_url)
            modified = True
    return new_text, modified


# ── State Management ──────────────────────────────────────────────────────────

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


# ── Main ──────────────────────────────────────────────────────────────────────

async def run():
    state = load_state()
    bot_api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    async with TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH) as client:
        for channel in SOURCE_CHANNELS:
            channel = channel.strip()
            if not channel:
                continue

            last_id = state.get(channel, 0)
            new_last_id = last_id
            found_new = 0

            print(f"Checking: {channel} | Last seen ID: {last_id}")

            try:
                async for message in client.iter_messages(channel, min_id=last_id, limit=30):
                    if message.id <= last_id:
                        continue

                    text = message.text or message.caption or ""
                    if not text:
                        if message.id > new_last_id:
                            new_last_id = message.id
                        continue

                    new_text, modified = rewrite_message(text)

                    if modified:
                        new_text += f"\n\n🛒 Deals by @{YOUR_CHANNEL.lstrip('@')}"
                        payload = {
                            "chat_id": YOUR_CHANNEL,
                            "text": new_text,
                            "disable_web_page_preview": False,
                        }
                        resp = requests.post(bot_api_url, json=payload, timeout=15)
                        if resp.status_code == 200:
                            print(f"  ✅ Posted deal (msg {message.id})")
                            found_new += 1
                        else:
                            print(f"  ❌ Failed: {resp.text}")

                    if message.id > new_last_id:
                        new_last_id = message.id

            except Exception as e:
                print(f"  ⚠️ Error reading {channel}: {e}")

            state[channel] = new_last_id
            print(f"  New deals posted: {found_new}")

    save_state(state)
    print("Done.")


if __name__ == "__main__":
    asyncio.run(run())
