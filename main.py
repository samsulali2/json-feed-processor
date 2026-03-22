import os, re, json, asyncio, requests, hashlib, io, random, time
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID           = int(os.environ["A1"])
API_HASH         = os.environ["A2"]
BOT_TOKEN        = os.environ["A3"]
SESSION_STRING   = os.environ["A4"].strip()
YOUR_CHANNEL     = os.environ["A5"].strip().lstrip('@')
SOURCE_CHANNELS  = [c.strip().lstrip('@') for c in os.environ["A6"].split(",")]
AMAZON_AFFILIATE = os.environ["A7"].strip()
CUELINKS_API_KEY = os.environ.get("A8", "").strip()

STATE_FILE  = "last_seen.json"
SEEN_FILE   = "seen_web.json"
DEALS_FILE  = "deals.json"
MAX_DEALS   = 200
MAX_WEB_RUN = 5

# Rotate user agents to avoid detection
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
]

def get_headers():
    return {
        'User-Agent': random.choice(USER_AGENTS),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-IN,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
    }

CUELINKS_DOMAINS = {
    'flipkart.com', 'myntra.com', 'ajio.com', 'nykaa.com',
    'tatacliq.com', 'shopsy.in', 'meesho.com', 'jiomart.com',
    'croma.com', 'vijaysales.com', 'reliancedigital.com',
}

SHORTENERS = [
    'bitli.store', 'bit.ly', 'tiny.cc', 'ow.ly', 'ddime.in',
    'clnk.in', 'shrinkme.io', 'ouo.io', 'adf.ly', 'shorturl.at',
    'cutt.ly', 'rb.gy', 't.ly',
]

# Sites we scrape — skip their URLs when rewriting
SKIP_DOMAINS = [
    'dealsmagnet.com', 'desidime.com', 'freekaamaal.com', 'lootdunia.com',
    'dealsbazaar.in', 'telegram.me', 't.me', 'hcti.io', 'play.google.com',
    'instagram.com', 'twitter.com', 'facebook.com', 'youtube.com',
]


# ── URL helpers ───────────────────────────────────────────────────────────────

def is_amazon(url):
    return bool(re.search(r'amazon\.in|amazon\.com|amzn\.to|amzn\.in|a\.co/', url))

def is_cuelinks_supported(url):
    return any(d in url for d in CUELINKS_DOMAINS)

def is_shortener(url):
    return any(s in url for s in SHORTENERS)

def expand_short_url(url):
    try:
        r = requests.head(url, allow_redirects=True, timeout=8, headers=get_headers())
        return r.url
    except Exception:
        return url

def inject_amazon_tag(url, tag):
    url = re.sub(r'[?&]tag=[^&]*', '', url)
    url = re.sub(r'[?&]ascsubtag=[^&]*', '', url)
    sep = '&' if '?' in url else '?'
    return f"{url}{sep}tag={tag}"

def shorten_url(url):
    try:
        resp = requests.get(f'https://tinyurl.com/api-create.php?url={url}', timeout=10)
        if resp.status_code == 200 and resp.text.startswith('http'):
            return resp.text.strip()
    except Exception:
        pass
    return url

def process_amazon_url(url):
    if re.search(r'amzn\.to|amzn\.in|a\.co/', url):
        url = expand_short_url(url)
    if not is_amazon(url):
        return None
    asin = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', url)
    if asin:
        clean = f"https://www.amazon.in/dp/{asin.group(1)}?tag={AMAZON_AFFILIATE}"
    else:
        url = re.sub(r'/ref=[^/?&]*', '', url)
        clean = inject_amazon_tag(url, AMAZON_AFFILIATE)
    return shorten_url(clean)

def process_cuelinks_url(url):
    if not CUELINKS_API_KEY or not is_cuelinks_supported(url):
        return None
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
    if is_shortener(url):
        print(f"    🔗 Expanding: {url}")
        expanded = expand_short_url(url)
        if expanded and expanded != url and not is_shortener(expanded):
            url = expanded
            print(f"    🔗 → {url[:80]}")
        else:
            return None
    if is_amazon(url) or re.search(r'amzn\.to|amzn\.in|a\.co/', url):
        return process_amazon_url(url)
    if is_cuelinks_supported(url):
        return process_cuelinks_url(url)
    return None

def get_amazon_image_url(url):
    """Get Amazon product image from ASIN — free, official, no auth needed"""
    asin = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', url)
    if asin:
        return f"https://m.media-amazon.com/images/I/{asin.group(1)}._SL500_.jpg"
    return ''

