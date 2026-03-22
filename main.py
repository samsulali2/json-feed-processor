"""
Telegram Affiliate Deal Bot  v9.0
===================================
Fixes in this version:
  - fktr.in (Flipkart shortener) added to SHORTENER_DOMAINS
  - URL extraction fixed: filters 'X : https://...' table artifacts
  - All functions deduplicated (file was corrupted with multiple copies)
  - 12-point pre-post checklist
  - Groq AI quality check (optional)
  - Duplicate hash persistence across runs
"""

import os, re, json, asyncio, requests, hashlib, io, random, time, traceback
import urllib.parse
from datetime import datetime, timezone
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl

VERSION = "9.0"

# ── Config ────────────────────────────────────────────────────────────────────
def _require(key):
    val = os.environ.get(key, '').strip()
    if not val:
        raise SystemExit(f"❌ Secret {key} is missing — set it in GitHub Secrets")
    return val

API_ID          = int(_require("A1"))
API_HASH        = _require("A2")
BOT_TOKEN       = _require("A3")
SESSION_STRING  = _require("A4")
YOUR_CHANNEL    = _require("A5").lstrip('@')
SOURCE_CHANNELS = [c.strip().lstrip('@') for c in _require("A6").split(",") if c.strip()]
AMAZON_TAG      = _require("A7")
CUELINKS_KEY    = os.environ.get("A8", "").strip()
GROQ_API_KEY    = os.environ.get("GROQ_API_KEY", "").strip()

STATE_FILE  = "last_seen.json"
DEALS_FILE  = "deals.json"
HASHES_FILE = "seen_hashes.json"
MAX_DEALS   = 200
MAX_HASHES  = 2000

# Short-link services that MUST be expanded to get the real product URL
SHORTENER_DOMAINS = [
    'ddime.in', 'amzn.clnk.in', 'clnk.in',   # deal channel shorteners
    'amzn.to', 'amzn.in', 'a.co/',             # Amazon shorteners
    'fktr.in', 'dl.flipkart.com',              # Flipkart shorteners
    'bitli.store', 'bit.ly', 'cutt.ly',        # generic shorteners
    'rb.gy', 't.ly', 'tiny.cc', 'ow.ly',
    'shorturl.at', 'tinyurl.com',
]

# Deal aggregator sites — their URLs are NOT product pages
SOURCE_SITE_DOMAINS = [
    'desidime.com', 'dealsmagnet.com', 'freekaamaal.com',
    'lootdunia.com', 'dealsbazaar.in', 'hcti.io',
]

# Stores supported by Cuelinks
CUELINKS_DOMAINS = [
    'flipkart.com', 'myntra.com', 'ajio.com', 'nykaa.com',
    'tatacliq.com', 'shopsy.in', 'meesho.com', 'jiomart.com',
    'croma.com',
]

# Social / noise URLs to skip
IGNORE_URL_DOMAINS = [
    't.me', 'telegram.me', 'instagram.com', 'twitter.com',
    'facebook.com', 'youtube.com', 'play.google.com',
]

# Noise line prefixes to strip from messages
NOISE_PREFIXES = [
    'on #', 'read more', 'buy now', 'link:', 'join ',
    'follow', 'share ', 'deals by', 'source:', 'via ',
    'forwarded', 'channel:',
]

BROWSE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'en-IN,en;q=0.9',
}


# ─────────────────────────────────────────────────────────────────────────────
# URL UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

def extract_text_urls(text):
    """
    Extract URLs from plain text.
    KEY FIX: filters desidime table artifacts like 'a : https://fktr.in/Mrq'
    where a single letter + space + colon + space precedes the URL.
    """
    if not text:
        return []
    results = []
    for m in re.finditer(r'https?://\S+', text):
        pos = m.start()
        url = m.group()
        # Strip trailing punctuation
        while url and url[-1] in '.,;:!?)>':
            url = url[:-1]
        if not url:
            continue
        # Skip "X : https://..." single-letter table artifacts from desidime
        before4 = text[max(0, pos - 4):pos]
        if re.match(r'^[a-zA-Z] : $', before4):
            continue
        # Accept if at start of string or preceded by whitespace/punctuation
        if pos == 0:
            results.append(url)
        elif text[pos - 1] in ' \t\n\r([,:;=':
            results.append(url)
    return results

