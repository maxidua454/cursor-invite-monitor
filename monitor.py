"""
CURSOR INVITE LINK MONITOR v3
- SeleniumBase UC mode (Cloudflare/Turnstile auto-bypass via reconnect trick)
- Multi-account support (monitor multiple teams)
- Detects removal → auto-rejoin with last known link
- Detects logout → auto re-login
- Email notifications on any change
- Full history logging (old link, new link, timestamp)
- Self-healing: browser crash, network error → auto-restart
- Never dies: outer restart loop catches everything
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

# Paths
BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
HISTORY_PATH = BASE_DIR / "link_history.json"
LOG_FILE = BASE_DIR / "monitor.log"

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("cursor-monitor")

# Auth
AUTH_URL = "https://authenticator.cursor.sh/"
DASHBOARD_URL = "https://cursor.com/dashboard"
MEMBERS_URL = "https://cursor.com/dashboard/members"
API_ME = "https://cursor.com/api/auth/me"
API_STRIPE = "https://cursor.com/api/auth/stripe"


def cprint(color, symbol, msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{color}{ts} [{symbol}] {msg}{Style.RESET_ALL}")
    log.info(f"[{symbol}] {msg}")


# ============================================================
# CONFIG & HISTORY
# ============================================================
def load_config():
    # If config.json doesn't exist, create from env vars (for cloud deploy)
    if not CONFIG_PATH.exists():
        cprint(Fore.YELLOW, "!!", "No config.json found, building from environment variables...")
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
            "headless": True,
        }
        save_config(cfg)
        return cfg

    with open(CONFIG_PATH, "r") as f:
        cfg = json.load(f)
    # Env var overrides
    env_map = {
        "NOTIFICATION_EMAIL": "notification_email",
        "GMAIL_APP_PASSWORD": "gmail_app_password",
        "CHECK_INTERVAL": "check_interval_seconds",
    }
    for env_key, cfg_key in env_map.items():
        val = os.environ.get(env_key)
        if val:
            cfg[cfg_key] = int(val) if cfg_key == "check_interval_seconds" else val
    # Override account credentials from env if present
    cursor_email = os.environ.get("CURSOR_EMAIL")
    cursor_password = os.environ.get("CURSOR_PASSWORD")
    if cursor_email and cfg.get("accounts"):
        cfg["accounts"][0]["cursor_email"] = cursor_email
    if cursor_password and cfg.get("accounts"):
        cfg["accounts"][0]["cursor_password"] = cursor_password
    known = os.environ.get("KNOWN_INVITE_LINK")
    if known and cfg.get("accounts"):
        cfg["accounts"][0]["known_invite_link"] = known
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
    """Send HTML email via Gmail SMTP."""
    email_addr = cfg.get("notification_email", "")
    app_pw = cfg.get("gmail_app_password", "")
    if not email_addr or not app_pw or app_pw == "NEED_APP_PASSWORD":
        cprint(Fore.YELLOW, "!!", "Email not configured - skipping notification")
        cprint(Fore.YELLOW, "!!", f"SUBJECT: {subject}")
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
# BROWSER HELPERS (SeleniumBase UC mode)
# ============================================================
IS_DOCKER = os.path.exists("/.dockerenv") or os.environ.get("RENDER", "")


def create_browser(headless=True):
    """Create a SeleniumBase undetected Chrome browser."""
    cprint(Fore.CYAN, ">>", f"Creating browser (headless={headless}, docker={IS_DOCKER})...")
    if IS_DOCKER:
        driver = Driver(
            uc=True, headless=True,
            uc_cdp_events=True,
            chromium_arg="--no-sandbox,--disable-dev-shm-usage,--disable-gpu,--disable-software-rasterizer",
        )
    else:
        driver = Driver(uc=True, headless=headless)
    cprint(Fore.GREEN, "OK", "Browser ready")
    return driver


def quit_browser(driver):
    try:
        driver.quit()
    except Exception:
        pass


def solve_cloudflare(driver, context="page"):
    """
    Handle Cloudflare / Turnstile using the reconnect trick.
    Disconnects CDP so CF can't detect automation, waits, reconnects.
    """
    for attempt in range(3):
        try:
            is_challenge = driver.execute_script("""
                return document.title.includes('Just a moment')
                    || document.querySelector('#challenge-running') !== null
                    || document.querySelector('#challenge-stage') !== null
                    || document.querySelector('.cf-browser-verification') !== null
                    || document.querySelector('iframe[src*="turnstile"]') !== null
                    || document.querySelector('[class*="turnstile"]') !== null
                    || document.querySelector('#cf-turnstile') !== null;
            """)

            if not is_challenge:
                return True  # No challenge

            cprint(Fore.YELLOW, "CF", f"Turnstile on {context} (attempt {attempt+1}/3) - reconnect trick...")
            try:
                driver.reconnect(8)
            except Exception as re:
                cprint(Fore.YELLOW, "..", f"reconnect failed: {re}, waiting...")
                time.sleep(8)
            time.sleep(1)

            still_blocked = driver.execute_script("""
                return document.title.includes('Just a moment')
                    || document.querySelector('#challenge-running') !== null;
            """)

            if not still_blocked:
                cprint(Fore.GREEN, "OK", "Cloudflare bypassed!")
                return True

            # Check for token
            token = driver.execute_script("""
                var inp = document.querySelector('input[name="cf-turnstile-response"]');
                if (inp && inp.value && inp.value.length > 20) return true;
                var ta = document.querySelector('textarea[name="cf-turnstile-response"]');
                if (ta && ta.value && ta.value.length > 20) return true;
                return false;
            """)
            if token:
                cprint(Fore.GREEN, "OK", "Turnstile solved!")
                return True

            cprint(Fore.YELLOW, "..", f"Attempt {attempt+1} didn't clear, trying longer...")
            try:
                driver.reconnect(12)
            except Exception:
                time.sleep(12)
            time.sleep(1)

        except Exception as e:
            cprint(Fore.YELLOW, "..", f"CF solve error: {str(e)[:60]}")
            time.sleep(1)

    cprint(Fore.RED, "!!", "Could not bypass Cloudflare after 3 attempts")
    return False


def wait_for_any(driver, selectors, timeout=15):
    """Wait for any selector to appear. Returns matched selector or None."""
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
    """
    Full login flow via authenticator.cursor.sh:
    1. Open auth page
    2. Enter email → Continue
    3. Enter password → Sign In
    4. Handle Cloudflare/Turnstile
    5. Wait for dashboard redirect
    """
    cprint(Fore.CYAN, ">>", f"Logging in as {email}...")

    try:
        # Step 1: Open auth page
        try:
            driver.uc_open_with_reconnect(AUTH_URL, reconnect_time=6)
        except Exception as e:
            cprint(Fore.YELLOW, "..", f"uc_open failed ({e}), using regular get...")
            driver.get(AUTH_URL)
        time.sleep(3)

        # Handle CF on landing
        if not solve_cloudflare(driver, "login landing"):
            cprint(Fore.RED, "!!", "CF blocked on landing page")
            return False

        # Step 2: Enter email
        cprint(Fore.WHITE, "->", "Entering email...")
        email_sel = wait_for_any(driver, [
            'input[name="email"]',
            'input[type="email"]',
            'input[placeholder*="email" i]',
        ], timeout=15)

        if not email_sel:
            # Maybe CF page, try again
            solve_cloudflare(driver, "email page")
            email_sel = wait_for_any(driver, [
                'input[name="email"]', 'input[type="email"]',
            ], timeout=10)

        if not email_sel:
            cprint(Fore.RED, "!!", "Email field not found")
            return False

        driver.type(email_sel, email)
        cprint(Fore.GREEN, "OK", f"Email entered: {email}")

        # Step 3: Click Continue
        continue_sel = wait_for_any(driver, [
            'button[type="submit"]',
            'button:contains("Continue")',
        ], timeout=5)

        if continue_sel:
            driver.click(continue_sel)
        else:
            driver.execute_script("""
                var btns = document.querySelectorAll('button');
                for (var b of btns) {
                    if (b.textContent.includes('Continue')) { b.click(); break; }
                }
            """)

        time.sleep(2)

        # Step 4: Enter password
        cprint(Fore.WHITE, "->", "Entering password...")
        pw_sel = wait_for_any(driver, [
            'input[name="password"]',
            'input[type="password"]',
        ], timeout=15)

        if not pw_sel:
            cprint(Fore.RED, "!!", "Password field not found")
            return False

        driver.type(pw_sel, password)
        cprint(Fore.GREEN, "OK", "Password entered")

        # Step 5: Click Sign In
        signin_sel = wait_for_any(driver, [
            'button[type="submit"]',
            'button:contains("Sign in")',
            'button:contains("Sign In")',
        ], timeout=5)

        if signin_sel:
            driver.click(signin_sel)
        else:
            driver.execute_script("""
                var btns = document.querySelectorAll('button');
                for (var b of btns) {
                    if (b.textContent.includes('Sign in') || b.textContent.includes('Sign In')) {
                        b.click(); break;
                    }
                }
            """)

        cprint(Fore.GREEN, "OK", "Sign In clicked")

        # Step 6: Handle post-signin Turnstile
        time.sleep(2)
        solve_cloudflare(driver, "post-signin")

        # Re-click Sign In if still on auth page
        try:
            if "authenticator" in driver.current_url:
                driver.execute_script("""
                    var btns = document.querySelectorAll('button');
                    for (var b of btns) {
                        if (b.textContent.includes('Sign in')) { b.click(); break; }
                    }
                """)
                time.sleep(2)
        except Exception:
            pass

        # Step 7: Wait for dashboard redirect
        cprint(Fore.WHITE, "->", "Waiting for dashboard...")
        for wait_sec in range(40):
            try:
                url = driver.current_url
            except Exception:
                time.sleep(1)
                continue

            if "cursor.com" in url and "authenticator" not in url:
                cprint(Fore.GREEN, "OK", f"Login successful! URL: {url}")
                return True

            # Check for CF during redirect
            if wait_sec > 0 and wait_sec % 5 == 0:
                try:
                    is_cf = driver.execute_script(
                        "return document.title.includes('Just a moment');"
                    )
                    if is_cf:
                        cprint(Fore.YELLOW, "CF", "CF during redirect, reconnecting...")
                        driver.reconnect(6)
                except Exception:
                    pass

            time.sleep(1)

        # Check for error messages
        try:
            src = driver.get_page_source().lower()
            if "incorrect" in src or "invalid" in src or "wrong" in src:
                cprint(Fore.RED, "!!", "Invalid email or password!")
                return False
        except Exception:
            pass

        cprint(Fore.RED, "!!", f"Login timeout (stuck at: {driver.current_url[:60]})")
        return False

    except Exception as e:
        cprint(Fore.RED, "!!", f"Login error: {e}")
        return False


# ============================================================
# TEAM STATUS CHECK
# ============================================================
def check_team_status(driver):
    """
    Check if we're still on the team.
    Returns: (status, detail)
      "active"     - on team, dashboard accessible
      "removed"    - kicked from team
      "logged_out" - session expired
      "free_plan"  - no longer on team plan
    """
    try:
        driver.get(DASHBOARD_URL)
        time.sleep(3)

        url = driver.current_url

        # Redirected to login?
        if "authenticator" in url or "login" in url:
            return "logged_out", "Redirected to login"

        # Check page content
        try:
            page_text = driver.execute_script("return document.body.innerText;")
        except Exception:
            return "logged_out", "Cannot read page"

        if "Team Plan" in page_text or "Team plan" in page_text:
            return "active", "Team Plan detected"

        if "Free" in page_text and "Plan" in page_text:
            return "free_plan", "Free plan - may have been removed"

        # Try members page
        driver.get(MEMBERS_URL)
        time.sleep(2)

        url = driver.current_url
        if "members" not in url:
            return "removed", f"Redirected away from members: {url[:50]}"

        try:
            has_invite = driver.execute_script("""
                var btns = document.querySelectorAll('button');
                for (var b of btns) {
                    if (b.textContent.includes('Invite')) return true;
                }
                return false;
            """)
            if has_invite:
                return "active", "Members page with Invite button"
        except Exception:
            pass

        return "active", "Members page accessible"

    except Exception as e:
        return "logged_out", str(e)


# ============================================================
# INVITE LINK EXTRACTION
# ============================================================
def extract_invite_link(driver):
    """
    Go to members page, click Invite, click "Copy Invite Link",
    and capture the link via clipboard interception.
    """
    try:
        # Navigate to members page
        if "members" not in driver.current_url:
            driver.get(MEMBERS_URL)
            time.sleep(3)
            solve_cloudflare(driver, "members page")

        # Click Invite button
        cprint(Fore.WHITE, "->", "Clicking Invite button...")
        invite_clicked = driver.execute_script("""
            var btns = document.querySelectorAll('button');
            for (var b of btns) {
                if (b.textContent.trim() === 'Invite' ||
                    b.textContent.includes('Invite')) {
                    b.click();
                    return true;
                }
            }
            // Also check <a> tags
            var links = document.querySelectorAll('a');
            for (var a of links) {
                if (a.textContent.includes('Invite')) {
                    a.click();
                    return true;
                }
            }
            return false;
        """)

        if not invite_clicked:
            cprint(Fore.RED, "!!", "Invite button not found")
            return None

        time.sleep(1.5)

        # Method 1: Intercept clipboard when "Copy Invite Link" is clicked
        cprint(Fore.WHITE, "->", "Clicking 'Copy Invite Link'...")
        link = driver.execute_script("""
            return new Promise((resolve, reject) => {
                // Override clipboard
                const orig = navigator.clipboard.writeText.bind(navigator.clipboard);
                navigator.clipboard.writeText = async (text) => {
                    resolve(text);
                    navigator.clipboard.writeText = orig;
                    return orig(text);
                };
                // Click the button
                var btns = document.querySelectorAll('button');
                var found = false;
                for (var b of btns) {
                    if (b.textContent.includes('Copy Invite Link') ||
                        b.textContent.includes('Copy invite link') ||
                        b.textContent.includes('Copy Invite')) {
                        b.click();
                        found = true;
                        break;
                    }
                }
                if (!found) reject(new Error('Copy button not found'));
                setTimeout(() => reject(new Error('clipboard timeout')), 5000);
            });
        """)

        if link and "cursor.com" in link:
            cprint(Fore.GREEN, "OK", f"Got link: {link}")
            return link.strip()

    except Exception as e:
        cprint(Fore.YELLOW, "!!", f"Clipboard method failed: {e}")

    # Method 2: Search page HTML for invite URL
    try:
        src = driver.get_page_source()
        match = re.search(
            r"https://cursor\.com/team/accept-invite\?code=[a-f0-9]+", src
        )
        if match:
            link = match.group(0)
            cprint(Fore.GREEN, "OK", f"Got link (HTML): {link}")
            return link
    except Exception as e:
        cprint(Fore.YELLOW, "!!", f"HTML search failed: {e}")

    # Method 3: Walk DOM
    try:
        link = driver.execute_script("""
            var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            while (walker.nextNode()) {
                var m = walker.currentNode.textContent.match(
                    /https:\\/\\/cursor\\.com\\/team\\/accept-invite\\?code=[a-f0-9]+/
                );
                if (m) return m[0];
            }
            return null;
        """)
        if link:
            cprint(Fore.GREEN, "OK", f"Got link (DOM): {link}")
            return link
    except Exception as e:
        cprint(Fore.YELLOW, "!!", f"DOM walk failed: {e}")

    # Method 4: Check for link in any href attributes
    try:
        link = driver.execute_script("""
            var anchors = document.querySelectorAll('a[href*="accept-invite"]');
            if (anchors.length > 0) return anchors[0].href;
            return null;
        """)
        if link:
            cprint(Fore.GREEN, "OK", f"Got link (href): {link}")
            return link
    except Exception:
        pass

    cprint(Fore.RED, "!!", "Could not extract invite link")
    return None


def close_modal(driver):
    """Close the invite modal if open."""
    try:
        driver.execute_script("""
            // Click X/close button
            var closeBtns = document.querySelectorAll(
                'button[aria-label="Close"], [class*="close"], button svg'
            );
            for (var b of closeBtns) {
                var el = b.closest('button');
                if (el) { el.click(); return; }
            }
            // Click outside modal (backdrop)
            var backdrops = document.querySelectorAll('[class*="backdrop"], [class*="overlay"]');
            for (var bd of backdrops) { bd.click(); return; }
        """)
        time.sleep(0.5)
    except Exception:
        pass


# ============================================================
# AUTO-JOIN
# ============================================================
def auto_join_invite(driver, email, password, link):
    """Accept invite link. Already logged in, just visit the link."""
    cprint(Fore.CYAN, ">>", f"Auto-joining: {link}")
    try:
        driver.get(link)
        time.sleep(3)
        solve_cloudflare(driver, "invite page")

        page_text = driver.execute_script(
            "return document.body.innerText;"
        ) or ""

        # Already a member?
        if "already" in page_text.lower() and "member" in page_text.lower():
            cprint(Fore.GREEN, "OK", "Already a member!")
            return True

        # Click accept/join
        joined = driver.execute_script("""
            var btns = document.querySelectorAll('button');
            for (var b of btns) {
                var t = b.textContent.toLowerCase();
                if (t.includes('accept') || t.includes('join')) {
                    b.click();
                    return true;
                }
            }
            return false;
        """)

        if joined:
            time.sleep(3)
            cprint(Fore.GREEN, "OK", "Clicked accept/join!")
            return True

        cprint(Fore.YELLOW, "!!", "No accept/join button found")
        return False

    except Exception as e:
        cprint(Fore.RED, "!!", f"Auto-join error: {e}")
        return False


# ============================================================
# MONITOR FOR ONE ACCOUNT
# ============================================================
def monitor_account(account, cfg):
    """Monitor a single account's invite link."""
    name = account.get("name", account["cursor_email"])
    email = account["cursor_email"]
    password = account["cursor_password"]
    known_link = account.get("known_invite_link", "")
    interval = cfg.get("check_interval_seconds", 5)
    headless = cfg.get("headless", True)
    history = load_history()

    cprint(Fore.CYAN, "==", f"Monitor starting for: {name}")
    cprint(Fore.CYAN, "==", f"Interval: {interval}s | Known link: {known_link[:50]}...")

    while True:  # Outer restart loop
        driver = None
        try:
            monitor_status["status"] = "creating_browser"
            driver = create_browser(headless=headless)

            # Login
            monitor_status["status"] = "logging_in"
            for attempt in range(5):
                if login_to_cursor(driver, email, password):
                    break
                cprint(Fore.YELLOW, "..", f"Login attempt {attempt+1}/5 failed, retrying in 10s...")
                monitor_status["status"] = f"login_retry_{attempt+1}"
                time.sleep(10)
            else:
                cprint(Fore.RED, "!!", "All login attempts failed. Retrying in 60s...")
                monitor_status["status"] = "login_failed"
                monitor_status["errors"] += 1
                quit_browser(driver)
                time.sleep(60)
                continue

            # Check team status
            status, detail = check_team_status(driver)
            cprint(Fore.CYAN, ">>", f"Team status: {status} ({detail})")

            if status in ("removed", "free_plan"):
                cprint(Fore.RED, "!!", "REMOVED from team! Trying to rejoin...")
                send_email(cfg,
                    f"REMOVED from Cursor Team - {name}",
                    f"<h2>Removed from team!</h2><p>Account: {email}</p>"
                    f"<p>Attempting rejoin with: {known_link}</p>")
                if known_link:
                    auto_join_invite(driver, email, password, known_link)
                    time.sleep(3)
                    status, detail = check_team_status(driver)
                    if status == "active":
                        cprint(Fore.GREEN, "OK", "Rejoined successfully!")
                        send_email(cfg,
                            f"REJOINED Cursor Team - {name}",
                            f"<h2>Rejoined!</h2><p>Account: {email}</p>")
                    else:
                        # Try all historical links
                        for rec in reversed(history):
                            link = rec.get("new_link") or rec.get("old_link", "")
                            if link and link != known_link:
                                auto_join_invite(driver, email, password, link)
                                time.sleep(2)
                                status, _ = check_team_status(driver)
                                if status == "active":
                                    break

            # Get initial invite link
            if status == "active":
                cprint(Fore.WHITE, "->", "Getting initial invite link...")
                current_link = extract_invite_link(driver)
                if current_link:
                    if current_link != known_link:
                        cprint(Fore.YELLOW, "!!", f"Link in config differs! Updating...")
                        known_link = current_link
                        account["known_invite_link"] = current_link
                        save_config(cfg)
                else:
                    cprint(Fore.YELLOW, "!!", "Could not get initial link")

            # ===== MAIN MONITOR LOOP =====
            check_count = 0
            consecutive_fails = 0
            last_relogin = time.time()
            last_status_check = time.time()

            while True:
                time.sleep(interval)
                check_count += 1

                try:
                    # Status check every 2 minutes
                    if time.time() - last_status_check > 120:
                        status, detail = check_team_status(driver)
                        last_status_check = time.time()

                        if status == "logged_out":
                            cprint(Fore.YELLOW, "!!", "Session expired! Re-logging in...")
                            login_to_cursor(driver, email, password)
                            last_relogin = time.time()
                            continue

                        if status in ("removed", "free_plan"):
                            cprint(Fore.RED, "!!", f"STATUS: {status}")
                            send_email(cfg,
                                f"REMOVED - {name}",
                                f"<h2>Removed from team!</h2>"
                                f"<p>Account: {email}</p>"
                                f"<p>Status: {status}</p>"
                                f"<p>Trying to rejoin with: {known_link}</p>")

                            # Try rejoin
                            if known_link:
                                auto_join_invite(driver, email, password, known_link)
                                time.sleep(3)
                                new_status, _ = check_team_status(driver)
                                if new_status == "active":
                                    cprint(Fore.GREEN, "OK", "Rejoined!")
                                    send_email(cfg, f"REJOINED - {name}",
                                        f"<h2>Rejoined team!</h2><p>{email}</p>")
                                else:
                                    # Keep trying every 10 seconds
                                    cprint(Fore.YELLOW, "..", "Rejoin failed, will keep trying...")
                                    rejoin_tries = 0
                                    while rejoin_tries < 30:
                                        time.sleep(10)
                                        rejoin_tries += 1
                                        # Reload config in case link updated externally
                                        cfg = load_config()
                                        for acc in cfg["accounts"]:
                                            if acc["cursor_email"] == email:
                                                known_link = acc.get("known_invite_link", known_link)
                                        if auto_join_invite(driver, email, password, known_link):
                                            s, _ = check_team_status(driver)
                                            if s == "active":
                                                send_email(cfg, f"REJOINED - {name}",
                                                    f"<h2>Rejoined!</h2><p>{email}</p>")
                                                break
                            continue

                    # Refresh session every 25 min
                    if time.time() - last_relogin > 1500:
                        cprint(Fore.CYAN, ">>", "Refreshing session...")
                        login_to_cursor(driver, email, password)
                        last_relogin = time.time()

                    # Reload members page and extract link
                    close_modal(driver)
                    driver.get(MEMBERS_URL)
                    time.sleep(2)

                    # Handle CF if it appears
                    solve_cloudflare(driver, "members refresh")

                    new_link = extract_invite_link(driver)

                    if new_link is None:
                        consecutive_fails += 1
                        if consecutive_fails % 3 == 0:
                            cprint(Fore.YELLOW, "!!", f"Failed {consecutive_fails} times")
                        if consecutive_fails >= 5:
                            status, _ = check_team_status(driver)
                            if status == "logged_out":
                                login_to_cursor(driver, email, password)
                                last_relogin = time.time()
                            elif status in ("removed", "free_plan"):
                                if known_link:
                                    auto_join_invite(driver, email, password, known_link)
                                    login_to_cursor(driver, email, password)
                                    last_relogin = time.time()
                            consecutive_fails = 0
                        continue

                    consecutive_fails = 0

                    # Update health status
                    monitor_status["last_check"] = datetime.now().isoformat()
                    monitor_status["checks"] = check_count
                    monitor_status["current_link"] = new_link
                    monitor_status["status"] = "running"

                    # ===== LINK CHANGED! =====
                    if new_link != known_link and known_link:
                        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
                        cprint(Fore.GREEN, "!!", "=" * 50)
                        cprint(Fore.GREEN, "!!", f"LINK CHANGED at {now}")
                        cprint(Fore.RED, "<<", f"OLD: {known_link}")
                        cprint(Fore.GREEN, ">>", f"NEW: {new_link}")
                        cprint(Fore.GREEN, "!!", "=" * 50)

                        # Save history
                        record = {
                            "timestamp": now,
                            "account": email,
                            "old_link": known_link,
                            "new_link": new_link,
                            "check_number": check_count,
                        }
                        history.append(record)
                        save_history(history)
                        monitor_status["link_changes"] += 1

                        # Update config
                        known_link = new_link
                        account["known_invite_link"] = new_link
                        save_config(cfg)

                        # Email
                        send_email(cfg,
                            f"LINK CHANGED - {name} - {now}",
                            f"""
                            <h2>Cursor Invite Link Changed!</h2>
                            <table border="1" cellpadding="8" style="border-collapse:collapse;">
                                <tr><td><b>Account</b></td><td>{email}</td></tr>
                                <tr><td><b>Time</b></td><td>{now}</td></tr>
                                <tr><td><b>Old Link</b></td><td>{record['old_link']}</td></tr>
                                <tr><td><b>New Link</b></td><td><a href="{new_link}">{new_link}</a></td></tr>
                                <tr><td><b>Check #</b></td><td>{check_count}</td></tr>
                            </table>
                            <br>
                            <a href="{new_link}" style="background:#4CAF50;color:white;padding:12px 24px;text-decoration:none;border-radius:5px;">
                                Join with New Link
                            </a>
                            """)

                        # Auto-join
                        if account.get("auto_join", True):
                            auto_join_invite(driver, email, password, new_link)

                    elif not known_link and new_link:
                        # First time getting link
                        known_link = new_link
                        account["known_invite_link"] = new_link
                        save_config(cfg)
                        cprint(Fore.GREEN, "OK", f"Initial link saved: {new_link}")

                    # Heartbeat
                    if check_count % 100 == 0:
                        cprint(Fore.CYAN, ">>", f"Check #{check_count}: OK - link unchanged")

                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    cprint(Fore.RED, "!!", f"Check #{check_count} error: {e}")
                    consecutive_fails += 1

        except KeyboardInterrupt:
            cprint(Fore.YELLOW, "!!", "Stopped by user")
            break
        except Exception as e:
            cprint(Fore.RED, "!!", f"Fatal error: {e} - restarting in 30s...")
            time.sleep(30)
        finally:
            if driver:
                quit_browser(driver)


