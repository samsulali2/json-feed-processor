"""
Telegram Affiliate Deal Bot - Simple & Reliable Version
Logic:
  1. Read each Telegram source channel
  2. For every new message that has a URL:
     - Expand any short URLs (ddime.in, bit.ly etc) to get real URL
     - If Amazon → inject affiliate tag → shorten with TinyURL
     - If Flipkart/Myntra etc → Cuelinks → shorten with TinyURL
     - Remove source site reference lines from message
     - Post clean message with affiliate link to our channel
     - Save to deals.json for website
"""

import os, re, json, asyncio, requests, hashlib, io, random
from datetime import datetime, timezone
from telethon import TelegramClient
from telethon.sessions import StringSession

# ── Config ────────────────────────────────────────────────────────────────────
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

# Domains that indicate the URL is a source site reference, not a product link
SOURCE_SITE_DOMAINS = [
    'desidime.com', 'ddime.in', 'dealsmagnet.com', 'freekaamaal.com',
    'lootdunia.com', 'dealsbazaar.in', 'hcti.io',
]

# Domains we can convert to Cuelinks affiliate
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

def expand_url(url, timeout=8):
    """Follow redirects to get the real URL"""
    try:
        r = requests.head(url, allow_redirects=True, timeout=timeout, headers=HEADERS)
        return r.url
    except Exception:
        try:
            r = requests.get(url, allow_redirects=True, timeout=timeout, headers=HEADERS, stream=True)
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
    """Short URLs that need to be expanded"""
    short_domains = ['amzn.to', 'amzn.in', 'a.co/', 'bitli.store', 'bit.ly',
                     'ddime.in', 'clnk.in', 'cutt.ly', 'rb.gy', 't.ly',
                     'tiny.cc', 'ow.ly', 'shorturl.at']
    return any(d in url for d in short_domains)

def make_amazon_affiliate(url):
    """Convert any Amazon URL to clean affiliate URL"""
    # Extract ASIN
    asin = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', url)
    if asin:
        clean = f"https://www.amazon.in/dp/{asin.group(1)}?tag={AMAZON_TAG}"
    else:
        # Remove existing tag and add ours
        url = re.sub(r'[?&]tag=[^&]*', '', url)
        url = re.sub(r'[?&]ascsubtag=[^&]*', '', url)
        url = re.sub(r'/ref=[^/?&]*', '', url)
        sep = '&' if '?' in url else '?'
        clean = f"{url}{sep}tag={AMAZON_TAG}"
    return clean

def make_cuelinks_affiliate(url):
    """Convert Flipkart/Myntra etc URL to Cuelinks affiliate URL"""
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
    """Shorten URL with TinyURL"""
    try:
        resp = requests.get(f'https://tinyurl.com/api-create.php?url={url}', timeout=10)
        if resp.status_code == 200 and resp.text.startswith('http'):
            return resp.text.strip()
    except Exception:
        pass
    return url

def get_amazon_image(url):
    """Get Amazon product image URL from ASIN"""
    asin = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', url)
    if asin:
        return f"https://m.media-amazon.com/images/I/{asin.group(1)}._SL500_.jpg"
    return ''

def process_url(url):
    """
    Main URL processor:
    Returns (affiliate_url, image_url) or (None, None) if not monetizable
    """
    # Step 1: expand if short URL
    if needs_expanding(url):
        print(f"    expanding: {url[:60]}")
        expanded = expand_url(url)
        if expanded != url:
            print(f"    → {expanded[:80]}")
            url = expanded

    # Step 2: convert to affiliate
    if is_amazon(url):
        aff = make_amazon_affiliate(url)
        short = shorten(aff)
        image = get_amazon_image(aff)
        return short, image

    if is_flipkart_family(url):
        aff = make_cuelinks_affiliate(url)
        if aff:
            short = shorten(aff)
            return short, ''
        return None, None

    return None, None


# ── Message processor ─────────────────────────────────────────────────────────

