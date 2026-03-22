"""
Telegram Affiliate Deal Bot — Render.com version (continuous loop)
- Runs forever, checks for new deals every 5 minutes
- Scrapes: desidime.com, freekaamaal.com, dealsmagnet.com, lootdunia.com
- Also reads Telegram source channels
- Amazon  → clean amazon.in/dp/ASIN?tag=xxx (via TinyURL to hide tag)
- Flipkart/Myntra/Ajio etc → Cuelinks API
- Posts everything to your Telegram channel
"""

import os, re, json, asyncio, requests, hashlib, time
from bs4 import BeautifulSoup
from telethon import TelegramClient
from telethon.sessions import StringSession

# ── Config ────────────────────────────────────────────────────────────────────
API_ID           = int(os.environ["A1"])
API_HASH         = os.environ["A2"]
BOT_TOKEN        = os.environ["A3"]
YOUR_CHANNEL     = os.environ["A5"].strip().lstrip('@')
SOURCE_CHANNELS  = [c.strip().lstrip('@') for c in os.environ["A6"].split(",")]
AMAZON_AFFILIATE = os.environ["A7"].strip()
CUELINKS_API_KEY = os.environ.get("A8", "").strip()
SESSION_STRING   = os.environ["A4"].strip()
STATE_FILE       = "last_seen.json"
SEEN_FILE        = "seen_web.json"
DEALS_FILE       = "deals.json"
MAX_DEALS        = 200
MAX_WEB_PER_RUN  = 5
INTERVAL_SECONDS = 300   # 5 minutes

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  'Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'en-IN,en;q=0.9',
}

CUELINKS_DOMAINS = {
    'flipkart.com', 'myntra.com', 'ajio.com', 'nykaa.com',
    'tatacliq.com', 'shopsy.in', 'meesho.com', 'jiomart.com',
    'croma.com', 'vijaysales.com', 'reliancedigital.com',
}
# ─────────────────────────────────────────────────────────────────────────────


# ── URL helpers ───────────────────────────────────────────────────────────────

def is_amazon(url):
    return bool(re.search(r'amazon\.in|amazon\.com|amzn\.to|amzn\.in|a\.co/', url))

def is_cuelinks_supported(url):
    return any(d in url for d in CUELINKS_DOMAINS)

def inject_amazon_tag(url, tag):
    url = re.sub(r'[?&]tag=[^&]*', '', url)
    url = re.sub(r'[?&]ascsubtag=[^&]*', '', url)
    sep = '&' if '?' in url else '?'
    return f"{url}{sep}tag={tag}"

def expand_short_url(url):
    try:
        r = requests.head(url, allow_redirects=True, timeout=8, headers=HEADERS)
        return r.url
    except Exception:
        return url

def process_amazon_url(url):
    if re.search(r'amzn\.to|amzn\.in|a\.co/', url):
        url = expand_short_url(url)
    if not is_amazon(url):
        return None
    asin_match = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', url)
    if asin_match:
        asin = asin_match.group(1)
        clean = f"https://www.amazon.in/dp/{asin}?tag={AMAZON_AFFILIATE}"
        return shorten_url(clean)
    url = re.sub(r'/ref=[^/?&]*', '', url)
    return shorten_url(inject_amazon_tag(url, AMAZON_AFFILIATE))

