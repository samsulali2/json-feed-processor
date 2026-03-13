"""
Telegram Affiliate Deal Bot
- Scrapes: desidime.com, freekaamaal.com, dealsmagnet.com, lootdunia.com
- Also reads Telegram source channels
- Amazon  → Amazon Associates tag
- Others  → Cuelinks API
- Shortens URLs via TinyURL
- Posts everything to your Telegram channel
"""

import os, re, json, asyncio, requests, hashlib
from bs4 import BeautifulSoup
from telethon import TelegramClient
from telethon.sessions import StringSession

# ── Config ────────────────────────────────────────────────────────────────────
API_ID           = int(os.environ["TELEGRAM_API_ID"])
API_HASH         = os.environ["TELEGRAM_API_HASH"]
BOT_TOKEN        = os.environ["TELEGRAM_BOT_TOKEN"]
YOUR_CHANNEL     = os.environ["YOUR_CHANNEL_USERNAME"].strip().lstrip('@')
SOURCE_CHANNELS  = [c.strip().lstrip('@') for c in os.environ["SOURCE_CHANNELS"].split(",")]
AMAZON_AFFILIATE = os.environ["AMAZON_AFFILIATE_ID"].strip()
CUELINKS_API_KEY = os.environ.get("CUELINKS_API_KEY", "").strip()
SESSION_STRING   = os.environ["TELEGRAM_SESSION_STRING"].strip()
STATE_FILE       = "last_seen.json"
SEEN_FILE        = "seen_web.json"
MAX_WEB_PER_RUN  = 5   # max new deals per website per run

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  'Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'en-IN,en;q=0.9',
}
# ─────────────────────────────────────────────────────────────────────────────


# ── URL helpers ───────────────────────────────────────────────────────────────

def expand_short_url(url):
    try:
        r = requests.head(url, allow_redirects=True, timeout=10, headers=HEADERS)
        return r.url
    except Exception:
        return url

def inject_amazon_tag(url, tag):
    url = re.sub(r'([&?])tag=[^&]*', '', url)
    sep = '&' if '?' in url else '?'
    return f"{url}{sep}tag={tag}"

def is_amazon(url):
    return bool(re.match(
        r'https?://(?:www\.)?(?:amazon\.in|amazon\.com|amzn\.to|amzn\.in|a\.co)', url))

def process_amazon_url(url):
    if not is_amazon(url):
        return None
    full = expand_short_url(url)
    full = re.sub(r'/ref=[^?&]*', '', full)
    return inject_amazon_tag(full, AMAZON_AFFILIATE)

def process_cuelinks_url(url):
    if not CUELINKS_API_KEY or is_amazon(url):
        return None
    try:
        expanded = expand_short_url(url)
        resp = requests.get('https://api.cuelinks.com/v1/affiliate-url',
                            params={'apiKey': CUELINKS_API_KEY, 'url': expanded}, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            aff = data.get('affiliateUrl') or data.get('url')
            if aff and aff != expanded:
                return aff
    except Exception as e:
        print(f"    Cuelinks error: {e}")
    return None

def make_affiliate(url):
    return process_amazon_url(url) or process_cuelinks_url(url)

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
    """Replace all links in a Telegram message with affiliate versions."""
    urls = extract_urls(text)
    modified = False
    new_text = text
    for url in urls:
        aff = make_affiliate(url)
        if aff and aff != url:
            short = shorten_url(aff)
            new_text = new_text.replace(url, short)
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
        for item in soup.select('li.deal-item, div.deal-item, .sdealitem')[:30]:
            title_el = item.select_one('a.title, h2 a, h3 a, .deal-title a')
            link_el  = item.select_one('a.btn-go-to-store, a.go-to-store, a[href*="go/"]')
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            url   = link_el['href'] if link_el else title_el.get('href', '')
            if url and not url.startswith('http'):
                url = 'https://www.desidime.com' + url
            if title and url:
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
        for item in soup.select('article, .deal-box, .td-item-block')[:30]:
            title_el = item.select_one('h2 a, h3 a, .entry-title a, .td-module-title a')
            link_el  = item.select_one('a.dealBtn, a.btn-deal, a[href*="amazon"], a[href*="flipkart"]')
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
        for item in soup.select('article, .deal, .post, .product-item')[:30]:
            title_el = item.select_one('h2 a, h3 a, .entry-title a, .title a')
            link_el  = item.select_one('a[href*="amazon"], a[href*="flipkart"], a.buy-btn, a.deal-link')
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
        for item in soup.select('article, .post, .deal-item, .loot-item')[:30]:
            title_el = item.select_one('h2 a, h3 a, .entry-title a, .post-title a')
            link_el  = item.select_one('a[href*="amazon"], a[href*="flipkart"], a.buy-now, a.grab-deal')
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


# ── Main ──────────────────────────────────────────────────────────────────────

async def run():
    state   = load_state()
    seen    = load_seen()
    bot_api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    total   = 0

    print(f"Amazon: {AMAZON_AFFILIATE} | Cuelinks: {'on' if CUELINKS_API_KEY else 'off'}")

    # ── 1. Scrape deal websites ──────────────────────────────────────────────
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
                seen.add(deal.uid)   # mark as seen so we skip it next time too
                continue
            short = shorten_url(aff)
            msg   = deal.to_telegram(YOUR_CHANNEL, short)
            ok, resp = post_telegram(bot_api, msg)
            if ok:
                print(f"  ✅ {deal.title[:60]}")
                seen.add(deal.uid)
                posted += 1
                total  += 1
            else:
                print(f"  ❌ {resp[:80]}")

    # ── 2. Telegram source channels ──────────────────────────────────────────
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
    print(f"\n✅ Total posted this run: {total}")

if __name__ == '__main__':
    asyncio.run(run())