def extract_urls(text):
    return re.findall(r'https?://[^\s\)\]>\"\']+', text or '')

def get_best_url(urls):
    if not urls: return ''
    for url in urls:
        if is_amazon(url) or re.search(r'amzn\.to|amzn\.in|a\.co/', url):
            return url
    for url in urls:
        if is_cuelinks_supported(url):
            return url
    for url in urls:
        if is_shortener(url):
            return url
    return urls[0]

def clean_message(text):
    """Remove source site URLs and hashtag lines from message"""
    lines = text.split('\n')
    clean_lines = []
    for line in lines:
        stripped = line.strip()
        # Skip empty lines (will re-add spacing later)
        if not stripped:
            clean_lines.append('')
            continue
        # Skip lines that contain only source site URLs
        if any(d in stripped for d in SKIP_DOMAINS):
            continue
        # Skip lines that are ONLY hashtags like "On #Flipkart #Category"
        if stripped.startswith('On #') or re.match(r'^#+\w+(\s+#+\w+)*$', stripped):
            continue
        # Skip lines starting with "Link:" 
        if stripped.lower().startswith('link:'):
            continue
        clean_lines.append(line)
    text = '\n'.join(clean_lines)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text


    urls = extract_urls(text)
    modified = False
    new_text = text
    for url in urls:
        if any(d in url for d in SKIP_DOMAINS):
            continue
        aff = make_affiliate(url)
        if aff and aff != url:
            new_text = new_text.replace(url, aff)
            modified = True
    return new_text, modified


# ── Telegraph image upload ────────────────────────────────────────────────────

