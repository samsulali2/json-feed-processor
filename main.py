import os, re, json, asyncio, requests, hashlib, io, random
from datetime import datetime, timezone
from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID           = int(os.environ["A1"])
API_HASH         = os.environ["A2"]
BOT_TOKEN        = os.environ["A3"]
SESSION_STRING   = os.environ["A4"].strip()
YOUR_CHANNEL     = os.environ["A5"].strip().lstrip('@')
SOURCE_CHANNELS  = [c.strip().lstrip('@') for c in os.environ["A6"].split(",") if c.strip()]
AMAZON_TAG       = os.environ["A7"].strip()
CUELINKS_KEY     = os.environ.get("A8", "").strip()

STATE_FILE = "last_seen.json"
DEALS_FILE = "deals.json"
MAX_DEALS  = 200

SOURCE_SITE_DOMAINS = [
    'desidime.com', 'dealsmagnet.com', 'freekaamaal.com',
    'lootdunia.com', 'dealsbazaar.in', 'hcti.io',
]

SHORTENER_DOMAINS = [
    'ddime.in', 'amzn.to', 'amzn.in', 'a.co/',
    'bitli.store', 'bit.ly', 'clnk.in', 'cutt.ly',
    'rb.gy', 't.ly', 'tiny.cc', 'ow.ly', 'shorturl.at',
]

CUELINKS_DOMAINS = [
    'flipkart.com', 'myntra.com', 'ajio.com', 'nykaa.com',
    'tatacliq.com', 'shopsy.in', 'meesho.com', 'jiomart.com',
    'croma.com',
]

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'en-IN,en;q=0.9',
}

# ── URL utilities ─────────────────────────────────────────────────────────────

def extract_urls(text):
    return re.findall(r'https?://[^\s\)\]>\"\']+', text or '')

def expand_url(url):
    try:
        r = requests.head(url, allow_redirects=True, timeout=8, headers=HEADERS)
        return r.url
    except Exception:
        try:
            r = requests.get(url, allow_redirects=True, timeout=8, headers=HEADERS, stream=True)
            return r.url
        except Exception:
            return url

def is_amazon(url):
    return bool(re.search(r'amazon\.in|amazon\.com|amzn\.to|amzn\.in', url))

def is_flipkart_family(url):
    return any(d in url for d in CUELINKS_DOMAINS)

def is_source_site(url):
    return any(d in url for d in SOURCE_SITE_DOMAINS)

def needs_expanding(url):
    return any(d in url for d in SHORTENER_DOMAINS)

def make_amazon_affiliate(url):
    asin = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', url)
    if asin:
        return f"https://www.amazon.in/dp/{asin.group(1)}?tag={AMAZON_TAG}"
    url = re.sub(r'[?&]tag=[^&]*', '', url)
    url = re.sub(r'[?&]ascsubtag=[^&]*', '', url)
    url = re.sub(r'/ref=[^/?&]*', '', url)
    sep = '&' if '?' in url else '?'
    return f"{url}{sep}tag={AMAZON_TAG}"

def make_cuelinks_affiliate(url):
    if not CUELINKS_KEY:
        return None
    try:
        resp = requests.get(
            'https://api.cuelinks.com/v1/affiliate-url',
            params={'apiKey': CUELINKS_KEY, 'url': url},
            timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            aff = data.get('affiliateUrl') or data.get('url')
            if aff and aff != url:
                return aff
    except Exception as e:
        print(f"    Cuelinks error: {e}")
    return None

def shorten(url):
    try:
        resp = requests.get(f'https://tinyurl.com/api-create.php?url={url}', timeout=10)
        if resp.status_code == 200 and resp.text.startswith('http'):
            return resp.text.strip()
    except Exception:
        pass
    return url

def get_amazon_image(url):
    asin = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', url)
    if asin:
        return f"https://m.media-amazon.com/images/I/{asin.group(1)}._SL500_.jpg"
    return ''

def process_url(url):
    """Returns (affiliate_url, image_url) or (None, None)"""
    if needs_expanding(url):
        expanded = expand_url(url)
        if expanded != url:
            print(f"    expanded: {url[:50]} → {expanded[:60]}")
            url = expanded
    if is_amazon(url):
        aff   = make_amazon_affiliate(url)
        short = shorten(aff)
        image = get_amazon_image(aff)
        return short, image
    if is_flipkart_family(url):
        aff = make_cuelinks_affiliate(url)
        if aff:
            return shorten(aff), ''
    return None, None

def process_message(raw_text):
    """Returns (clean_text, affiliate_url, image_url)"""
    if not raw_text or not raw_text.strip():
        return None, None, None

    urls = extract_urls(raw_text)
    if not urls:
        return None, None, None

    IGNORE = ['t.me', 'telegram.me', 'instagram.com', 'twitter.com',
              'facebook.com', 'youtube.com', 'hcti.io', 'play.google.com']

    working_text   = raw_text
    best_affiliate = None
    best_image     = ''

    for url in urls:
        if any(d in url for d in IGNORE):
            continue
        aff, img = process_url(url)
        if aff:
            working_text = working_text.replace(url, aff)
            if not best_affiliate:
                best_affiliate = aff
                best_image     = img or ''
        else:
            if is_source_site(url) or needs_expanding(url):
                working_text = working_text.replace(url, '')

    lines = working_text.split('\n')
    clean_lines = []
    for line in lines:
        s = line.strip()
        if not s:
            clean_lines.append('')
            continue
        if s.startswith('On #'):
            continue
        if re.match(r'^#\w', s):
            continue
        if s.lower().startswith('read more'):
            continue
        if s.lower().startswith('buy now'):
            continue
        if re.match(r'^link:\s*$', s, re.IGNORECASE):
            continue
        if s.endswith(':') and len(s) < 20 and not extract_urls(s):
            continue
        clean_lines.append(line)

    clean_text = re.sub(r'\n{3,}', '\n\n', '\n'.join(clean_lines)).strip()
    if not clean_text:
        return None, None, None

    return clean_text, best_affiliate, best_image

# ── State / Deals ─────────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)

def load_deals():
    if os.path.exists(DEALS_FILE):
        with open(DEALS_FILE) as f:
            return json.load(f)
    return []

def save_deals(deals):
    with open(DEALS_FILE, 'w') as f:
        json.dump(deals[:MAX_DEALS], f, ensure_ascii=False, indent=2)

def add_deal(deals, text, url, source, image):
    deals.insert(0, {
        'text':      text,
        'url':       url or '',
        'source':    source,
        'image':     image or '',
        'timestamp': datetime.now(timezone.utc).isoformat()
    })
    return deals

def post_to_telegram(text):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                'chat_id':                  f'@{YOUR_CHANNEL}',
                'text':                     text,
                'disable_web_page_preview': True,
            },
            timeout=15
        )
        return r.status_code == 200, r.text
    except Exception as e:
        return False, str(e)

