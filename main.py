```python
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


# ---------- URL HELPERS ----------

def extract_asin(url):
    m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", url)
    return m.group(1) if m else None


def expand_short_url(url):
    try:
        r = requests.head(url, allow_redirects=True, timeout=8)
        return r.url
    except:
        return url


def normalize_url(url):
    return url.split("?")[0].rstrip("/")


def process_amazon(url):

    if "amzn.to" in url:
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

    if "amazon" in url or "amzn.to" in url:
        return process_amazon(url)

    return None


def extract_urls(text):
    return re.findall(r"https?://[^\s]+", text or "")


# ---------- STATE ----------

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


# ---------- TELEGRAM POST ----------

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


# ---------- DEAL CLASS ----------

class Deal:

    def __init__(self, title, url):

        self.title = title.strip()[:300]

        asin = extract_asin(url)

        if asin:
            self.uid = f"asin_{asin}"
        else:
            self.uid = hashlib.md5(url.encode()).hexdigest()[:12]

        self.url = normalize_url(url)


# ---------- SCRAPERS ----------

def scrape_desidime():

    deals = []

    try:

        r = requests.get(
            "https://www.desidime.com/deals",
            headers=HEADERS,
            timeout=15
        )

        soup = BeautifulSoup(r.text, "html.parser")

        for item in soup.select("article")[:30]:

            a = item.select_one("h2 a") or item.select_one("h3 a")

            if not a:
                continue

            title = a.get_text(strip=True)
            url = a.get("href")

            if not url:
                continue

            if not url.startswith("http"):
                url = "https://www.desidime.com" + url

            deals.append(Deal(title, url))

    except Exception as e:
        print("desidime error", e)

    return deals


def scrape_freekaamaal():

    deals = []

    try:

        r = requests.get(
            "https://www.freekaamaal.com/",
            headers=HEADERS,
            timeout=15
        )

        soup = BeautifulSoup(r.text, "html.parser")

        for a in soup.select("article h3 a")[:30]:

            title = a.get_text(strip=True)
            url = a.get("href")

            if url:
                deals.append(Deal(title, url))

    except Exception as e:
        print("freekaamaal error", e)

    return deals


def scrape_lootdunia():

    deals = []

    try:

        r = requests.get(
            "https://lootdunia.com/",
            headers=HEADERS,
            timeout=15
        )

        soup = BeautifulSoup(r.text, "html.parser")

        for a in soup.select("article h2 a")[:30]:

            title = a.get_text(strip=True)
            url = a.get("href")

            if url:
                deals.append(Deal(title, url))

    except Exception as e:
        print("lootdunia error", e)

    return deals


SCRAPERS = [
    scrape_desidime,
    scrape_freekaamaal,
    scrape_lootdunia
]


# ---------- ONE RUN ----------

async def one_run():

    seen = load_seen()
    state = load_state()

    posted_this_run = set()

    total = 0

    print("Checking websites")

    for scraper in SCRAPERS:

        try:
            deals = scraper()
        except:
            continue

        posted = 0

        for deal in deals:

            if posted >= MAX_WEB_PER_RUN:
                break

            if deal.uid in seen or deal.uid in posted_this_run:
                continue

            aff = make_affiliate(deal.url)

            if not aff:
                continue

            msg = f"🔥 {deal.title}\n\n🔗 {aff}\n\n🛒 Deals by @{YOUR_CHANNEL}"

            posted_this_run.add(deal.uid)
            seen.add(deal.uid)

            if post_telegram(msg):

                print("posted", deal.title[:60])

                posted += 1
                total += 1

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

            async for msg in client.iter_messages(channel, min_id=last_id, limit=50):

                text = msg.text or msg.caption or ""

                urls = extract_urls(text)

                for url in urls:

                    if "amzn.to" in url:
                        url = expand_short_url(url)

                    aff = make_affiliate(url)

                    if not aff:
                        continue

                    asin = extract_asin(url)

                    uid = f"asin_{asin}" if asin else hashlib.md5(url.encode()).hexdigest()[:12]

                    if uid in seen or uid in posted_this_run:
                        continue

                    new_text = text.replace(url, aff)
                    new_text += f"\n\n🛒 Deals by @{YOUR_CHANNEL}"

                    posted_this_run.add(uid)
                    seen.add(uid)

                    if post_telegram(new_text):

                        print(f"posted telegram deal from {channel}")

                        total += 1

                        await asyncio.sleep(2)

                if msg.id > new_last:
                    new_last = msg.id

            state[channel] = new_last

        except Exception as e:

            print(f"Skipping channel {channel} — error: {e}")

    await client.disconnect()

    save_seen(seen)
    save_state(state)

    print("Run finished:", total)


# ---------- MAIN ----------

async def main():

    print("Bot started")

    await one_run()

    print("Bot finished")


if __name__ == "__main__":

    asyncio.run(main())
```