def process_cuelinks_url(url):
    if not CUELINKS_API_KEY:
        return None
    if not is_cuelinks_supported(url):
        return None
    if len(url) < 60:
        url = expand_short_url(url)
    try:
        resp = requests.get(
            'https://api.cuelinks.com/v1/affiliate-url',
            params={'apiKey': CUELINKS_API_KEY, 'url': url},
            timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            aff = data.get('affiliateUrl') or data.get('url')
            if aff and aff != url:
                return shorten_url(aff)
    except Exception as e:
        print(f"    Cuelinks error: {e}")
    return None

def make_affiliate(url):
    if is_amazon(url) or re.search(r'amzn\.to|amzn\.in|a\.co/', url):
        return process_amazon_url(url)
    if is_cuelinks_supported(url):
        return process_cuelinks_url(url)
    return None

def shorten_url(url):
    try:
        resp = requests.get(f'https://tinyurl.com/api-create.php?url={url}', timeout=10)
        if resp.status_code == 200 and resp.text.startswith('http'):
            return resp.text.strip()
    except Exception:
        pass
    return url

def extract_urls(text):
    return re.findall(r'https?://[^\s\)\]>\"\']+', text or '')

def rewrite_message(text):
    urls = extract_urls(text)
    modified = False
    new_text = text
    for url in urls:
        aff = make_affiliate(url)
        if aff and aff != url:
            new_text = new_text.replace(url, aff)
            modified = True
    return new_text, modified


# ── Deal class ────────────────────────────────────────────────────────────────

class Deal:
    def __init__(self, title, url, source):
        self.title  = title.strip()[:300]
        self.url    = url
        self.source = source
        self.uid    = hashlib.md5(url.encode()).hexdigest()[:12]

    def to_telegram(self, channel, short_url):
        return f"🔥 {self.title}\n\n🔗 {short_url}\n\n🛒 Deals by @{channel}"


# ── Scrapers ──────────────────────────────────────────────────────────────────

def scrape_desidime():
    deals = []
    try:
        r = requests.get('https://www.desidime.com/deals', headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, 'html.parser')
        items = (
            soup.select('li.deal-item') or
            soup.select('div.deal-item') or
            soup.select('.sdeal') or
            soup.select('article')
        )
        for item in items[:40]:
            title_el = (
                item.select_one('a.title') or
                item.select_one('h2 a') or
                item.select_one('h3 a') or
                item.select_one('.deal-title a') or
                item.select_one('a[href*="/deals/"]')
            )
            link_el = (
                item.select_one('a[href*="amazon"]') or
                item.select_one('a[href*="flipkart"]') or
                item.select_one('a[href*="myntra"]') or
                item.select_one('a[href*="ajio"]') or
                item.select_one('a.btn-go-to-store') or
                item.select_one('a.go-to-store')
            )
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            if not title or len(title) < 5:
                continue
            url = link_el['href'] if link_el else title_el.get('href', '')
            if not url:
                continue
            if not url.startswith('http'):
                url = 'https://www.desidime.com' + url
            deals.append(Deal(title, url, 'desidime'))
    except Exception as e:
        print(f"  desidime error: {e}")
    print(f"  desidime: {len(deals)} found")
    return deals


def scrape_freekaamaal():
    deals = []
    try:
        r = requests.get('https://www.freekaamaal.com/', headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, 'html.parser')
        items = (
            soup.select('article.jeg_post') or
            soup.select('article') or
            soup.select('.deal-box') or
            soup.select('.td-item-block')
        )
        for item in items[:40]:
            title_el = (
                item.select_one('h3.jeg_post_title a') or
                item.select_one('h2 a') or
                item.select_one('h3 a') or
                item.select_one('.entry-title a') or
                item.select_one('.td-module-title a')
            )
            link_el = (
                item.select_one('a[href*="amazon"]') or
                item.select_one('a[href*="flipkart"]') or
                item.select_one('a[href*="myntra"]') or
                item.select_one('a.dealBtn') or
                item.select_one('a.btn-deal')
            )
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            url   = link_el['href'] if link_el else title_el.get('href', '')
            if not url or not url.startswith('http'):
                continue
            deals.append(Deal(title, url, 'freekaamaal'))
    except Exception as e:
        print(f"  freekaamaal error: {e}")
    print(f"  freekaamaal: {len(deals)} found")
    return deals


def scrape_dealsmagnet():
    deals = []
    try:
        r = requests.get('https://dealsmagnet.com/', headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, 'html.parser')
        items = (
            soup.select('article') or
            soup.select('.deal') or
            soup.select('.post')
        )
        for item in items[:40]:
            title_el = (
                item.select_one('h2 a') or
                item.select_one('h3 a') or
                item.select_one('.entry-title a') or
                item.select_one('.title a')
            )
            link_el = (
                item.select_one('a[href*="amazon"]') or
                item.select_one('a[href*="flipkart"]') or
                item.select_one('a.buy-btn') or
                item.select_one('a.deal-link')
            )
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            url   = link_el['href'] if link_el else title_el.get('href', '')
            if not url or not url.startswith('http'):
                continue
            deals.append(Deal(title, url, 'dealsmagnet'))
    except Exception as e:
        print(f"  dealsmagnet error: {e}")
    print(f"  dealsmagnet: {len(deals)} found")
    return deals


def scrape_lootdunia():
    deals = []
    try:
        r = requests.get('https://lootdunia.com/', headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, 'html.parser')
        items = (
            soup.select('article') or
            soup.select('.post') or
            soup.select('.deal-item')
        )
        for item in items[:40]:
            title_el = (
                item.select_one('h2 a') or
                item.select_one('h3 a') or
                item.select_one('.entry-title a')
            )
            link_el = (
                item.select_one('a[href*="amazon"]') or
                item.select_one('a[href*="flipkart"]') or
                item.select_one('a.buy-now') or
                item.select_one('a.grab-deal')
            )
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            url   = link_el['href'] if link_el else title_el.get('href', '')
            if not url or not url.startswith('http'):
                continue
            deals.append(Deal(title, url, 'lootdunia'))
    except Exception as e:
        print(f"  lootdunia error: {e}")
    print(f"  lootdunia: {len(deals)} found")
    return deals


SCRAPERS = {
    'desidime':    scrape_desidime,
    'freekaamaal': scrape_freekaamaal,
    'dealsmagnet': scrape_dealsmagnet,
    'lootdunia':   scrape_lootdunia,
}


# ── State helpers ─────────────────────────────────────────────────────────────

def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()

def save_seen(seen):
    with open(SEEN_FILE, 'w') as f:
        json.dump(list(seen)[-2000:], f)

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)