async def upload_to_telegraph(client, msg):
    try:
        photo_bytes = await client.download_media(msg.photo, bytes)
        if photo_bytes:
            files = {'file': ('image.jpg', io.BytesIO(photo_bytes), 'image/jpeg')}
            resp  = requests.post('https://telegra.ph/upload', files=files, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and data:
                    return f"https://telegra.ph{data[0]['src']}"
    except Exception as e:
        print(f"    telegraph error: {e}")
    return ''

# ── Main ──────────────────────────────────────────────────────────────────────

async def run():
    state  = load_state()
    deals  = load_deals()
    total  = 0

    print(f"Channel  : @{YOUR_CHANNEL}")
    print(f"Amazon   : {AMAZON_TAG}")
    print(f"Cuelinks : {'on' if CUELINKS_KEY else 'off'}")
    print(f"Sources  : {len(SOURCE_CHANNELS)} channels")
    print(f"Session  : {SESSION_STRING[:20]}...")

    posted_hashes = set()

    print("\nConnecting to Telegram...")
    try:
        client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            print("❌ SESSION EXPIRED — regenerate A4 session string via Colab")
            return
        print("✅ Connected")
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        return

    async with client:
        for channel in SOURCE_CHANNELS:
            if not channel:
                continue

            last_id     = state.get(channel, 0)
            new_last_id = last_id
            found       = 0
            limit       = 5 if last_id == 0 else 20

            print(f"\n── {channel} (last={last_id}, limit={limit}) ──")

            try:
                count = 0
                async for msg in client.iter_messages(channel, min_id=last_id, limit=limit):
                    count += 1
                    if msg.id > new_last_id:
                        new_last_id = msg.id

                    raw = getattr(msg, 'text', '') or getattr(msg, 'caption', '') or ''
                    print(f"  msg {msg.id}: {len(raw)} chars | has_photo={bool(getattr(msg,'photo',None))}")

                    if not raw.strip():
                        continue

                    msg_hash = hashlib.md5(raw[:80].encode()).hexdigest()[:8]
                    if msg_hash in posted_hashes:
                        print(f"    → duplicate skip")
                        continue

                    clean_text, aff_url, image_url = process_message(raw)
                    print(f"    → clean={bool(clean_text)} aff={bool(aff_url)} img={bool(image_url)}")

                    if not clean_text:
                        continue

                    if not image_url and getattr(msg, 'photo', None):
                        image_url = await upload_to_telegraph(client, msg)

                    final = clean_text
                    if aff_url:
                        final += f"\n\n🔗 {aff_url}"
                    final += f"\n\n🛒 Deals by @{YOUR_CHANNEL}"

                    ok, resp = post_to_telegram(final)
                    if ok:
                        print(f"    ✅ posted!")
                        posted_hashes.add(msg_hash)
                        deals = add_deal(deals, final, aff_url or '', channel, image_url)
                        found += 1
                        total += 1
                    else:
                        print(f"    ❌ {resp[:100]}")

                print(f"  scanned {count} msgs | posted {found}")

            except Exception as e:
                import traceback
                print(f"  ❌ ERROR: {e}")
                traceback.print_exc()

            state[channel] = new_last_id

    save_state(state)
    save_deals(deals)
    print(f"\n✅ Done: {total} posted | {len(deals)} on website")

if __name__ == '__main__':
    asyncio.run(run())
