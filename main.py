import os
import re
import json
import asyncio
import requests
import hashlib
import time
import traceback
import fcntl
from datetime import datetime, date
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
from telethon import TelegramClient
from telethon.sessions import StringSession

# ========================= CONFIG =========================
API_ID = int(os.environ["A1"])
API_HASH = os.environ["A2"]
BOT_TOKEN = os.environ["A3"]
SESSION_STRING = os.environ["A4"]
YOUR_CHANNEL = os.environ["A5"].strip().lstrip("@")
SOURCE_CHANNELS = [c.strip().lstrip("@") for c in os.environ["A6"].split(",")]
AMAZON_AFFILIATE = os.environ["A7"].strip()

STATE_FILE = "last_seen.json"
SEEN_FILE = "seen_web.json"
LOCK_FILE = "deal_bot.lock"
DAILY_STATE_FILE = "daily_posts.json"

DAILY_MORNING_HOUR = 8    # 8:00 AM IST
DAILY_EVENING_HOUR = 19   # 7:00 PM IST

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "en-IN,en;q=0.9"
}
# =========================================================

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
        m = re.search(p, url, re.IGNORECASE)
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
    asin = extract_asin(url)
    if not asin:
        return None
    aff = f"https://www.amazon.in/dp/{asin}?tag={AMAZON_AFFILIATE}"
    try:
        r = requests.get(f"https://tinyurl.com/api-create.php?url={aff}", timeout=8)
        if r.status_code == 200:
            return r.text.strip()
    except:
        pass
    return aff

def make_affiliate(url):
    expanded = expand_short_url(url)
    if "amazon.in" not in expanded.lower():
        return None
    return process_amazon(expanded)

def extract_all_urls(msg):
    urls = []
    text = getattr(msg, "text", "") or getattr(msg, "caption", "")
    if text:
        urls += re.findall(r"https?://[^\s]+", text)
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
    if msg.buttons:
        for row in msg.buttons:
            for button in row:
                if hasattr(button, "url") and button.url:
                    urls.append(button.url)
    return list(set(urls))

def load_seen():
    if not os.path.exists(SEEN_FILE):
        return set()
    try:
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    except Exception as e:
        print(f"⚠️ Corrupted {SEEN_FILE}, resetting: {e}")
        return set()

def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen)[-2000:], f)  # keep last 2000 to limit size

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Corrupted {STATE_FILE}, resetting: {e}")
        return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def load_daily_state():
    if not os.path.exists(DAILY_STATE_FILE):
        return {"last_morning": "", "last_evening": ""}
    try:
        with open(DAILY_STATE_FILE) as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Corrupted {DAILY_STATE_FILE}, resetting: {e}")
        return {"last_morning": "", "last_evening": ""}

def save_daily_state(state):
    with open(DAILY_STATE_FILE, "w") as f:
        json.dump(state, f)

def post_telegram(text):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": f"@{YOUR_CHANNEL}", "text": text, "disable_web_page_preview": False},
            timeout=15
        )
        return r.status_code == 200
    except Exception as e:
        print("Telegram post failed:", e)
        return False

def post_poll(question, options):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendPoll",
            json={
                "chat_id": f"@{YOUR_CHANNEL}",
                "question": question,
                "options": options,
                "is_anonymous": False,
                "allows_multiple_answers": False
            },
            timeout=15
        )
        return r.status_code == 200
    except Exception as e:
        print("Poll failed:", e)
        return False

def send_morning_engagement():
    text = f"""🌅 Good Morning, Deal Hunters!

How are you liking the deals posted today?

Please vote in the poll below 👇

💡 Tip: Add your family & friends so they never miss a deal!
👉 @{YOUR_CHANNEL}"""
    if post_telegram(text):
        question = "How do you rate today's deals so far? 🔥"
        options = ["Excellent! ❤️", "Good 👍", "Average 🤔", "Needs improvement 👎"]
        post_poll(question, options)
        print("✅ Posted morning engagement + poll")

def send_evening_engagement():
    text = f"""🌆 Good Evening, Deal Lovers!

How were today's deals overall?

Vote below and let us know!

💡 Help us grow: Add your family & friends!
👉 @{YOUR_CHANNEL}"""
    if post_telegram(text):
        question = "How would you rate today's deals overall? 🔥"
        options = ["Excellent! ❤️", "Good 👍", "Average 🤔", "Needs improvement 👎"]
        post_poll(question, options)
        print("✅ Posted evening engagement + poll")

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
                if asin:
                    deals.append(Deal("Amazon Deal", href))
        except Exception as e:
            print("Amazon scrape error:", e)
    return deals