def extract_href_urls(text):
    """Extract URLs from HTML href attributes"""
    return [u for u in re.findall(r'href=["\']([^"\']+)["\']', text or '')
            if u.startswith('http')]

def extract_all_urls_from_msg(msg):
    """
    Extract URLs from ALL sources in a Telegram message:
    1. HTML href attributes (some channels post raw HTML)
    2. Plain text URLs
    3. Hidden entity URLs ([Buy Now](url) → entity.url)
    """
    seen = []
    raw  = msg.text or msg.message or ''

    # 1. HTML hrefs
    if 'href=' in raw:
        for url in extract_href_urls(raw):
            if url not in seen:
                seen.append(url)

    # 2. Plain text URLs
    for url in extract_text_urls(raw):
        if url not in seen:
            seen.append(url)

    # 3. Entity URLs (hidden hyperlinks)
    if msg.entities:
        for ent in msg.entities:
            if isinstance(ent, MessageEntityTextUrl):
                if ent.url and ent.url not in seen:
                    seen.append(ent.url)
            elif isinstance(ent, MessageEntityUrl):
                u = raw[ent.offset: ent.offset + ent.length]
                if u and u not in seen:
                    seen.append(u)
    return seen

def is_amazon(url):        return bool(re.search(r'amazon\.in|amazon\.com', url or ''))
def is_flipkart_fam(url):  return any(d in (url or '') for d in CUELINKS_DOMAINS)
def is_source_site(url):   return any(d in (url or '') for d in SOURCE_SITE_DOMAINS)
def is_shortener(url):     return any(d in (url or '') for d in SHORTENER_DOMAINS)
def is_ignorable(url):     return any(d in (url or '') for d in IGNORE_URL_DOMAINS)
def is_junk_url(url):      return is_source_site(url) or is_shortener(url) or is_ignorable(url)

def is_valid_url(url):
    if not url or not url.startswith('https://'):
        return False
    p = urllib.parse.urlparse(url)
    return bool(p.netloc) and bool(p.path)

def expand_url(url, timeout=10):
    """Follow all redirects. Returns (final_url, success_bool)."""
    for method in ('HEAD', 'GET'):
        try:
            if method == 'HEAD':
                r = requests.head(url, allow_redirects=True, timeout=timeout, headers=BROWSE_HEADERS)
            else:
                r = requests.get(url, allow_redirects=True, timeout=timeout, headers=BROWSE_HEADERS, stream=True)
            if r.url and is_valid_url(r.url):
                return r.url, True
        except Exception:
            continue
    return url, False

