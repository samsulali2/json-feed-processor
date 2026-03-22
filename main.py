"""
Telegram Affiliate Deal Bot  v5.0 — DEFINITIVE FIX
===================================================
ROOT CAUSE of all previous failures:
  Telegram messages often use hyperlinked text: [Buy Now](https://ddime.in/xxx)
  Telethon's msg.text gives "Buy Now" — the URL is INVISIBLE in the text.
  The URL lives in msg.entities (MessageEntityTextUrl).
  All previous versions only scanned msg.text and missed these URLs entirely.

This version:
  1. Extracts URLs from BOTH msg.text AND msg.entities
  2. Expands ALL shorteners (ddime.in, amzn.clnk.in, bit.ly etc)
  3. Converts to affiliate (Amazon tag / Cuelinks)
  4. Downloads product image ourselves (browser headers) → sendPhoto
  5. Posts clean message with working buy link
"""

import os, re, json, asyncio, requests, hashlib, io, random, time, traceback
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import (MessageEntityTextUrl, MessageEntityUrl,
                                MessageEntityBold, MessageEntityItalic)

VERSION = "5.0"

# ── Config ────────────────────────────────────────────────────────────────────
API_ID          = int(os.environ["A1"])
API_HASH        = os.environ["A2"]
BOT_TOKEN       = os.environ["A3"]
SESSION_STRING  = os.environ["A4"].strip()
YOUR_CHANNEL    = os.environ["A5"].strip().lstrip('@')
SOURCE_CHANNELS = [c.strip().lstrip('@') for c in os.environ["A6"].split(",") if c.strip()]
AMAZON_TAG      = os.environ["A7"].strip()
CUELINKS_KEY    = os.environ.get("A8", "").strip()

STATE_FILE = "last_seen.json"
DEALS_FILE = "deals.json"
MAX_DEALS  = 200

# ALL shortener/redirect domains — expand these to get real product URL
SHORTENER_DOMAINS = [
    'ddime.in', 'amzn.clnk.in', 'clnk.in',   # ← amzn.clnk.in was missing!
    'amzn.to', 'amzn.in', 'a.co/',
    'bitli.store', 'bit.ly', 'cutt.ly',
    'rb.gy', 't.ly', 'tiny.cc', 'ow.ly', 'shorturl.at',
    'dl.flipkart.com',  # Flipkart short links
]

# Deal aggregator pages — these are NOT product URLs, remove them
SOURCE_SITE_DOMAINS = [
    'desidime.com', 'dealsmagnet.com', 'freekaamaal.com',
    'lootdunia.com', 'dealsbazaar.in', 'hcti.io',
]

# Cuelinks-supported stores
CUELINKS_DOMAINS = [
    'flipkart.com', 'myntra.com', 'ajio.com', 'nykaa.com',
    'tatacliq.com', 'shopsy.in', 'meesho.com', 'jiomart.com',
    'croma.com',
]

# Completely ignore these in message text
IGNORE_URL_DOMAINS = [
    't.me', 'telegram.me', 'instagram.com', 'twitter.com',
    'facebook.com', 'youtube.com', 'play.google.com', 'hcti.io',
]

BROWSE_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                   'AppleWebKit/537.36 (KHTML, like Gecko) '
                   'Chrome/120.0.0.0 Safari/537.36'),
    'Accept-Language': 'en-IN,en;q=0.9',
}

# ── URL helpers ───────────────────────────────────────────────────────────────

def extract_text_urls(text):
    """Extract plain URLs visible in message text"""
    return re.findall(r'https?://[^\s\)\]>\"\'<\u2019\u201d]+', text or '')

def extract_all_urls_from_msg(msg):
    """
    THE KEY FIX: Extract URLs from BOTH text AND entities.
    Telegram hyperlinked text [Buy Now](url) hides the URL in entities.
    Returns list of (url, entity_type) tuples.
    """
    urls = []
    text = msg.text or msg.message or ''

    # 1. Plain URLs in text
    for url in extract_text_urls(text):
        urls.append(url)

    # 2. URLs hidden in message entities (hyperlinked text)
    if msg.entities:
        for entity in msg.entities:
            if isinstance(entity, MessageEntityTextUrl):
                # This is [display text](url) — the url is in entity.url
                if entity.url and entity.url not in urls:
                    urls.append(entity.url)
            elif isinstance(entity, MessageEntityUrl):
                # Plain URL entity
                url = text[entity.offset: entity.offset + entity.length]
                if url and url not in urls:
                    urls.append(url)

    return urls

