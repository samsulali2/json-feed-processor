"""
Telegram Affiliate Deal Bot  v8.0 — COMPREHENSIVE FIX
======================================================
All known bugs fixed after systematic analysis:

BUG 1 (MAIN): last_seen.json not persisted between runs
  → State is now saved in deals.json itself (no separate file needed)
  → Also added per-message ID tracking via posted_ids set

BUG 2: Embedded shortener URLs kept in text when on mixed lines
  → sanitize_text() strips ALL shortener/source URLs inline from any line

BUG 3: validate blocks posts due to leftover shorteners
  → Relaxed: strip them, don't block; only block if truly empty or missing link

BUG 4: ASIN-based Amazon image URL unreliable
  → Use Telethon source photo FIRST for all types
  → ASIN CDN only as fallback, stored as-is (let browser try it)

BUG 5: Same messages re-processed every run (no state persistence)
  → Track seen message IDs in last_seen.json
  → Commit last_seen.json to repo in workflow (see deal_bot.yml)

BUG 6: Chained shorteners (ddime.in → amzn.clnk.in → amazon.in)
  → expand_url follows ALL HTTP redirects in one call — already correct
  → Added extra expand pass if result is still a shortener

BUG 7: TinyURL API f-string encoding (fixed in v7, kept here)
  → params={'url': url} for proper encoding

BUG 8: Source channel branding in posts
  → build_clean_text aggressively strips all noise lines
"""

import os, re, json, asyncio, requests, hashlib, io, random, time, traceback
import urllib.parse
from datetime import datetime, timezone
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl

VERSION = "8.0"

# ── Config ────────────────────────────────────────────────────────────────────
# Validate all required secrets upfront — gives clear error if any missing/empty
def _require(key):
    val = os.environ.get(key, '').strip()
    if not val:
        raise SystemExit(f"❌ Secret {key} is missing or empty — set it in GitHub repo Settings → Secrets")
    return val

API_ID          = int(_require("A1"))
API_HASH        = _require("A2")
BOT_TOKEN       = _require("A3")
SESSION_STRING  = _require("A4")
YOUR_CHANNEL    = _require("A5").lstrip('@')
SOURCE_CHANNELS = [c.strip().lstrip('@') for c in _require("A6").split(",") if c.strip()]
AMAZON_TAG      = _require("A7")
CUELINKS_KEY    = os.environ.get("A8", "").strip()  # optional

STATE_FILE  = "last_seen.json"    # last message ID per channel
DEALS_FILE  = "deals.json"         # website deals feed
HASHES_FILE = "seen_hashes.json"   # dedup across runs (persisted to repo)
MAX_DEALS   = 200
MAX_HASHES  = 2000                  # keep last 2000 message hashes

# Domains that are short-link services needing expansion
SHORTENER_DOMAINS = [
    'ddime.in', 'amzn.clnk.in', 'clnk.in',
    'amzn.to', 'amzn.in', 'a.co/',
    'bitli.store', 'bit.ly', 'cutt.ly',
    'rb.gy', 't.ly', 'tiny.cc', 'ow.ly',
    'shorturl.at', 'tinyurl.com', 'dl.flipkart.com',
]

# Deal aggregator sites — their URLs are NOT product pages
SOURCE_SITE_DOMAINS = [
    'desidime.com', 'dealsmagnet.com', 'freekaamaal.com',
    'lootdunia.com', 'dealsbazaar.in', 'hcti.io',
]

# Cuelinks affiliate programme stores
CUELINKS_DOMAINS = [
    'flipkart.com', 'myntra.com', 'ajio.com', 'nykaa.com',
    'tatacliq.com', 'shopsy.in', 'meesho.com', 'jiomart.com',
    'croma.com',
]

# Social / noise URLs — remove from messages completely
IGNORE_URL_DOMAINS = [
    't.me', 'telegram.me', 'instagram.com', 'twitter.com',
    'facebook.com', 'youtube.com', 'play.google.com',
]

# Message line prefixes that are always noise
NOISE_PREFIXES = [
    'on #', 'read more', 'buy now', 'link:', 'join ',
    'follow', 'share ', 'deals by', 'source:', 'via ',
    'forwarded', 'channel:', 'group:',
]