def expand_url_fully(url):
    """Expand shortener, handling double-hop chains (e.g. ddime.in → amzn.clnk.in → amazon.in)"""
    if not is_shortener(url):
        return url, True
    final, ok = expand_url(url)
    if not ok:
        return url, False
    # If result is still a shortener, expand once more
    if is_shortener(final) and final != url:
        print(f"    ↗↗ double-hop detected: {final[:50]}")
        final2, ok2 = expand_url(final)
        if ok2 and final2 != final:
            return final2, True
    return final, True

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
    if not CUELINKS_KEY:
        return None
    try:
        r = requests.get('https://api.cuelinks.com/v1/affiliate-url',
                         params={'apiKey': CUELINKS_KEY, 'url': url}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            print(f"    Cuelinks: {str(data)[:100]}")
            aff = (data.get('affiliateUrl') or data.get('affiliate_url') or
                   data.get('shortUrl')     or data.get('short_url'))
            if aff and aff != url and is_valid_url(aff):
                return aff
            print("    Cuelinks: returned same/invalid URL")
        else:
            print(f"    Cuelinks HTTP {r.status_code}: {r.text[:80]}")
    except Exception as e:
        print(f"    Cuelinks error: {e}")
    return None

def shorten(url):
    if not is_valid_url(url):
        return url
    try:
        r = requests.get('https://tinyurl.com/api-create.php', params={'url': url}, timeout=10)
        result = r.text.strip()
        if r.status_code == 200 and result.startswith('https://tinyurl.com/') and len(result) > 24:
            return result
        print(f"    TinyURL invalid: {result[:50]}")
    except Exception as e:
        print(f"    TinyURL error: {e}")
    return url

def resolve_to_affiliate(raw_url):
    """Convert URL → (affiliate_url, image_cdn_url). Returns (None,None) if unusable."""
    url = raw_url

    # Expand shorteners
    if is_shortener(url):
        expanded, ok = expand_url_fully(url)
        if not ok:
            print(f"    ✗ expand failed: {url[:50]}")
            return None, None
        if is_source_site(expanded) or is_ignorable(expanded):
            print(f"    ✗ expanded to noise: {expanded[:50]}")
            return None, None
        if not is_amazon(expanded) and not is_flipkart_fam(expanded):
            print(f"    ✗ expanded to unknown store: {expanded[:50]}")
            return None, None
        print(f"    ↗ {url[:40]} → {expanded[:55]}")
        url = expanded

    if is_ignorable(url) or is_source_site(url):
        return None, None

    if is_amazon(url):
        aff   = make_amazon_affiliate(url)
        short = shorten(aff)
        image = get_amazon_image_cdn(aff)
        print(f"    ✅ Amazon → {short[:55]}")
        return short, image

    if is_flipkart_fam(url):
        aff = make_cuelinks_affiliate(url)
        if aff:
            short = shorten(aff)
            print(f"    ✅ Cuelinks → {short[:55]}")
            return short, ''
        short = shorten(url)
        print(f"    ⚠️ Cuelinks failed, direct → {short[:55]}")
        return short, ''

    print(f"    ✗ unrecognised store: {url[:50]}")
    return None, None


# ─────────────────────────────────────────────────────────────────────────────
# IMAGE HANDLING
# ─────────────────────────────────────────────────────────────────────────────

def download_image_bytes(image_url, referer='https://www.amazon.in/'):
    if not image_url:
        return None, None
    try:
        h = {**BROWSE_HEADERS, 'Referer': referer,
             'Accept': 'image/avif,image/webp,image/apng,image/*;q=0.9'}
        r = requests.get(image_url, headers=h, timeout=15, stream=True)
        ctype = r.headers.get('content-type', '')
        if r.status_code == 200 and 'image' in ctype and len(r.content) > 3000:
            print(f"    📷 {len(r.content)//1024}KB downloaded")
            return r.content, ctype
    except Exception as e:
        print(f"    📷 download failed: {e}")
    return None, None

async def get_photo_from_source(tg_client, msg):
    """
    Download photo bytes directly via Telethon (for sendPhoto).
    Also tries to upload to Telegraph for website URL storage.
    Returns (bytes_or_None, telegraph_url_or_empty).
    """
    if not getattr(msg, 'photo', None):
        return None, ''
    try:
        data = await tg_client.download_media(msg.photo, bytes)
        if not data or len(data) < 3000:
            print("    📷 photo too small")
            return None, ''
        print(f"    📷 {len(data)//1024}KB downloaded via Telethon")
        # Upload to Telegraph for website storage (non-critical)
        telegraph_url = ''
        try:
            r = requests.post('https://telegra.ph/upload',
                              files={'file': ('p.jpg', io.BytesIO(data), 'image/jpeg')},
                              timeout=15)
            if r.status_code == 200:
                res = r.json()
                if isinstance(res, list) and res:
                    telegraph_url = f"https://telegra.ph{res[0]['src']}"
                    print(f"    📷 Telegraph URL saved: {telegraph_url}")
        except Exception as te:
            print(f"    📷 Telegraph upload skipped: {te}")
        return data, telegraph_url
    except Exception as e:
        print(f"    📷 Telethon download failed: {e}")
    return None, ''

# ─────────────────────────────────────────────────────────────────────────────
# MESSAGE TEXT BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def strip_html(text):
    return re.sub(r'<[^>]+>', ' ', text) if ('<' in (text or '') and '>' in (text or '')) else text

def sanitize_text(text, affiliate_url):
    for url in extract_text_urls(text):
        if is_junk_url(url) and url != affiliate_url:
            text = text.replace(url, '')
    return text

def build_clean_text(msg, affiliate_url):
    raw = msg.text or msg.message or ''
    # Strip HTML if present
    if '<' in raw and '>' in raw:
        raw = strip_html(raw)
        raw = re.sub(r'\s+', ' ', raw).strip()
    lines = raw.split('\n')
    clean = []
    for line in lines:
        s  = line.strip()
        sl = s.lower()
        if not s:
            clean.append('')
            continue
        if any(sl.startswith(p) for p in NOISE_PREFIXES): continue
        if re.match(r'^#\w', s): continue
        if re.match(r'^@\w', s): continue
        line_urls = extract_text_urls(s)
        if line_urls and all(is_junk_url(u) for u in line_urls):
            continue
        clean.append(line)
    result = re.sub(r'\n{3,}', '\n\n', '\n'.join(clean)).strip()
    result = sanitize_text(result, affiliate_url)
    # Remove orphan label lines
    final = []
    for line in result.split('\n'):
        s = line.strip()
        if re.match(r'^[\w\s]{2,20}:\s*$', s) and not extract_text_urls(s):
            continue
        final.append(line)
    result = re.sub(r'\n{3,}', '\n\n', '\n'.join(final)).strip()
    if affiliate_url:
        result += f"\n\n🔗 {affiliate_url}"
    return result


# ─────────────────────────────────────────────────────────────────────────────
# PRE-POST CHECKLIST (12 checks)
# ─────────────────────────────────────────────────────────────────────────────

class CheckResult:
    def __init__(self):
        self.fixed  = []
        self.failed = []
    def fix(self, name, detail=""):
        self.fixed.append(f"{name}" + (f": {detail}" if detail else ""))
    def fail(self, name, detail=""):
        self.failed.append(f"{name}" + (f": {detail}" if detail else ""))
    @property
    def is_good(self):
        return len(self.failed) == 0
    def summary(self):
        lines = [f"  🔧 {f}" for f in self.fixed] + [f"  ❌ {f}" for f in self.failed]
        return '\n'.join(lines) if lines else "  ✅ all checks passed"

def run_checklist(text, affiliate_url):
    r = CheckResult()

    # 1. HTML tags
    if re.search(r'<[a-zA-Z][^>]*>', text):
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        r.fix("HTML stripped")

    # 2. Source site URLs
    leaks = [u for u in extract_text_urls(text) if is_source_site(u)]
    if leaks:
        for u in leaks: text = text.replace(u, '')
        r.fix("source URLs removed", str(len(leaks)))

    # 3. Shortener URLs (except our affiliate)
    short_leaks = [u for u in extract_text_urls(text)
                   if is_shortener(u) and u != affiliate_url]
    if short_leaks:
        for u in short_leaks: text = text.replace(u, '')
        r.fix("shortener URLs removed", str(len(short_leaks)))

    # 4. Must have affiliate link
    if not affiliate_url:
        r.fail("no affiliate URL")
        return text, affiliate_url, r

    # 5. TinyURL homepage check
    if affiliate_url.rstrip('/') == 'https://tinyurl.com':
        r.fail("TinyURL homepage — shortening failed")
        return text, affiliate_url, r

    # 6. Affiliate link in text
    if affiliate_url not in text:
        text += f"\n\n🔗 {affiliate_url}"
        r.fix("affiliate URL appended")

    # 7. Orphan label lines
    fixed_lines = []
    removed = 0
    for line in text.split('\n'):
        s = line.strip()
        if re.match(r'^[\w\s]{2,20}:\s*$', s) and not extract_text_urls(s):
            removed += 1
            continue
        fixed_lines.append(line)
    if removed:
        text = re.sub(r'\n{3,}', '\n\n', '\n'.join(fixed_lines)).strip()
        r.fix("orphan labels removed", str(removed))

    # 8. Must have meaningful content
    meaningful = re.sub(r'https?://\S+', '', text)
    meaningful = re.sub(r'[🔗🛒\s]', '', meaningful)
    if len(meaningful) < 10:
        r.fail("empty content", f"only {len(meaningful)} chars")
        return text, affiliate_url, r

    # 9. Text length
    if len(text) > 4096:
        text = text[:3900] + f"\n\n🔗 {affiliate_url}"
        r.fix("trimmed to 4096")

    # 10. Duplicate channel branding
    count = text.lower().count('@' + YOUR_CHANNEL.lower())
    if count > 1:
        text = re.sub(re.escape('@' + YOUR_CHANNEL), '', text, flags=re.IGNORECASE).strip()
        text += f"\n\n🛒 Deals by @{YOUR_CHANNEL}"
        r.fix("duplicate branding removed")

    # 11. Price symbols (info only — never fail)
    # 12. Product title
    title_part = text.split('🔗')[0].strip()
    words = [w for w in title_part.split() if len(w) > 2]
    if len(words) < 3:
        r.fail("no product title", f"only {len(words)} words")
        return text, affiliate_url, r

    return text, affiliate_url, r

def groq_quality_check(text, affiliate_url):
    if not GROQ_API_KEY:
        return True, "no key"
    snippet = text.split('🔗')[0].strip()[:400]
    prompt = (f'Check this deal post. Reply ONLY with JSON {{"ok":true/false,"reason":"one line"}}.\n'
              f'Reject if: contains HTML tags, source site names (desidime/dealsmagnet), '
              f'raw short URLs (ddime.in/fktr.in), or has no product name.\n\nPost:\n{snippet}')
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={"model": "llama-3.1-8b-instant",
                  "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": 60, "temperature": 0},
            timeout=8
        )
        if r.status_code == 200:
            content = r.json()['choices'][0]['message']['content'].strip()
            m = re.search(r'\{.*\}', content, re.DOTALL)
            if m:
                data = json.loads(m.group())
                return data.get('ok', True), data.get('reason', 'ok')
    except Exception as e:
        print(f"    Groq error: {e}")
    return True, "groq error (allow through)"


# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM BOT API
# ─────────────────────────────────────────────────────────────────────────────

def post_photo_bytes(chat_id, img_bytes, caption, ctype='image/jpeg'):
    """Send image bytes via multipart upload (for Telegraph/Telethon photos)"""
    try:
        clean_cap = re.sub(r'<[^>]+>', '', caption)[:1024]
        ext = 'jpg' if 'jpeg' in ctype else ctype.split('/')[-1]
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
            data={'chat_id': chat_id, 'caption': clean_cap},
            files={'photo': (f'product.{ext}', img_bytes, ctype)},
            timeout=30,
        )
        if r.status_code != 200:
            print(f"    sendPhoto(bytes) error: {r.text[:100]}")
        return r.status_code == 200, r.text
    except Exception as e:
        return False, str(e)

def post_photo_url(chat_id, image_url, caption):
    """
    Send image by URL — Telegram's servers fetch the image directly.
    Works for Amazon CDN, Telegraph etc where GitHub Actions IPs are blocked.
    """
    try:
        clean_cap = re.sub(r'<[^>]+>', '', caption)[:1024]
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
            json={'chat_id': chat_id, 'photo': image_url, 'caption': clean_cap},
            timeout=20,
        )
        if r.status_code != 200:
            print(f"    sendPhoto(url) error: {r.text[:100]}")
        return r.status_code == 200, r.text
    except Exception as e:
        return False, str(e)

