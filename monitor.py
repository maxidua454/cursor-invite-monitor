"""
CURSOR INVITE LINK MONITOR v7
- Pure HTTP monitoring (no browser, no Cloudflare issues)
- Session cookies from get_cookies.py (run locally once)
- Auto-detects link changes, removal, session expiry
- Email notifications
- Visual dashboard + JSON API
- Self-healing, never dies
"""

import sys
import os
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

try:
    import requests
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests"])
    import requests

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
COOKIE_PATH = BASE_DIR / "cookies.json"
HISTORY_PATH = BASE_DIR / "link_history.json"
LOG_FILE = BASE_DIR / "monitor.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("cursor-monitor")

DASHBOARD_URL = "https://cursor.com/dashboard"
MEMBERS_URL = "https://cursor.com/dashboard/members"
SETTINGS_URL = "https://cursor.com/dashboard/settings"
INVITE_LINK_API = "https://cursor.com/api/dashboard/get-team-invite-link"

# Browser-like headers to avoid basic blocks
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}


def cprint(symbol, msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{ts} [{symbol}] {msg}")
    log.info(f"[{symbol}] {msg}")


# ============================================================
# CONFIG & COOKIES
# ============================================================
def load_config():
    if not CONFIG_PATH.exists():
        cprint("!!", "No config.json, building from env vars...")
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
    # Override with env vars
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


def load_cookies():
    """Load session cookies from env var or file."""
    # Priority 1: env var SESSION_COOKIES (JSON string)
    env_cookies = os.environ.get("SESSION_COOKIES", "")
    if env_cookies:
        try:
            cookies = json.loads(env_cookies)
            cprint("OK", f"Loaded {len(cookies)} cookies from env var")
            return cookies
        except json.JSONDecodeError:
            cprint("!!", "SESSION_COOKIES env var is not valid JSON")

    # Priority 2: cookies.json file
    if COOKIE_PATH.exists():
        with open(COOKIE_PATH, "r") as f:
            cookies = json.load(f)
        cprint("OK", f"Loaded {len(cookies)} cookies from cookies.json")
        return cookies

    cprint("!!", "No cookies found! Run get_cookies.py locally first.")
    return {}


def save_cookies(cookies):
    with open(COOKIE_PATH, "w") as f:
        json.dump(cookies, f, indent=2)


def load_history():
    if HISTORY_PATH.exists():
        with open(HISTORY_PATH, "r") as f:
            return json.load(f)
    return []


def save_history(history):
    with open(HISTORY_PATH, "w") as f:
        json.dump(history, f, indent=4)


# ============================================================
# HTTP SESSION
# ============================================================
class CursorHTTP:
    """HTTP client for Cursor dashboard using session cookies."""

    def __init__(self, cookies):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.team_id = cookies.get("team_id", "")
        # Set cookies
        for name, value in cookies.items():
            self.session.cookies.set(name, value, domain=".cursor.com")
        self.valid = True

    def check_session(self):
        """Check if session is still valid."""
        try:
            resp = self.session.get(DASHBOARD_URL, allow_redirects=False, timeout=15)
            # If redirected to auth, session expired
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location", "")
                if "authenticator" in location or "login" in location:
                    cprint("!!", "Session expired (redirect to login)")
                    self.valid = False
                    return False, "session_expired"
                # Redirect to another cursor.com page is OK
                return True, "redirect"

            if resp.status_code == 403:
                cprint("!!", "403 Forbidden — cookies may be invalid or CF blocked")
                self.valid = False
                return False, "forbidden"

            if resp.status_code == 200:
                text = resp.text
                if "authenticator" in text and "Sign in" in text:
                    self.valid = False
                    return False, "session_expired"
                return True, "ok"

            cprint("!!", f"Unexpected status: {resp.status_code}")
            return False, f"status_{resp.status_code}"
        except Exception as e:
            cprint("!!", f"Session check error: {str(e)[:60]}")
            return False, str(e)[:60]

    def get_team_status(self):
        """Check team membership status."""
        try:
            resp = self.session.get(DASHBOARD_URL, timeout=15)
            if resp.status_code != 200:
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location", "")
                    if "authenticator" in location:
                        return "logged_out", "Session expired"
                return "error", f"HTTP {resp.status_code}"

            text = resp.text
            if "Team Plan" in text:
                return "active", "Team Plan"
            if "Free" in text and "Plan" in text:
                return "free_plan", "Free plan — may be removed"
            if "authenticator" in text:
                return "logged_out", "Session expired"
            return "active", "Dashboard accessible"
        except Exception as e:
            return "error", str(e)[:60]

    def get_invite_link(self):
        """Extract invite link from members page."""
        try:
            resp = self.session.get(MEMBERS_URL, timeout=15)
            if resp.status_code != 200:
                cprint("!!", f"Members page HTTP {resp.status_code}")
                return None, resp.status_code

            html = resp.text

            # Method 1: Regex search for invite link
            m = re.search(r'https://cursor\.com/team/accept-invite\?code=[a-f0-9]+', html)
            if m:
                return m.group(0), 200

            # Method 2: Look for invite code in JSON data / Next.js props
            m = re.search(r'"inviteCode"\s*:\s*"([a-f0-9]+)"', html)
            if m:
                return f"https://cursor.com/team/accept-invite?code={m.group(1)}", 200

            # Method 3: Look for code in any format
            m = re.search(r'accept-invite\?code=([a-f0-9]+)', html)
            if m:
                return f"https://cursor.com/team/accept-invite?code={m.group(1)}", 200

            # Method 4: Check Next.js __NEXT_DATA__ for invite info
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
            if m:
                try:
                    next_data = json.loads(m.group(1))
                    # Search recursively for invite code
                    data_str = json.dumps(next_data)
                    code_match = re.search(r'accept-invite\?code=([a-f0-9]+)', data_str)
                    if code_match:
                        return f"https://cursor.com/team/accept-invite?code={code_match.group(1)}", 200
                except:
                    pass

            # Check if we can see members at all
            if "Members" in html or "members" in html:
                cprint("..", "Members page loaded but no invite link found in HTML")
                cprint("..", "The link may only appear after clicking 'Invite' button")
                return None, 200
            elif "authenticator" in html or "Sign in" in html:
                cprint("!!", "Session expired")
                return None, 401
            else:
                cprint("..", f"Unknown page content (length={len(html)})")
                return None, 200

        except Exception as e:
            cprint("!!", f"Get invite link error: {str(e)[:60]}")
            return None, 0

    def get_invite_link_via_api(self):
        """Get invite link via Cursor's dashboard API.
        Returns (link, status) where status is 'ok', 'unauthorized', or 'error'.
        """
        if not self.team_id:
            cprint("!!", "No team_id in cookies, cannot call invite API")
            return None, "error"
        try:
            resp = self.session.post(
                INVITE_LINK_API,
                json={"teamId": self.team_id},
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Referer": MEMBERS_URL,
                    "Origin": "https://cursor.com",
                },
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                link = data.get("inviteLink", "")
                if link:
                    cprint("OK", f"Got invite link from API: {link}")
                    return link, "ok"
                cprint("..", "API returned 200 but no inviteLink field")
                return None, "ok"
            elif resp.status_code == 401:
                # Could be session expired OR removed from team
                error_msg = ""
                try:
                    error_msg = resp.json().get("error", {}).get("message", "")
                except:
                    pass
                cprint("!!", f"API 401 — {error_msg or 'session/team issue'}")
                self.valid = False
                return None, "unauthorized"
            else:
                cprint("..", f"Invite API returned {resp.status_code}")
                return None, "error"
        except Exception as e:
            cprint("!!", f"Invite API error: {str(e)[:60]}")
            return None, "error"

    def join_with_invite_link(self, invite_link):
        """Accept an invite link to rejoin the team. Returns (success, detail)."""
        if not invite_link:
            return False, "no_link"
        try:
            cprint(">>", f"Attempting to join: {invite_link}")
            resp = self.session.get(invite_link, timeout=15, allow_redirects=True)
            final_url = resp.url
            status = resp.status_code
            text = resp.text[:500]
            cprint(">>", f"Join response: HTTP {status} → {final_url}")

            if status == 200 and ("dashboard" in final_url or "Team Plan" in text):
                cprint("OK", "REJOIN SUCCESSFUL via GET redirect!")
                return True, "joined_via_get"

            # Some invite flows need a POST/accept action
            # Try POST to accept-invite endpoint
            resp2 = self.session.post(
                invite_link,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Referer": invite_link,
                    "Origin": "https://cursor.com",
                },
                json={},
                timeout=15,
            )
            cprint(">>", f"Join POST response: HTTP {resp2.status_code}")
            if resp2.status_code == 200:
                cprint("OK", "REJOIN SUCCESSFUL via POST!")
                return True, "joined_via_post"

            # Check if we're back on the team by testing the API
            link, api_status = self.get_invite_link_via_api()
            if link:
                cprint("OK", "REJOIN CONFIRMED — invite API works again!")
                return True, "confirmed_via_api"

            return False, f"http_{status}"
        except Exception as e:
            cprint("!!", f"Join error: {str(e)[:60]}")
            return False, str(e)[:60]


