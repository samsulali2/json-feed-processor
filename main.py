"""
Telegram Affiliate Deal Bot  v14.0
====================================
Changes from v13.0:
  - Fixed broken build_clean_text() — had duplicate/misplaced lines from bad edit
  - Fixed run_checklist() step 3 — now removes raw Amazon/Flipkart URLs with
    OTHER people's affiliate tags, while keeping our own tinyurl affiliate link
  - sanitize_text() now also strips raw Amazon/Flipkart URLs not belonging to us
  - All other affiliate tags in text are replaced with our tag before cleaning
"""

import os, re, json, asyncio, requests, hashlib, io, random, time, traceback, base64
import urllib.parse
from datetime import datetime, timezone
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl
try:
    from feed_scraper import fetch_all_deals as fetch_web_deals
    WEB_FEEDS_ENABLED = True
except ImportError:
    WEB_FEEDS_ENABLED = False

VERSION = "14.0"

# ── Config ────────────────────────────────────────────────────────────────────
def _require(key):
    val = os.environ.get(key, '').strip()
    if not val:
        raise SystemExit(f"❌ Secret {key} missing — set in GitHub Secrets")
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
IMGBB_KEY       = os.environ.get("IMGBB_API_KEY", "").strip()

STATE_FILE  = "last_seen.json"
DEALS_FILE  = "deals.json"
HASHES_FILE = "seen_hashes.json"
MAX_DEALS   = 200
MAX_HASHES  = 2000

SHORTENER_DOMAINS = [
    'ddime.in', 'amzn.clnk.in', 'clnk.in',
    'amzn.to', 'amzn.in', 'a.co/',
    'fktr.in', 'dl.flipkart.com',
    'bitli.store', 'bit.ly', 'cutt.ly',
    'rb.gy', 't.ly', 'tiny.cc', 'ow.ly',
    'shorturl.at', 'tinyurl.com',
]

SOURCE_SITE_DOMAINS = [
    'desidime.com', 'dealsmagnet.com', 'freekaamaal.com',
    'lootdunia.com', 'dealsbazaar.in', 'hcti.io',
]

CUELINKS_DOMAINS = [
    'flipkart.com', 'myntra.com', 'ajio.com', 'nykaa.com',
    'tatacliq.com', 'shopsy.in', 'meesho.com', 'jiomart.com',
    'croma.com', 'snapdeal.com', 'bigbasket.com', 'swiggy.com',
    'zomato.com', 'blinkit.com', 'healthkart.com', 'purplle.com',
    'firstcry.com', 'pepperfry.com', 'lenskart.com',
]

IGNORE_URL_DOMAINS = [
    't.me', 'telegram.me', 'instagram.com', 'twitter.com',
    'facebook.com', 'youtube.com', 'play.google.com',
]

NOISE_PREFIXES = [
    'on #', 'read more', 'buy now', 'link:', 'join ',
    'follow', 'share ', 'deals by', 'source:', 'via ',
    'forwarded', 'channel:',
]

BROWSE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'en-IN,en;q=0.9',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
}


# ─────────────────────────────────────────────────────────────────────────────
# URL UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

def extract_text_urls(text):
    if not text:
        return []
    results = []
    for m in re.finditer(r'https?://\S+', text):
        pos = m.start()
        url = m.group().rstrip('.,;:!?)>')
        if not url:
            continue
        before = text[:pos]
        if re.search(r'(?:^|\s)([a-zA-Z]) : $', before):
            continue
        if pos == 0 or text[pos - 1] in ' \t\n\r([,:;=':
            results.append(url)
    return results

def extract_href_urls(text):
    return [u for u in re.findall(r'href=["\']([^"\']+)["\']', text or '') if u.startswith('http')]

