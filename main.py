"""
Telegram Affiliate Deal Bot  v7.0 — FINAL
==========================================
All known bugs fixed:
  1. TinyURL homepage bug — proper URL encoding + result validation
  2. Dead shortener links — expand MUST succeed or skip the URL entirely
  3. Unknown URLs never shortener-wrapped blindly
  4. Pre-publish validator runs correctly
  5. Images: Amazon CDN first, then Telethon photo → Telegraph (real product photo)
  6. Affiliate link always present in final post
"""

import os, re, json, asyncio, requests, hashlib, io, random, time, traceback
import urllib.parse
from datetime import datetime, timezone
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl

VERSION = "7.0"

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

# Short-link services — MUST be expanded before use
# If expansion fails → URL is dead → skip entirely
SHORTENER_DOMAINS = [
    'ddime.in', 'amzn.clnk.in', 'clnk.in',
    'amzn.to', 'amzn.in', 'a.co/',
    'bitli.store', 'bit.ly', 'cutt.ly',
    'rb.gy', 't.ly', 'tiny.cc', 'ow.ly',
    'shorturl.at', 'tinyurl.com', 'dl.flipkart.com',
]

# Deal aggregator pages — not product URLs, remove from message
SOURCE_SITE_DOMAINS = [
    'desidime.com', 'dealsmagnet.com', 'freekaamaal.com',
    'lootdunia.com', 'dealsbazaar.in', 'hcti.io',
]

# Stores supported by Cuelinks affiliate programme
CUELINKS_DOMAINS = [
    'flipkart.com', 'myntra.com', 'ajio.com', 'nykaa.com',
    'tatacliq.com', 'shopsy.in', 'meesho.com', 'jiomart.com',
    'croma.com',
]

# URLs to ignore completely (social/noise)
IGNORE_URL_DOMAINS = [
    't.me', 'telegram.me', 'instagram.com', 'twitter.com',
    'facebook.com', 'youtube.com', 'play.google.com', 'hcti.io',
]

# Noise line prefixes to strip from messages
NOISE_PREFIXES = [
    'on #', 'read more', 'buy now', 'link:', 'join ',
    'follow', 'share ', 'deals by', 'source:',
]

BROWSE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'en-IN,en;q=0.9',
}


# ─────────────────────────────────────────────────────────────────────────────
# URL UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

def extract_text_urls(text):
    """Extract plain URLs visible in text"""
    return re.findall(r'https?://[^\s\)\]>\"\'<\u2019\u201d]+', text or '')

def extract_all_urls_from_msg(msg):
    """
    Extract URLs from BOTH visible text AND hidden Telegram entities.
    [Buy Now](https://ddime.in/xxx) → URL is in entity.url, invisible in msg.text
    """
    seen = []
    text = msg.text or msg.message or ''
    for url in extract_text_urls(text):
        if url not in seen:
            seen.append(url)
    if msg.entities:
        for ent in msg.entities:
            if isinstance(ent, MessageEntityTextUrl):
                if ent.url and ent.url not in seen:
                    seen.append(ent.url)
            elif isinstance(ent, MessageEntityUrl):
                u = text[ent.offset: ent.offset + ent.length]
                if u and u not in seen:
                    seen.append(u)
    return seen

def is_amazon(url):       return bool(re.search(r'amazon\.in|amazon\.com', url or ''))
def is_flipkart_family(u): return any(d in (u or '') for d in CUELINKS_DOMAINS)
def is_source_site(url):  return any(d in (url or '') for d in SOURCE_SITE_DOMAINS)
def is_shortener(url):    return any(d in (url or '') for d in SHORTENER_DOMAINS)
def is_ignorable(url):    return any(d in (url or '') for d in IGNORE_URL_DOMAINS)

def is_valid_product_url(url):
    """Basic check: must be https and have a meaningful path"""
    if not url or not url.startswith('https://'):
        return False
    parsed = urllib.parse.urlparse(url)
    return bool(parsed.netloc) and len(parsed.path) > 1

