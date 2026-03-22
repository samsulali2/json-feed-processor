"""
Telegram Affiliate Deal Bot  v6.0
==================================
Fixes in this version:
  1. PRE-PUBLISH VALIDATION — every post checked before sending
  2. IMAGE FIX — use Telethon photo (real product image) first,
     NOT og:image which returns store logos on Flipkart/Myntra
  3. LINK GUARANTEE — affiliate link always appended explicitly,
     never silently dropped
  4. WEBSITE IMAGE FIX — saves Telegraph URL for non-Amazon images
     so website shows actual product photo not store logo
"""

import os, re, json, asyncio, requests, hashlib, io, random, time, traceback
from datetime import datetime, timezone
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl

VERSION = "6.0"

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

SHORTENER_DOMAINS = [
    'ddime.in', 'amzn.clnk.in', 'clnk.in',
    'amzn.to', 'amzn.in', 'a.co/',
    'bitli.store', 'bit.ly', 'cutt.ly',
    'rb.gy', 't.ly', 'tiny.cc', 'ow.ly',
    'shorturl.at', 'dl.flipkart.com',
]

SOURCE_SITE_DOMAINS = [
    'desidime.com', 'dealsmagnet.com', 'freekaamaal.com',
    'lootdunia.com', 'dealsbazaar.in', 'hcti.io',
]

CUELINKS_DOMAINS = [
    'flipkart.com', 'myntra.com', 'ajio.com', 'nykaa.com',
    'tatacliq.com', 'shopsy.in', 'meesho.com', 'jiomart.com',
    'croma.com',
]

IGNORE_URL_DOMAINS = [
    't.me', 'telegram.me', 'instagram.com', 'twitter.com',
    'facebook.com', 'youtube.com', 'play.google.com', 'hcti.io',
]

BROWSE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'en-IN,en;q=0.9',
}

# ── URL helpers ───────────────────────────────────────────────────────────────

def extract_text_urls(text):
    return re.findall(r'https?://[^\s\)\]>\"\'<\u2019\u201d]+', text or '')

def extract_all_urls_from_msg(msg):
    """Extract URLs from BOTH plain text AND hidden entity URLs"""
    urls = []
    text = msg.text or msg.message or ''
    for url in extract_text_urls(text):
        urls.append(url)
    if msg.entities:
        for entity in msg.entities:
            if isinstance(entity, MessageEntityTextUrl):
                if entity.url and entity.url not in urls:
                    urls.append(entity.url)
            elif isinstance(entity, MessageEntityUrl):
                url = text[entity.offset: entity.offset + entity.length]
                if url and url not in urls:
                    urls.append(url)
    return urls

def expand_url(url, timeout=8):
    try:
        r = requests.head(url, allow_redirects=True, timeout=timeout, headers=BROWSE_HEADERS)
        if r.url != url: return r.url
    except Exception:
        pass
    try:
        r = requests.get(url, allow_redirects=True, timeout=timeout, headers=BROWSE_HEADERS, stream=True)
        return r.url
    except Exception:
        return url

def is_amazon(url):      return bool(re.search(r'amazon\.in|amazon\.com', url or ''))
def is_flipkart_family(url): return any(d in (url or '') for d in CUELINKS_DOMAINS)
def is_source_site(url): return any(d in (url or '') for d in SOURCE_SITE_DOMAINS)
def is_shortener(url):   return any(d in (url or '') for d in SHORTENER_DOMAINS)
def is_ignorable(url):   return any(d in (url or '') for d in IGNORE_URL_DOMAINS)

def get_asin(url):
    m = re.search(r'/(?:dp|gp/product|d)/([A-Z0-9]{10})(?:[/?&]|$)', url or '')
    return m.group(1) if m else None

def get_amazon_image_cdn(url):
    asin = get_asin(url)
    return f"https://m.media-amazon.com/images/I/{asin}._SL500_.jpg" if asin else ''

def make_amazon_affiliate(url):
    asin = get_asin(url)
    if asin:
        return f"https://www.amazon.in/dp/{asin}?tag={AMAZON_TAG}"
    url = re.sub(r'[?&]tag=[^&]+', '', url)
    url = re.sub(r'[?&]ascsubtag=[^&]+', '', url)
    url = re.sub(r'/ref=[^/?&]+', '', url)
    url = url.rstrip('?&')
    return url + ('&' if '?' in url else '?') + f"tag={AMAZON_TAG}"