def extract_all_urls_from_msg(msg):
    seen = []
    raw  = msg.text or msg.message or ''
    if 'href=' in raw:
        for url in extract_href_urls(raw):
            if url not in seen: seen.append(url)
    for url in extract_text_urls(raw):
        if url not in seen: seen.append(url)
    if msg.entities:
        for ent in msg.entities:
            if isinstance(ent, MessageEntityTextUrl):
                if ent.url and ent.url not in seen: seen.append(ent.url)
            elif isinstance(ent, MessageEntityUrl):
                u = raw[ent.offset: ent.offset + ent.length]
                if u and u not in seen: seen.append(u)
    return seen

def is_amazon(url):       return bool(re.search(r'amazon\.in|amazon\.com', url or ''))
def is_flipkart_fam(url): return any(d in (url or '') for d in CUELINKS_DOMAINS)
def is_source_site(url):  return any(d in (url or '') for d in SOURCE_SITE_DOMAINS)
def is_shortener(url):    return any(d in (url or '') for d in SHORTENER_DOMAINS)
def is_ignorable(url):    return any(d in (url or '') for d in IGNORE_URL_DOMAINS)
def is_junk_url(url):     return is_source_site(url) or is_shortener(url) or is_ignorable(url)

def is_our_affiliate(url):
    """True if this URL is our own affiliate tinyurl or contains our Amazon tag."""
    if not url:
        return False
    if 'tinyurl.com' in url:
        return True  # all tinyurls we generate are ours
    if is_amazon(url) and AMAZON_TAG in url:
        return True
    return False

def is_foreign_product_url(url):
    """
    True if URL is a raw Amazon/Flipkart URL with someone else's affiliate tag
    or no tag at all — i.e. it should be stripped from display text.
    Our own tinyurl affiliate link is NOT foreign.
    """
    if not url:
        return False
    if is_our_affiliate(url):
        return False
    if is_amazon(url):
        return True   # raw amazon.in URL that isn't our affiliate link
    if is_flipkart_fam(url):
        return True   # raw flipkart URL that isn't our affiliate link
    return False

def is_valid_url(url):
    if not url or not url.startswith('https://'): return False
    p = urllib.parse.urlparse(url)
    return bool(p.netloc) and bool(p.path)

def expand_url(url, timeout=10):
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
    if not is_shortener(url): return url, True
    final, ok = expand_url(url)
    if not ok: return url, False
    if is_shortener(final) and final != url:
        print(f"    ↗↗ double-hop: {final[:50]}")
        final2, ok2 = expand_url(final)
        if ok2 and final2 != final: return final2, True
    return final, True

def get_asin(url):
    m = re.search(r'/(?:dp|gp/product|d)/([A-Z0-9]{10})(?:[/?&]|$)', url or '')
    if m: return m.group(1)
    m2 = re.search(r'[?&](?:creativeASIN|ASIN)=([A-Z0-9]{10})', url or '')
    return m2.group(1) if m2 else None

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
            print(f"    Cuelinks: {str(data)[:100]}")
            aff = (data.get('affiliateUrl') or data.get('affiliate_url') or
                   data.get('shortUrl')     or data.get('short_url'))
            if aff and aff != url and is_valid_url(aff): return aff
            print("    Cuelinks: same/invalid URL")
        else:
            print(f"    Cuelinks HTTP {r.status_code}: {r.text[:80]}")
    except Exception as e:
        print(f"    Cuelinks error: {e}")
    return None

