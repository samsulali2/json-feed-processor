"""
Telegram Affiliate Deal Bot
===========================
Reads source Telegram channels, injects affiliate links,
downloads product images and posts to our channel + website.

Priority order:
  A. Reliable images (download ourselves, send via Bot API sendPhoto)
  B. Bullet-proof affiliate link replacement
  C. HTML formatting, rate-limit delays, better logging
"""

import os, re, json, asyncio, requests, hashlib, io, random, time, traceback
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from telethon import TelegramClient
from telethon.sessions import StringSession

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

# Domains that are deal aggregator pages — their URLs are NOT product links
SOURCE_SITE_DOMAINS = [
    'desidime.com', 'dealsmagnet.com', 'freekaamaal.com',
    'lootdunia.com', 'dealsbazaar.in', 'hcti.io',
]

# Short link services — must be expanded to get the real product URL
SHORTENER_DOMAINS = [
    'ddime.in', 'amzn.to', 'amzn.in', 'a.co/',
    'bitli.store', 'bit.ly', 'clnk.in', 'cutt.ly',
    'rb.gy', 't.ly', 'tiny.cc', 'ow.ly', 'shorturl.at',
]

# Domains supported by Cuelinks affiliate programme
CUELINKS_DOMAINS = [
    'flipkart.com', 'myntra.com', 'ajio.com', 'nykaa.com',
    'tatacliq.com', 'shopsy.in', 'meesho.com', 'jiomart.com',
    'croma.com',
]

# Noise lines to strip from messages
IGNORE_PREFIXES = ['on #', 'read more', 'buy now', 'join ', 'follow us']

# Browser-like headers for HTTP requests
BROWSE_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'en-IN,en;q=0.9',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
}

# ═════════════════════════════════════════════════════════════════════════════
# SECTION A — IMAGE HANDLING
# Strategy:
#   1. Extract ASIN from Amazon URL → build CDN image URL
#   2. Try to download the image ourselves (browser headers + Referer)
#   3. If download succeeds → send via Bot API sendPhoto (multipart upload)
#   4. If download fails → send text-only message (no broken image)
#   5. For Flipkart/others → try scraping og:image from product page
#   6. If source Telegram message had a photo → download via Telethon as fallback
# ═════════════════════════════════════════════════════════════════════════════

def get_asin(url):
    """Extract Amazon ASIN from any Amazon URL"""
    m = re.search(r'/(?:dp|gp/product|d)/([A-Z0-9]{10})(?:[/?]|$)', url)
    return m.group(1) if m else None

def get_amazon_image_url(asin):
    """Build Amazon CDN image URL from ASIN"""
    return f"https://m.media-amazon.com/images/I/{asin}._SL500_.jpg"

def download_image(image_url, referer='https://www.amazon.in/'):
    """
    Download image bytes from URL using browser-like headers.
    Returns (bytes, content_type) or (None, None) on failure.
    WHY: Telegram's own URL fetcher is unreliable for e-commerce CDNs in 2025.
    We download ourselves and upload as a file — 100% reliable.
    """
    try:
        headers = {
            **BROWSE_HEADERS,
            'Referer': referer,
            'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
        }
        r = requests.get(image_url, headers=headers, timeout=12, stream=True)
        if r.status_code == 200 and 'image' in r.headers.get('content-type', ''):
            data = r.content
            if len(data) > 1000:  # sanity check — real image > 1KB
                print(f"    📷 downloaded {len(data)//1024}KB from {image_url[:60]}")
                return data, r.headers.get('content-type', 'image/jpeg')
    except Exception as e:
        print(f"    📷 download failed: {e}")
    return None, None

def get_og_image(product_url):
    """
    Scrape og:image from a Flipkart/Myntra etc product page.
    Returns image URL string or None.
    WHY: Non-Amazon products don't have a predictable CDN image URL pattern.
    """
    try:
        r = requests.get(product_url, headers=BROWSE_HEADERS, timeout=10)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, 'html.parser')
            og = soup.find('meta', property='og:image')
            if og and og.get('content'):
                return og['content']
    except Exception as e:
        print(f"    og:image scrape failed: {e}")
    return None