async def upload_to_telegraph(photo_bytes):
    try:
        files = {'file': ('image.jpg', io.BytesIO(photo_bytes), 'image/jpeg')}
        resp = requests.post('https://telegra.ph/upload', files=files, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and data:
                return f"https://telegra.ph{data[0]['src']}"
    except Exception as e:
        print(f"    Telegraph error: {e}")
    return ''


# ── Deal class ────────────────────────────────────────────────────────────────

class Deal:
    def __init__(self, title, url, source, image_url='', price='', original_price='', discount=''):
        self.title          = title.strip()[:300]
        self.url            = url
        self.source         = source
        self.image_url      = image_url
        self.price          = price
        self.original_price = original_price
        self.discount       = discount
        self.uid            = hashlib.md5(url.encode()).hexdigest()[:12]

    def to_telegram(self, channel, short_url):
        msg = f"🔥 {self.title}\n"
        if self.discount:
            msg += f"🏷️ {self.discount}% OFF"
        if self.price:
            msg += f" | ✅ ₹{self.price}"
        if self.original_price:
            msg += f" ❌ ₹{self.original_price}"
        msg += f"\n\n🔗 {short_url}\n\n🛒 Deals by @{channel}"
        return msg


# ── Scrapers ──────────────────────────────────────────────────────────────────
# NOTE: We only read publicly visible deal information (titles, prices).
# We do NOT use site images — we use Amazon's own CDN for product images.
# We add random delays between requests to be respectful.

def safe_get(url, delay_range=(2, 5)):
    """Fetch URL with random delay and rotating user agent"""
    time.sleep(random.uniform(*delay_range))
    return requests.get(url, headers=get_headers(), timeout=15)

def scrape_desidime():
    """Read public deal titles and links from desidime.com/deals"""
    deals = []
    try:
        r = safe_get('https://www.desidime.com/deals')
        soup = BeautifulSoup(r.text, 'html.parser')
        items = soup.select('li.deal-item') or soup.select('div.deal-item') or soup.select('article')
        for item in items[:40]:
            title_el = item.select_one('a.title') or item.select_one('h2 a') or item.select_one('h3 a')
            link_el  = (item.select_one('a[href*="amazon"]') or
                       item.select_one('a[href*="flipkart"]') or
                       item.select_one('a.btn-go-to-store'))
            if not title_el: continue
            title = title_el.get_text(strip=True)
            url   = link_el['href'] if link_el else title_el.get('href', '')
            if not url: continue
            if not url.startswith('http'): url = 'https://www.desidime.com' + url
            if title and len(title) > 5:
                deals.append(Deal(title, url, 'desidime'))
    except Exception as e:
        print(f"  desidime error: {e}")
    print(f"  desidime: {len(deals)} found")
    return deals

def scrape_freekaamaal():
    """Read public deal titles and links from freekaamaal.com"""
    deals = []
    try:
        r = safe_get('https://www.freekaamaal.com/')
        soup = BeautifulSoup(r.text, 'html.parser')
        items = soup.select('article') or soup.select('.deal-box')
        for item in items[:40]:
            title_el = (item.select_one('h2 a') or item.select_one('h3 a') or
                       item.select_one('.entry-title a'))
            link_el  = (item.select_one('a[href*="amazon"]') or
                       item.select_one('a[href*="flipkart"]'))
            if not title_el: continue
            title = title_el.get_text(strip=True)
            url   = link_el['href'] if link_el else title_el.get('href', '')
            if not url or not url.startswith('http'): continue
            deals.append(Deal(title, url, 'freekaamaal'))
    except Exception as e:
        print(f"  freekaamaal error: {e}")
    print(f"  freekaamaal: {len(deals)} found")
    return deals

def scrape_dealsmagnet():
    """
    Read public deal info from dealsmagnet.com/new
    - Only uses publicly visible titles, prices (factual data — not copyrightable)
    - Does NOT use their images — uses Amazon CDN instead
    - Does NOT use their redirect URLs — extracts real store links
    """
    deals = []
    try:
        r = safe_get('https://www.dealsmagnet.com/new')
        soup = BeautifulSoup(r.text, 'html.parser')
        items = soup.select('div.col-lg-4, div.col-md-6')
        print(f"  dealsmagnet: {len(items)} containers found")

        for item in items[:60]:
            try:
                title_el = item.select_one('.details-block .title a') or item.select_one('.title a')
                if not title_el: continue
                title = title_el.get_text(strip=True)
                if not title or len(title) < 5: continue

                # Price info (factual data)
                price_el    = item.select_one('.DiscountedPrice')
                orig_el     = item.select_one('.OriginalPrice')
                discount_el = item.select_one('.Discount')
                price    = price_el.get_text(strip=True).replace('₹','').replace(',','').strip() if price_el else ''
                orig     = orig_el.get_text(strip=True).replace('₹','').replace(',','').strip() if orig_el else ''
                discount = re.sub(r'[^\d]', '', discount_el.get_text()) if discount_el else ''

                # Get deal page link — we'll expand to real store URL
                link_el = item.select_one('a[href*="/deal/"]')
                deal_page = link_el['href'] if link_el else ''
                if deal_page and not deal_page.startswith('http'):
                    deal_page = 'https://www.dealsmagnet.com' + deal_page

                # Look for direct Amazon/Flipkart links in the card
                store_link = (item.select_one('a[href*="amazon.in"]') or
                             item.select_one('a[href*="flipkart.com"]') or
                             item.select_one('a[href*="myntra.com"]'))
                buy_url = store_link['href'] if store_link else deal_page
                if not buy_url: continue

                # No dealsmagnet images — website will use Amazon CDN
                deals.append(Deal(title, buy_url, 'dealsmagnet', '', price, orig, discount))

            except Exception:
                continue

    except Exception as e:
        print(f"  dealsmagnet error: {e}")
    print(f"  dealsmagnet: {len(deals)} found")
    return deals

def scrape_lootdunia():
    """Read public deal titles and links from lootdunia.com"""
    deals = []
    try:
        r = safe_get('https://lootdunia.com/')
        soup = BeautifulSoup(r.text, 'html.parser')
        items = soup.select('article') or soup.select('.post')
        for item in items[:40]:
            title_el = (item.select_one('h2 a') or item.select_one('h3 a') or
                       item.select_one('.entry-title a'))
            link_el  = (item.select_one('a[href*="amazon"]') or
                       item.select_one('a[href*="flipkart"]'))
            if not title_el: continue
            title = title_el.get_text(strip=True)
            url   = link_el['href'] if link_el else title_el.get('href', '')
            if not url or not url.startswith('http'): continue
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


# ── State / Deals helpers ─────────────────────────────────────────────────────

def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f: return set(json.load(f))
    return set()

def save_seen(seen):
    with open(SEEN_FILE, 'w') as f: json.dump(list(seen)[-2000:], f)

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f: return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, 'w') as f: json.dump(state, f)

def load_deals():
    if os.path.exists(DEALS_FILE):
        with open(DEALS_FILE) as f: return json.load(f)
    return []