def shorten(url):
    if not is_valid_url(url): return url
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
    """Returns (affiliate_url, product_page_url)."""
    url = raw_url
    product_url = ''

    if is_shortener(url):
        expanded, ok = expand_url_fully(url)
        if not ok:
            print(f"    ✗ expand failed: {url[:50]}")
            return None, ''
        if is_source_site(expanded) or is_ignorable(expanded):
            print(f"    ✗ expanded to noise: {expanded[:50]}")
            return None, ''
        if not is_amazon(expanded) and not is_flipkart_fam(expanded):
            print(f"    ✗ expanded to unknown: {expanded[:50]}")
            return None, ''
        print(f"    ↗ {url[:40]} → {expanded[:55]}")
        url = expanded

    if is_ignorable(url) or is_source_site(url):
        return None, ''

    if is_amazon(url):
        product_url = url
        aff   = make_amazon_affiliate(url)
        short = shorten(aff)
        print(f"    ✅ Amazon → {short[:55]}")
        return short, product_url

    if is_flipkart_fam(url):
        product_url = url
        aff = make_cuelinks_affiliate(url)
        if aff:
            short = shorten(aff)
            print(f"    ✅ Cuelinks → {short[:55]}")
            return short, product_url
        short = shorten(url)
        print(f"    ⚠️ Cuelinks failed, direct → {short[:55]}")
        return short, product_url

    print(f"    ✗ unrecognised store: {url[:50]}")
    return None, ''


# ─────────────────────────────────────────────────────────────────────────────
# PRICE UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

_PRICE_RE = re.compile(
    r'₹\s*[\d,]+'
    r'|Deal\s*Price\s*[:\-]?\s*[\d,]+\s*Rs'
    r'|Rs\.?\s*[\d,]+'
    r'|[\d,]+\s*₹'
    r'|(?:@|at)\s*(?:Rs\.?|₹)?\s*[\d,]+'
    r'|[\-–]\s*Rs\.?\s*[\d,]+',
    re.IGNORECASE
)

def has_price_in_text(text):
    return bool(_PRICE_RE.search(text or ''))

def extract_price_from_text(text):
    if not text:
        return ''
    m = re.search(r'₹\s*([\d,]+)', text)
    if m: return '₹' + m.group(1).replace(' ', '')
    m = re.search(r'Deal\s*Price\s*[:\-]?\s*([\d,]+)\s*Rs', text, re.I)
    if m: return '₹' + m.group(1)
    m = re.search(r'Rs\.?\s*([\d,]+)', text, re.I)
    if m: return '₹' + m.group(1)
    m = re.search(r'([\d,]+)\s*₹', text)
    if m: return '₹' + m.group(1)
    m = re.search(r'(?:@|at)\s*(?:Rs\.?|₹)?\s*([\d,]+)', text, re.I)
    if m: return '₹' + m.group(1).replace(',', '')
    m = re.search(r'[\-–]\s*Rs\.?\s*([\d,]+)', text, re.I)
    if m: return '₹' + m.group(1)
    return ''


# ─────────────────────────────────────────────────────────────────────────────
# MICROLINK UTILITY
# ─────────────────────────────────────────────────────────────────────────────

def microlink_fetch(url):
    """
    Call microlink.io once → returns (image_url, price_str, title_str).
    Free tier: 100 req/day, no API key needed.
    Not blocked by GitHub Actions (unlike Amazon CDN).
    """
    if not url:
        return '', '', ''
    try:
        r = requests.get(
            'https://api.microlink.io',
            params={'url': url, 'meta': 'false'},
            timeout=14,
        )
        if r.status_code != 200:
            print(f"    🔍 microlink failed ({r.status_code})")
            return '', '', ''

        data    = r.json().get('data') or {}
        img     = data.get('image') or {}
        img_url = img.get('url', '') if isinstance(img, dict) else ''
        if img_url and not img_url.startswith('http'):
            img_url = ''

        desc  = data.get('description', '') or ''
        title = data.get('title', '') or ''
        price = extract_price_from_text(title + ' ' + desc)

        if img_url:  print(f"    🔍 microlink img: {img_url[:70]}")
        if price:    print(f"    💰 microlink price: {price}")

        return img_url, price, title

    except Exception as e:
        print(f"    🔍 microlink error: {e}")
        return '', '', ''


# ─────────────────────────────────────────────────────────────────────────────
# IMAGE UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