def send_photo_to_telegram(chat_id, image_bytes, caption, content_type='image/jpeg'):
    """
    Upload image file to Telegram via Bot API sendPhoto (multipart).
    WHY: Sending as a file upload bypasses Telegram's URL-fetching issues.
    Returns (ok, response_text)
    """
    try:
        ext = 'jpg' if 'jpeg' in content_type else content_type.split('/')[-1]
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
            data={
                'chat_id':    chat_id,
                'caption':    caption,
                'parse_mode': 'HTML',
            },
            files={'photo': (f'product.{ext}', image_bytes, content_type)},
            timeout=30,
        )
        return r.status_code == 200, r.text
    except Exception as e:
        return False, str(e)

def send_text_to_telegram(chat_id, text):
    """Send plain HTML text message via Bot API"""
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                'chat_id':                  chat_id,
                'text':                     text,
                'parse_mode':               'HTML',
                'disable_web_page_preview': True,
            },
            timeout=15,
        )
        return r.status_code == 200, r.text
    except Exception as e:
        return False, str(e)

async def get_telegram_photo(tg_client, msg):
    """Download photo from a Telegram message via Telethon. Fallback only."""
    try:
        data = await tg_client.download_media(msg.photo, bytes)
        if data and len(data) > 1000:
            return data, 'image/jpeg'
    except Exception as e:
        print(f"    Telethon photo download failed: {e}")
    return None, None


# ═════════════════════════════════════════════════════════════════════════════
# SECTION B — AFFILIATE LINK PROCESSING
# Strategy:
#   1. Extract all URLs from message text
#   2. Expand shorteners (ddime.in, bit.ly etc) to real product URLs
#   3. Convert real URLs to affiliate versions (Amazon tag / Cuelinks)
#   4. Replace original URLs in text with affiliate versions
#   5. Remove any remaining source-site or un-monetizable URLs
#   6. If main affiliate URL not visible in text → append it explicitly
# ═════════════════════════════════════════════════════════════════════════════

def extract_urls(text):
    return re.findall(r'https?://[^\s\)\]>\"\'<]+', text or '')

def expand_url(url, timeout=8):
    """Follow all redirects to get the final destination URL"""
    try:
        r = requests.head(url, allow_redirects=True, timeout=timeout,
                          headers=BROWSE_HEADERS)
        return r.url
    except Exception:
        try:  # some servers block HEAD — try GET
            r = requests.get(url, allow_redirects=True, timeout=timeout,
                             headers=BROWSE_HEADERS, stream=True)
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

def is_ignorable(url):
    """URLs we should skip entirely"""
    noise = ['t.me', 'telegram.me', 'instagram.com', 'twitter.com',
             'facebook.com', 'youtube.com', 'play.google.com', 'hcti.io']
    return any(d in url for d in noise)

def make_amazon_affiliate(url):
    """Inject our Amazon tag into any Amazon URL, preserving clean ASIN URL"""
    asin = get_asin(url)
    if asin:
        return f"https://www.amazon.in/dp/{asin}?tag={AMAZON_TAG}"
    # No ASIN found — strip existing tags and inject ours
    url = re.sub(r'[?&]tag=[^&]*', '', url)
    url = re.sub(r'[?&]ascsubtag=[^&]*', '', url)
    url = re.sub(r'/ref=[^/?&]*', '', url)
    url = url.rstrip('?&')
    sep = '&' if '?' in url else '?'
    return f"{url}{sep}tag={AMAZON_TAG}"