# ============================================================
# HEALTH SERVER (keeps Render alive + UptimeRobot ping target)
# ============================================================
monitor_status = {
    "started": None,
    "last_check": None,
    "checks": 0,
    "link_changes": 0,
    "current_link": "",
    "status": "starting",
    "errors": 0,
}


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            body = json.dumps(monitor_status, indent=2, default=str)
            self.wfile.write(body.encode())
        elif self.path == "/ping":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"pong")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress request logs


def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    cprint(Fore.CYAN, ">>", f"Health server on port {port} (UptimeRobot: /ping)")
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


# ============================================================
# MAIN
# ============================================================
def main():
    print()
    print(f"{Fore.CYAN}{'='*60}")
    print(f"{Fore.CYAN}  CURSOR INVITE LINK MONITOR v3")
    print(f"{Fore.CYAN}  SeleniumBase UC | Cloudflare Bypass | Auto-Rejoin")
    print(f"{Fore.CYAN}{'='*60}")
    print()

    # Start health server for UptimeRobot / Render
    start_health_server()
    monitor_status["started"] = datetime.now().isoformat()

    cfg = load_config()
    accounts = [a for a in cfg.get("accounts", []) if a.get("enabled", True)]

    if not accounts:
        cprint(Fore.RED, "!!", "No accounts configured in config.json!")
        return

    cprint(Fore.CYAN, ">>", f"Monitoring {len(accounts)} account(s)")

    if len(accounts) == 1:
        # Single account - run directly
        monitor_account(accounts[0], cfg)
    else:
        # Multiple accounts - run in parallel threads
        threads = []
        for acc in accounts:
            t = threading.Thread(
                target=monitor_account, args=(acc, cfg), daemon=True
            )
            t.start()
            threads.append(t)
            time.sleep(3)  # Stagger starts

        # Wait for all
        try:
            for t in threads:
                t.join()
        except KeyboardInterrupt:
            cprint(Fore.YELLOW, "!!", "Stopped by user")


if __name__ == "__main__":
    main()