def upload_to_imgbb(image_source):
    """Upload image (bytes or URL) to imgbb. Returns permanent URL or fallback."""
    if not IMGBB_KEY:
        return image_source if isinstance(image_source, str) else ''
    if not image_source:
        return ''
    try:
        if isinstance(image_source, bytes):
            payload = {"image": base64.b64encode(image_source).decode("utf-8")}
        else:
            payload = {"image": image_source}
        r = requests.post(
            "https://api.imgbb.com/1/upload",
            params={"key": IMGBB_KEY},
            data=payload,
            timeout=20,
        )
        if r.status_code == 200:
            data = r.json()
            if data.get("success"):
                hosted = data["data"]["url"]
                print(f"    🖼️ imgbb: {hosted[:70]}")
                return hosted
        print(f"    🖼️ imgbb failed ({r.status_code}): {r.text[:80]}")
    except Exception as e:
        print(f"    🖼️ imgbb error: {e}")
    return image_source if isinstance(image_source, str) else ''


async def get_telethon_photo_bytes(tg_client, msg):
    """Download photo bytes via Telethon. Returns (bytes, telegraph_url)."""
    if not getattr(msg, 'photo', None):
        return None, ''
    try:
        data = await tg_client.download_media(msg.photo, bytes)
        if not data or len(data) < 3000:
            print("    📷 photo too small")
            return None, ''
        print(f"    📷 {len(data)//1024}KB via Telethon ✅")

        telegraph_url = ''
        try:
            r = requests.post(
                'https://telegra.ph/upload',
                files={'file': ('p.jpg', io.BytesIO(data), 'image/jpeg')},
                timeout=12,
            )
            if r.status_code == 200:
                res = r.json()
                if isinstance(res, list) and res:
                    telegraph_url = f"https://telegra.ph{res[0]['src']}"
                    print(f"    📷 Telegraph URL: {telegraph_url}")
        except Exception as te:
            print(f"    📷 Telegraph skipped: {te}")

        return data, telegraph_url
    except Exception as e:
        print(f"    📷 Telethon failed: {e}")
    return None, ''


# ─────────────────────────────────────────────────────────────────────────────
# MESSAGE TEXT BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def strip_html(text):
    return re.sub(r'<[^>]+>', ' ', text) if ('<' in (text or '') and '>' in (text or '')) else text

def sanitize_text(text, affiliate_url):
    """
    Remove all junk URLs AND raw product URLs (with foreign affiliate tags)
    from text, but keep our own affiliate_url intact.
    """
    for url in extract_text_urls(text):
        if url == affiliate_url:
            continue
        if is_junk_url(url) or is_foreign_product_url(url):
            text = text.replace(url, '')
    return text

def build_clean_text(msg, affiliate_url):
    """
    Build clean post text from a Telegram message.
    - Strips HTML, noise prefixes, hashtags, @mentions
    - Removes all junk/shortener/source-site URLs from lines
    - Removes lines that contain ONLY foreign product URLs
      (e.g. raw amazon.in links with someone else's tag)
    - Keeps product title, price, description lines
    - Appends our affiliate_url at the end
    """
    raw = msg.text or msg.message or ''
    if '<' in raw and '>' in raw:
        raw = strip_html(raw)
        raw = re.sub(r'\s+', ' ', raw).strip()

    lines = raw.split('\n')
    clean = []

    for line in lines:
        s  = line.strip()
        sl = s.lower()

        # Keep empty lines as spacers
        if not s:
            clean.append('')
            continue

        # Drop noise prefix lines
        if any(sl.startswith(p) for p in NOISE_PREFIXES):
            continue

        # Drop hashtag-only lines
        if re.match(r'^#\w', s):
            continue

        # Drop @mention-only lines
        if re.match(r'^@\w', s):
            continue

        # Strip junk URLs and foreign product URLs inline from this line
        line_cleaned = line
        for u in extract_text_urls(s):
            if is_junk_url(u) or is_foreign_product_url(u):
                line_cleaned = line_cleaned.replace(u, '')

        s_cleaned = line_cleaned.strip()

        # Drop line if nothing meaningful left after URL removal
        if not s_cleaned:
            continue

        # Drop line if it contained ONLY URLs that are all junk/foreign
        line_urls = extract_text_urls(s)
        if line_urls and all(
            is_junk_url(u) or is_foreign_product_url(u)
            for u in line_urls
        ):
            continue

        clean.append(line_cleaned)

    result = re.sub(r'\n{3,}', '\n\n', '\n'.join(clean)).strip()

    # Final sanitize pass — catches anything missed above
    result = sanitize_text(result, affiliate_url)

    # Drop orphan label lines like "Buy Here:" with nothing after
    final = []
    for line in result.split('\n'):
        s = line.strip()
        if re.match(r'^[\w\s]{2,20}:\s*$', s) and not extract_text_urls(s):
            continue
        final.append(line)

    result = re.sub(r'\n{3,}', '\n\n', '\n'.join(final)).strip()

    # Append our affiliate link
    if affiliate_url:
        result += f"\n\n🔗 {affiliate_url}"

    return result


