"""
feed_scraper.py — Web deal sources for the affiliate bot
=========================================================
Fetches deals from RSS feeds and Amazon's deals RSS.
Uses rss2json.com as a proxy (free, no signup, works from GitHub Actions).

Returns deals in same format as Telegram messages so they pass through
the same affiliate URL pipeline and checklist in main.py.
"""

import re
import json
import hashlib
import requests
import os

BROWSE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'en-IN,en;q=0.9',
}

# rss2json.com converts any RSS to clean JSON API — free, no auth needed
RSS2JSON = "https://api.rss2json.com/v1/api.json"

# Deal RSS feeds
FEED_SOURCES = [
    {
        "name":  "desidime",
        "rss":   "https://www.desidime.com/deals.rss",
        "type":  "deals",
    },
    {
        "name":  "freekaamaal",
        "rss":   "https://www.freekaamaal.com/feed",
        "type":  "deals",
    },
    {
        "name":  "dealsmagnet",
        "rss":   "https://www.dealsmagnet.com/feed",
        "type":  "deals",
    },
    {
        "name":  "lootdunia",
        "rss":   "https://www.lootdunia.com/feed",
        "type":  "deals",
    },
    {
        "name":  "amazon_goldbox",
        "rss":   "https://www.amazon.in/rss/goldbox",
        "type":  "amazon",
    },
    {
        "name":  "amazon_movers",
        "rss":   "https://www.amazon.in/rss/movers-and-shakers/electronics/976419031",
        "type":  "amazon",
    },
]


def fetch_rss_via_proxy(rss_url, count=15):
    """
    Fetch RSS feed via rss2json.com proxy.
    Returns list of items: [{title, link, thumbnail, description, pubDate}]
    """
    try:
        params = {"rss_url": rss_url, "count": count, "order_by": "pubDate"}
        rss2json_key = os.environ.get("RSS2JSON_API_KEY", "").strip()
        if rss2json_key:
            params["api_key"] = rss2json_key
        r = requests.get(
            RSS2JSON,
            params=params,
            headers=BROWSE_HEADERS,
            timeout=15,
        )
        if r.status_code != 200:
            print(f"    rss2json HTTP {r.status_code} for {rss_url[:50]}")
            return []
        data = r.json()
        if data.get("status") != "ok":
            print(f"    rss2json error: {data.get('message','?')[:80]}")
            return []
        items = data.get("items", [])
        print(f"    ✅ {len(items)} items from {rss_url[:50]}")
        return items
    except Exception as e:
        print(f"    rss2json fetch failed: {e}")
        return []


def extract_urls_from_text(text):
    """Extract all URLs from text/HTML."""
    return re.findall(r'https?://[^\s\'"<>]+', text or '')


def extract_image_from_item(item):
    """Get best image URL from RSS item."""
    # Method 1: thumbnail field (rss2json provides this)
    thumb = item.get("thumbnail", "")
    if thumb and thumb.startswith("http") and not thumb.endswith(".gif"):
        return thumb

    # Method 2: enclosure
    enc = item.get("enclosure", {})
    if isinstance(enc, dict):
        link = enc.get("link", "") or enc.get("url", "")
        if link and "image" in enc.get("type", "image"):
            return link

    # Method 3: first image in description HTML
    desc = item.get("description", "") or item.get("content", "")
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', desc or '')
    if m:
        img = m.group(1)
        if img.startswith("http") and not img.endswith(".gif"):
            return img

    return ""


def clean_html(text):
    """Strip HTML tags, return plain text."""
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def extract_product_url_from_item(item):
    """
    Find the actual product URL (Amazon/Flipkart) from an RSS item.
    Deal sites often use redirect URLs in their RSS — we follow them.
    """
    # Direct link
    link = item.get("link", "")

    # Check description for direct Amazon/Flipkart links
    desc = item.get("description", "") or item.get("content", "")
    all_urls = extract_urls_from_text(desc)

    amazon_urls   = [u for u in all_urls if "amazon.in" in u or "amzn." in u]
    flipkart_urls = [u for u in all_urls if "flipkart.com" in u or "fktr.in" in u]

    if amazon_urls:
        return amazon_urls[0]
    if flipkart_urls:
        return flipkart_urls[0]

    return link  # return source site URL — will be handled by resolve_to_affiliate


class FakeMsgForPipeline:
    """
    Wraps an RSS feed item to look like a Telegram message
    so it can pass through the same pipeline in main.py.
    """
    def __init__(self, title, description, url, image_url):
        # Build text like a Telegram deal message
        desc_clean = clean_html(description)[:300]
        self.text = f"{title}\n\n{desc_clean}\n\n{url}"
        self.message = self.text
        self.photo = None        # no photo attached — we use scraped image_url
        self.entities = []
        self._image_url = image_url
        self._product_url = url

    def __repr__(self):
        return f"<FeedItem: {self.text[:60]}>"


def fetch_all_deals(seen_hashes: set, max_per_source=10):
    """
    Fetch deals from all RSS sources.
    Returns list of (FakeMsgForPipeline, source_name, image_url, product_url)
    Only returns deals not already in seen_hashes.
    """
    results = []

    for source in FEED_SOURCES:
        name = source["name"]
        rss  = source["rss"]
        print(f"\n  [feed] {name}")

        items = fetch_rss_via_proxy(rss, count=max_per_source)
        found = 0

        for item in items:
            title       = clean_html(item.get("title", "")).strip()
            description = item.get("description", "") or item.get("content", "")
            product_url = extract_product_url_from_item(item)
            image_url   = extract_image_from_item(item)
            pub_date    = item.get("pubDate", "")

            if not title or not product_url:
                continue

            # Dedup by title hash
            item_hash = hashlib.md5(title[:120].encode()).hexdigest()[:10]
            if item_hash in seen_hashes:
                continue

            # Skip source-site-only URLs (no Amazon/Flipkart link found)
            source_domains = [
                'desidime.com', 'freekaamaal.com', 'dealsmagnet.com',
                'lootdunia.com', 'dealsbazaar.in',
            ]
            if any(d in product_url for d in source_domains):
                # Try to extract Amazon/Flipkart URL from description
                desc_urls = extract_urls_from_text(description)
                amazon_fk = [u for u in desc_urls
                             if 'amazon.in' in u or 'amzn.' in u
                             or 'flipkart.com' in u or 'fktr.in' in u]
                if amazon_fk:
                    product_url = amazon_fk[0]
                else:
                    # No direct product URL — skip
                    continue

            msg = FakeMsgForPipeline(title, description, product_url, image_url)
            results.append((msg, name, image_url, product_url))
            found += 1

            if found >= max_per_source:
                break

        print(f"    → {found} new deals from {name}")

    print(f"\n  [feed] total new: {len(results)} deals from {len(FEED_SOURCES)} sources")
    return results


if __name__ == "__main__":
    # Test run
    deals = fetch_all_deals(set(), max_per_source=3)
    for msg, source, img, url in deals[:5]:
        print(f"\n--- {source} ---")
        print(f"Text: {msg.text[:100]}")
        print(f"URL:  {url[:60]}")
        print(f"Img:  {img[:60]}")