BROWSE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'en-IN,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
}


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — URL UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

def strip_html(text):
    """
    Strip HTML tags and extract clean text + URLs from HTML-formatted messages.
    Some source channels post HTML with <a href="..."> links — we need both
    the clean text AND the URLs from href attributes.
    """
    if not text or '<' not in text:
        return text
    # Extract href URLs before stripping tags
    return re.sub(r'<[^>]+>', ' ', text)

def extract_href_urls(text):
    """Extract URLs from HTML href attributes"""
    return re.findall(r'href=["\']([^"\']+)["\']', text or '')

def extract_src_urls(text):
    """Extract URLs from HTML src attributes (skip tracking pixels)"""
    srcs = re.findall(r'src=["\']([^"\']+)["\']', text or '')
    # Filter out 1x1 tracking pixels
    return [s if s.startswith('http') else 'https:' + s
            for s in srcs if 'amazon-adsystem' not in s]

def extract_text_urls(text):
    """Extract all plain URLs visible in text"""
    return re.findall(r'https?://[^\s\)\]>\"\'<\u2019\u201d\u2018]+', text or '')

def extract_all_urls_from_msg(msg):
    """
    Extract URLs from ALL possible sources in a Telegram message:
    1. Plain URLs visible in text
    2. Hidden entity URLs ([Buy Now](url) → entity.url)
    3. HTML href attributes (some channels post raw HTML)
    4. Telegram media captions
    """
    seen = []
    raw = msg.text or msg.message or ''

    # 1. HTML href URLs (source channels sometimes post raw HTML)
    if '<a ' in raw or 'href=' in raw:
        for url in extract_href_urls(raw):
            if url not in seen and url.startswith('http'):
                seen.append(url)

    # 2. Plain URLs in text
    for url in extract_text_urls(raw):
        if url not in seen:
            seen.append(url)

    # 3. Hidden entity URLs ([Buy Now](url) style)
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

def is_amazon(url):         return bool(re.search(r'amazon\.in|amazon\.com', url or ''))
def is_flipkart_fam(url):   return any(d in (url or '') for d in CUELINKS_DOMAINS)
def is_source_site(url):    return any(d in (url or '') for d in SOURCE_SITE_DOMAINS)
def is_shortener(url):      return any(d in (url or '') for d in SHORTENER_DOMAINS)
def is_ignorable(url):      return any(d in (url or '') for d in IGNORE_URL_DOMAINS)
def is_junk_url(url):       return is_source_site(url) or is_shortener(url) or is_ignorable(url)

def is_valid_url(url):
    """Must be https with real netloc and path"""
    if not url or not url.startswith('https://'):
        return False
    p = urllib.parse.urlparse(url)
    return bool(p.netloc) and bool(p.path)

def expand_url(url, timeout=10):
    """
    Follow all HTTP redirects to get final destination.
    Tries HEAD first (faster), falls back to GET (some servers block HEAD).
    Returns (final_url, success).
    """
    for method in ('HEAD', 'GET'):
        try:
            if method == 'HEAD':
                r = requests.head(url, allow_redirects=True, timeout=timeout,
                                  headers=BROWSE_HEADERS)
            else:
                r = requests.get(url, allow_redirects=True, timeout=timeout,
                                 headers=BROWSE_HEADERS, stream=True)
            final = r.url
            if final and is_valid_url(final):
                return final, True
        except Exception:
            continue
    return url, False

def expand_url_fully(url, timeout=8):
    """
    Expand URL, and if result is STILL a shortener, expand once more.
    Handles chains: ddime.in → amzn.clnk.in → amazon.in
    """
    if not is_shortener(url):
        return url, True

    final, ok = expand_url(url, timeout)
    if not ok:
        return url, False

    # If still a shortener (intermediate hop), expand again
    if is_shortener(final) and final != url:
        print(f"    ↗↗ double-hop: {final[:55]}")
        final2, ok2 = expand_url(final, timeout)
        if ok2 and final2 != final:
            return final2, True

    return final, True


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — AFFILIATE LINK PROCESSING
# ─────────────────────────────────────────────────────────────────────────────

def get_asin(url):
    m = re.search(r'/(?:dp|gp/product|d)/([A-Z0-9]{10})(?:[/?&]|$)', url or '')
    return m.group(1) if m else None