# ─────────────────────────────────────────────────────────────────────────────
# PRE-POST CHECKLIST
# ─────────────────────────────────────────────────────────────────────────────

class CheckResult:
    def __init__(self):
        self.fixed  = []
        self.failed = []
    def fix(self, name, detail=""):
        self.fixed.append(name + (f": {detail}" if detail else ""))
    def fail(self, name, detail=""):
        self.failed.append(name + (f": {detail}" if detail else ""))
    @property
    def is_good(self): return len(self.failed) == 0
    def summary(self):
        lines = [f"  🔧 {f}" for f in self.fixed] + [f"  ❌ {f}" for f in self.failed]
        return '\n'.join(lines) if lines else "  ✅ all checks passed"

def run_checklist(text, affiliate_url):
    r = CheckResult()

    # 1. Strip HTML
    if re.search(r'<[a-zA-Z][^>]*>', text):
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        r.fix("HTML stripped")

    # 2. Remove source site URLs
    leaks = [u for u in extract_text_urls(text) if is_source_site(u)]
    if leaks:
        for u in leaks: text = text.replace(u, '')
        r.fix("source URLs removed", str(len(leaks)))

    # 3. Remove ALL foreign URLs — shorteners, raw Amazon/Flipkart with other tags
    #    Keep only our own affiliate_url
    foreign = [
        u for u in extract_text_urls(text)
        if u != affiliate_url and (
            is_shortener(u) or
            is_foreign_product_url(u)
        )
    ]
    if foreign:
        for u in foreign: text = text.replace(u, '')
        r.fix("foreign URLs removed", str(len(foreign)))

    # 4. Must have affiliate link
    if not affiliate_url:
        r.fail("no affiliate URL"); return text, affiliate_url, r

    # 5. TinyURL homepage check
    if affiliate_url.rstrip('/') == 'https://tinyurl.com':
        r.fail("TinyURL homepage"); return text, affiliate_url, r

    # 6. Affiliate link in text
    if affiliate_url not in text:
        text += f"\n\n🔗 {affiliate_url}"
        r.fix("affiliate URL appended")

    # 7. Orphan label lines
    fixed_lines, removed = [], 0
    for line in text.split('\n'):
        s = line.strip()
        if re.match(r'^[\w\s]{2,20}:\s*$', s) and not extract_text_urls(s):
            removed += 1; continue
        fixed_lines.append(line)
    if removed:
        text = re.sub(r'\n{3,}', '\n\n', '\n'.join(fixed_lines)).strip()
        r.fix("orphan labels", str(removed))

    # 8. Must have content
    meaningful = re.sub(r'https?://\S+', '', text)
    meaningful = re.sub(r'[🔗🛒\s]', '', meaningful)
    if len(meaningful) < 10:
        r.fail("empty content"); return text, affiliate_url, r

    # 9. Length cap
    if len(text) > 4096:
        text = text[:3900] + f"\n\n🔗 {affiliate_url}"
        r.fix("trimmed")

    # 10. Product title check
    words = [w for w in text.split('🔗')[0].split() if len(w) > 2]
    if len(words) < 2:
        r.fail("no product title", f"{len(words)} words"); return text, affiliate_url, r

    return text, affiliate_url, r