def save_deal(deals, text, url, source, image_url=''):
    deals.insert(0, {
        'text':      text,
        'url':       url,
        'source':    source,
        'image':     image_url,
        'timestamp': datetime.now(timezone.utc).isoformat()
    })
    deals = deals[:MAX_DEALS]
    with open(DEALS_FILE, 'w') as f: json.dump(deals, f, ensure_ascii=False, indent=2)
    return deals

def post_telegram(bot_api, text):
    r = requests.post(bot_api, json={
        'chat_id': f'@{YOUR_CHANNEL}',
        'text': text,
        'disable_web_page_preview': True
    }, timeout=15)
    return r.status_code == 200, r.text


# ── Main ──────────────────────────────────────────────────────────────────────

async def run():
    state   = load_state()
    seen    = load_seen()
    deals   = load_deals()
    bot_api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    total   = 0

    print(f"Amazon: {AMAZON_AFFILIATE} | Cuelinks: {'on' if CUELINKS_API_KEY else 'off'}")

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
            if posted >= MAX_WEB_RUN: break
            if deal.uid in seen: continue
            aff = make_affiliate(deal.url)
            if not aff:
                seen.add(deal.uid)
                continue
            msg = deal.to_telegram(YOUR_CHANNEL, aff)
            ok, resp = post_telegram(bot_api, msg)
            if ok:
                print(f"  ✅ {deal.title[:60]}")
                seen.add(deal.uid)
                # Always use Amazon CDN for images — never third-party site images
                image_url = get_amazon_image_url(aff) if is_amazon(aff) else ''
                deals = save_deal(deals, msg, aff, site_name, image_url)
                posted += 1
                total  += 1
            else:
                print(f"  ❌ {resp[:80]}")

    # ── 2. Telegram channels ─────────────────────────────────────────────────
    print("\n── Telegram channels ──")
    async with TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH) as client:
        for channel in SOURCE_CHANNELS:
            if not channel: continue
            last_id     = state.get(channel, 0)
            new_last_id = last_id
            found       = 0
            limit       = 5 if last_id == 0 else 20
            print(f"\n[{channel}] last_id: {last_id}")
            try:
                async def read_ch(ch=channel, lid=last_id, lim=limit):
                    nonlocal new_last_id, found, total, deals
                    async for msg in client.iter_messages(ch, min_id=lid, limit=lim):
                        if msg.id <= lid: continue
                        text = getattr(msg, 'text', '') or getattr(msg, 'caption', '') or ''
                        if not text:
                            if msg.id > new_last_id: new_last_id = msg.id
                            continue

                        # Clean source site references first
                        text = clean_message(text)
                        if not text.strip():
                            if msg.id > new_last_id: new_last_id = msg.id
                            continue

                        new_text, modified = rewrite_message(text)

                        # Image: Amazon CDN (instant) or Telegraph upload
                        image_url = ''
                        all_urls  = extract_urls(new_text)
                        deal_url  = get_best_url(all_urls)

                        if deal_url and is_amazon(deal_url):
                            image_url = get_amazon_image_url(deal_url)
                        elif hasattr(msg, 'photo') and msg.photo:
                            try:
                                photo_bytes = await client.download_media(msg.photo, bytes)
                                if photo_bytes:
                                    image_url = await upload_to_telegraph(photo_bytes)
                                    if image_url:
                                        print(f"    📷 {image_url}")
                            except Exception as e:
                                print(f"    📷 error: {e}")

                        urls = extract_urls(new_text)
                        if urls or new_text.strip():
                            new_text += f"\n\n🛒 Deals by @{YOUR_CHANNEL}"
                            ok, resp = post_telegram(bot_api, new_text)
                            if ok:
                                print(f"  ✅ msg {msg.id} {'(aff)' if modified else ''} {'📷' if image_url else ''}")
                                deals = save_deal(deals, new_text, deal_url, ch, image_url)
                                found += 1
                                total += 1
                            else:
                                print(f"  ❌ {resp[:80]}")
                        if msg.id > new_last_id: new_last_id = msg.id

                await asyncio.wait_for(read_ch(), timeout=25)
            except asyncio.TimeoutError:
                print(f"  ⏱️ Timeout — skipping")
            except Exception as e:
                print(f"  ⚠️ {e}")

            state[channel] = new_last_id
            print(f"  Posted: {found}")

    save_state(state)
    save_seen(seen)
    print(f"\n✅ Total: {total} posted | deals.json: {len(deals)} entries")

if __name__ == '__main__':
    asyncio.run(run())