def expand_url(url, timeout=10):
    """
    Follow all redirects to get final destination URL.
    Returns (expanded_url, success_bool).
    """
    try:
        r = requests.head(url, allow_redirects=True, timeout=timeout,
                          headers=BROWSE_HEADERS)
        if r.url and r.url != url:
            return r.url, True
    except Exception:
        pass
    try:
        r = requests.get(url, allow_redirects=True, timeout=timeout,
                         headers=BROWSE_HEADERS, stream=True)
        if r.url and r.url != url:
            return r.url, True
        return r.url, True  # even if same URL, at least confirmed reachable
    except Exception:
        return url, False

def shorten(url):
    """
    Shorten URL via TinyURL API.
    BUG FIX: use params= for proper URL encoding (not f-string with raw &s).
    BUG FIX: validate returned URL is a real TinyURL, not their homepage.
    Returns shortened URL, or original URL if shortening fails.
    """
    if not is_valid_product_url(url):
        return url
    try:
        r = requests.get(
            'https://tinyurl.com/api-create.php',
            params={'url': url},          # ← params= handles encoding automatically
            timeout=10
        )
        result = r.text.strip()
        # Validate: must start with https://tinyurl.com/ AND have a path segment
        # TinyURL homepage returns 'https://tinyurl.com/' — path is just '/'
        if (r.status_code == 200
                and result.startswith('https://tinyurl.com/')
                and len(result) > len('https://tinyurl.com/') + 3):
            return result
        print(f"    TinyURL returned invalid: {result[:60]}")
    except Exception as e:
        print(f"    TinyURL error: {e}")
    return url  # fall back to original (still a working link)

def get_asin(url):
    m = re.search(r'/(?:dp|gp/product|d)/([A-Z0-9]{10})(?:[/?&]|$)', url or '')
    return m.group(1) if m else None

def get_amazon_image_cdn(amazon_url):
    asin = get_asin(amazon_url)
    return f"https://m.media-amazon.com/images/I/{asin}._SL500_.jpg" if asin else ''

def make_amazon_affiliate(url):
    """Inject our Amazon affiliate tag, preserving clean ASIN URL"""
    asin = get_asin(url)
    if asin:
        return f"https://www.amazon.in/dp/{asin}?tag={AMAZON_TAG}"
    # No ASIN — strip old tags and inject ours
    url = re.sub(r'[?&]tag=[^&]+', '', url)
    url = re.sub(r'[?&]ascsubtag=[^&]+', '', url)
    url = re.sub(r'/ref=[^/?&]+', '', url)
    url = url.rstrip('?&')
    sep = '&' if '?' in url else '?'
    return f"{url}{sep}tag={AMAZON_TAG}"

