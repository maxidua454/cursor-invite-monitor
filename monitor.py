"""
CURSOR INVITE LINK MONITOR v6
- SeleniumBase UC mode with Xvfb virtual display in Docker
- Cloudflare/Turnstile bypass via reconnect trick
- Multi-account support
- Detects removal → auto-rejoin
- Email notifications
- Health endpoint for UptimeRobot
- Self-healing, never dies
"""

import sys
import subprocess
import os

# Auto-install deps
for pkg in ["seleniumbase", "colorama"]:
    try:
        __import__(pkg)
    except ImportError:
        print(f"[*] Installing {pkg}...")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", pkg, "--quiet"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

import json
import time
import re
import smtplib
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from pathlib import Path
from seleniumbase import Driver
from colorama import init, Fore, Style

init(autoreset=True)

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
HISTORY_PATH = BASE_DIR / "link_history.json"
LOG_FILE = BASE_DIR / "monitor.log"
IS_DOCKER = os.path.exists("/.dockerenv") or os.environ.get("RENDER", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("cursor-monitor")

AUTH_URL = "https://authenticator.cursor.sh/"
DASHBOARD_URL = "https://cursor.com/dashboard"
MEMBERS_URL = "https://cursor.com/dashboard/members"


def cprint(color, symbol, msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{color}{ts} [{symbol}] {msg}{Style.RESET_ALL}")
    log.info(f"[{symbol}] {msg}")


# ============================================================
# CONFIG & HISTORY
# ============================================================
def load_config():
    if not CONFIG_PATH.exists():
        cprint(Fore.YELLOW, "!!", "No config.json, building from env vars...")
        cfg = {
            "accounts": [{
                "name": os.environ.get("ACCOUNT_NAME", "Main"),
                "cursor_email": os.environ.get("CURSOR_EMAIL", ""),
                "cursor_password": os.environ.get("CURSOR_PASSWORD", ""),
                "known_invite_link": os.environ.get("KNOWN_INVITE_LINK", ""),
                "auto_join": True,
                "enabled": True,
            }],
            "notification_email": os.environ.get("NOTIFICATION_EMAIL", ""),
            "gmail_app_password": os.environ.get("GMAIL_APP_PASSWORD", ""),
            "check_interval_seconds": int(os.environ.get("CHECK_INTERVAL", "5")),
        }
        save_config(cfg)
        return cfg

    with open(CONFIG_PATH, "r") as f:
        cfg = json.load(f)
    for env_key, cfg_key in [
        ("NOTIFICATION_EMAIL", "notification_email"),
        ("GMAIL_APP_PASSWORD", "gmail_app_password"),
    ]:
        val = os.environ.get(env_key)
        if val:
            cfg[cfg_key] = val
    val = os.environ.get("CHECK_INTERVAL")
    if val:
        cfg["check_interval_seconds"] = int(val)
    for env_key, acc_key in [
        ("CURSOR_EMAIL", "cursor_email"),
        ("CURSOR_PASSWORD", "cursor_password"),
        ("KNOWN_INVITE_LINK", "known_invite_link"),
    ]:
        val = os.environ.get(env_key)
        if val and cfg.get("accounts"):
            cfg["accounts"][0][acc_key] = val
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=4)


def load_history():
    if HISTORY_PATH.exists():
        with open(HISTORY_PATH, "r") as f:
            return json.load(f)
    return []


def save_history(history):
    with open(HISTORY_PATH, "w") as f:
        json.dump(history, f, indent=4)


# ============================================================
# EMAIL
# ============================================================
def send_email(cfg, subject, body):
    email_addr = cfg.get("notification_email", "")
    app_pw = cfg.get("gmail_app_password", "")
    if not email_addr or not app_pw or app_pw == "NEED_APP_PASSWORD":
        cprint(Fore.YELLOW, "!!", f"Email skip | {subject}")
        return False
    try:
        msg = MIMEMultipart()
        msg["From"] = email_addr
        msg["To"] = email_addr
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "html"))
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(email_addr, app_pw)
            server.send_message(msg)
        cprint(Fore.GREEN, "OK", "Email sent!")
        return True
    except Exception as e:
        cprint(Fore.RED, "!!", f"Email failed: {e}")
        return False


