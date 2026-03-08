"""
Telegram Affiliate Deal Bot
- Amazon links → replaced with your Amazon Associates affiliate tag
- Flipkart, Myntra, Ajio and other links → converted via Cuelinks API
- Shortens all affiliate URLs via TinyURL
- Posts to your Telegram channel
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
YOUR_CHANNEL       = os.environ["YOUR_CHANNEL_USERNAME"].strip().lstrip('@')
SOURCE_CHANNELS    = [c.strip().lstrip('@') for c in os.environ["SOURCE_CHANNELS"].split(",")]
AMAZON_AFFILIATE   = os.environ["AMAZON_AFFILIATE_ID"].strip()
CUELINKS_API_KEY   = os.environ.get("CUELINKS_API_KEY", "").strip()
SESSION_STRING     = os.environ["TELEGRAM_SESSION_STRING"].strip()
STATE_FILE         = "last_seen.json"
# ─────────────────────────────────────────────────────────────────────────────


# ── URL Helpers ───────────────────────────────────────────────────────────────

def expand_short_url(url):
    """Follow redirects to resolve short links like amzn.to, fkrt.it"""
    try:
        r = requests.head(url, allow_redirects=True, timeout=10)
        return r.url
    except Exception:
        return url


def inject_amazon_tag(url, tag):
    """Replace or add Amazon affiliate tag= parameter"""
    url = re.sub(r"([&?])tag=[^&]*", "", url)
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}tag={tag}"


def process_amazon_url(url):
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


def process_cuelinks_url(url):
    """Convert Flipkart, Myntra, Ajio and other links via Cuelinks API"""
    if not CUELINKS_API_KEY:
        return None

    # Skip Amazon links — handled separately
    amazon_patterns = [
        r"https?://(?:www\.)?amazon\.in",
        r"https?://(?:www\.)?amazon\.com",
        r"https?://amzn\.to",
        r"https?://amzn\.in",
        r"https?://(?:www\.)?a\.co",
    ]
    if any(re.match(p, url) for p in amazon_patterns):
        return None

    try:
        expanded = expand_short_url(url)
        resp = requests.get(
            "https://api.cuelinks.com/v1/affiliate-url",
            params={
                "apiKey": CUELINKS_API_KEY,
                "url": expanded,
            },
            timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            affiliate_url = data.get("affiliateUrl") or data.get("url")
            if affiliate_url and affiliate_url != expanded:
                return affiliate_url
    except Exception as e:
        print(f"    Cuelinks API error: {e}")
    return None


def shorten_url(url):
    """Shorten a URL using TinyURL free API"""
    try:
        resp = requests.get(
            f"https://tinyurl.com/api-create.php?url={url}",
            timeout=10
        )
        if resp.status_code == 200 and resp.text.startswith("http"):
            return resp.text.strip()
    except Exception:
        pass
    return url


def extract_urls(text):
    return re.findall(r"https?://[^\s\)\]>\"']+", text or "")


def rewrite_message(text):
    """Replace all deal links with affiliate versions. Returns (new_text, was_modified)."""
    urls = extract_urls(text)
    modified = False
    new_text = text
    for url in urls:
        affiliate_url = process_amazon_url(url) or process_cuelinks_url(url)
        if affiliate_url and affiliate_url != url:
            short_url = shorten_url(affiliate_url)
            new_text = new_text.replace(url, short_url)
            modified = True
    return new_text, modified


# ── State Management ──────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


# ── Main ──────────────────────────────────────────────────────────────────────

async def run():
    state = load_state()
    bot_api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    print(f"Source channels: {SOURCE_CHANNELS}")
    print(f"Your channel: {YOUR_CHANNEL}")
    print(f"Cuelinks API: {'enabled' if CUELINKS_API_KEY else 'disabled'}")

    async with TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH) as client:
        for channel in SOURCE_CHANNELS:
            if not channel:
                continue

            last_id = state.get(channel, 0)
            new_last_id = last_id
            found_new = 0

            print(f"Checking: {channel} | Last seen ID: {last_id}")

            try:
                async for message in client.iter_messages(channel, min_id=last_id, limit=50):
                    if message.id <= last_id:
                        continue

                    text = getattr(message, "text", "") or getattr(message, "caption", "") or ""
                    if not text:
                        if message.id > new_last_id:
                            new_last_id = message.id
                        continue

                    new_text, modified = rewrite_message(text)

                    if modified:
                        new_text += f"\n\n🛒 Deals by @{YOUR_CHANNEL}"
                        payload = {
                            "chat_id": f"@{YOUR_CHANNEL}",
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