def expand_url(url, timeout=8):
    """Follow all redirects to get the final destination URL"""
    try:
        r = requests.head(url, allow_redirects=True, timeout=timeout,
                          headers=BROWSE_HEADERS)
        final = r.url
        if final != url:
            return final
    except Exception:
        pass
    try:
        r = requests.get(url, allow_redirects=True, timeout=timeout,
                         headers=BROWSE_HEADERS, stream=True)
        return r.url
    except Exception:
        return url

def is_amazon(url):
    return bool(re.search(r'amazon\.in|amazon\.com', url))

def is_flipkart_family(url):
    return any(d in url for d in CUELINKS_DOMAINS)

def is_source_site(url):
    return any(d in url for d in SOURCE_SITE_DOMAINS)

def is_shortener(url):
    return any(d in url for d in SHORTENER_DOMAINS)

def is_ignorable(url):
    return any(d in url for d in IGNORE_URL_DOMAINS)

def get_asin(url):
    m = re.search(r'/(?:dp|gp/product|d)/([A-Z0-9]{10})(?:[/?&]|$)', url)
    return m.group(1) if m else None

def make_amazon_affiliate(url):
    asin = get_asin(url)
    if asin:
        return f"https://www.amazon.in/dp/{asin}?tag={AMAZON_TAG}"
    url = re.sub(r'[?&]tag=[^&]+', '', url)
    url = re.sub(r'[?&]ascsubtag=[^&]+', '', url)
    url = re.sub(r'/ref=[^/?&]+', '', url)
    url = url.rstrip('?&')
    sep = '&' if '?' in url else '?'
    return f"{url}{sep}tag={AMAZON_TAG}"

def make_cuelinks_affiliate(url):
    if not CUELINKS_KEY:
        return None
    try:
        r = requests.get('https://api.cuelinks.com/v1/affiliate-url',
                         params={'apiKey': CUELINKS_KEY, 'url': url},
                         timeout=10)
        if r.status_code == 200:
            data = r.json()
            print(f"    Cuelinks response: {data}")
            # Try multiple possible field names
            aff = (data.get('affiliateUrl') or
                   data.get('affiliate_url') or
                   data.get('url') or
                   data.get('shortUrl') or
                   data.get('short_url'))
            if aff and aff != url:
                return aff
            print(f"    Cuelinks returned same URL or empty")
        else:
            print(f"    Cuelinks HTTP {r.status_code}: {r.text[:120]}")
    except Exception as e:
        print(f"    Cuelinks error: {e}")
    return None

def shorten(url):
    try:
        r = requests.get(f'https://tinyurl.com/api-create.php?url={url}', timeout=10)
        if r.status_code == 200 and r.text.startswith('http'):
            return r.text.strip()
    except Exception:
        pass
    return url

def get_amazon_image_cdn(url):
    asin = get_asin(url)
    if asin:
        return f"https://m.media-amazon.com/images/I/{asin}._SL500_.jpg"
    return ''

def resolve_to_affiliate(url):
    """
    Given any URL (possibly shortened), return (affiliate_url, image_cdn_url).
    Returns (None, None) only if truly unusable (noise/social/source site).
    NEVER drops a valid Amazon/Flipkart URL — always returns at least a shortened link.
    """
    original = url

    # Step 1: Expand shorteners
    if is_shortener(url):
        expanded = expand_url(url)
        if expanded != url:
            print(f"    ↗ {url[:55]} → {expanded[:65]}")
            url = expanded
        else:
            print(f"    ✗ could not expand {url[:55]}")
            # Still try to use it if it points to a known store
            # (some shorteners return 200 but don't redirect — rare)

    # Step 2: Skip noise/social/source sites
    if is_ignorable(url):
        return None, None
    if is_source_site(url):
        return None, None

    # Step 3: Amazon — inject affiliate tag
    if is_amazon(url):
        aff   = make_amazon_affiliate(url)
        short = shorten(aff)
        image = get_amazon_image_cdn(aff)
        print(f"    ✅ Amazon → {short[:60]}")
        return short, image

    # Step 4: Flipkart/Myntra/etc — try Cuelinks, fallback to direct URL
    if is_flipkart_family(url):
        aff = make_cuelinks_affiliate(url)
        if aff:
            short = shorten(aff)
            print(f"    ✅ Cuelinks → {short[:60]}")
            return short, None
        # Cuelinks failed — use direct URL shortened (no commission but link works)
        short = shorten(url)
        print(f"    ⚠️ Cuelinks failed, using direct → {short[:60]}")
        return short, None

    # Step 5: Unknown store — still shorten and use rather than drop
    # (better to have a working link with no commission than no link at all)
    short = shorten(url)
    print(f"    📎 unknown store, shortened → {short[:60]}")
    return short, None