def make_cuelinks_affiliate(url):
    """Convert Flipkart/Myntra/etc URL to Cuelinks affiliate URL"""
    if not CUELINKS_KEY:
        return None
    try:
        r = requests.get(
            'https://api.cuelinks.com/v1/affiliate-url',
            params={'apiKey': CUELINKS_KEY, 'url': url},
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            print(f"    Cuelinks resp: {data}")
            aff = (data.get('affiliateUrl') or data.get('affiliate_url') or
                   data.get('shortUrl')     or data.get('short_url'))
            if aff and aff != url and is_valid_product_url(aff):
                return aff
            print("    Cuelinks: returned same/invalid URL")
        else:
            print(f"    Cuelinks HTTP {r.status_code}: {r.text[:100]}")
    except Exception as e:
        print(f"    Cuelinks error: {e}")
    return None

def resolve_to_affiliate(raw_url):
    """
    Convert any URL to (affiliate_url, image_cdn_url).

    Rules:
    - Shorteners MUST expand successfully, otherwise skip (dead link)
    - Amazon → inject tag → shorten
    - Flipkart/etc → Cuelinks → shorten; fallback to direct URL if Cuelinks fails
    - Source sites / social → skip
    - Unknown URLs → skip (don't create broken TinyURLs for random links)

    Returns (None, None) if URL cannot be monetised or is unusable.
    """
    url = raw_url

    # Step 1: expand shorteners — MUST succeed
    if is_shortener(url):
        expanded, ok = expand_url(url)
        if ok and expanded != url and is_valid_product_url(expanded):
            print(f"    ↗ {url[:45]} → {expanded[:55]}")
            url = expanded
        else:
            # Expansion failed or circular → link is dead → skip
            print(f"    ✗ shortener expand failed (dead link): {url[:50]}")
            return None, None

    # Step 2: skip noise after expansion
    if is_ignorable(url) or is_source_site(url):
        return None, None

    # Step 3: Amazon
    if is_amazon(url):
        aff   = make_amazon_affiliate(url)
        short = shorten(aff)
        image = get_amazon_image_cdn(aff)
        print(f"    ✅ Amazon → {short[:55]}")
        return short, image

    # Step 4: Flipkart / Myntra / other Cuelinks stores
    if is_flipkart_family(url):
        aff = make_cuelinks_affiliate(url)
        if aff:
            short = shorten(aff)
            print(f"    ✅ Cuelinks → {short[:55]}")
            return short, ''
        # Cuelinks failed → use direct URL (no commission but link works)
        short = shorten(url)
        print(f"    ⚠️ Cuelinks failed, direct → {short[:55]}")
        return short, ''

    # Step 5: Unknown URL type — skip rather than create a meaningless TinyURL
    print(f"    ✗ unrecognised URL type, skipping: {url[:55]}")
    return None, None


# ─────────────────────────────────────────────────────────────────────────────
# IMAGE HANDLING
# ─────────────────────────────────────────────────────────────────────────────

def download_image(image_url, referer='https://www.amazon.in/'):
    """Download image bytes ourselves — Telegram's URL fetcher is unreliable"""
    if not image_url: return None, None
    try:
        h = {**BROWSE_HEADERS,
             'Referer': referer,
             'Accept': 'image/avif,image/webp,image/apng,image/*;q=0.8'}
        r = requests.get(image_url, headers=h, timeout=15, stream=True)
        ctype = r.headers.get('content-type', '')
        if r.status_code == 200 and 'image' in ctype:
            data = r.content
            if len(data) > 3000:   # real image > 3KB
                print(f"    📷 {len(data)//1024}KB ok")
                return data, ctype
            print(f"    📷 too small ({len(data)}B) — placeholder/error image")
    except Exception as e:
        print(f"    📷 download failed: {e}")
    return None, None

async def get_photo_from_msg(tg_client, msg):
    """
    Download photo via Telethon + upload to Telegraph.
    Returns (telegraph_url_string) or ''.
    WHY: The source channel's photo IS the product image — always correct.
    Better than og:image scraping which returns store logos.
    """
    try:
        data = await tg_client.download_media(msg.photo, bytes)
        if not data or len(data) < 3000:
            return ''
        files = {'file': ('img.jpg', io.BytesIO(data), 'image/jpeg')}
        r = requests.post('https://telegra.ph/upload', files=files, timeout=20)
        if r.status_code == 200:
            res = r.json()
            if isinstance(res, list) and res:
                turl = f"https://telegra.ph{res[0]['src']}"
                print(f"    📷 Telegraph → {turl}")
                return turl
    except Exception as e:
        print(f"    📷 Telegraph upload failed: {e}")
    return ''


# ─────────────────────────────────────────────────────────────────────────────
# MESSAGE TEXT BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def build_clean_text(msg, affiliate_url):
    """
    Return clean message text with:
    - All noise / source-site lines removed
    - All shortener / source-site URLs removed
    - Affiliate link appended at bottom (always present)
    """
    raw = msg.text or msg.message or ''
    lines = raw.split('\n')
    clean = []

    for line in lines:
        s  = line.strip()
        sl = s.lower()

        if not s:
            clean.append('')
            continue

        # Drop noise label lines
        if any(sl.startswith(p) for p in NOISE_PREFIXES):
            continue
        if re.match(r'^#\w', s):   # pure hashtag line
            continue

        # Drop lines whose only URLs are source/shortener/social
        line_urls = extract_text_urls(s)
        if line_urls:
            all_junk = all(
                is_source_site(u) or is_shortener(u) or is_ignorable(u)
                for u in line_urls
            )
            if all_junk:
                continue

        clean.append(line)

    result = re.sub(r'\n{3,}', '\n\n', '\n'.join(clean)).strip()

    # ALWAYS append affiliate link — guaranteed presence
    if affiliate_url:
        result += f"\n\n🔗 {affiliate_url}"

    return result


# ─────────────────────────────────────────────────────────────────────────────
# PRE-PUBLISH VALIDATOR
# ─────────────────────────────────────────────────────────────────────────────

def validate_and_fix(text, affiliate_url):
    """
    Validate post before sending. Auto-fix what we can.
    Returns (final_text, ok_bool, reason_str).
    """
    # Fix 1: remove any leaked shortener/source URLs
    for url in extract_text_urls(text):
        if (is_shortener(url) or is_source_site(url)) and url != affiliate_url:
            print(f"    🔧 removing leaked URL: {url[:50]}")
            text = text.replace(url, '')

    # Fix 2: remove orphan label lines (e.g. "Link: " with no URL after it)
    fixed_lines = []
    for line in text.split('\n'):
        s = line.strip()
        if re.match(r'^[\w\s]+:\s*$', s) and len(s) < 30 and not extract_text_urls(s):
            continue
        fixed_lines.append(line)
    text = re.sub(r'\n{3,}', '\n\n', '\n'.join(fixed_lines)).strip()

    # Fix 3: ensure affiliate link is present
    if affiliate_url and affiliate_url not in text:
        text += f"\n\n🔗 {affiliate_url}"

    # ── Validations ──────────────────────────────────────────────────────────
    if not text.strip():
        return text, False, "empty text after cleaning"

    if not affiliate_url:
        return text, False, "no affiliate link"

    if affiliate_url not in text:
        return text, False, "affiliate URL missing from text after all fixes"

    remaining_shorteners = [u for u in extract_text_urls(text)
                             if is_shortener(u) and u != affiliate_url]
    if remaining_shorteners:
        return text, False, f"shortener still in text: {remaining_shorteners[0][:40]}"

    remaining_source = [u for u in extract_text_urls(text) if is_source_site(u)]
    if remaining_source:
        return text, False, f"source site still in text: {remaining_source[0][:40]}"

    if len(text) > 4096:
        text = text[:4050] + f"\n\n🔗 {affiliate_url}"

    return text, True, "ok"


# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM BOT API SENDERS
# ─────────────────────────────────────────────────────────────────────────────

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
            json={'chat_id':                  chat_id,
                  'text':                     text[:4096],
                  'disable_web_page_preview': True},
            timeout=15,
        )
        return r.status_code == 200, r.text
    except Exception as e:
        return False, str(e)