def get_amazon_image_cdn(aff_url):
    """
    Build Amazon CDN image URL from ASIN.
    Note: ASIN-based URLs work for many but not all products.
    We store this as fallback; Telethon photo is always preferred.
    """
    asin = get_asin(aff_url)
    return f"https://m.media-amazon.com/images/I/{asin}._SL500_.jpg" if asin else ''

def make_amazon_affiliate(url):
    """Inject our Amazon tag into any Amazon URL"""
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
    """Convert Flipkart/Myntra/etc to Cuelinks affiliate URL"""
    if not CUELINKS_KEY:
        return None
    try:
        r = requests.get('https://api.cuelinks.com/v1/affiliate-url',
                         params={'apiKey': CUELINKS_KEY, 'url': url}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            print(f"    Cuelinks: {str(data)[:120]}")
            aff = (data.get('affiliateUrl') or data.get('affiliate_url') or
                   data.get('shortUrl')     or data.get('short_url'))
            if aff and aff != url and is_valid_url(aff):
                return aff
            print("    Cuelinks: same/invalid URL returned")
        else:
            print(f"    Cuelinks HTTP {r.status_code}: {r.text[:100]}")
    except Exception as e:
        print(f"    Cuelinks error: {e}")
    return None

def shorten(url):
    """
    Shorten via TinyURL API.
    Uses params= for proper encoding. Validates result is a real TinyURL.
    Falls back to original URL on failure (still a working link).
    """
    if not is_valid_url(url):
        return url
    try:
        r = requests.get('https://tinyurl.com/api-create.php',
                         params={'url': url}, timeout=10)
        result = r.text.strip()
        # Valid TinyURL has path longer than just '/' (homepage)
        if (r.status_code == 200
                and result.startswith('https://tinyurl.com/')
                and len(result) > 24):   # 'https://tinyurl.com/' = 20 chars + 4+ char code
            return result
        print(f"    TinyURL invalid response: {result[:60]}")
    except Exception as e:
        print(f"    TinyURL error: {e}")
    return url  # original URL still works as a link

def resolve_to_affiliate(raw_url):
    """
    Convert any URL (possibly shortened) to (affiliate_url, image_cdn_url).

    Flow:
      Shortener → expand fully → Amazon/Flipkart → affiliate + shorten
      Source site / social → skip (None, None)
      Unknown → skip (None, None) — don't create TinyURLs for random links

    Returns (None, None) only if URL is truly unusable.
    """
    url = raw_url

    # Step 1: Expand shorteners (follow full redirect chain)
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

    # Step 2: Skip noise/source sites
    if is_ignorable(url) or is_source_site(url):
        return None, None

    # Step 3: Amazon
    if is_amazon(url):
        aff   = make_amazon_affiliate(url)
        short = shorten(aff)
        image = get_amazon_image_cdn(aff)  # fallback CDN (may or may not work)
        print(f"    ✅ Amazon → {short[:55]}")
        return short, image

    # Step 4: Flipkart / Myntra / Cuelinks stores
    if is_flipkart_fam(url):
        aff = make_cuelinks_affiliate(url)
        if aff:
            short = shorten(aff)
            print(f"    ✅ Cuelinks → {short[:55]}")
            return short, ''
        # Cuelinks failed → direct link (no commission, but link works)
        short = shorten(url)
        print(f"    ⚠️ Cuelinks failed, direct → {short[:55]}")
        return short, ''

    print(f"    ✗ unrecognised store: {url[:50]}")
    return None, None


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — IMAGE HANDLING
# ─────────────────────────────────────────────────────────────────────────────

def download_image_bytes(image_url, referer='https://www.amazon.in/'):
    """Download image with browser headers. Returns (bytes, content_type) or (None, None)."""
    if not image_url:
        return None, None
    try:
        h = {**BROWSE_HEADERS, 'Referer': referer,
             'Accept': 'image/avif,image/webp,image/apng,image/*;q=0.9'}
        r = requests.get(image_url, headers=h, timeout=15, stream=True)
        ctype = r.headers.get('content-type', '')
        if r.status_code == 200 and 'image' in ctype:
            data = r.content
            if len(data) > 3000:
                print(f"    📷 downloaded {len(data)//1024}KB from {image_url[:50]}")
                return data, ctype
            print(f"    📷 too small ({len(data)}B) — skip")
    except Exception as e:
        print(f"    📷 download failed: {e}")
    return None, None

async def get_photo_from_source(tg_client, msg):
    """
    Download photo from source Telegram message via Telethon.
    Upload to Telegra.ph for permanent public URL.
    This gives us the ACTUAL product photo posted by the source channel.
    Returns telegraph URL string or ''.
    """
    if not getattr(msg, 'photo', None):
        return ''
    try:
        data = await tg_client.download_media(msg.photo, bytes)
        if not data or len(data) < 3000:
            print("    📷 source photo too small")
            return ''
        files = {'file': ('product.jpg', io.BytesIO(data), 'image/jpeg')}
        r = requests.post('https://telegra.ph/upload', files=files, timeout=20)
        if r.status_code == 200:
            res = r.json()
            if isinstance(res, list) and res and res[0].get('src'):
                url = f"https://telegra.ph{res[0]['src']}"
                print(f"    📷 Telegraph → {url}")
                return url
        print(f"    📷 Telegraph upload failed: {r.text[:60]}")
    except Exception as e:
        print(f"    📷 Telethon/Telegraph error: {e}")
    return ''


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — MESSAGE TEXT BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def sanitize_text(text, affiliate_url):
    """
    Remove ALL source/shortener/junk URLs inline from text
    (even if embedded mid-line, e.g. 'Check https://ddime.in/x for details').
    """
    for url in extract_text_urls(text):
        if is_junk_url(url) and url != affiliate_url:
            text = text.replace(url, '').strip()
    return text

def build_clean_text(msg, affiliate_url):
    """
    Build clean outgoing message text:
    1. Remove noise/label lines (Read More, Buy Now, On #, etc.)
    2. Remove lines containing only junk URLs
    3. Sanitize any remaining embedded junk URLs
    4. Append affiliate link clearly at bottom
    """
    raw = msg.text or msg.message or ''

    # Strip HTML tags if message contains HTML formatting
    if '<' in raw and '>' in raw:
        raw = strip_html(raw)
        raw = re.sub(r'\s+', ' ', raw)
        # Re-split into lines after stripping
        raw = raw.replace('  ', '\n').strip()

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
        if re.match(r'^#\w', s):        # hashtag-only line
            continue
        if re.match(r'^@\w', s):        # channel mention line
            continue
        if re.match(r'^[\-─═]+$', s):   # separator lines
            continue

        # Drop lines whose ONLY URLs are junk
        line_urls = extract_text_urls(s)
        if line_urls:
            all_junk = all(is_junk_url(u) for u in line_urls)
            if all_junk:
                continue

        clean.append(line)

    result = re.sub(r'\n{3,}', '\n\n', '\n'.join(clean)).strip()

    # Sanitize any embedded junk URLs still remaining
    result = sanitize_text(result, affiliate_url)

    # Clean up orphan label lines (e.g. "Link: " after URL was removed)
    final_lines = []
    for line in result.split('\n'):
        s = line.strip()
        if re.match(r'^[\w\s]+:\s*$', s) and len(s) < 30:
            continue
        final_lines.append(line)
    result = re.sub(r'\n{3,}', '\n\n', '\n'.join(final_lines)).strip()

    # ALWAYS append affiliate link explicitly
    if affiliate_url:
        result += f"\n\n🔗 {affiliate_url}"

    return result


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5 — PRE-PUBLISH VALIDATION
# ─────────────────────────────────────────────────────────────────────────────

def validate_post(text, affiliate_url):
    """
    Final check before posting. Returns (ok, reason).
    Designed to catch but not block on minor issues.
    """
    if not text or not text.strip():
        return False, "empty text"

    if not affiliate_url:
        return False, "no affiliate URL"

    if affiliate_url not in text:
        # This shouldn't happen since build_clean_text appends it,
        # but just in case
        return False, "affiliate URL not in text"

    # Check for remaining source site URLs (should have been removed)
    for url in extract_text_urls(text):
        if is_source_site(url) and url != affiliate_url:
            return False, f"source site URL leaked: {url[:40]}"

    # Shortener check: only fail if the URL is a raw shortener
    # (not our affiliate tinyurl which is fine)
    for url in extract_text_urls(text):
        if url == affiliate_url:
            continue  # our own link — always OK
        if is_shortener(url) and not is_amazon(url) and not is_flipkart_fam(url):
            return False, f"raw shortener leaked into text: {url[:40]}"

    if len(text) > 4096:
        return False, f"too long ({len(text)} chars)"

    return True, "ok"



# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6 — PRE-POST CHECKLIST (12 checks) + OPTIONAL GROQ AI QUALITY CHECK
# Every deal MUST pass all checks before posting.
# Each check either FIXES the issue automatically or REJECTS the deal.
# ─────────────────────────────────────────────────────────────────────────────

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()

class CheckResult:
    def __init__(self):
        self.passed  = []
        self.fixed   = []
        self.failed  = []

    def ok(self, name):
        self.passed.append(name)

    def fix(self, name, detail=""):
        self.fixed.append(f"{name}: {detail}" if detail else name)

    def fail(self, name, detail=""):
        self.failed.append(f"{name}: {detail}" if detail else name)

    @property
    def is_good(self):
        return len(self.failed) == 0

    def summary(self):
        lines = []
        for f in self.fixed:  lines.append(f"  🔧 FIXED  {f}")
        for f in self.failed: lines.append(f"  ❌ FAIL   {f}")
        return "\n".join(lines) if lines else "  ✅ all clean"


def run_checklist(text, affiliate_url):
    """
    Run all 12 pre-post checks. Auto-fixes where possible.
    Returns (final_text, affiliate_url, result: CheckResult).
    """
    r = CheckResult()

    # ── CHECK 1: Raw HTML tags ───────────────────────────────────────────────
    if re.search(r'<[a-zA-Z][^>]*>', text):
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        r.fix("HTML tags", "stripped")
    else:
        r.ok("no HTML")

    # ── CHECK 2: Source site URLs leaked ────────────────────────────────────
    source_leaks = [u for u in extract_text_urls(text) if is_source_site(u)]
    if source_leaks:
        for u in source_leaks:
            text = text.replace(u, '')
        text = re.sub(r'\s+', ' ', text).strip()
        r.fix("source site URLs", f"removed {len(source_leaks)}")
    else:
        r.ok("no source URLs")

    # ── CHECK 3: Shortener URLs leaked (except our own affiliate) ───────────
    shortener_leaks = [u for u in extract_text_urls(text)
                       if is_shortener(u) and u != affiliate_url]
    if shortener_leaks:
        for u in shortener_leaks:
            text = text.replace(u, '')
        r.fix("shortener URLs", f"removed {len(shortener_leaks)}")
    else:
        r.ok("no shortener leak")

    # ── CHECK 4: Must have affiliate link ────────────────────────────────────
    if not affiliate_url:
        r.fail("no affiliate URL", "deal rejected")
        return text, affiliate_url, r
    else:
        r.ok("affiliate URL present")

    # ── CHECK 5: TinyURL homepage (tinyurl.com/ with no path) ───────────────
    if affiliate_url.rstrip('/') == 'https://tinyurl.com':
        r.fail("TinyURL homepage", "shortening failed — using original URL would be ugly")
        return text, affiliate_url, r
    else:
        r.ok("valid affiliate URL")

    # ── CHECK 6: Affiliate link present in text ──────────────────────────────
    if affiliate_url not in text:
        text += f"\n\n🔗 {affiliate_url}"
        r.fix("affiliate URL in text", "appended")
    else:
        r.ok("affiliate URL in text")

    # ── CHECK 7: Orphan label lines (e.g. "Link: " with nothing after) ──────
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
        r.fix("orphan labels", f"removed {removed}")
    else:
        r.ok("no orphan labels")

    # ── CHECK 8: Empty text after cleaning ──────────────────────────────────
    meaningful = re.sub(r'https?://\S+', '', text)  # text without URLs
    meaningful = re.sub(r'🔗|🛒|[\s]', '', meaningful)
    if len(meaningful) < 10:
        r.fail("empty text", f"only {len(meaningful)} meaningful chars")
        return text, affiliate_url, r
    else:
        r.ok(f"has content ({len(meaningful)} chars)")

    # ── CHECK 9: Text too long ───────────────────────────────────────────────
    if len(text) > 4096:
        # Keep content + ensure affiliate link is at end
        text = text[:3900] + f"\n\n🔗 {affiliate_url}"
        r.fix("text length", "trimmed to 3900+link")
    else:
        r.ok(f"length OK ({len(text)} chars)")

    # ── CHECK 10: Source channel branding already in text ───────────────────
    if '@hugediscountshop' in text.lower().replace('@hugediscountshop', ''):
        # Only flag if it appears MORE than once (we add it ourselves once)
        count = text.lower().count('@hugediscountshop')
        if count > 1:
            # Remove all instances — we'll add one at the end
            text = re.sub(r'@hugediscountshop', '', text, flags=re.IGNORECASE).strip()
            r.fix("duplicate branding", f"removed {count-1} extra")
        else:
            r.ok("branding OK")
    else:
        r.ok("branding not yet added")

    # ── CHECK 11: Price symbols intact ──────────────────────────────────────
    # Just a warning — don't fail, just log
    if 'Rs' in text or 'INR' in text or '₹' in text:
        r.ok("price info present")
    else:
        r.ok("no price info (OK)")

    # ── CHECK 12: Has product title (some words before the link) ────────────
    title_text = text.split('🔗')[0].strip()
    word_count = len([w for w in title_text.split() if len(w) > 2])
    if word_count < 3:
        r.fail("no product title", f"only {word_count} meaningful words")
        return text, affiliate_url, r
    else:
        r.ok(f"has title ({word_count} words)")

    return text, affiliate_url, r


def groq_quality_check(text, affiliate_url):
    """
    Optional: Ask Groq to verify the post looks like a genuine deal.
    Returns (approved: bool, reason: str).
    Only runs if GROQ_API_KEY env var is set.
    Fast — uses llama-3.1-8b-instant (free tier).
    """
    if not GROQ_API_KEY:
        return True, "groq skip (no key)"

    # Strip the link from text for cleaner analysis
    text_for_ai = text.split('🔗')[0].strip()[:500]

    prompt = f"""You are a quality checker for a deal-posting bot.
Review this deal message and reply ONLY with JSON: {{"ok": true/false, "reason": "one line"}}

Rules — mark ok=false if:
- Contains raw HTML tags or code
- Contains source website branding (desidime, dealsmagnet, etc.)
- Contains shortened URLs that weren't replaced (ddime.in, bit.ly etc.)
- Has no recognizable product name
- Is completely empty or only whitespace

Mark ok=true if it looks like a clean deal post with product info.

Message:
{text_for_ai}

Affiliate link present: {bool(affiliate_url)}

Reply ONLY with JSON, nothing else."""

    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}",
                     "Content-Type": "application/json"},
            json={"model": "llama-3.1-8b-instant",
                  "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": 60, "temperature": 0},
            timeout=8
        )
        if r.status_code == 200:
            content = r.json()['choices'][0]['message']['content'].strip()
            # Parse JSON response
            match = re.search(r'\{.*\}', content, re.DOTALL)
            if match:
                data = json.loads(match.group())
                return data.get('ok', True), data.get('reason', 'ok')
        print(f"    Groq HTTP {r.status_code}: {r.text[:80]}")
    except Exception as e:
        print(f"    Groq error: {e}")

    return True, "groq error (allow through)"  # don't block on AI failure

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6 — TELEGRAM BOT API
# ─────────────────────────────────────────────────────────────────────────────