def make_cuelinks_affiliate(url):
    if not CUELINKS_KEY: return None
    try:
        r = requests.get('https://api.cuelinks.com/v1/affiliate-url',
                         params={'apiKey': CUELINKS_KEY, 'url': url}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            print(f"    Cuelinks: {data}")
            aff = (data.get('affiliateUrl') or data.get('affiliate_url') or
                   data.get('shortUrl') or data.get('short_url'))
            if aff and aff != url:
                return aff
            print("    Cuelinks: returned same/empty URL")
        else:
            print(f"    Cuelinks HTTP {r.status_code}: {r.text[:100]}")
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

def resolve_to_affiliate(url):
    """
    Returns (affiliate_url, image_cdn_url_or_empty).
    NEVER drops a valid product URL — always returns at least a shortened direct link.
    """
    # Expand shorteners first
    if is_shortener(url):
        expanded = expand_url(url)
        if expanded != url:
            print(f"    ↗ {url[:50]} → {expanded[:60]}")
            url = expanded
        else:
            print(f"    ✗ expand failed: {url[:50]}")

    # Skip noise
    if is_ignorable(url) or is_source_site(url):
        return None, None

    # Amazon
    if is_amazon(url):
        aff   = make_amazon_affiliate(url)
        short = shorten(aff)
        image = get_amazon_image_cdn(aff)
        print(f"    ✅ Amazon → {short[:55]}")
        return short, image

    # Flipkart/Myntra/etc
    if is_flipkart_family(url):
        aff = make_cuelinks_affiliate(url)
        if aff:
            short = shorten(aff)
            print(f"    ✅ Cuelinks → {short[:55]}")
            return short, ''   # image handled via Telethon photo
        # Cuelinks failed — use direct link (no commission but link works)
        short = shorten(url)
        print(f"    ⚠️ Cuelinks failed, direct → {short[:55]}")
        return short, ''

    # Any other valid-looking URL
    short = shorten(url)
    print(f"    📎 other → {short[:55]}")
    return short, ''

# ── Image handling ────────────────────────────────────────────────────────────

def download_image(image_url, referer='https://www.amazon.in/'):
    """Download image bytes with browser headers. Returns (bytes, ctype) or (None, None)."""
    try:
        h = {**BROWSE_HEADERS, 'Referer': referer,
             'Accept': 'image/avif,image/webp,image/apng,image/*;q=0.8'}
        r = requests.get(image_url, headers=h, timeout=12, stream=True)
        ctype = r.headers.get('content-type', '')
        if r.status_code == 200 and 'image' in ctype:
            data = r.content
            if len(data) > 2000:  # real image > 2KB
                print(f"    📷 {len(data)//1024}KB downloaded")
                return data, ctype
            else:
                print(f"    📷 too small ({len(data)} bytes) — likely placeholder")
    except Exception as e:
        print(f"    📷 download failed: {e}")
    return None, None

async def upload_to_telegraph(client, msg):
    """
    Download photo from Telegram message via Telethon and upload to Telegraph.
    Returns public Telegraph URL or empty string.
    WHY: Telethon gives us the ACTUAL product photo (set by the source channel).
    This is always the correct product image — better than og:image scraping.
    """
    try:
        data = await client.download_media(msg.photo, bytes)
        if not data or len(data) < 2000:
            return ''
        files = {'file': ('img.jpg', io.BytesIO(data), 'image/jpeg')}
        r = requests.post('https://telegra.ph/upload', files=files, timeout=20)
        if r.status_code == 200:
            result = r.json()
            if isinstance(result, list) and result:
                url = f"https://telegra.ph{result[0]['src']}"
                print(f"    📷 Telegraph: {url}")
                return url
    except Exception as e:
        print(f"    📷 Telegraph upload failed: {e}")
    return ''

# ── Message text builder ──────────────────────────────────────────────────────

NOISE_PREFIXES = ['on #', 'read more', 'buy now', 'link:', 'join ', 'follow', 'share ']

def build_clean_text(msg, affiliate_url):
    """
    Build clean outgoing message text.
    - Strips noise lines
    - Strips ALL source/shortener URLs from text (we add our own link)
    - Appends affiliate link clearly at bottom
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

        # Remove noise label lines
        if any(sl.startswith(p) for p in NOISE_PREFIXES): continue
        if re.match(r'^#\w', s): continue  # hashtag-only lines

        # Check for URLs in this line
        line_urls = extract_text_urls(s)
        if line_urls:
            # Remove lines whose URLs are all noise/shorteners/source sites
            all_removable = all(
                is_source_site(u) or is_shortener(u) or is_ignorable(u)
                for u in line_urls
            )
            if all_removable:
                continue

        clean.append(line)

    result = re.sub(r'\n{3,}', '\n\n', '\n'.join(clean)).strip()

    # ALWAYS append affiliate link explicitly at bottom
    # This guarantees the link is present even if text cleaning removes everything
    if affiliate_url:
        result += f"\n\n🔗 {affiliate_url}"

    return result

# ── Pre-publish validator ─────────────────────────────────────────────────────

def validate_post(clean_text, affiliate_url, channel):
    """
    Thorough check before publishing. Returns (ok, reason).
    Catches all the issues we've seen in production.
    """
    # 1. Must have text
    if not clean_text or not clean_text.strip():
        return False, "empty text"

    # 2. Must have affiliate/product link
    if not affiliate_url:
        return False, "no affiliate link"

    # 3. Link must be present in the text
    if affiliate_url not in clean_text:
        return False, f"affiliate URL not in text (will append)"

    # 4. Text must not contain raw shortener URLs (they should have been replaced/removed)
    text_urls = extract_text_urls(clean_text)
    shortener_leaks = [u for u in text_urls if is_shortener(u) and u != affiliate_url]
    if shortener_leaks:
        return False, f"shortener URLs leaked into text: {shortener_leaks[:2]}"

    # 5. Text must not contain source site URLs
    source_leaks = [u for u in text_urls if is_source_site(u)]
    if source_leaks:
        return False, f"source site URLs leaked: {source_leaks[:2]}"

    # 6. Text length sanity check
    if len(clean_text) > 4096:
        return False, f"text too long ({len(clean_text)} chars, Telegram limit=4096)"

    return True, "ok"

def sanitize_post(clean_text, affiliate_url):
    """
    Auto-fix any issues found by validate_post.
    Returns sanitized text.
    """
    # Remove any leaked shortener/source URLs from text
    for url in extract_text_urls(clean_text):
        if (is_shortener(url) or is_source_site(url)) and url != affiliate_url:
            clean_text = clean_text.replace(url, '')

    # Remove empty label lines (e.g. "Link: " after URL removal)
    lines = []
    for line in clean_text.split('\n'):
        s = line.strip()
        if re.match(r'^[\w\s]+:\s*$', s) and len(s) < 25:
            continue  # skip "Link: " "Read More: " etc
        lines.append(line)
    clean_text = re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()

    # Ensure affiliate link is in text
    if affiliate_url and affiliate_url not in clean_text:
        clean_text += f"\n\n🔗 {affiliate_url}"

    return clean_text

# ── Telegram API ──────────────────────────────────────────────────────────────

def send_photo(chat_id, img_bytes, caption, ctype='image/jpeg'):
    try:
        ext = 'jpg' if 'jpeg' in ctype else ctype.split('/')[-1]
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
            data={'chat_id': chat_id, 'caption': caption[:1024]},
            files={'photo': (f'img.{ext}', img_bytes, ctype)},
            timeout=30,
        )
        return r.status_code == 200, r.text
    except Exception as e:
        return False, str(e)

def send_text(chat_id, text):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={'chat_id': chat_id, 'text': text[:4096],
                  'disable_web_page_preview': True},
            timeout=15,
        )
        return r.status_code == 200, r.text
    except Exception as e:
        return False, str(e)

# ── State / Deals ─────────────────────────────────────────────────────────────

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
    state   = load_json(STATE_FILE, {})
    deals   = load_json(DEALS_FILE, [])
    total   = 0
    chat_id = f'@{YOUR_CHANNEL}'

    print(f"v{VERSION} | @{YOUR_CHANNEL} | amazon={AMAZON_TAG} | cuelinks={'on' if CUELINKS_KEY else 'off'}")
    print(f"sources: {SOURCE_CHANNELS}")

    posted_hashes = set()

    print("\nConnecting...")
    try:
        client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            print("❌ SESSION EXPIRED — regenerate A4 via Google Colab")
            return
        me = await client.get_me()
        print(f"✅ Connected as {me.first_name}")
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        traceback.print_exc()
        return

    async with client:
        for channel in SOURCE_CHANNELS:
            if not channel: continue

            last_id     = state.get(channel, 0)
            new_last_id = last_id
            found = count = 0
            limit = 5 if last_id == 0 else 20

            print(f"\n{'─'*55}")
            print(f"  {channel}  (last={last_id})")

            try:
                async for msg in client.iter_messages(channel, min_id=last_id, limit=limit):
                    count += 1
                    if msg.id > new_last_id:
                        new_last_id = msg.id

                    raw_text  = msg.text or msg.message or ''
                    has_photo = bool(getattr(msg, 'photo', None))
                    print(f"\n  MSG {msg.id}: {len(raw_text)}ch photo={has_photo}")

                    if not raw_text.strip() and not has_photo:
                        print("    skip: no content")
                        continue

                    # Dedup
                    msg_hash = hashlib.md5(raw_text[:120].encode()).hexdigest()[:10]
                    if msg_hash in posted_hashes:
                        print("    skip: duplicate")
                        continue

                    # ── 1. Extract all URLs ──────────────────────────────────
                    all_urls = extract_all_urls_from_msg(msg)
                    print(f"    urls: {all_urls}")

                    # ── 2. Resolve affiliate URL ─────────────────────────────
                    affiliate_url = None
                    image_cdn     = None

                    for url in all_urls:
                        if is_ignorable(url): continue
                        aff, img = resolve_to_affiliate(url)
                        if aff:
                            affiliate_url = aff
                            image_cdn     = img or ''
                            break

                    print(f"    affiliate: {affiliate_url or 'NONE'}")

                    # Skip if no affiliate link AND no photo (nothing useful to post)
                    if not affiliate_url and not has_photo:
                        print("    skip: no affiliate URL and no photo")
                        continue

                    # ── 3. Build text ────────────────────────────────────────
                    clean = build_clean_text(msg, affiliate_url)

                    # ── 4. PRE-PUBLISH VALIDATION ────────────────────────────
                    ok_check, reason = validate_post(clean, affiliate_url, channel)
                    if not ok_check:
                        print(f"    ⚠️ validation: {reason} — auto-fixing...")
                        clean = sanitize_post(clean, affiliate_url)
                        # Re-validate after fix
                        ok_check, reason = validate_post(clean, affiliate_url, channel)
                        if not ok_check:
                            print(f"    ✗ still invalid after fix: {reason} — skipping")
                            continue

                    clean += f"\n\n🛒 Deals by @{YOUR_CHANNEL}"

                    # ── 5. Get image ─────────────────────────────────────────
                    img_bytes = None
                    img_type  = 'image/jpeg'
                    img_saved = ''

                    # Priority 1: Amazon CDN (instant, reliable)
                    if image_cdn:
                        img_bytes, img_type = download_image(image_cdn)
                        if img_bytes:
                            img_saved = image_cdn

                    # Priority 2: Telethon photo from source message
                    # (This is the ACTUAL product photo — always correct)
                    # Use for Flipkart/Myntra/any non-Amazon with photo
                    if not img_bytes and has_photo:
                        print("    📷 using source photo via Telethon...")
                        telegraph_url = await upload_to_telegraph(client, msg)
                        if telegraph_url:
                            # Download the Telegraph URL to send as photo
                            img_bytes, img_type = download_image(telegraph_url, referer='https://telegra.ph/')
                            img_saved = telegraph_url

                    # ── 6. Post ──────────────────────────────────────────────
                    posted_ok = False
                    post_resp = ''

                    if img_bytes:
                        posted_ok, post_resp = send_photo(chat_id, img_bytes, clean, img_type)
                        if not posted_ok:
                            print(f"    sendPhoto failed ({post_resp[:60]}), falling back to text")
                            posted_ok, post_resp = send_text(chat_id, clean)
                    else:
                        posted_ok, post_resp = send_text(chat_id, clean)

                    if posted_ok:
                        print(f"    ✅ POSTED {'📷' if img_bytes else '📝'} | link={affiliate_url[:40] if affiliate_url else 'none'}")
                        posted_hashes.add(msg_hash)
                        deals = add_deal(deals, clean, affiliate_url or '', channel, img_saved)
                        found += 1
                        total += 1
                        time.sleep(random.uniform(1.5, 3.0))
                    else:
                        print(f"    ❌ FAILED: {post_resp[:120]}")

                print(f"\n  scanned={count} posted={found}")

            except Exception as e:
                print(f"  ❌ {channel} error: {e}")
                traceback.print_exc()

            state[channel] = new_last_id

    save_json(STATE_FILE, state)
    save_json(DEALS_FILE, deals)
    print(f"\n{'='*55}")
    print(f"v{VERSION} done: {total} posted | {len(deals)} on website")

if __name__ == '__main__':
    asyncio.run(run())