# ============================================================
# EMAIL
# ============================================================
def send_email(cfg, subject, body):
    email_addr = cfg.get("notification_email", "")
    app_pw = cfg.get("gmail_app_password", "")
    if not email_addr or not app_pw or app_pw == "NEED_APP_PASSWORD":
        cprint("!!", f"Email skip (not configured) | {subject}")
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
        cprint("OK", "Email sent!")
        return True
    except Exception as e:
        cprint("!!", f"Email failed: {e}")
        return False


# ============================================================
# HEALTH SERVER + DASHBOARD
# ============================================================
monitor_status = {
    "started": None, "last_check": None, "checks": 0,
    "link_changes": 0, "current_link": "", "status": "starting",
    "errors": 0, "last_error": "", "session_valid": False,
}
link_history_log = []

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
.badge.expired{{background:#2d0f0f;color:#f85149}}
.alert{{background:#2d0f0f;border:1px solid #f85149;border-radius:8px;padding:16px;margin-bottom:16px;color:#f85149}}
</style></head><body>
<h1>Cursor Invite Link Monitor v7</h1>

{alert_html}

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

<p style="color:#8b949e;font-size:11px;margin-top:20px;text-align:center">
v7 HTTP mode | Auto-refreshes every 10s | <a href="/api" style="color:#58a6ff">JSON API</a>
</p>
</body></html>"""


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/api", "/health", "/ping"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            data = {**monitor_status, "history": link_history_log[-20:]}
            self.wfile.write(json.dumps(data, indent=2, default=str).encode())
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            try:
                s = monitor_status
                status_text = s["status"]
                if "running" in status_text:
                    badge = '<span class="badge running">RUNNING</span>'
                elif "expired" in status_text:
                    badge = f'<span class="badge expired">SESSION EXPIRED</span>'
                elif "error" in status_text:
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
                    last_check = str(last_check)[:19].replace("T", " ")

                alert_html = ""
                if not s.get("session_valid", True):
                    alert_html = '<div class="alert"><strong>Session Expired!</strong> Run <code>python get_cookies.py</code> locally and update the SESSION_COOKIES env var on Render.</div>'

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
                    alert_html=alert_html,
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
    cprint(">>", f"Health server on :{port}")
    threading.Thread(target=server.serve_forever, daemon=True).start()


# ============================================================
# MONITOR LOOP
# ============================================================
def monitor_account(account, cfg, cookies):
    name = account.get("name", account.get("cursor_email", "Main"))
    known_link = account.get("known_invite_link", "")
    interval = cfg.get("check_interval_seconds", 5)
    history = load_history()

    cprint("==", f"Monitor: {name} | {interval}s interval | HTTP mode")

    http = CursorHTTP(cookies)

    # Initial session check
    monitor_status["status"] = "checking_session"
    valid, detail = http.check_session()
    if not valid:
        cprint("!!", f"Session invalid: {detail}")
        monitor_status["status"] = "session_expired"
        monitor_status["session_valid"] = False
        monitor_status["last_error"] = f"Session invalid: {detail}. Run get_cookies.py locally!"
        send_email(cfg, f"SESSION EXPIRED - {name}",
            "<h2>Session Expired!</h2>"
            "<p>Run <code>python get_cookies.py</code> on your computer, "
            "then update SESSION_COOKIES env var on Render.</p>")
        # Keep server running so dashboard shows the error
        while True:
            time.sleep(60)
            # Re-check in case cookies were updated via env var
            new_cookies = load_cookies()
            if new_cookies != cookies:
                cprint(">>", "New cookies detected, retrying...")
                cookies = new_cookies
                http = CursorHTTP(cookies)
                valid, detail = http.check_session()
                if valid:
                    cprint("OK", "Session restored!")
                    monitor_status["session_valid"] = True
                    break
    else:
        cprint("OK", f"Session valid! ({detail})")
        monitor_status["session_valid"] = True

    # Check team status
    monitor_status["status"] = "checking_team"
    status, detail = http.get_team_status()
    cprint(">>", f"Team: {status} ({detail})")

    if status in ("removed", "free_plan"):
        cprint("!!", "NOT ON TEAM!")
        send_email(cfg, f"REMOVED FROM TEAM - {name}",
            f"<h2>Removed from team!</h2><p>Status: {status} ({detail})</p>")

    # Initial invite link extraction (API first, then HTML fallback)
    monitor_status["status"] = "extracting_link"
    link, api_status = http.get_invite_link_via_api()
    if not link:
        cprint("..", "API failed, trying HTML scrape...")
        link, code = http.get_invite_link()
    if link:
        cprint("OK", f"Invite link: {link}")
        if link != known_link:
            known_link = link
            account["known_invite_link"] = link
            save_config(cfg)

    if not link and known_link:
        cprint("..", f"Using known link from config: {known_link}")
        link = known_link

    monitor_status["current_link"] = known_link or ""
    monitor_status["status"] = "running"
    check_count = 0
    consecutive_errors = 0
    last_session_check = time.time()
    rejoin_attempts = 0

    cprint("OK", "Monitoring started!")
    cprint(">>", f"Known link: {known_link or 'none'}")

    while True:
        time.sleep(interval)
        check_count += 1

        try:
            # ── SESSION CHECK (every 2 min) ──
            if time.time() - last_session_check > 120:
                valid, detail = http.check_session()
                last_session_check = time.time()
                if not valid:
                    cprint("!!", f"Session expired: {detail}")
                    monitor_status["status"] = "session_expired"
                    monitor_status["session_valid"] = False
                    monitor_status["last_error"] = f"Session expired: {detail}"
                    send_email(cfg, f"SESSION EXPIRED - {name}",
                        "<h2>Session Expired!</h2>"
                        "<p>Your Cursor session cookies have expired.</p>"
                        "<h3>How to fix:</h3>"
                        "<ol>"
                        "<li>Open <b>cursor.com</b> in your browser and log in</li>"
                        "<li>Use a cookie extension (EditThisCookie / Cookie-Editor) to export all cookies as JSON</li>"
                        "<li>Go to <a href='https://dashboard.render.com'>Render Dashboard</a> → cursor-invite-monitor → Environment</li>"
                        "<li>Update <b>SESSION_COOKIES</b> with the new cookie JSON (compact, one line)</li>"
                        "<li>Click <b>Save Changes</b> — Render will auto-redeploy</li>"
                        "</ol>")
                    # Keep checking every 60s for updated cookies
                    while True:
                        time.sleep(60)
                        new_cookies = load_cookies()
                        if new_cookies != cookies:
                            cprint(">>", "New cookies detected, retrying...")
                            cookies = new_cookies
                            http = CursorHTTP(new_cookies)
                            valid, _ = http.check_session()
                            if valid:
                                cprint("OK", "Session restored!")
                                monitor_status["session_valid"] = True
                                monitor_status["status"] = "running"
                                last_session_check = time.time()
                                break
                    continue

            # ── GET INVITE LINK (primary check every cycle) ──
            new_link, api_status = http.get_invite_link_via_api()

            if api_status == "unauthorized":
                # ── REMOVED FROM TEAM — INSTANT REJOIN ──
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
                cprint("!!", "=" * 50)
                cprint("!!", f"REMOVED FROM TEAM DETECTED at {now}")
                cprint("!!", "=" * 50)
                monitor_status["status"] = "removed_rejoining"
                monitor_status["last_error"] = f"Removed at {now}, auto-rejoining..."

                rejoin_link = known_link
                if not rejoin_link:
                    cprint("!!", "NO KNOWN INVITE LINK — cannot auto-rejoin!")
                    send_email(cfg, f"REMOVED - NO INVITE LINK - {name}",
                        f"<h2>Removed from team but no invite link to rejoin!</h2>"
                        f"<p>Time: {now}</p>")
                    # Fall through to keep monitoring
                else:
                    # Try to rejoin IMMEDIATELY
                    rejoin_attempts = 0
                    max_rejoin_attempts = 30
                    while rejoin_attempts < max_rejoin_attempts:
                        rejoin_attempts += 1
                        cprint(">>", f"Rejoin attempt #{rejoin_attempts}...")
                        success, detail = http.join_with_invite_link(rejoin_link)
                        if success:
                            rejoin_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
                            cprint("OK", "=" * 50)
                            cprint("OK", f"REJOINED TEAM at {rejoin_time}!")
                            cprint("OK", f"Method: {detail}")
                            cprint("OK", "=" * 50)
                            monitor_status["status"] = "running"
                            monitor_status["last_error"] = f"Rejoined at {rejoin_time} ({detail})"
                            send_email(cfg, f"REJOINED TEAM - {name}",
                                f"<h2>Auto-Rejoined Team!</h2>"
                                f"<table border='1' cellpadding='8'>"
                                f"<tr><td><b>Removed at</b></td><td>{now}</td></tr>"
                                f"<tr><td><b>Rejoined at</b></td><td>{rejoin_time}</td></tr>"
                                f"<tr><td><b>Method</b></td><td>{detail}</td></tr>"
                                f"<tr><td><b>Attempt</b></td><td>#{rejoin_attempts}</td></tr>"
                                f"<tr><td><b>Link used</b></td><td>{rejoin_link}</td></tr>"
                                f"</table>")
                            break
                        # Wait briefly before retry
                        time.sleep(2)

                    if rejoin_attempts >= max_rejoin_attempts:
                        cprint("!!", f"REJOIN FAILED after {max_rejoin_attempts} attempts!")
                        monitor_status["status"] = "rejoin_failed"
                        monitor_status["last_error"] = f"Rejoin failed after {max_rejoin_attempts} attempts"
                        send_email(cfg, f"REJOIN FAILED - {name}",
                            f"<h2>Auto-Rejoin Failed!</h2>"
                            f"<p>Removed at: {now}</p>"
                            f"<p>Tried {max_rejoin_attempts} times with link:</p>"
                            f"<p>{rejoin_link}</p>"
                            f"<p><b>The invite link may have been revoked.</b></p>"
                            f"<p>Get a new invite link and update KNOWN_INVITE_LINK env var.</p>")
                continue

            if not new_link:
                new_link, code = http.get_invite_link()
                if code == 401:
                    last_session_check = 0  # Force session check next cycle
                    continue

            monitor_status["last_check"] = datetime.now().isoformat()
            monitor_status["checks"] = check_count

            if new_link is None:
                consecutive_errors += 1
                if consecutive_errors >= 30:
                    cprint("!!", "Too many consecutive failures, checking session...")
                    last_session_check = 0
                    consecutive_errors = 0
                elif consecutive_errors % 10 == 0:
                    cprint("..", f"#{check_count}: No link found ({consecutive_errors} consecutive)")
                continue

            consecutive_errors = 0
            monitor_status["current_link"] = new_link

            # LINK CHANGED!
            if new_link != known_link and known_link:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
                cprint("!!", "=" * 50)
                cprint("!!", f"LINK CHANGED at {now}")
                cprint("<<", f"OLD: {known_link}")
                cprint(">>", f"NEW: {new_link}")
                cprint("!!", "=" * 50)

                record = {
                    "timestamp": now, "account": name,
                    "old_link": known_link, "new_link": new_link,
                    "check_number": check_count,
                }
                history.append(record)
                save_history(history)
                monitor_status["link_changes"] += 1
                link_history_log.append({"time": now, "old": known_link, "new": new_link})

                known_link = new_link
                account["known_invite_link"] = new_link
                save_config(cfg)

                send_email(cfg, f"LINK CHANGED - {name}",
                    f"<h2>Invite Link Changed!</h2>"
                    f"<table border='1' cellpadding='8'>"
                    f"<tr><td><b>Time</b></td><td>{now}</td></tr>"
                    f"<tr><td><b>Old</b></td><td>{record['old_link']}</td></tr>"
                    f"<tr><td><b>New</b></td><td><a href='{new_link}'>{new_link}</a></td></tr>"
                    f"</table><br>"
                    f"<a href='{new_link}' style='background:#4CAF50;color:white;padding:12px 24px;"
                    f"text-decoration:none;border-radius:5px;'>Join Now</a>")

            elif not known_link and new_link:
                known_link = new_link
                account["known_invite_link"] = new_link
                save_config(cfg)

            if check_count % 200 == 0:
                cprint(">>", f"#{check_count}: OK | Link: ...{(known_link or '')[-20:]}")

        except KeyboardInterrupt:
            raise
        except Exception as e:
            cprint("!!", f"#{check_count}: {e}")
            monitor_status["last_error"] = str(e)[:100]
            monitor_status["errors"] += 1
            consecutive_errors += 1


def main():
    print(f"\n{'='*60}")
    print(f"  CURSOR INVITE LINK MONITOR v7")
    print(f"  Pure HTTP Mode | No Browser | No Cloudflare")
    print(f"{'='*60}\n")

    start_health_server()
    monitor_status["started"] = datetime.now().isoformat()

    cfg = load_config()
    cookies = load_cookies()

    if not cookies:
        cprint("!!", "NO COOKIES! Set SESSION_COOKIES env var or run get_cookies.py locally.")
        monitor_status["status"] = "no_cookies"
        monitor_status["last_error"] = "No session cookies. Run get_cookies.py locally!"
        # Keep server alive so dashboard shows the error
        while True:
            time.sleep(60)
            cookies = load_cookies()
            if cookies:
                break

    accounts = [a for a in cfg.get("accounts", []) if a.get("enabled", True)]
    if not accounts:
        accounts = [{"name": "Main", "known_invite_link": os.environ.get("KNOWN_INVITE_LINK", "")}]

    if len(accounts) == 1:
        monitor_account(accounts[0], cfg, cookies)
    else:
        threads = []
        for acc in accounts:
            t = threading.Thread(target=monitor_account, args=(acc, cfg, cookies), daemon=True)
            t.start()
            threads.append(t)
            time.sleep(1)
        try:
            for t in threads:
                t.join()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