def groq_quality_check(text, affiliate_url):
    """Advisory only — never blocks posting."""
    if not GROQ_API_KEY: return True, "no key"
    snippet = text.split('🔗')[0].strip()[:400]
    prompt = (
        f'Check this deal post. Reply ONLY with JSON {{"ok":true/false,"reason":"one line"}}.\n'
        f'Reject if: contains raw HTML tags or raw source site URLs (desidime.com/dealsmagnet.com).\n'
        f'Do NOT reject for: channel names, product names, tinyurl links.\n\nPost:\n{snippet}'
    )
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 60,
                "temperature": 0,
            },
            timeout=8,
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

def _extract_tg_file_url(resp_json):
    try:
        if resp_json.get('ok'):
            file_id = resp_json['result']['photo'][-1]['file_id']
            fp = requests.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
                params={'file_id': file_id}, timeout=8,
            ).json()
            file_path = fp.get('result', {}).get('file_path', '')
            if file_path:
                url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
                print(f"    📷 Telegram URL: {url[:70]}")
                return url
    except Exception as e:
        print(f"    📷 could not extract Telegram URL: {e}")
    return ''

def post_photo_bytes(chat_id, img_bytes, caption, ctype='image/jpeg'):
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
            print(f"    sendPhoto(bytes) error: {r.text[:120]}")
            return False, ''
        return True, _extract_tg_file_url(r.json())
    except Exception as e:
        print(f"    sendPhoto(bytes) exception: {e}")
        return False, ''

def post_photo_url(chat_id, image_url, caption):
    try:
        clean_cap = re.sub(r'<[^>]+>', '', caption)[:1024]
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
            json={'chat_id': chat_id, 'photo': image_url, 'caption': clean_cap},
            timeout=20,
        )
        if r.status_code != 200:
            print(f"    sendPhoto(url) error: {r.text[:120]}")
        return r.status_code == 200, _extract_tg_file_url(r.json())
    except Exception as e:
        print(f"    sendPhoto(url) exception: {e}")
        return False, ''

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
        return r.status_code == 200, ''
    except Exception as e:
        print(f"    sendMessage exception: {e}")
        return False, ''


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