def post_photo(chat_id, img_bytes, caption, ctype='image/jpeg'):
    """Wrapper: use bytes if available, else fall back to text"""
    return post_photo_bytes(chat_id, img_bytes, caption, ctype)

def post_text(chat_id, text):
    try:
        clean = re.sub(r'<[^>]+>', '', text)
        clean = re.sub(r'\s+', ' ', clean).strip()[:4096]
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={'chat_id': chat_id, 'text': clean, 'disable_web_page_preview': True},
            timeout=15,
        )
        if r.status_code != 200:
            print(f"    sendMessage error: {r.text[:100]}")
        return r.status_code == 200, r.text
    except Exception as e:
        return False, str(e)


# ─────────────────────────────────────────────────────────────────────────────
# STATE / DEALS
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
    state         = load_json(STATE_FILE, {})
    deals         = load_json(DEALS_FILE, [])
    posted_hashes = set(load_json(HASHES_FILE, []))
    total         = 0
    chat_id       = f'@{YOUR_CHANNEL}'

    print(f"v{VERSION} | @{YOUR_CHANNEL} | tag={AMAZON_TAG} | cuelinks={'on' if CUELINKS_KEY else 'off'} | groq={'on' if GROQ_API_KEY else 'off'}")
    print(f"Sources: {SOURCE_CHANNELS}")
    print(f"Loaded {len(posted_hashes)} seen hashes")

    # Connect
    print("\nConnecting...")
    try:
        client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            print("❌ SESSION EXPIRED — regenerate A4 via Google Colab")
            return
        me = await client.get_me()
        print(f"✅ Connected as {me.first_name} (@{me.username})")
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
            print(f"  {channel}  last_id={last_id}  limit={limit}")

            try:
                # Quick check: are there new messages?
                latest = await client.get_messages(channel, limit=1)
                if latest:
                    latest_id    = latest[0].id
                    new_avail    = max(0, latest_id - last_id)
                    print(f"  latest_id={latest_id}  new≈{new_avail}")
                    if new_avail == 0:
                        print("  ⏭ no new messages")
                        continue

                async for msg in client.iter_messages(channel, min_id=last_id, limit=limit):
                    count += 1
                    if msg.id > new_last_id:
                        new_last_id = msg.id

                    raw_text  = msg.text or msg.message or ''
                    has_photo = bool(getattr(msg, 'photo', None))
                    print(f"\n  MSG {msg.id}: {len(raw_text)}ch  photo={has_photo}")

                    if not raw_text.strip() and not has_photo:
                        print("    skip: no content")
                        continue

                    # Dedup
                    msg_hash = hashlib.md5(raw_text[:200].encode()).hexdigest()[:10]
                    if msg_hash in posted_hashes:
                        print("    skip: duplicate")
                        continue

                    # 1. Extract URLs
                    all_urls = extract_all_urls_from_msg(msg)
                    print(f"    urls: {all_urls}")

                    # 2. Resolve affiliate URL
                    affiliate_url = None
                    image_cdn     = ''
                    for url in all_urls:
                        if is_ignorable(url): continue
                        aff, img = resolve_to_affiliate(url)
                        if aff:
                            affiliate_url = aff
                            image_cdn     = img or ''
                            break

                    print(f"    affiliate: {affiliate_url or 'NONE'}")

                    if not affiliate_url and not has_photo:
                        print("    skip: no affiliate and no photo")
                        continue

                    # 3. Build clean text
                    clean = build_clean_text(msg, affiliate_url)

                    # 4. Checklist
                    clean, affiliate_url, chk = run_checklist(clean, affiliate_url)
                    print(chk.summary())
                    if not chk.is_good:
                        print("    ✗ checklist FAILED — skipping")
                        continue

                    # 5. Groq AI check (optional)
                    if GROQ_API_KEY:
                        ai_ok, ai_reason = groq_quality_check(clean, affiliate_url)
                        if not ai_ok:
                            print(f"    🤖 Groq REJECTED: {ai_reason}")
                            continue
                        print(f"    🤖 Groq OK: {ai_reason}")

                    # Append branding
                    final_text = clean + f"\n\n🛒 Deals by @{YOUR_CHANNEL}"

                    # 6. Get image
                    img_bytes = None
                    img_saved = ''  # stored in deals.json for website

                    if has_photo:
                        print("    📷 downloading via Telethon...")
                        img_bytes, telegraph_url = await get_photo_from_source(client, msg)
                        if img_bytes:
                            # Telegraph URL for website; fallback to Amazon CDN
                            img_saved = telegraph_url or image_cdn or ''
                        else:
                            print("    📷 Telethon failed, no image bytes")

                    # 7. Post
                    ok_post = False
                    resp    = ''

                    if img_bytes:
                        # Send raw bytes — most reliable, no external URL dependency
                        ok_post, resp = post_photo_bytes(chat_id, img_bytes, final_text)
                        if not ok_post:
                            print(f"    sendPhoto(bytes) failed: {resp[:80]}")
                            ok_post, resp = post_text(chat_id, final_text)

                    elif image_cdn:
                        # No source photo but Amazon deal — use CDN URL
                        # Telegram's servers fetch it (their IPs are not blocked)
                        img_saved = image_cdn
                        ok_post, resp = post_photo_url(chat_id, image_cdn, final_text)
                        if not ok_post:
                            print(f"    sendPhoto(url) failed: {resp[:80]}")
                            ok_post, resp = post_text(chat_id, final_text)

                    else:
                        # No image — text only
                        ok_post, resp = post_text(chat_id, final_text)

                    if ok_post:
                        print(f"    ✅ POSTED {'📷' if img_bytes else '📝'}")
                        posted_hashes.add(msg_hash)
                        deals = add_deal(deals, final_text, affiliate_url or '', channel, img_saved)
                        found += 1
                        total += 1
                        time.sleep(random.uniform(2.0, 4.0))
                    else:
                        print(f"    ❌ FAILED: {resp[:120]}")

                print(f"\n  scanned={count}  posted={found}")

            except Exception as e:
                print(f"  ❌ {channel}: {e}")
                traceback.print_exc()

            state[channel] = new_last_id

    save_json(STATE_FILE, state)
    save_json(DEALS_FILE, deals)
    save_json(HASHES_FILE, list(posted_hashes)[-MAX_HASHES:])
    print(f"\n{'='*55}")
    print(f"v{VERSION} done: {total} posted | {len(deals)} on website")

if __name__ == '__main__':
    asyncio.run(run())
