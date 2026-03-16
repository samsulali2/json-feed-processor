import os
import re
import json
import asyncio
import requests
import hashlib
from bs4 import BeautifulSoup
from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = int(os.environ["A1"])
API_HASH = os.environ["A2"]
BOT_TOKEN = os.environ["A3"]
SESSION_STRING = os.environ["A4"]

YOUR_CHANNEL = os.environ["A5"].strip().lstrip("@")
SOURCE_CHANNELS = [c.strip().lstrip("@") for c in os.environ["A6"].split(",")]

AMAZON_AFFILIATE = os.environ["A7"].strip()

STATE_FILE = "last_seen.json"
SEEN_FILE = "seen_web.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept-Language": "en-IN,en;q=0.9"
}

MAX_WEB_PER_RUN = 5


def clean_html(text):
    if not text:
        return ""
    return re.sub(r"<.*?>", "", text).strip()


def extract_asin(url):

    patterns = [
        r"/dp/([A-Z0-9]{10})",
        r"/gp/product/([A-Z0-9]{10})",
        r"/product/([A-Z0-9]{10})",
        r"asin=([A-Z0-9]{10})"
    ]

    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)

    return None


def expand_short_url(url):

    try:
        r = requests.head(url, allow_redirects=True, timeout=8)
        return r.url
    except:
        return url


def process_amazon(url):

    url = expand_short_url(url)

    asin = extract_asin(url)

    if not asin:
        return None

    aff = f"https://www.amazon.in/dp/{asin}?tag={AMAZON_AFFILIATE}"

    try:
        r = requests.get(
            f"https://tinyurl.com/api-create.php?url={aff}",
            timeout=8
        )
        if r.status_code == 200:
            return r.text.strip()
    except:
        pass

    return aff


def make_affiliate(url):

        # always expand first
        url = expand_short_url(url)

        # check for amazon after expansion
        if "amazon." in url:
            return process_amazon(url)

        return None


def extract_all_urls(msg):

    urls = []

    text = getattr(msg, "text", "") or getattr(msg, "caption", "")

    if text:
        urls += re.findall(r"https?://[^\s]+", text)

    # extract entity URLs (hidden links)
    if msg.entities:
        for ent in msg.entities:

            if hasattr(ent, "url") and ent.url:
                urls.append(ent.url)

            if hasattr(ent, "offset") and hasattr(ent, "length"):
                try:
                    hidden = text[ent.offset:ent.offset + ent.length]
                    if hidden.startswith("http"):
                        urls.append(hidden)
                except:
                    pass

    # extract button URLs
    if msg.buttons:
        for row in msg.buttons:
            for button in row:
                if hasattr(button, "url") and button.url:
                    urls.append(button.url)

    return list(set(urls))


def load_seen():

    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))

    return set()


def save_seen(seen):

    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen)[-2000:], f)


def load_state():

    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)

    return {}


def save_state(state):

    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def post_telegram(text):

    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={
            "chat_id": f"@{YOUR_CHANNEL}",
            "text": text,
            "disable_web_page_preview": False
        },
        timeout=15
    )

    return r.status_code == 200


class Deal:

    def __init__(self, title, url):

        self.title = clean_html(title)[:300]

        asin = extract_asin(url)

        if asin:
            self.uid = f"asin_{asin}"
        else:
            self.uid = hashlib.md5(url.encode()).hexdigest()[:12]

        self.url = url


def scrape_amazon_deals():

    deals = []

    pages = [
        "https://www.amazon.in/gp/goldbox",
        "https://www.amazon.in/deals",
        "https://www.amazon.in/gp/goldbox?dealType=LIGHTNING_DEAL"
    ]

    for page in pages:

        try:

            r = requests.get(page, headers=HEADERS, timeout=15)

            soup = BeautifulSoup(r.text, "html.parser")

            for a in soup.select("a[href*='/dp/']"):

                href = a.get("href")

                if not href:
                    continue

                if not href.startswith("http"):
                    href = "https://www.amazon.in" + href

                asin = extract_asin(href)

                if not asin:
                    continue

                deals.append(Deal("Amazon Deal", href))

        except Exception as e:

            print("amazon scrape error:", e)

    return deals


async def one_run():

    seen = load_seen()
    state = load_state()

    posted_this_run = set()
    total = 0

    print("Checking Amazon deals")

    for deal in scrape_amazon_deals():

        if deal.uid in seen:
            continue

        aff = make_affiliate(deal.url)

        if not aff:
            continue

        msg = f"🔥 {deal.title}\n\n🔗 {aff}\n\n🛒 Deals by @{YOUR_CHANNEL}"

        if post_telegram(msg):

            seen.add(deal.uid)

            total += 1

            print("posted amazon deal")

            await asyncio.sleep(2)

    print("Checking telegram channels")

    client = TelegramClient(
        StringSession(SESSION_STRING),
        API_ID,
        API_HASH
    )

    await client.connect()

    for channel in SOURCE_CHANNELS:

        try:

            last_id = state.get(channel, 0)
            new_last = last_id

            print("Scanning channel:", channel)

            async for msg in client.iter_messages(channel, limit=200):

                if msg.id <= last_id:
                    continue

                text = clean_html(
                    getattr(msg, "text", "") or getattr(msg, "caption", "")
                )

                urls = extract_all_urls(msg)

                for url in urls:

                    aff = make_affiliate(url)

                    if not aff:
                        continue

                    asin = extract_asin(url)

                    uid = f"asin_{asin}" if asin else hashlib.md5(url.encode()).hexdigest()[:12]

                    if uid in seen or uid in posted_this_run:
                        continue

                    new_text = text.replace(url, aff)
                    new_text += f"\n\n🛒 Deals by @{YOUR_CHANNEL}"

                    if post_telegram(new_text):

                        seen.add(uid)
                        posted_this_run.add(uid)

                        total += 1

                        print("posted telegram deal")

                        await asyncio.sleep(2)

                if msg.id > new_last:
                    new_last = msg.id

            state[channel] = new_last

        except Exception as e:

            print("Skipping channel", channel, e)

    await client.disconnect()

    save_seen(seen)
    save_state(state)

    print("Run finished:", total)


async def main():

    print("Bot started")

    await one_run()

    print("Bot finished")


if __name__ == "__main__":
    asyncio.run(main())