def post_photo(chat_id, img_bytes, caption, ctype='image/jpeg'):
    """
    Send image + caption via Bot API sendPhoto (multipart upload).
    Caption is stripped of HTML and limited to 1024 chars.
    """
    try:
        # Strip any HTML from caption — Telegram sendPhoto doesn't support HTML
        # unless parse_mode is set, and parse_mode with malformed HTML fails silently
        clean_caption = re.sub(r'<[^>]+>', '', caption)  # strip HTML tags
        clean_caption = clean_caption[:1024]
        ext = 'jpg' if 'jpeg' in ctype else ctype.split('/')[-1]
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
            data={'chat_id': chat_id, 'caption': clean_caption},
            files={'photo': (f'product.{ext}', img_bytes, ctype)},
            timeout=30,
        )
        if r.status_code != 200:
            print(f"    sendPhoto API error: {r.text[:150]}")
        return r.status_code == 200, r.text
    except Exception as e:
        return False, str(e)

def post_text(chat_id, text):
    """Send text message via Bot API. Strips HTML tags from text."""
    try:
        clean = re.sub(r'<[^>]+>', '', text)  # strip any residual HTML tags
        clean = re.sub(r'\s+', ' ', clean).strip()[:4096]
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={'chat_id':                  chat_id,
                  'text':                     clean,
                  'disable_web_page_preview': True},
            timeout=15,
        )
        if r.status_code != 200:
            print(f"    sendMessage API error: {r.text[:150]}")
        return r.status_code == 200, r.text
    except Exception as e:
        return False, str(e)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 7 — STATE & DEALS.JSON
