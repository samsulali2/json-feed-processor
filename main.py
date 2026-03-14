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
CUELINKS_API_KEY = os.environ.get("A8", "").strip()

STATE_FILE = "last_seen.json"
SEEN_FILE = "seen_web.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept-Language": "en-IN,en;q=0.9",
}

MAX_WEB_PER_RUN = 5


# ---------------- URL HELPERS ----------------

def extract_asin(url):
    m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", url)
    if m:
        return m.group(1)
    return None


def normalize_url(url):
    url = url.split("?")[0]
    return url.rstrip("/")


def expand_short_url(url):
    try:
        r = requests.head(url, allow_redirects=True, timeout=8)
        return r.url
    except:
        return url


def process_amazon_url(url):
    if "amzn.to" in url:
        url = expand_short_url(url)

    asin = extract_asin(url)
    if asin:
        clean = f"https://www.amazon.in/dp/{asin}?tag={AMAZON_AFFILIATE}"
        return clean

    return None


def shorten_url(url):
    try:
        r = requests.get(f"https://tinyurl.com/api-create.php?url={url}", timeout=8)
        if r.status_code == 200:
            return r.text.strip()
    except:
        pass
    return url


def make_affiliate(url):
    if "amazon" in url or "amzn.to" in url:
        aff = process_amazon_url(url)
        if aff:
            return shorten_url(aff)
    return None


def extract_urls(text):
    return re.findall(r"https?://[^\s]+", text or "")


# ---------------- STATE ----------------

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


# ---------------- TELEGRAM POST ----------------

def post_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    r = requests.post(
        url,
        json={
            "chat_id": f"@{YOUR_CHANNEL}",
            "text": text,
            "disable_web_page_preview": False
        },
        timeout=15,
    )

    return r.status_code == 200


# ---------------- DEAL CLASS ----------------

class Deal:
    def __init__(self, title, url):
        self.title = title.strip()[:300]
        self.url = normalize_url(url)

        asin = extract_asin(url)

        if asin:
            self.uid = f"asin_{asin}"
        else:
            self.uid = hashlib.md5(self.url.encode()).hexdigest()[:12]


# ---------------- SCRAPER ----------------

def scrape_desidime():
    deals = []

    try:
        r = requests.get("https://www.desidime.com/deals", headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")

        for item in soup.select("article")[:30]:

            a = item.select_one("h2 a") or item.select_one("h3 a")

            if not a:
                continue

            title = a.get_text(strip=True)
            link = a.get("href")

            if not link:
                continue

            if not link.startswith("http"):
                link = "https://www.desidime.com" + link

            deals.append(Deal(title, link))

    except Exception as e:
        print("desidime error", e)

    return deals


SCRAPERS = [
    scrape_desidime
]


# ---------------- ONE RUN ----------------

async def one_run():

    seen = load_seen()
    state = load_state()

    total = 0

    print("Checking websites...")

    for scraper in SCRAPERS:

        try:
            deals = scraper()
        except:
            continue

        posted = 0

        for deal in deals:

            if posted >= MAX_WEB_PER_RUN:
                break

            if deal.uid in seen:
                continue

            aff = make_affiliate(deal.url)

            if not aff:
                continue

            msg = f"🔥 {deal.title}\n\n🔗 {aff}\n\n🛒 Deals by @{YOUR_CHANNEL}"

            ok = post_telegram(msg)

            if ok:
                print("posted", deal.title[:60])
                seen.add(deal.uid)
                posted += 1
                total += 1

                await asyncio.sleep(2)

    print("Checking telegram channels...")

    async with TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH) as client:

        for channel in SOURCE_CHANNELS:

            last_id = state.get(channel, 0)
            new_last = last_id

            async for msg in client.iter_messages(channel, min_id=last_id, limit=50):

                text = msg.text or msg.caption or ""

                if not text:
                    continue

                urls = extract_urls(text)

                for url in urls:

                    aff = make_affiliate(url)

                    if not aff:
                        continue

                    uid = extract_asin(url) or hashlib.md5(url.encode()).hexdigest()[:12]

                    if uid in seen:
                        continue

                    new_text = text.replace(url, aff)
                    new_text += f"\n\n🛒 Deals by @{YOUR_CHANNEL}"

                    ok = post_telegram(new_text)

                    if ok:
                        seen.add(uid)
                        total += 1
                        print("posted telegram deal")

                        await asyncio.sleep(2)

                if msg.id > new_last:
                    new_last = msg.id

            state[channel] = new_last

    save_state(state)
    save_seen(seen)

    print("Run finished. Posted:", total)


# ---------------- MAIN ----------------

async def main():
    print("Bot started")
    await one_run()
    print("Bot finished")


if __name__ == "__main__":
    asyncio.run(main())