async def one_run():
    lock_fd = None
    try:
        lock_fd = open(LOCK_FILE, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another instance is running → skipping this cron run")
        return
    except Exception as e:
        print("Lock error:", e)
        return

    start_time = time.time()
    print(f"[{datetime.now(ZoneInfo('Asia/Kolkata')).strftime('%Y-%m-%d %H:%M:%S')}] Starting run...")

    try:
        seen = load_seen()
        state = load_state()
        total = 0

        # ===================== AMAZON DEALS =====================
        print("Checking Amazon deals...")
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
                print("Posted Amazon deal")
                await asyncio.sleep(1.2)

        # ===================== DAILY ENGAGEMENT =====================
        print("Checking daily morning/evening posts...")
        daily_state = load_daily_state()
        today = date.today().isoformat()
        now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
        hour = now_ist.hour
        updated = False

        if daily_state.get("last_morning") != today and hour == DAILY_MORNING_HOUR:
            send_morning_engagement()
            daily_state["last_morning"] = today
            updated = True

        if daily_state.get("last_evening") != today and hour == DAILY_EVENING_HOUR:
            send_evening_engagement()
            daily_state["last_evening"] = today
            updated = True

        if updated:
            save_daily_state(daily_state)

        # ===================== TELEGRAM CHANNELS =====================
        print("Checking telegram channels...")
        client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
        await client.connect()

        for channel in SOURCE_CHANNELS:
            try:
                last_id = state.get(channel, 0)
                new_last = last_id
                print(f"Scanning channel: {channel}")

                async for msg in client.iter_messages(channel, limit=200):
                    if msg.id <= last_id:
                        continue

                    message_uid = f"msg_{channel}_{msg.id}"
                    if message_uid in seen:
                        print(f"Skipping duplicate message {msg.id} from {channel}")
                        continue

                    text = clean_html(getattr(msg, "text", "") or getattr(msg, "caption", ""))
                    urls = extract_all_urls(msg)

                    new_text = text
                    has_affiliate = False
                    aff_urls = []

                    for url in urls:
                        aff = make_affiliate(url)
                        if aff:
                            new_text = new_text.replace(url, aff)
                            has_affiliate = True
                            aff_urls.append(aff)

                    if has_affiliate:
                        # Simple suspicious price filter (anti-fake/spam)
                        price_match = re.search(r'[₹₹]?\s*(\d{1,3}(?:,\d{3})*|\d+)\s*(?:,|\*|$)', new_text + text, re.IGNORECASE)
                        skip = False
                        if price_match:
                            try:
                                price_str = price_match.group(1).replace(',', '')
                                price = int(price_str)
                                if price < 5000 and ("refrigerator" in new_text.lower() or "fridge" in new_text.lower()):
                                    print(f"Skipping suspiciously low price deal ({price}) from {channel} msg {msg.id}")
                                    skip = True
                            except:
                                pass

                        if skip:
                            continue

                        new_text += f"\n\n🛒 Deals by @{YOUR_CHANNEL}"

                        print(f"Posting from {channel} msg {msg.id} - found {len(aff_urls)} affiliate links")

                        if post_telegram(new_text):
                            # Mark message + all ASINs/URLs as seen
                            seen.add(message_uid)
                            for url in urls:
                                asin = extract_asin(url)
                                uid = f"asin_{asin}" if asin else hashlib.md5(url.encode()).hexdigest()[:12]
                                seen.add(uid)
                            total += 1
                            print("Posted telegram deal")
                            await asyncio.sleep(1.2)
                    else:
                        if urls:
                            print(f"No valid Amazon.in affiliate found in {channel} msg {msg.id} (urls: {len(urls)})")

                    if msg.id > new_last:
                        new_last = msg.id

                state[channel] = new_last

            except Exception as e:
                print(f"Skipping channel {channel}: {e}")

        await client.disconnect()

        save_seen(seen)
        save_state(state)

        duration = time.time() - start_time
        print(f"✅ Run finished: {total} deals in {duration:.1f} seconds | Seen items: {len(seen):,}")

    except Exception as e:
        print(f"💥 FATAL ERROR in run: {e}")
        traceback.print_exc()
    finally:
        if lock_fd:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                lock_fd.close()
            except:
                pass

async def main():
    print("Bot started")
    await one_run()
    print("Bot finished")

if __name__ == "__main__":
    asyncio.run(main())