def process_message(raw_text):
    """
    Process a Telegram message:
    - Find all URLs
    - Try to get affiliate link for each
    - Clean up source site lines
    - Return (clean_text, affiliate_url, image_url)
    """
    if not raw_text:
        return None, None, None

    urls = extract_urls(raw_text)
    if not urls:
        return None, None, None

    affiliate_url = None
    image_url = ''

    # Try each URL to find an affiliate-able one
    for url in urls:
        # Skip Telegram, social media, image links
        if any(d in url for d in ['t.me', 'telegram.me', 'instagram.com',
                                   'twitter.com', 'facebook.com', 'youtube.com',
                                   'hcti.io', 'play.google.com']):
            continue

        aff, img = process_url(url)
        if aff:
            affiliate_url = aff
            image_url = img or ''
            break

    # Clean the message text
    lines = raw_text.split('\n')
    clean_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            clean_lines.append('')
            continue
        # Remove lines that are just source site references
        skip = False
        for url in extract_urls(stripped):
            if is_source_site(url):
                skip = True
                break
        if skip:
            continue
        # Remove common noise lines
        if stripped.startswith('On #'):
            continue
        if stripped.lower().startswith('read more'):
            continue
        if stripped.lower().startswith('buy now') and not affiliate_url:
            continue
        clean_lines.append(line)

    clean_text = re.sub(r'\n{3,}', '\n\n', '\n'.join(clean_lines)).strip()

    if not clean_text:
        return None, None, None

    return clean_text, affiliate_url, image_url


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

def post_to_telegram(bot_token, channel, text):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                'chat_id':                  f'@{channel}',
                'text':                     text,
                'disable_web_page_preview': True,
            },
            timeout=15
        )
        return r.status_code == 200, r.text
    except Exception as e:
        return False, str(e)


# ── Telegraph upload ──────────────────────────────────────────────────────────

async def upload_to_telegraph(client, msg):
    """Download photo from Telegram msg and upload to Telegraph"""
    try:
        photo_bytes = await client.download_media(msg.photo, bytes)
        if photo_bytes:
            files = {'file': ('image.jpg', io.BytesIO(photo_bytes), 'image/jpeg')}
            resp = requests.post('https://telegra.ph/upload', files=files, timeout=15)
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

    print(f"Channel: @{YOUR_CHANNEL}")
    print(f"Amazon tag: {AMAZON_TAG}")
    print(f"Cuelinks: {'on' if CUELINKS_KEY else 'off'}")
    print(f"Sources: {len(SOURCE_CHANNELS)} channels")

    posted_hashes = set()  # prevent cross-channel duplicates within same run

    async with TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH) as client:
        for channel in SOURCE_CHANNELS:
            if not channel:
                continue

            last_id     = state.get(channel, 0)
            new_last_id = last_id
            found       = 0
            limit       = 5 if last_id == 0 else 20

            print(f"\n── {channel} (last_id={last_id}) ──")

            try:
                async def read_channel(ch=channel, lid=last_id, lim=limit):
                    nonlocal new_last_id, found, total, deals

                    async for msg in client.iter_messages(ch, min_id=lid, limit=lim):
                        if msg.id <= lid:
                            continue

                        # Update last seen
                        if msg.id > new_last_id:
                            new_last_id = msg.id

                        # Get message text
                        raw = getattr(msg, 'text', '') or getattr(msg, 'caption', '') or ''
                        if not raw.strip():
                            continue

                        # Deduplicate across channels
                        msg_hash = hashlib.md5(raw[:80].encode()).hexdigest()[:8]
                        if msg_hash in posted_hashes:
                            print(f"  ⏭️  msg {msg.id} duplicate — skipping")
                            continue

                        # Process message
                        clean_text, affiliate_url, image_url = process_message(raw)

                        if not clean_text:
                            print(f"  ⬜ msg {msg.id} — no content after clean")
                            continue

                        # If no affiliate URL found but message has a photo, try Telegraph
                        if not image_url and hasattr(msg, 'photo') and msg.photo:
                            image_url = await upload_to_telegraph(client, msg)

                        # Build final message
                        final_text = clean_text
                        if affiliate_url:
                            final_text += f"\n\n🔗 {affiliate_url}"
                        final_text += f"\n\n🛒 Deals by @{YOUR_CHANNEL}"

                        # Post to Telegram
                        ok, resp = post_to_telegram(BOT_TOKEN, YOUR_CHANNEL, final_text)
                        if ok:
                            print(f"  ✅ msg {msg.id} {'→ affiliate' if affiliate_url else '(no aff)'}")
                            posted_hashes.add(msg_hash)
                            deals = add_deal(deals, final_text, affiliate_url or '', ch, image_url)
                            found += 1
                            total += 1
                        else:
                            print(f"  ❌ msg {msg.id}: {resp[:80]}")

                await asyncio.wait_for(read_channel(), timeout=30)

            except asyncio.TimeoutError:
                print(f"  ⏱️ timeout")
            except Exception as e:
                print(f"  ⚠️ {e}")

            state[channel] = new_last_id
            print(f"  posted: {found}")

    # Save everything
    save_state(state)
    save_deals(deals)
    print(f"\n✅ Done: {total} posted | {len(deals)} deals on website")

if __name__ == '__main__':
    asyncio.run(run())