# ============================================================
# BROWSER HELPERS
# ============================================================
def create_browser():
    """
    Create SeleniumBase UC browser.
    In Docker: uses Xvfb (virtual display) + headed mode for better CF bypass.
    Locally: headless mode.
    """
    cprint(Fore.CYAN, ">>", f"Creating browser (docker={IS_DOCKER})...")
    if IS_DOCKER:
        driver = Driver(
            uc=True,
            headed=True,
            uc_cdp_events=True,
            chromium_arg=(
                "--no-sandbox,--disable-dev-shm-usage,--disable-gpu,"
                "--disable-software-rasterizer,--disable-extensions,"
                "--disable-background-timer-throttling,"
                "--disable-backgrounding-occluded-windows,"
                "--disable-renderer-backgrounding,"
                "--window-size=1280,720"
            ),
        )
    else:
        driver = Driver(uc=True, headless=False)
    cprint(Fore.GREEN, "OK", "Browser ready")
    return driver


def quit_browser(driver):
    try:
        driver.quit()
    except Exception:
        pass


def safe_reconnect(driver, wait=8):
    """Reconnect with fallback for Docker."""
    try:
        driver.reconnect(wait)
    except Exception as e:
        cprint(Fore.YELLOW, "..", f"reconnect fallback: {str(e)[:40]}")
        time.sleep(wait)


def solve_cloudflare(driver, context="page"):
    for attempt in range(4):
        try:
            is_cf = driver.execute_script("""
                return document.title.includes('Just a moment')
                    || document.querySelector('#challenge-running') !== null
                    || document.querySelector('#challenge-stage') !== null
                    || document.querySelector('iframe[src*="turnstile"]') !== null
                    || document.querySelector('[class*="turnstile"]') !== null
                    || document.querySelector('#cf-turnstile') !== null;
            """)
            if not is_cf:
                return True

            cprint(Fore.YELLOW, "CF", f"{context} (attempt {attempt+1}/4)")

            # Method 1: reconnect trick (disconnect/reconnect bypasses CF)
            safe_reconnect(driver, 8)
            time.sleep(1)

            still = driver.execute_script(
                "return document.title.includes('Just a moment') || "
                "document.querySelector('#challenge-running') !== null;"
            )
            if not still:
                cprint(Fore.GREEN, "OK", "CF bypassed!")
                return True

            # Check if token was auto-solved
            token = driver.execute_script("""
                var inp = document.querySelector('input[name="cf-turnstile-response"]');
                if (inp && inp.value && inp.value.length > 20) return true;
                return false;
            """)
            if token:
                cprint(Fore.GREEN, "OK", "Turnstile solved!")
                return True

            # Method 2: longer reconnect
            safe_reconnect(driver, 12)
            time.sleep(2)
        except Exception as e:
            cprint(Fore.YELLOW, "..", f"CF error: {str(e)[:50]}")
            time.sleep(1)

    cprint(Fore.RED, "!!", "CF bypass failed")
    return False