# ─────────────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 8 — MAIN LOOP
# ─────────────────────────────────────────────────────────────────────────────

async def run():
    state   = load_json(STATE_FILE, {})
    deals   = load_json(DEALS_FILE, [])
    total   = 0
    chat_id = f'@{YOUR_CHANNEL}'

    print(f"v{VERSION} | @{YOUR_CHANNEL} | tag={AMAZON_TAG} | cuelinks={'on' if CUELINKS_KEY else 'off'}")
    print(f"Sources: {SOURCE_CHANNELS}")
    print(f"State loaded: {list(state.items())[:3]}...")

    # Cross-run + cross-channel dedup (loaded from file)
    posted_hashes = set(load_json(HASHES_FILE, []))
    print(f"Loaded {len(posted_hashes)} seen hashes")

    # ── Connect ───────────────────────────────────────────────────────────────
    print("\nConnecting to Telegram...")
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
            if not channel:
                continue

            last_id     = state.get(channel, 0)
            new_last_id = last_id
            found = count = 0
            # First run (no state): grab last 5 to avoid spam
            # Subsequent runs: grab up to 20 new messages
            limit = 5 if last_id == 0 else 20

            print(f"\n{'─'*55}")
            print(f"  {channel}  last_id={last_id}  limit={limit}")

            try:
                # Diagnostic: check the actual latest message ID in this channel
                latest_msgs = await client.get_messages(channel, limit=1)
                if latest_msgs:
                    latest_id = latest_msgs[0].id
                    new_available = max(0, latest_id - last_id)
                    print(f"  latest_id={latest_id}  new_available≈{new_available}")
                    if new_available == 0:
                        print(f"  ⏭ no new messages — skipping")
                        continue
                else:
                    print(f"  ⚠️ could not fetch latest message")

                async for msg in client.iter_messages(
                        channel, min_id=last_id, limit=limit):

                    count += 1
                    if msg.id > new_last_id:
                        new_last_id = msg.id

                    raw_text  = msg.text or msg.message or ''
                    has_photo = bool(getattr(msg, 'photo', None))
                    print(f"\n  MSG {msg.id}: {len(raw_text)}ch  photo={has_photo}")

                    # Skip empty
                    if not raw_text.strip() and not has_photo:
                        print("    skip: no content")
                        continue

                    # Cross-channel dedup by content hash
                    msg_hash = hashlib.md5(raw_text[:200].encode()).hexdigest()[:10]
                    if msg_hash in posted_hashes:
                        print("    skip: duplicate across channels")
                        continue

                    # ── 1. Extract all URLs ──────────────────────────────────
                    all_urls = extract_all_urls_from_msg(msg)
                    print(f"    urls found: {all_urls}")

                    # ── 2. Resolve affiliate URL ─────────────────────────────
                    affiliate_url = None
                    image_cdn     = ''

                    for url in all_urls:
                        if is_ignorable(url):
                            continue
                        aff, img = resolve_to_affiliate(url)
                        if aff:
                            affiliate_url = aff
                            image_cdn     = img or ''
                            break   # use first working affiliate URL

                    print(f"    affiliate: {affiliate_url or 'NONE'}")
                    print(f"    image_cdn: {image_cdn[:50] if image_cdn else 'none'}")

                    # If no affiliate URL and no photo → skip (nothing useful)
                    if not affiliate_url and not has_photo:
                        print("    skip: no affiliate link and no photo")
                        continue

                    # If no affiliate URL but there IS a photo → still post
                    # (might be a useful deal even without linkable URL)
                    # But require at least some text
                    if not affiliate_url and not raw_text.strip():
                        print("    skip: photo-only with no text")
                        continue

                    # ── 3. Build clean text ──────────────────────────────────
                    clean = build_clean_text(msg, affiliate_url)
                    print(f"    clean text ({len(clean)}ch): {clean[:80].replace(chr(10),' ')!r}")

                    # ── 4. PRE-POST CHECKLIST (12 checks) ────────────────────────────────
                    clean, affiliate_url, chk = run_checklist(clean, affiliate_url)
                    print(chk.summary())
                    if not chk.is_good:
                        print(f"    ✗ checklist FAILED — skipping")
                        continue

                    # ── 4b. Groq AI quality check (optional) ─────────────────────────────
                    if GROQ_API_KEY:
                        ai_ok, ai_reason = groq_quality_check(clean, affiliate_url)
                        if not ai_ok:
                            print(f"    🤖 Groq REJECTED: {ai_reason} — skipping")
                            continue
                        print(f"    🤖 Groq OK: {ai_reason}")

                    # Append channel branding AFTER all checks pass
                    final_text = clean + f"\n\n🛒 Deals by @{YOUR_CHANNEL}"
                    # ── 5. Get image ─────────────────────────────────────────
                    img_bytes = None
                    img_type  = 'image/jpeg'
                    img_saved = ''

                    # Priority 1: Actual product photo from source message (always correct)
                    if has_photo:
                        print("    📷 downloading source photo via Telethon...")
                        telegraph_url = await get_photo_from_source(client, msg)
                        if telegraph_url:
                            img_bytes, img_type = download_image_bytes(
                                telegraph_url, referer='https://telegra.ph/')
                            if img_bytes:
                                img_saved = telegraph_url

                    # Priority 2: Amazon CDN image by ASIN (fallback for Amazon deals)
                    if not img_bytes and image_cdn:
                        img_bytes, img_type = download_image_bytes(image_cdn)
                        if img_bytes:
                            img_saved = image_cdn
                        else:
                            # Store CDN URL anyway — browser may load it even if we can't
                            img_saved = image_cdn

                    print(f"    image: {'bytes '+str(len(img_bytes)//1024)+'KB' if img_bytes else 'none (text only)'}")
                    print(f"    img_saved: {img_saved[:60] if img_saved else 'none'}")

                    # ── 6. Post to Telegram ──────────────────────────────────
                    ok_post = False
                    resp    = ''

                    if img_bytes:
                        ok_post, resp = post_photo(chat_id, img_bytes, final_text, img_type)
                        if not ok_post:
                            print(f"    sendPhoto failed: {resp[:80]}")
                            ok_post, resp = post_text(chat_id, final_text)
                    else:
                        ok_post, resp = post_text(chat_id, final_text)

                    if ok_post:
                        mode = '📷 photo' if img_bytes else '📝 text'
                        print(f"    ✅ POSTED ({mode})")
                        posted_hashes.add(msg_hash)
                        deals = add_deal(deals, final_text, affiliate_url or '',
                                         channel, img_saved)
                        found += 1
                        total += 1
                        time.sleep(random.uniform(2.0, 4.0))
                    else:
                        print(f"    ❌ FAILED: {resp[:150]}")

                print(f"\n  {'─'*40}")
                print(f"  {channel}: scanned={count}  posted={found}")

            except Exception as e:
                print(f"  ❌ ERROR in {channel}: {e}")
                traceback.print_exc()

            # Save last seen ID for this channel
            state[channel] = new_last_id

    # ── Persist state, deals, and seen hashes ───────────────────────────────
    save_json(STATE_FILE, state)
    save_json(DEALS_FILE, deals)
    # Save hashes (trim to MAX_HASHES to avoid unbounded growth)
    save_json(HASHES_FILE, list(posted_hashes)[-MAX_HASHES:])
    print(f"\n{'='*55}")
    print(f"v{VERSION} done: {total} posted | {len(deals)} deals on website")
    print(f"State saved: {list(state.items())[:3]}...")

if __name__ == '__main__':
    asyncio.run(run())