def post_telegram(bot_api, text):
    r = requests.post(bot_api, json={
        'chat_id': f'@{YOUR_CHANNEL}',
        'text': text,
        'disable_web_page_preview': False
    }, timeout=15)
    return r.status_code == 200, r.text


def load_deals():
    if os.path.exists(DEALS_FILE):
        with open(DEALS_FILE) as f:
            return json.load(f)
    return []

def save_deal(deals, text, url, source):
    from datetime import datetime, timezone
    deals.insert(0, {"text": text, "url": url, "source": source, "timestamp": datetime.now(timezone.utc).isoformat()})
    deals = deals[:MAX_DEALS]
    with open(DEALS_FILE, "w") as f:
        json.dump(deals, f, ensure_ascii=False, indent=2)
    return deals

# ── One run ───────────────────────────────────────────────────────────────────

async def one_run():
    state   = load_state()
    seen    = load_seen()
    bot_api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    total   = 0

    # ── 1. Websites ──────────────────────────────────────────────────────────
    print("\n── Websites ──")
    for site_name, scraper in SCRAPERS.items():
        print(f"\n[{site_name}]")
        try:
            site_deals = scraper()
        except Exception as e:
            print(f"  Crashed: {e}")
            continue
        posted = 0
        for deal in site_deals:
            if posted >= MAX_WEB_PER_RUN:
                break
            if deal.uid in seen:
                continue
            aff = make_affiliate(deal.url)
            if not aff:
                seen.add(deal.uid)
                continue
            msg = deal.to_telegram(YOUR_CHANNEL, aff)
            ok, resp = post_telegram(bot_api, msg)
            if ok:
                print(f"  ✅ {deal.title[:60]}")
                seen.add(deal.uid)
                posted += 1
                total  += 1
            else:
                print(f"  ❌ {resp[:80]}")

    # ── 2. Telegram channels ─────────────────────────────────────────────────
    print("\n── Telegram channels ──")
    async with TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH) as client:
        for channel in SOURCE_CHANNELS:
            if not channel:
                continue
            last_id     = state.get(channel, 0)
            new_last_id = last_id
            found       = 0
            print(f"\n[{channel}] last id: {last_id}")
            try:
                async for msg in client.iter_messages(channel, min_id=last_id, limit=50):
                    if msg.id <= last_id:
                        continue
                    text = getattr(msg, 'text', '') or getattr(msg, 'caption', '') or ''
                    if not text:
                        if msg.id > new_last_id:
                            new_last_id = msg.id
                        continue
                    new_text, modified = rewrite_message(text)
                    if modified:
                        new_text += f"\n\n🛒 Deals by @{YOUR_CHANNEL}"
                        ok, resp = post_telegram(bot_api, new_text)
                        if ok:
                            print(f"  ✅ msg {msg.id}")
                            found += 1
                            total += 1
                        else:
                            print(f"  ❌ {resp[:80]}")
                    if msg.id > new_last_id:
                        new_last_id = msg.id
            except Exception as e:
                print(f"  ⚠️ {e}")
            state[channel] = new_last_id
            print(f"  Posted: {found}")

    save_state(state)
    save_seen(seen)
    print(f"\n✅ This run: {total} posted")


# ── Continuous loop ───────────────────────────────────────────────────────────

async def main():
    print(f"🤖 Bot started — running every {INTERVAL_SECONDS // 60} minutes")
    while True:
        try:
            print(f"\n{'='*50}")
            print(f"🔄 Run started")
            print(f"{'='*50}")
            await one_run()
        except Exception as e:
            print(f"❌ Run crashed: {e}")
        print(f"\n⏳ Sleeping {INTERVAL_SECONDS // 60} minutes...")
        await asyncio.sleep(INTERVAL_SECONDS)

if __name__ == '__main__':
    asyncio.run(main())

# This block is already in main.py — ignore if duplicate
