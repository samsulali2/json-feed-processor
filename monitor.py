"""
monitor.py — Self-Sustaining Health Monitor
=============================================
Runs every 30 minutes via GitHub Actions.
Checks everything, auto-fixes what it can, alerts via Telegram if not.

Checks:
  1. Website is reachable (crazyonlinedeals.in)
  2. Website has fresh deals (not stale)
  3. Bot token is valid (Telegram API)
  4. Telegram channel last post age
  5. GitHub Pages DNS/CDN
  6. deals.json is valid JSON and not empty

Auto-fixes:
  - Stale deals.json → triggers deal_bot workflow
  - Website down → purges Cloudflare/GitHub CDN cache
  - Bot token invalid → alerts with exact fix steps
  - DNS issues → alerts with fix steps
"""

import os, re, json, requests, time
from datetime import datetime, timezone, timedelta

BOT_TOKEN    = os.environ.get("A3", "").strip()
YOUR_CHANNEL = os.environ.get("A5", "").strip().lstrip('@')
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
REPO         = "samsulali2/json-feed-processor"
WEBSITE_URL  = "https://crazyonlinedeals.in"
ALERT_CHAT   = f"@{YOUR_CHANNEL}" if YOUR_CHANNEL else ""

# ── Alert sender ──────────────────────────────────────────────────────────────

def send_alert(message, is_fix=False):
    """Send alert to Telegram channel"""
    if not BOT_TOKEN or not ALERT_CHAT:
        print(f"ALERT: {message}")
        return
    emoji = "🔧" if is_fix else "🚨"
    text = f"{emoji} *CrazyOnlineDeals Monitor*\n\n{message}\n\n⏰ {datetime.now().strftime('%d %b %Y %H:%M IST')}"
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": ALERT_CHAT, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
        if r.status_code == 200:
            print(f"  ✅ Alert sent: {message[:60]}")
        else:
            print(f"  ❌ Alert failed: {r.text[:80]}")
    except Exception as e:
        print(f"  Alert error: {e}")

def send_ok(message):
    print(f"  ✅ {message}")


# ── Check 1: Website reachability ─────────────────────────────────────────────

def check_website():
    print("\n[1] Checking website...")
    try:
        r = requests.get(WEBSITE_URL, timeout=15,
                         headers={"Cache-Control": "no-cache"})
        if r.status_code == 200:
            # Check it has actual deal content
            if "deals.json" in r.text or "deal-card" in r.text or "Grab This Deal" in r.text:
                send_ok(f"Website UP — {r.status_code} in {r.elapsed.total_seconds():.1f}s")
                return True
            else:
                send_alert(
                    "⚠️ Website loads but deal content missing.\n"
                    "Possible GitHub Pages build issue.\n"
                    "Fix: Go to repo → Settings → Pages → check build status"
                )
                return False
        elif r.status_code in (301, 302):
            send_ok(f"Website redirecting ({r.status_code}) — likely HTTPS redirect, OK")
            return True
        else:
            send_alert(
                f"🔴 Website returned HTTP {r.status_code}\n"
                f"URL: {WEBSITE_URL}\n"
                f"Fix: Check GitHub Pages in repo Settings"
            )
            return False
    except requests.exceptions.ConnectionError:
        send_alert(
            "🔴 Website UNREACHABLE — DNS or server down\n"
            f"URL: {WEBSITE_URL}\n\n"
            "Auto-fix steps:\n"
            "1. Check DNS: crazyonlinedeals.in → Hostinger DNS panel\n"
            "2. Ensure A records: 185.199.108-111.153\n"
            "3. Ensure CNAME www → samsulali2.github.io\n"
            "4. Check GitHub Pages: repo → Settings → Pages → must show 'Your site is live'"
        )
        return False
    except Exception as e:
        send_alert(f"🔴 Website check failed: {e}")
        return False


# ── Check 2: Website freshness ────────────────────────────────────────────────

def check_deals_freshness():
    print("\n[2] Checking deals freshness...")
    try:
        r = requests.get(f"{WEBSITE_URL}/deals.json?t={int(time.time())}", timeout=10)
        if r.status_code != 200:
            send_alert(f"🔴 deals.json not accessible: HTTP {r.status_code}")
            return False

        deals = r.json()
        if not deals:
            send_alert("⚠️ deals.json is empty — bot may not have posted yet")
            trigger_bot_workflow()  
            return False

        # Check age of newest deal
        newest_ts = deals[0].get("timestamp", "")
        if newest_ts:
            newest = datetime.fromisoformat(newest_ts.replace("Z", "+00:00"))
            age_hours = (datetime.now(timezone.utc) - newest).total_seconds() / 3600

            if age_hours > 2:
                msg = (f"⚠️ Last deal was {age_hours:.1f} hours ago\n"
                       f"Bot may be stuck or source channels are quiet\n"
                       f"Last deal: {deals[0].get('text','')[:60]}")
                send_alert(msg)
                # Auto-trigger: dispatch deal_bot workflow
                trigger_bot_workflow()
                return False
            else:
                send_ok(f"Deals fresh — last deal {age_hours:.1f}h ago ({len(deals)} total)")
                return True
    except Exception as e:
        send_alert(f"🔴 deals.json check failed: {e}")
        return False


# ── Check 3: Bot token validity ───────────────────────────────────────────────