def wait_for_any(driver, selectors, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for sel in selectors:
            try:
                if driver.is_element_present(sel):
                    return sel
            except Exception:
                continue
        time.sleep(0.5)
    return None


# ============================================================
# LOGIN
# ============================================================
def login_to_cursor(driver, email, password):
    cprint(Fore.CYAN, ">>", f"Logging in as {email}...")
    try:
        monitor_status["status"] = "login:opening_auth"
        try:
            driver.uc_open_with_reconnect(AUTH_URL, reconnect_time=6)
        except Exception as e:
            cprint(Fore.YELLOW, "..", f"uc_open fallback: {str(e)[:50]}")
            driver.get(AUTH_URL)
        time.sleep(3)

        current = str(driver.current_url)
        page_title = ""
        try:
            page_title = driver.execute_script("return document.title;") or ""
        except Exception:
            pass
        cprint(Fore.CYAN, ">>", f"Auth page: {current[:60]} | title: {page_title[:40]}")
        monitor_status["status"] = f"login:at_{current[:30]}"

        if "cursor.com/dashboard" in current:
            time.sleep(2)
            try:
                text = driver.execute_script("return document.body.innerText;") or ""
                if "Team Plan" in text or "Overview" in text or "Settings" in text:
                    cprint(Fore.GREEN, "OK", "Already logged in!")
                    return True
            except Exception:
                pass
            cprint(Fore.YELLOW, "!!", "Dashboard URL but not logged in, proceeding...")
            driver.get(AUTH_URL)
            time.sleep(3)

        monitor_status["status"] = "login:solving_cf"
        solve_cloudflare(driver, "login landing")

        monitor_status["status"] = "login:finding_email"
        email_sel = wait_for_any(driver, [
            'input[name="email"]', 'input[type="email"]',
        ], timeout=15)
        if not email_sel:
            solve_cloudflare(driver, "email page")
            email_sel = wait_for_any(driver, [
                'input[name="email"]', 'input[type="email"]',
            ], timeout=10)
        if not email_sel:
            # Dump page info for debugging
            try:
                title = driver.execute_script("return document.title;") or ""
                url = driver.current_url
                body_text = (driver.execute_script("return document.body.innerText;") or "")[:200]
                cprint(Fore.RED, "!!", f"Email field not found | URL: {url[:60]} | Title: {title} | Body: {body_text[:100]}")
                monitor_status["last_error"] = f"No email field | {title} | {url[:60]}"
            except Exception:
                cprint(Fore.RED, "!!", "Email field not found (page unreadable)")
                monitor_status["last_error"] = "No email field (page unreadable)"
            return False

        monitor_status["status"] = "login:typing_email"
        driver.type(email_sel, email)
        cprint(Fore.GREEN, "OK", f"Email: {email}")

        cont = wait_for_any(driver, ['button[type="submit"]'], timeout=5)
        if cont:
            driver.click(cont)
        else:
            driver.execute_script("""
                var btns = document.querySelectorAll('button');
                for (var b of btns) { if (b.textContent.includes('Continue')) { b.click(); break; } }
            """)
        time.sleep(2)

        monitor_status["status"] = "login:finding_password"
        pw_sel = wait_for_any(driver, [
            'input[name="password"]', 'input[type="password"]',
        ], timeout=15)
        if not pw_sel:
            try:
                title = driver.execute_script("return document.title;") or ""
                url = driver.current_url
                body_text = (driver.execute_script("return document.body.innerText;") or "")[:200]
                cprint(Fore.RED, "!!", f"Password field not found | URL: {url[:60]} | Title: {title} | Body: {body_text[:100]}")
                monitor_status["last_error"] = f"No password field | {title} | {url[:60]}"
            except Exception:
                cprint(Fore.RED, "!!", "Password field not found")
                monitor_status["last_error"] = "No password field"
            return False

        monitor_status["status"] = "login:typing_password"
        driver.type(pw_sel, password)
        cprint(Fore.GREEN, "OK", "Password entered")

        # KEY TRICK: Schedule Sign In click via setTimeout, then disconnect
        # Chrome before it fires. When the click happens, Chrome is disconnected
        # from Selenium so Cloudflare can't detect automation during the redirect.
        monitor_status["status"] = "login:clicking_signin"
        cprint(Fore.CYAN, ">>", "Scheduling Sign In click + disconnect...")

        # Schedule the click to fire in 1 second
        driver.execute_script("""
            setTimeout(function() {
                var btn = document.querySelector('button[type="submit"]');
                if (btn) { btn.click(); return; }
                var btns = document.querySelectorAll('button');
                for (var b of btns) {
                    if (b.textContent.includes('Sign in')) { b.click(); return; }
                }
            }, 1000);
        """)

        # Disconnect BEFORE the click fires
        try:
            driver.disconnect()
            cprint(Fore.GREEN, "OK", "Disconnected before click")
        except Exception as e:
            cprint(Fore.YELLOW, "..", f"disconnect: {str(e)[:40]}")

        # Wait while disconnected — click fires, form submits, CF challenge
        # happens, redirect completes — all without Selenium connected
        monitor_status["status"] = "login:disconnected_waiting"
        time.sleep(12)

        # Reconnect
        try:
            driver.reconnect(3)
            cprint(Fore.GREEN, "OK", "Reconnected")
        except Exception as e:
            cprint(Fore.YELLOW, "..", f"reconnect: {str(e)[:40]}")
            time.sleep(3)

        monitor_status["status"] = "login:checking_result"

        # Dump page info right after reconnect
        try:
            url = driver.current_url
            title = driver.execute_script("return document.title;") or ""
            body = (driver.execute_script("return document.body.innerText;") or "")[:200]
            cprint(Fore.CYAN, ">>", f"After reconnect URL: {url[:60]}")
            cprint(Fore.CYAN, ">>", f"Title: {title}")
            cprint(Fore.CYAN, ">>", f"Body: {body[:100]}")
            monitor_status["last_error"] = f"post-reconnect: {title} | {body[:80]}"
        except Exception as e:
            cprint(Fore.YELLOW, "..", f"Page read err: {str(e)[:40]}")

        # Check if we landed on dashboard
        for w in range(15):
            try:
                url = driver.current_url
                cprint(Fore.CYAN, ">>", f"Post-login URL: {url[:60]}") if w == 0 else None
            except Exception:
                time.sleep(1)
                continue

            if "cursor.com" in url and "authenticator" not in url:
                cprint(Fore.GREEN, "OK", f"Login success! {url}")
                return True

            # If still on CF, try reconnect again
            if w == 5:
                try:
                    is_cf = driver.execute_script(
                        "return document.title.includes('Just a moment');"
                    )
                    if is_cf:
                        cprint(Fore.YELLOW, "CF", "Still on CF, reconnecting...")
                        safe_reconnect(driver, 10)
                except Exception:
                    pass

            # If still stuck at 10s, try direct dashboard nav
            if w == 10:
                monitor_status["status"] = "login:try_direct_nav"
                cprint(Fore.CYAN, ">>", "Trying direct nav to dashboard...")
                try:
                    driver.uc_open_with_reconnect(DASHBOARD_URL, reconnect_time=6)
                except Exception:
                    driver.get(DASHBOARD_URL)
                time.sleep(2)
                solve_cloudflare(driver, "dashboard")
                try:
                    url = driver.current_url
                    if "cursor.com" in url and "authenticator" not in url:
                        text = driver.execute_script("return document.body.innerText;") or ""
                        if any(k in text for k in ["Team Plan", "Overview", "Settings", "Usage"]):
                            cprint(Fore.GREEN, "OK", f"Login success (direct)! {url}")
                            return True
                except Exception:
                    pass
            time.sleep(1)

        try:
            final_url = driver.current_url
            title = driver.execute_script("return document.title;") or ""
            body_text = (driver.execute_script("return document.body.innerText;") or "")[:300]
            cprint(Fore.RED, "!!", f"Login timeout | URL: {final_url[:60]} | Title: {title}")
            cprint(Fore.RED, "!!", f"Body: {body_text[:200]}")
            monitor_status["last_error"] = f"Timeout | {title} | {body_text[:150]}"
        except Exception:
            final_url = "unknown"
            monitor_status["last_error"] = f"Timeout at {final_url}"
        return False
    except Exception as e:
        cprint(Fore.RED, "!!", f"Login error: {e}")
        monitor_status["last_error"] = f"Login error: {str(e)[:100]}"
        return False


# ============================================================
# TEAM STATUS & INVITE LINK
# ============================================================
def check_team_status(driver):
    try:
        driver.get(DASHBOARD_URL)
        time.sleep(3)
        url = str(driver.current_url)
        if "authenticator" in url or "login" in url:
            return "logged_out", "Redirected to login"
        text = driver.execute_script("return document.body.innerText;") or ""
        if "Team Plan" in text:
            return "active", "Team Plan"
        if "Free" in text and "Plan" in text:
            return "free_plan", "Free plan"
        driver.get(MEMBERS_URL)
        time.sleep(2)
        if "members" not in str(driver.current_url):
            return "removed", "Redirected from members"
        has_invite = driver.execute_script("""
            var btns = document.querySelectorAll('button');
            for (var b of btns) { if (b.textContent.includes('Invite')) return true; }
            return false;
        """)
        if has_invite:
            return "active", "Members page OK"
        return "active", "Members accessible"
    except Exception as e:
        return "logged_out", str(e)


def extract_invite_link(driver):
    try:
        if "members" not in str(driver.current_url):
            driver.get(MEMBERS_URL)
            time.sleep(3)
            solve_cloudflare(driver, "members")

        invite_clicked = driver.execute_script("""
            var btns = document.querySelectorAll('button');
            for (var b of btns) {
                if (b.textContent.trim() === 'Invite' || b.textContent.includes('Invite')) {
                    b.click(); return true;
                }
            }
            return false;
        """)
        if not invite_clicked:
            cprint(Fore.RED, "!!", "Invite button not found")
            return None
        time.sleep(1.5)

        link = driver.execute_script("""
            return new Promise((resolve, reject) => {
                const orig = navigator.clipboard.writeText.bind(navigator.clipboard);
                navigator.clipboard.writeText = async (text) => {
                    resolve(text);
                    navigator.clipboard.writeText = orig;
                    return orig(text);
                };
                var btns = document.querySelectorAll('button');
                for (var b of btns) {
                    if (b.textContent.includes('Copy Invite Link') || b.textContent.includes('Copy invite link')) {
                        b.click(); break;
                    }
                }
                setTimeout(() => reject(new Error('timeout')), 5000);
            });
        """)
        if link and "cursor.com" in link:
            cprint(Fore.GREEN, "OK", f"Link: {link.strip()}")
            return link.strip()
    except Exception as e:
        cprint(Fore.YELLOW, "!!", f"Clipboard failed: {e}")

    try:
        src = driver.get_page_source()
        m = re.search(r'https://cursor\.com/team/accept-invite\?code=[a-f0-9]+', src)
        if m:
            cprint(Fore.GREEN, "OK", f"Link (HTML): {m.group(0)}")
            return m.group(0)
    except Exception:
        pass

    try:
        link = driver.execute_script("""
            var w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            while (w.nextNode()) {
                var m = w.currentNode.textContent.match(/https:\\/\\/cursor\\.com\\/team\\/accept-invite\\?code=[a-f0-9]+/);
                if (m) return m[0];
            }
            return null;
        """)
        if link:
            return link
    except Exception:
        pass

    return None


def close_modal(driver):
    try:
        driver.execute_script("""
            var btns = document.querySelectorAll('button[aria-label="Close"], [class*="close"]');
            for (var b of btns) { var el = b.closest('button'); if (el) { el.click(); return; } }
            var bds = document.querySelectorAll('[class*="backdrop"], [class*="overlay"]');
            for (var bd of bds) { bd.click(); return; }
        """)
        time.sleep(0.3)
    except Exception:
        pass


def auto_join_invite(driver, link):
    cprint(Fore.CYAN, ">>", f"Joining: {link}")
    try:
        driver.get(link)
        time.sleep(3)
        solve_cloudflare(driver, "invite")
        text = (driver.execute_script("return document.body.innerText;") or "").lower()
        if "already" in text and "member" in text:
            cprint(Fore.GREEN, "OK", "Already a member!")
            return True
        joined = driver.execute_script("""
            var btns = document.querySelectorAll('button');
            for (var b of btns) {
                var t = b.textContent.toLowerCase();
                if (t.includes('accept') || t.includes('join')) { b.click(); return true; }
            }
            return false;
        """)
        if joined:
            time.sleep(3)
            cprint(Fore.GREEN, "OK", "Joined!")
            return True
        return False
    except Exception as e:
        cprint(Fore.RED, "!!", f"Join error: {e}")
        return False


# ============================================================
# HEALTH SERVER
# ============================================================
monitor_status = {
    "started": None, "last_check": None, "checks": 0,
    "link_changes": 0, "current_link": "", "status": "starting",
    "errors": 0, "last_error": "",
}
link_history_log = []  # In-memory log: [{time, old, new}, ...]


DASHBOARD_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Cursor Monitor</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="10">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0d1117;color:#e6edf3;font-family:'Segoe UI',sans-serif;padding:20px}}
h1{{color:#58a6ff;margin-bottom:20px;font-size:24px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:20px;margin-bottom:16px}}
.card h2{{color:#8b949e;font-size:14px;text-transform:uppercase;margin-bottom:12px}}
.stat-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}}
.stat{{background:#21262d;border-radius:8px;padding:16px;text-align:center}}
.stat .val{{font-size:28px;font-weight:bold;color:#58a6ff}}
.stat .label{{font-size:12px;color:#8b949e;margin-top:4px}}
.stat.ok .val{{color:#3fb950}}
.stat.warn .val{{color:#d29922}}
.stat.err .val{{color:#f85149}}
.link-box{{background:#21262d;border-radius:8px;padding:16px;margin-top:12px;word-break:break-all}}
.link-box a{{color:#58a6ff;text-decoration:none;font-size:16px}}
.link-box a:hover{{text-decoration:underline}}
.link-box .time{{color:#8b949e;font-size:12px;margin-top:4px}}
table{{width:100%;border-collapse:collapse;margin-top:12px}}
th{{text-align:left;color:#8b949e;font-size:12px;padding:8px;border-bottom:1px solid #30363d}}
td{{padding:8px;border-bottom:1px solid #21262d;font-size:13px}}
td a{{color:#58a6ff;text-decoration:none}}
.badge{{display:inline-block;padding:4px 10px;border-radius:12px;font-size:12px;font-weight:600}}
.badge.running{{background:#0d2818;color:#3fb950}}
.badge.starting{{background:#2d2200;color:#d29922}}
.badge.error{{background:#2d0f0f;color:#f85149}}
.badge.login{{background:#1a1a3e;color:#bc8cff}}
</style></head><body>
<h1>Cursor Invite Link Monitor</h1>

<div class="card">
<h2>Status</h2>
<div class="stat-grid">
<div class="stat ok"><div class="val">{status_badge}</div><div class="label">Status</div></div>
<div class="stat"><div class="val">{checks}</div><div class="label">Checks Done</div></div>
<div class="stat {changes_class}"><div class="val">{link_changes}</div><div class="label">Link Changes</div></div>
<div class="stat {err_class}"><div class="val">{errors}</div><div class="label">Errors</div></div>
</div>
</div>

<div class="card">
<h2>Current Invite Link</h2>
<div class="link-box">
{current_link_html}
<div class="time">Last checked: {last_check}</div>
</div>
</div>

<div class="card">
<h2>Link Change History</h2>
{history_html}
</div>

<div class="card">
<h2>Details</h2>
<div class="stat-grid">
<div class="stat"><div class="val" style="font-size:14px">{started}</div><div class="label">Started</div></div>
<div class="stat"><div class="val" style="font-size:14px">{last_error_short}</div><div class="label">Last Error</div></div>
</div>
</div>

<p style="color:#8b949e;font-size:11px;margin-top:20px;text-align:center">Auto-refreshes every 10 seconds | <a href="/api" style="color:#58a6ff">JSON API</a></p>
</body></html>"""


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api" or self.path == "/health" or self.path == "/ping":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            data = {**monitor_status, "history": link_history_log[-20:]}
            self.wfile.write(json.dumps(data, indent=2, default=str).encode())
        else:
            # Dashboard HTML
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            try:
                s = monitor_status
                status_text = s["status"]
                if "running" in status_text:
                    badge = '<span class="badge running">RUNNING</span>'
                elif "login" in status_text:
                    badge = f'<span class="badge login">{status_text}</span>'
                elif "error" in status_text or "fail" in status_text:
                    badge = f'<span class="badge error">{status_text}</span>'
                else:
                    badge = f'<span class="badge starting">{status_text}</span>'

                link = s.get("current_link", "")
                if link:
                    link_html = f'<a href="{link}">{link}</a>'
                else:
                    link_html = '<span style="color:#8b949e">Not yet extracted...</span>'

                last_check = s.get("last_check", "Never")
                if last_check and last_check != "Never":
                    last_check = last_check[:19].replace("T", " ")

                # History table
                history = link_history_log[-20:]
                if history:
                    rows = ""
                    for h in reversed(history):
                        ts = h.get("time", "")[:19]
                        old = h.get("old", "")
                        new = h.get("new", "")
                        rows += f'<tr><td>{ts}</td><td><a href="{old}">{old[-20:]}</a></td><td><a href="{new}">{new[-20:]}</a></td></tr>'
                    hist_html = f'<table><tr><th>Time</th><th>Old Link</th><th>New Link</th></tr>{rows}</table>'
                else:
                    hist_html = '<p style="color:#8b949e;padding:12px">No changes detected yet</p>'

                started = (s.get("started") or "")[:19].replace("T", " ")
                last_err = s.get("last_error", "None")
                last_err_short = (last_err[:50] + "...") if len(last_err) > 50 else last_err

                html = DASHBOARD_HTML.format(
                    status_badge=badge,
                    checks=s["checks"],
                    link_changes=s["link_changes"],
                    changes_class="warn" if s["link_changes"] > 0 else "",
                    errors=s["errors"],
                    err_class="err" if s["errors"] > 0 else "",
                    current_link_html=link_html,
                    last_check=last_check,
                    history_html=hist_html,
                    started=started,
                    last_error_short=last_err_short,
                )
                self.wfile.write(html.encode())
            except Exception as e:
                err_html = f'<html><body style="background:#0d1117;color:#f85149;padding:40px;font-family:monospace"><h1>Dashboard Error</h1><pre>{e}</pre></body></html>'
                self.wfile.write(err_html.encode())

    def log_message(self, *a):
        pass


def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    cprint(Fore.CYAN, ">>", f"Health server on :{port}")
    threading.Thread(target=server.serve_forever, daemon=True).start()


# ============================================================
# MONITOR LOOP
# ============================================================
def monitor_account(account, cfg):
    name = account.get("name", account["cursor_email"])
    email = account["cursor_email"]
    password = account["cursor_password"]
    known_link = account.get("known_invite_link", "")
    interval = cfg.get("check_interval_seconds", 5)
    history = load_history()

    cprint(Fore.CYAN, "==", f"Monitor: {name} | {interval}s interval")

    while True:
        driver = None
        try:
            monitor_status["status"] = "creating_browser"
            driver = create_browser()

            monitor_status["status"] = "logging_in"
            for attempt in range(5):
                if login_to_cursor(driver, email, password):
                    break
                monitor_status["status"] = f"login_retry_{attempt+1}"
                cprint(Fore.YELLOW, "..", f"Retry {attempt+1}/5 in 10s...")
                time.sleep(10)
            else:
                monitor_status["status"] = "login_failed"
                monitor_status["errors"] += 1
                quit_browser(driver)
                time.sleep(60)
                continue

            status, detail = check_team_status(driver)
            cprint(Fore.CYAN, ">>", f"Status: {status} ({detail})")

            if status in ("removed", "free_plan"):
                cprint(Fore.RED, "!!", "REMOVED!")
                send_email(cfg, f"REMOVED - {name}",
                    f"<h2>Removed!</h2><p>{email}</p><p>Trying: {known_link}</p>")
                if known_link:
                    auto_join_invite(driver, known_link)
                    time.sleep(3)
                    status, _ = check_team_status(driver)

            if status == "active":
                current = extract_invite_link(driver)
                if current and current != known_link:
                    known_link = current
                    account["known_invite_link"] = current
                    save_config(cfg)

            monitor_status["status"] = "running"
            monitor_status["current_link"] = known_link
            check_count = 0
            fails = 0
            last_relogin = time.time()
            last_status = time.time()

            while True:
                time.sleep(interval)
                check_count += 1
                try:
                    if time.time() - last_status > 120:
                        st, dt = check_team_status(driver)
                        last_status = time.time()
                        if st == "logged_out":
                            login_to_cursor(driver, email, password)
                            last_relogin = time.time()
                            continue
                        if st in ("removed", "free_plan"):
                            send_email(cfg, f"REMOVED - {name}",
                                f"<h2>Removed!</h2><p>{email}</p>")
                            if known_link:
                                auto_join_invite(driver, known_link)
                            login_to_cursor(driver, email, password)
                            last_relogin = time.time()
                            continue

                    if time.time() - last_relogin > 1500:
                        login_to_cursor(driver, email, password)
                        last_relogin = time.time()

                    close_modal(driver)
                    driver.get(MEMBERS_URL)
                    time.sleep(2)
                    solve_cloudflare(driver, "refresh")

                    new_link = extract_invite_link(driver)

                    if new_link is None:
                        fails += 1
                        if fails >= 5:
                            st, _ = check_team_status(driver)
                            if st == "logged_out":
                                login_to_cursor(driver, email, password)
                                last_relogin = time.time()
                            elif st in ("removed", "free_plan"):
                                if known_link:
                                    auto_join_invite(driver, known_link)
                                login_to_cursor(driver, email, password)
                                last_relogin = time.time()
                            fails = 0
                        continue

                    fails = 0
                    monitor_status["last_check"] = datetime.now().isoformat()
                    monitor_status["checks"] = check_count
                    monitor_status["current_link"] = new_link

                    if new_link != known_link and known_link:
                        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
                        cprint(Fore.GREEN, "!!", "=" * 50)
                        cprint(Fore.GREEN, "!!", f"LINK CHANGED at {now}")
                        cprint(Fore.RED, "<<", f"OLD: {known_link}")
                        cprint(Fore.GREEN, ">>", f"NEW: {new_link}")
                        cprint(Fore.GREEN, "!!", "=" * 50)

                        record = {"timestamp": now, "account": email,
                                  "old_link": known_link, "new_link": new_link,
                                  "check_number": check_count}
                        history.append(record)
                        save_history(history)
                        monitor_status["link_changes"] += 1
                        link_history_log.append({
                            "time": now, "old": known_link, "new": new_link
                        })

                        known_link = new_link
                        account["known_invite_link"] = new_link
                        save_config(cfg)

                        send_email(cfg, f"LINK CHANGED - {name}",
                            f"<h2>Link Changed!</h2>"
                            f"<table border='1' cellpadding='8'>"
                            f"<tr><td><b>Time</b></td><td>{now}</td></tr>"
                            f"<tr><td><b>Old</b></td><td>{record['old_link']}</td></tr>"
                            f"<tr><td><b>New</b></td><td><a href='{new_link}'>{new_link}</a></td></tr>"
                            f"</table><br>"
                            f"<a href='{new_link}' style='background:#4CAF50;color:white;padding:12px 24px;"
                            f"text-decoration:none;border-radius:5px;'>Join Now</a>")

                        if account.get("auto_join", True):
                            auto_join_invite(driver, new_link)

                    elif not known_link and new_link:
                        known_link = new_link
                        account["known_invite_link"] = new_link
                        save_config(cfg)

                    if check_count % 100 == 0:
                        cprint(Fore.CYAN, ">>", f"#{check_count}: OK")

                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    cprint(Fore.RED, "!!", f"#{check_count}: {e}")
                    monitor_status["last_error"] = str(e)
                    monitor_status["errors"] += 1
                    fails += 1

        except KeyboardInterrupt:
            cprint(Fore.YELLOW, "!!", "Stopped")
            break
        except Exception as e:
            cprint(Fore.RED, "!!", f"Fatal: {e} - restart 30s...")
            monitor_status["last_error"] = str(e)
            monitor_status["errors"] += 1
            time.sleep(30)
        finally:
            if driver:
                quit_browser(driver)


def main():
    print(f"\n{Fore.CYAN}{'='*60}")
    print(f"{Fore.CYAN}  CURSOR INVITE LINK MONITOR v6")
    print(f"{Fore.CYAN}  SeleniumBase UC | Xvfb Docker | Auto-Rejoin")
    print(f"{Fore.CYAN}{'='*60}\n")

    start_health_server()
    monitor_status["started"] = datetime.now().isoformat()

    cfg = load_config()
    accounts = [a for a in cfg.get("accounts", []) if a.get("enabled", True)]

    if not accounts:
        cprint(Fore.RED, "!!", "No accounts!")
        return

    if len(accounts) == 1:
        monitor_account(accounts[0], cfg)
    else:
        threads = []
        for acc in accounts:
            t = threading.Thread(target=monitor_account, args=(acc, cfg), daemon=True)
            t.start()
            threads.append(t)
            time.sleep(3)
        try:
            for t in threads:
                t.join()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