def make_cuelinks_affiliate(url):
    """Convert a Flipkart/Myntra/etc URL to a Cuelinks affiliate URL"""
    if not CUELINKS_KEY:
        return None
    try:
        r = requests.get(
            'https://api.cuelinks.com/v1/affiliate-url',
            params={'apiKey': CUELINKS_KEY, 'url': url},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            aff  = data.get('affiliateUrl') or data.get('url')
            if aff and aff != url:
                return aff
        else:
            print(f"    Cuelinks HTTP {r.status_code}")
    except Exception as e:
        print(f"    Cuelinks error: {e}")
    return None

def shorten(url):
    """Shorten URL via TinyURL API"""
    try:
        r = requests.get(f'https://tinyurl.com/api-create.php?url={url}', timeout=10)
        if r.status_code == 200 and r.text.startswith('http'):
            return r.text.strip()
    except Exception:
        pass
    return url  # return original if shortening fails

def process_single_url(url):
    """
    Convert one URL to (affiliate_short_url, image_url_or_none).
    Handles expansion → affiliate conversion → shortening.
    Returns (None, None) if URL is not monetizable.
    """
    original = url

    # Step 1: Expand shorteners
    if needs_expanding(url):
        expanded = expand_url(url)
        if expanded != url:
            print(f"    ↗ {url[:50]} → {expanded[:70]}")
            url = expanded
        else:
            print(f"    ↗ could not expand {url[:50]}")

    # Step 2: Check if still a source site after expansion
    if is_source_site(url) or is_ignorable(url):
        return None, None

    # Step 3: Amazon affiliate
    if is_amazon(url):
        aff   = make_amazon_affiliate(url)
        short = shorten(aff)
        # Image URL from ASIN
        asin  = get_asin(aff)
        image = get_amazon_image_url(asin) if asin else ''
        print(f"    🛍 Amazon aff: {short[:60]}")
        return short, image

    # Step 4: Cuelinks affiliate (Flipkart, Myntra etc)
    if is_flipkart_family(url):
        aff = make_cuelinks_affiliate(url)
        if aff:
            short = shorten(aff)
            print(f"    🛍 Cuelinks aff: {short[:60]}")
            return short, None  # image handled via og:image scrape later
        return None, None

    return None, None

def clean_text(working_text):
    """
    Remove noise lines from message text.
    Called AFTER URL replacements so we don't lose URLs accidentally.
    """
    lines = working_text.split('\n')
    out   = []
    for line in lines:
        s = line.strip()
        if not s:
            out.append('')
            continue
        sl = s.lower()
        if any(sl.startswith(p) for p in IGNORE_PREFIXES):
            continue
        if re.match(r'^#\w', s):          # pure hashtag line
            continue
        if re.match(r'^link:\s*$', s, re.I):   # empty "Link:" label
            continue
        if re.match(r'^https?://\S+$', s):     # line is ONLY a bare URL (already replaced or noise)
            continue
        # Remove lines whose only content was a URL we deleted (now just whitespace/label)
        if s.endswith(':') and len(s) < 25 and not extract_urls(s):
            continue
        out.append(line)
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(out)).strip()

def process_message(raw_text):
    """
    Full message processor.
    Returns (clean_text, primary_affiliate_url, primary_image_cdn_url)
    """
    if not raw_text or not raw_text.strip():
        return None, None, None

    all_urls = extract_urls(raw_text)
    if not all_urls:
        return None, None, None

    working       = raw_text
    primary_aff   = None
    primary_image = None

    # Sort URLs by length descending — replace longest first to avoid
    # partial replacements (e.g. replacing amzn.in inside a longer URL)
    for url in sorted(all_urls, key=len, reverse=True):
        if is_ignorable(url):
            # Remove noise URLs from text entirely
            working = working.replace(url, '')
            continue

        aff, img = process_single_url(url)

        if aff:
            # Replace original URL with affiliate version in message body
            working = working.replace(url, aff)
            if not primary_aff:
                primary_aff   = aff
                primary_image = img
        else:
            # Not monetizable — remove from text (source site links, etc.)
            working = working.replace(url, '')

    # Clean up noise lines
    result = clean_text(working)
    if not result:
        return None, None, None

    # Ensure affiliate link is visible in final text
    if primary_aff and primary_aff not in result:
        result += f"\n\n🔗 <a href='{primary_aff}'>Buy Here</a>"

    return result, primary_aff, primary_image


# ═════════════════════════════════════════════════════════════════════════════
# SECTION C — STATE, DEALS.JSON, HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
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