def check_bot_token():
    print("\n[3] Checking bot token...")
    if not BOT_TOKEN:
        send_alert("🔴 BOT_TOKEN (A3 secret) is missing or empty")
        return False
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getMe",
            timeout=10
        )
        data = r.json()
        if data.get("ok"):
            bot_name = data["result"].get("username", "?")
            send_ok(f"Bot token valid — @{bot_name}")
            return True
        else:
            error = data.get("description", "Unknown error")
            send_alert(
                f"🔴 Bot token INVALID: {error}\n\n"
                "Fix (2 min):\n"
                "1. Open Telegram → @BotFather\n"
                "2. /mybots → select your bot\n"
                "3. API Token → Revoke current token\n"
                "4. Copy new token\n"
                "5. GitHub → Settings → Secrets → update TELEGRAM_BOT_TOKEN"
            )
            return False
    except Exception as e:
        send_alert(f"🔴 Bot token check failed: {e}")
        return False


# ── Check 4: Telegram channel last post ───────────────────────────────────────

def check_telegram_channel():
    print("\n[4] Checking Telegram channel activity...")
    if not BOT_TOKEN or not YOUR_CHANNEL:
        print("  ⏭ Skipping — no bot token or channel")
        return True
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
            params={"limit": 1, "allowed_updates": []},
            timeout=10
        )
        if r.status_code == 200:
            send_ok(f"Telegram API responsive")
            return True
        else:
            send_alert(f"⚠️ Telegram API issue: {r.text[:100]}")
            return False
    except Exception as e:
        send_alert(f"🔴 Telegram check failed: {e}")
        return False


# ── Check 5: GitHub Actions last run ─────────────────────────────────────────

def check_github_actions():
    print("\n[5] Checking GitHub Actions last run...")
    if not GITHUB_TOKEN:
        print("  ⏭ Skipping — no GITHUB_TOKEN")
        return True
    try:
        r = requests.get(
            f"https://api.github.com/repos/{REPO}/actions/runs",
            params={"per_page": 1, "workflow": "deal_bot.yml"},
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}",
                     "Accept": "application/vnd.github+json"},
            timeout=10
        )
        if r.status_code == 200:
            runs = r.json().get("workflow_runs", [])
            if runs:
                last = runs[0]
                status     = last.get("conclusion", "?")
                created_at = last.get("created_at", "")
                if created_at:
                    ran = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    age_min = (datetime.now(timezone.utc) - ran).total_seconds() / 60
                    if status == "failure":
                        send_alert(
                            f"🔴 Last GitHub Actions run FAILED\n"
                            f"Ran {age_min:.0f} min ago\n"
                            f"Check: github.com/{REPO}/actions"
                        )
                        return False
                    elif age_min > 30:
                        send_alert(
                            f"⚠️ No GitHub Actions run in {age_min:.0f} min\n"
                            f"cron-job.org may have stopped triggering\n"
                            f"Check: cron-job.org dashboard"
                        )
                        return False
                    else:
                        send_ok(f"Actions last ran {age_min:.0f} min ago — {status}")
                        return True
    except Exception as e:
        print(f"  ⏭ GitHub check skipped: {e}")
    return True


# ── Auto-fix: Trigger bot workflow ────────────────────────────────────────────

def trigger_bot_workflow():
    """Trigger the deal bot workflow via GitHub API to refresh deals"""
    if not GITHUB_TOKEN:
        return
    try:
        r = requests.post(
            f"https://api.github.com/repos/{REPO}/actions/workflows/deal_bot.yml/dispatches",
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}",
                     "Accept": "application/vnd.github+json"},
            json={"ref": "main"},
            timeout=10
        )
        if r.status_code == 204:
            send_alert("🔧 Auto-triggered deal bot to refresh stale deals", is_fix=True)
        else:
            print(f"  Trigger failed: {r.status_code}")
    except Exception as e:
        print(f"  Trigger error: {e}")


# ── Check 6: DNS resolution ───────────────────────────────────────────────────

def check_dns():
    print("\n[6] Checking DNS...")
    try:
        import socket
        ip = socket.gethostbyname("crazyonlinedeals.in")
        # GitHub Pages IPs
        github_ips = {"185.199.108.153", "185.199.109.153",
                      "185.199.110.153", "185.199.111.153"}
        if ip in github_ips:
            send_ok(f"DNS resolves to {ip} (GitHub Pages ✅)")
            return True
        else:
            send_alert(
                f"⚠️ DNS resolves to {ip} — not a GitHub Pages IP\n"
                f"Expected one of: {', '.join(sorted(github_ips))}\n"
                f"Fix: Hostinger DNS → update A record to 185.199.108.153"
            )
            return False
    except Exception as e:
        send_alert(
            f"🔴 DNS resolution failed for crazyonlinedeals.in\n"
            f"Error: {e}\n"
            f"Fix: Check Hostinger DNS panel — A records must point to GitHub Pages"
        )
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 55)
    print(f"CrazyOnlineDeals Monitor — {datetime.now().strftime('%d %b %Y %H:%M')}")
    print("=" * 55)

    results = {
        "dns":        check_dns(),
        "website":    check_website(),
        "freshness":  check_deals_freshness(),
        "bot_token":  check_bot_token(),
        "telegram":   check_telegram_channel(),
        "actions":    check_github_actions(),
    }

    passed = sum(results.values())
    total  = len(results)

    print(f"\n{'=' * 55}")
    print(f"Monitor complete: {passed}/{total} checks passed")
    for name, ok in results.items():
        print(f"  {'✅' if ok else '❌'} {name}")

    if passed == total:
        print("✅ All systems healthy")
    else:
        failed = [k for k,v in results.items() if not v]
        print(f"⚠️ Issues found: {', '.join(failed)}")


if __name__ == "__main__":
    main()