# ── Image handling ────────────────────────────────────────────────────────────

def download_image(image_url, referer='https://www.amazon.in/'):
    """Download image ourselves with browser headers — Telegram URL fetching is unreliable"""
    try:
        h = {**BROWSE_HEADERS, 'Referer': referer,
             'Accept': 'image/avif,image/webp,image/apng,image/*;q=0.8'}
        r = requests.get(image_url, headers=h, timeout=12, stream=True)
        if r.status_code == 200 and 'image' in r.headers.get('content-type', ''):
            data = r.content
            if len(data) > 1000:
                print(f"    📷 {len(data)//1024}KB from {image_url[:55]}")
                return data, r.headers.get('content-type', 'image/jpeg')
    except Exception as e:
        print(f"    📷 download failed: {e}")
    return None, None

def get_og_image(product_url):
    """Scrape og:image from Flipkart/Myntra product page"""
    try:
        r = requests.get(product_url, headers=BROWSE_HEADERS, timeout=10)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, 'html.parser')
            tag  = soup.find('meta', property='og:image')
            if tag and tag.get('content'):
                return tag['content']
    except Exception as e:
        print(f"    og:image failed: {e}")
    return None

async def get_telethon_photo(client, msg):
    """Last-resort: download photo via Telethon from original message"""
    try:
        data = await client.download_media(msg.photo, bytes)
        if data and len(data) > 1000:
            return data, 'image/jpeg'
    except Exception as e:
        print(f"    Telethon photo failed: {e}")
    return None, None

# ── Message text builder ──────────────────────────────────────────────────────

def build_clean_text(msg, affiliate_url):
    """
    Build clean message text:
    - Use raw message text (no entities/markdown artifacts like []())
    - Strip source site lines, noise lines
    - Append affiliate link explicitly
    """
    raw = msg.text or msg.message or ''

    lines = raw.split('\n')
    clean = []
    for line in lines:
        s = line.strip()
        if not s:
            clean.append('')
            continue

        sl = s.lower()

        # Remove lines that are noise labels
        if sl.startswith('on #'):              continue
        if sl.startswith('read more'):        continue
        if sl.startswith('buy now'):          continue
        if sl.startswith('link:'):            continue
        if sl.startswith('join '):            continue
        if sl.startswith('follow'):           continue
        if re.match(r'^#\w', s):              continue  # hashtag lines

        # Remove lines containing ONLY source/noise URLs
        line_urls = extract_text_urls(s)
        if line_urls:
            all_noise = all(
                is_source_site(u) or is_shortener(u) or is_ignorable(u)
                for u in line_urls
            )
            if all_noise:
                continue

        clean.append(line)

    result = re.sub(r'\n{3,}', '\n\n', '\n'.join(clean)).strip()

    # Always append the affiliate link clearly at the bottom
    if affiliate_url:
        result += f"\n\n🔗 {affiliate_url}"

    return result

# ── Telegram API senders ──────────────────────────────────────────────────────

def send_photo(chat_id, img_bytes, caption, ctype='image/jpeg'):
    """Upload image + caption via Bot API sendPhoto"""
    try:
        ext = 'jpg' if 'jpeg' in ctype else ctype.split('/')[-1]
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
            data={'chat_id': chat_id, 'caption': caption},
            files={'photo': (f'img.{ext}', img_bytes, ctype)},
            timeout=30,
        )
        return r.status_code == 200, r.text
    except Exception as e:
        return False, str(e)

def send_text(chat_id, text):
    """Send plain text via Bot API"""
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={'chat_id': chat_id, 'text': text,
                  'disable_web_page_preview': True},
            timeout=15,
        )
        return r.status_code == 200, r.text
    except Exception as e:
        return False, str(e)

# ── State / Deals helpers ─────────────────────────────────────────────────────

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f: return json.load(f)
        except Exception: pass
    return default

def save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def add_deal(deals, text, url, source, image_url):
    deals.insert(0, {
        'text':      text,
        'url':       url or '',
        'source':    source,
        'image':     image_url or '',
        'timestamp': datetime.now(timezone.utc).isoformat(),
    })
    return deals[:MAX_DEALS]

# ── Main ──────────────────────────────────────────────────────────────────────