def html_escape(text):
    """Escape text for Telegram HTML parse_mode"""
    return (text
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;'))


# ═════════════════════════════════════════════════════════════════════════════
# SECTION D — MAIN LOOP
# ═════════════════════════════════════════════════════════════════════════════

async def run():
    state  = load_json(STATE_FILE, {})
    deals  = load_json(DEALS_FILE, [])
    total  = 0
    chat_id = f'@{YOUR_CHANNEL}'

    print(f"Channel  : {chat_id}")
    print(f"Amazon   : {AMAZON_TAG}")
    print(f"Cuelinks : {'on' if CUELINKS_KEY else 'off'}")
    print(f"Sources  : {len(SOURCE_CHANNELS)} channels")
    print(f"Session  : {SESSION_STRING[:20]}...")

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

    # ── Read source channels ──────────────────────────────────────────────────
    async with client:
        for channel in SOURCE_CHANNELS:
            if not channel:
                continue

            last_id     = state.get(channel, 0)
            new_last_id = last_id
            found       = 0
            # First-ever run: only grab last 5 to avoid spam
            # Subsequent runs: grab up to 20 new messages
            limit = 5 if last_id == 0 else 20

            print(f"\n{'─'*50}")
            print(f"Channel: {channel}  (last_id={last_id}, limit={limit})")

            try:
                count = 0
                async for msg in client.iter_messages(
                    channel, min_id=last_id, limit=limit
                ):
                    count += 1
                    if msg.id > new_last_id:
                        new_last_id = msg.id

                    raw = (getattr(msg, 'text', '') or
                           getattr(msg, 'caption', '') or '')

                    has_photo = bool(getattr(msg, 'photo', None))
                    print(f"\n  msg {msg.id}: {len(raw)} chars | photo={has_photo}")

                    if not raw.strip() and not has_photo:
                        print(f"  ⬜ skipped (no text, no photo)")
                        continue

                    # Dedup across channels in this run
                    msg_hash = hashlib.md5(raw[:80].encode()).hexdigest()[:8]
                    if msg_hash in posted_hashes:
                        print(f"  ⏭  duplicate skip")
                        continue

                    # ── Process message ──────────────────────────────────────
                    clean, aff_url, image_cdn_url = process_message(raw)
                    print(f"  → clean={bool(clean)} | aff={aff_url and aff_url[:40]} | cdn_img={bool(image_cdn_url)}")

                    if not clean and not has_photo:
                        print(f"  ⬜ nothing to post")
                        continue

                    if not clean:
                        clean = ''  # photo-only message

                    # ── Build caption/text ────────────────────────────────────
                    caption = clean
                    caption += f"\n\n🛒 Deals by @{YOUR_CHANNEL}"

                    # ── Get image bytes ───────────────────────────────────────
                    image_bytes    = None
                    image_type     = 'image/jpeg'
                    final_image_url = ''  # what we save to deals.json

                    # A1. Try Amazon CDN image download
                    if image_cdn_url:
                        image_bytes, image_type = download_image(
                            image_cdn_url,
                            referer='https://www.amazon.in/'
                        )
                        if image_bytes:
                            final_image_url = image_cdn_url

                    # A2. Try og:image from Flipkart/other product page
                    if not image_bytes and aff_url and not is_amazon(aff_url):
                        og_url = get_og_image(aff_url)
                        if og_url:
                            image_bytes, image_type = download_image(
                                og_url,
                                referer=aff_url
                            )
                            if image_bytes:
                                final_image_url = og_url

                    # A3. Fallback: use Telethon to grab original message photo
                    if not image_bytes and has_photo:
                        print(f"  📷 falling back to Telethon photo download")
                        image_bytes, image_type = await get_telegram_photo(client, msg)
                        if image_bytes:
                            final_image_url = 'telegraph_fallback'

                    # ── Send to Telegram ─────────────────────────────────────
                    ok   = False
                    resp = ''

                    if image_bytes:
                        # Send as photo with caption
                        ok, resp = send_photo_to_telegram(
                            chat_id, image_bytes, caption, image_type
                        )
                        if not ok:
                            print(f"  ⚠️  sendPhoto failed ({resp[:80]}), trying text")
                            ok, resp = send_text_to_telegram(chat_id, caption)
                    else:
                        # No image — send text only
                        ok, resp = send_text_to_telegram(chat_id, caption)

                    # ── Handle result ─────────────────────────────────────────
                    if ok:
                        print(f"  ✅ posted ({'with photo' if image_bytes else 'text only'})")
                        posted_hashes.add(msg_hash)
                        deals = add_deal(deals, caption, aff_url or '', channel, final_image_url)
                        found += 1
                        total += 1
                        # Rate-limit guard — small sleep between posts
                        time.sleep(random.uniform(1.5, 3.5))
                    else:
                        print(f"  ❌ post failed: {resp[:120]}")

                print(f"\n  ── scanned {count} | posted {found}")

            except Exception as e:
                print(f"  ❌ channel error: {e}")
                traceback.print_exc()

            state[channel] = new_last_id

    # ── Save ──────────────────────────────────────────────────────────────────
    save_json(STATE_FILE, state)
    save_json(DEALS_FILE, deals)
    print(f"\n{'='*50}")
    print(f"✅ Done: {total} posted | {len(deals)} deals saved")

if __name__ == '__main__':
    asyncio.run(run())