# ─────────────────────────────────────────────────────────────────────────────
# STATE / DEALS.JSON
# ─────────────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

async def run():
    state   = load_json(STATE_FILE, {})
    deals   = load_json(DEALS_FILE, [])
    total   = 0
    chat_id = f'@{YOUR_CHANNEL}'

    print(f"v{VERSION} | @{YOUR_CHANNEL} | tag={AMAZON_TAG} | cuelinks={'on' if CUELINKS_KEY else 'off'}")
    print(f"sources: {SOURCE_CHANNELS}")

    posted_hashes = set()

    # ── Connect ───────────────────────────────────────────────────────────────
    print("\nConnecting to Telegram...")
    try:
        client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            print("❌ SESSION EXPIRED — regenerate secret A4 via Google Colab")
            return
        me = await client.get_me()
        print(f"✅ Connected as {me.first_name} (@{me.username})")
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
            found = count = 0
            limit = 5 if last_id == 0 else 20

            print(f"\n{'─'*55}")
            print(f"  {channel}  (last={last_id}, limit={limit})")

            try:
                async for msg in client.iter_messages(
                        channel, min_id=last_id, limit=limit):

                    count += 1
                    if msg.id > new_last_id:
                        new_last_id = msg.id

                    raw_text  = msg.text or msg.message or ''
                    has_photo = bool(getattr(msg, 'photo', None))
                    print(f"\n  MSG {msg.id}: {len(raw_text)}ch photo={has_photo}")

                    # Skip truly empty
                    if not raw_text.strip() and not has_photo:
                        print("    skip: no content")
                        continue

                    # Dedup across channels in this run
                    msg_hash = hashlib.md5(raw_text[:120].encode()).hexdigest()[:10]
                    if msg_hash in posted_hashes:
                        print("    skip: duplicate")
                        continue

                    # ── 1. Extract ALL URLs ──────────────────────────────────
                    all_urls = extract_all_urls_from_msg(msg)
                    print(f"    urls: {all_urls}")

                    # ── 2. Resolve affiliate URL ─────────────────────────────
                    affiliate_url = None
                    image_cdn     = None

                    for url in all_urls:
                        if is_ignorable(url):
                            continue
                        aff, img = resolve_to_affiliate(url)
                        if aff:
                            affiliate_url = aff
                            image_cdn     = img or ''
                            break   # first working affiliate URL wins

                    print(f"    affiliate: {affiliate_url or 'NONE ← will skip'}")

                    # Must have an affiliate URL OR a photo to be worth posting
                    if not affiliate_url and not has_photo:
                        print("    skip: no usable link and no photo")
                        continue

                    # ── 3. Build clean text ──────────────────────────────────
                    clean = build_clean_text(msg, affiliate_url)

                    # ── 4. Validate + auto-fix ───────────────────────────────
                    clean, ok, reason = validate_and_fix(clean, affiliate_url)
                    if not ok:
                        print(f"    ✗ validation failed: {reason} — skipping")
                        continue
                    print(f"    ✓ validation: {reason}")

                    # Append channel credit
                    clean += f"\n\n🛒 Deals by @{YOUR_CHANNEL}"

                    # ── 5. Get image ─────────────────────────────────────────
                    img_bytes = None
                    img_type  = 'image/jpeg'
                    img_saved = ''

                    # Priority 1: Amazon CDN (ASIN-based, instant)
                    if image_cdn:
                        img_bytes, img_type = download_image(image_cdn)
                        if img_bytes:
                            img_saved = image_cdn
                            print(f"    📷 source: Amazon CDN")

                    # Priority 2: Source message photo via Telethon → Telegraph
                    # (This is the REAL product photo the source channel posted)
                    if not img_bytes and has_photo:
                        print("    📷 downloading source photo...")
                        telegraph_url = await get_photo_from_msg(client, msg)
                        if telegraph_url:
                            img_bytes, img_type = download_image(
                                telegraph_url, referer='https://telegra.ph/')
                            if img_bytes:
                                img_saved = telegraph_url
                                print(f"    📷 source: Telethon→Telegraph")

                    # ── 6. Post ──────────────────────────────────────────────
                    ok_post = False
                    resp    = ''

                    if img_bytes:
                        ok_post, resp = send_photo(chat_id, img_bytes, clean, img_type)
                        if not ok_post:
                            print(f"    sendPhoto failed ({resp[:60]}), trying text")
                            ok_post, resp = send_text(chat_id, clean)
                    else:
                        ok_post, resp = send_text(chat_id, clean)

                    if ok_post:
                        mode = '📷' if img_bytes else '📝'
                        print(f"    ✅ POSTED {mode} | {affiliate_url[:45] if affiliate_url else ''}")
                        posted_hashes.add(msg_hash)
                        deals = add_deal(deals, clean, affiliate_url or '', channel, img_saved)
                        found += 1
                        total += 1
                        time.sleep(random.uniform(1.5, 3.0))
                    else:
                        print(f"    ❌ FAILED: {resp[:120]}")

                print(f"\n  scanned={count} posted={found}")

            except Exception as e:
                print(f"  ❌ {channel}: {e}")
                traceback.print_exc()

            state[channel] = new_last_id

    save_json(STATE_FILE, state)
    save_json(DEALS_FILE, deals)
    print(f"\n{'='*55}")
    print(f"v{VERSION} done: {total} posted | {len(deals)} on website")

if __name__ == '__main__':
    asyncio.run(run())