def add_deal(deals, text, url, source, image_url, product_url=''):
    deals.insert(0, {
        'text':        text,
        'url':         url or '',
        'product_url': product_url or '',
        'source':      source,
        'image':       image_url or '',
        'timestamp':   datetime.now(timezone.utc).isoformat(),
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

    print(f"v{VERSION} | @{YOUR_CHANNEL} | tag={AMAZON_TAG} | "
          f"cuelinks={'on' if CUELINKS_KEY else 'off'} | "
          f"groq={'on' if GROQ_API_KEY else 'off'} | "
          f"imgbb={'on' if IMGBB_KEY else 'OFF ⚠️'} | "
          f"microlink=on")
    print(f"Sources: {SOURCE_CHANNELS}")
    print(f"Loaded {len(posted_hashes)} seen hashes")

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
            limit = 5 if last_id == 0 else 50

            print(f"\n{'─'*55}")
            print(f"  {channel}  last_id={last_id}  limit={limit}")

            try:
                latest = await client.get_messages(channel, limit=1)
                if latest:
                    latest_id = latest[0].id
                    new_avail = max(0, latest_id - last_id)
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
                        print("    skip: no content"); continue

                    msg_hash = hashlib.md5(raw_text[:200].encode()).hexdigest()[:10]
                    if msg_hash in posted_hashes:
                        print("    skip: duplicate"); continue

                    # 1. Extract URLs
                    all_urls = extract_all_urls_from_msg(msg)
                    print(f"    urls: {all_urls}")

                    # 2. Resolve affiliate URL + product page URL
                    affiliate_url = None
                    product_url   = ''
                    for url in all_urls:
                        if is_ignorable(url): continue
                        aff, prod = resolve_to_affiliate(url)
                        if aff:
                            affiliate_url = aff
                            product_url   = prod
                            break

                    print(f"    affiliate: {affiliate_url or 'NONE'}")
                    if not affiliate_url:
                        print("    skip: no affiliate URL"); continue

                    # 3. Build clean text (strips all foreign URLs)
                    clean = build_clean_text(msg, affiliate_url)

                    # 4. Fetch image + price from microlink in one call
                    microlink_img   = ''
                    microlink_price = ''
                    if product_url:
                        microlink_img, microlink_price, _ = microlink_fetch(product_url)

                    # 5. Inject price if message has none
                    if microlink_price and not has_price_in_text(clean):
                        lines = clean.split('\n')
                        lines.insert(1, microlink_price)
                        clean = '\n'.join(lines)
                        print(f"    💰 price injected: {microlink_price}")

                    # 6. Checklist
                    clean, affiliate_url, chk = run_checklist(clean, affiliate_url)
                    print(chk.summary())
                    if not chk.is_good:
                        print("    ✗ checklist FAILED — skipping"); continue

                    # 7. Groq advisory
                    if GROQ_API_KEY:
                        ai_ok, ai_reason = groq_quality_check(clean, affiliate_url)
                        print(f"    🤖 Groq: {'OK' if ai_ok else 'warn'} — {ai_reason}")

                    final_text = clean + f"\n\n🛒 Deals by @{YOUR_CHANNEL}"

                    # ── IMAGE STRATEGY ─────────────────────────────────────
                    img_bytes    = None
                    img_saved    = microlink_img
                    real_img_url = microlink_img

                    if has_photo:
                        img_bytes, _ = await get_telethon_photo_bytes(client, msg)
                        if not img_bytes:
                            print("    📷 Telethon failed — using microlink img if available")

                    print(f"    img: bytes={'yes '+str(len(img_bytes)//1024)+'KB' if img_bytes else 'no'}  "
                          f"microlink={real_img_url[:50] if real_img_url else 'none'}")

                    # Post to Telegram
                    ok_post = False
                    tg_url  = ''

                    if img_bytes:
                        ok_post, tg_url = post_photo_bytes(chat_id, img_bytes, final_text)
                        if not ok_post:
                            print("    sendPhoto(bytes) failed, trying text")
                            ok_post, _ = post_text(chat_id, final_text)
                    elif real_img_url:
                        ok_post, tg_url = post_photo_url(chat_id, real_img_url, final_text)
                        if not ok_post:
                            print("    sendPhoto(url) failed, trying text")
                            ok_post, _ = post_text(chat_id, final_text)
                    else:
                        ok_post, _ = post_text(chat_id, final_text)

                    # Permanent image for website
                    if img_bytes:
                        img_saved = upload_to_imgbb(img_bytes)
                        print(f"    📷 website: imgbb from bytes ✅")
                    elif tg_url:
                        img_saved = upload_to_imgbb(tg_url)
                        print(f"    📷 website: imgbb from tg_url ✅")
                    elif img_saved:
                        img_saved = upload_to_imgbb(img_saved)
                        print(f"    📷 website: imgbb from microlink ✅ {img_saved[:55]}")
                    else:
                        print(f"    📷 website: no image — browser ASIN CDN fallback")

                    if ok_post:
                        mode = '📷 bytes' if img_bytes else ('📷 url' if real_img_url else '📝 text')
                        print(f"    ✅ POSTED {mode}")
                        posted_hashes.add(msg_hash)
                        deals = add_deal(
                            deals, final_text, affiliate_url or '',
                            channel, img_saved, product_url
                        )
                        found += 1
                        total += 1
                        time.sleep(random.uniform(2.0, 4.0))
                    else:
                        print(f"    ❌ FAILED")

                print(f"\n  scanned={count}  posted={found}")

            except Exception as e:
                print(f"  ❌ {channel}: {e}")
                traceback.print_exc()

            state[channel] = new_last_id

    # ── Web Feed Sources (RSS) ────────────────────────────────────────────────
    if WEB_FEEDS_ENABLED:
        print(f"\n{'─'*55}")
        print("  [WEB FEEDS] Fetching RSS sources...")
        try:
            feed_deals = fetch_web_deals(posted_hashes, max_per_source=8)
            for feed_msg, feed_source, feed_img, feed_product_url in feed_deals:

                all_urls = extract_all_urls_from_msg(feed_msg)
                if feed_product_url and feed_product_url not in all_urls:
                    all_urls.insert(0, feed_product_url)

                affiliate_url = None
                product_url   = ''
                for url in all_urls:
                    if is_ignorable(url): continue
                    aff, prod = resolve_to_affiliate(url)
                    if aff:
                        affiliate_url = aff
                        product_url   = prod
                        break

                if not affiliate_url:
                    print(f"    [feed] skip {feed_source}: no affiliate URL")
                    continue

                clean = build_clean_text(feed_msg, affiliate_url)

                feed_img_ml = feed_img or ''
                if product_url:
                    ml_img, ml_price, _ = microlink_fetch(product_url)
                    if ml_img and not feed_img_ml:
                        feed_img_ml = ml_img
                    if ml_price and not has_price_in_text(clean):
                        lines = clean.split('\n')
                        lines.insert(1, ml_price)
                        clean = '\n'.join(lines)

                clean, affiliate_url, chk = run_checklist(clean, affiliate_url)
                if not chk.is_good:
                    print(f"    [feed] skip {feed_source}: {chk.failed[0]}")
                    continue

                if GROQ_API_KEY:
                    ai_ok, ai_reason = groq_quality_check(clean, affiliate_url)
                    print(f"    [feed] Groq: {ai_reason}")

                final_text   = clean + f"\n\n🛒 Deals by @{YOUR_CHANNEL}"
                real_img_url = feed_img_ml
                img_saved    = feed_img_ml

                ok_post = False
                resp    = ''
                if real_img_url:
                    ok_post, resp = post_photo_url(chat_id, real_img_url, final_text)
                    if not ok_post:
                        ok_post, resp = post_text(chat_id, final_text)
                else:
                    ok_post, resp = post_text(chat_id, final_text)

                if ok_post and img_saved:
                    img_saved = upload_to_imgbb(img_saved)

                if ok_post:
                    msg_hash = hashlib.md5(feed_msg.text[:200].encode()).hexdigest()[:10]
                    posted_hashes.add(msg_hash)
                    deals = add_deal(
                        deals, final_text, affiliate_url,
                        feed_source, img_saved, product_url
                    )
                    total += 1
                    print(f"    ✅ [feed] POSTED from {feed_source} | img={img_saved[:40] if img_saved else 'none'}")
                    time.sleep(random.uniform(2.0, 3.5))
                else:
                    print(f"    ❌ [feed] FAILED from {feed_source}: {resp[:80]}")

        except Exception as e:
            print(f"  ❌ web feeds error: {e}")
            traceback.print_exc()

    save_json(STATE_FILE, state)
    save_json(DEALS_FILE, deals)
    save_json(HASHES_FILE, list(posted_hashes)[-MAX_HASHES:])
    print(f"\n{'='*55}")
    print(f"v{VERSION} done: {total} posted | {len(deals)} on website")

if __name__ == '__main__':
    asyncio.run(run())