async def run():
    state  = load_json(STATE_FILE, {})
    deals  = load_json(DEALS_FILE, [])
    total  = 0
    chat_id = f'@{YOUR_CHANNEL}'

    print(f"v{VERSION} | channel=@{YOUR_CHANNEL} | amazon={AMAZON_TAG} | cuelinks={'on' if CUELINKS_KEY else 'off'}")
    print(f"sources: {SOURCE_CHANNELS}")

    posted_hashes = set()

    # ── Connect ───────────────────────────────────────────────────────────────
    print("\nConnecting...")
    try:
        client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            print("❌ SESSION EXPIRED — regenerate secret A4 via Google Colab")
            return
        me = await client.get_me()
        print(f"✅ Connected as {me.first_name}")
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        traceback.print_exc()
        return

    async with client:
        for channel in SOURCE_CHANNELS:
            if not channel:
                continue

            last_id     = state.get(channel, 0)
            new_last_id = last_id
            found       = 0
            limit       = 5 if last_id == 0 else 20

            print(f"\n{'─'*55}")
            print(f"  {channel}  (last_id={last_id})")

            try:
                count = 0
                async for msg in client.iter_messages(channel, min_id=last_id, limit=limit):
                    count += 1
                    if msg.id > new_last_id:
                        new_last_id = msg.id

                    raw_text  = msg.text or msg.message or ''
                    has_photo = bool(getattr(msg, 'photo', None))

                    print(f"\n  MSG {msg.id}: {len(raw_text)} chars | photo={has_photo}")

                    # Skip empty messages with no photo
                    if not raw_text.strip() and not has_photo:
                        print("    skip: no text, no photo")
                        continue

                    # Dedup within this run
                    msg_hash = hashlib.md5(raw_text[:120].encode()).hexdigest()[:10]
                    if msg_hash in posted_hashes:
                        print("    skip: duplicate")
                        continue

                    # ── Extract ALL URLs (text + entities) ──────────────────
                    all_urls = extract_all_urls_from_msg(msg)
                    print(f"    urls found: {all_urls}")

                    # ── Find affiliate URL ───────────────────────────────────
                    affiliate_url = None
                    image_cdn     = None

                    for url in all_urls:
                        if is_ignorable(url):
                            continue
                        aff, img = resolve_to_affiliate(url)
                        if aff:
                            affiliate_url = aff
                            image_cdn     = img
                            break  # use first working affiliate URL

                    print(f"    affiliate: {affiliate_url}")

                    # ── Build clean message text ─────────────────────────────
                    clean = build_clean_text(msg, affiliate_url)
                    if not clean.strip():
                        print("    skip: empty after cleaning")
                        continue

                    # ── Get image ────────────────────────────────────────────
                    img_bytes  = None
                    img_type   = 'image/jpeg'
                    img_saved  = ''

                    # Try Amazon CDN image
                    if image_cdn:
                        img_bytes, img_type = download_image(image_cdn)
                        if img_bytes:
                            img_saved = image_cdn

                    # Try og:image from Flipkart/other
                    if not img_bytes and affiliate_url and not is_amazon(affiliate_url or ''):
                        og = get_og_image(affiliate_url)
                        if og:
                            img_bytes, img_type = download_image(og, referer=affiliate_url)
                            if img_bytes:
                                img_saved = og

                    # Fallback: Telethon photo from original message
                    if not img_bytes and has_photo:
                        print("    📷 trying Telethon fallback...")
                        img_bytes, img_type = await get_telethon_photo(client, msg)
                        if img_bytes:
                            img_saved = 'telethon'

                    # ── Post to Telegram ─────────────────────────────────────
                    ok   = False
                    resp = ''

                    if img_bytes:
                        ok, resp = send_photo(chat_id, img_bytes, clean, img_type)
                        if not ok:
                            print(f"    sendPhoto failed: {resp[:80]}, trying text...")
                            ok, resp = send_text(chat_id, clean)
                    else:
                        ok, resp = send_text(chat_id, clean)

                    if ok:
                        print(f"    ✅ POSTED {'📷' if img_bytes else '📝'}")
                        posted_hashes.add(msg_hash)
                        deals = add_deal(deals, clean, affiliate_url or '', channel, img_saved)
                        found += 1
                        total += 1
                        time.sleep(random.uniform(1.5, 3.5))
                    else:
                        print(f"    ❌ FAILED: {resp[:120]}")

                print(f"\n  scanned={count} posted={found}")

            except Exception as e:
                print(f"  ❌ ERROR in {channel}: {e}")
                traceback.print_exc()

            state[channel] = new_last_id

    save_json(STATE_FILE, state)
    save_json(DEALS_FILE, deals)
    print(f"\n{'='*55}")
    print(f"v{VERSION} done: {total} posted | {len(deals)} on website")

if __name__ == '__main__':
    asyncio.run(run())
