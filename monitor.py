"""
CURSOR INVITE LINK MONITOR v8 — ULTRA-FAST
- Checks every 1 second (configurable)
- Instant removal detection + auto-rejoin in milliseconds
- Full event log with timestamps (every action recorded)
- Email alerts: removal, rejoin, session expiry, link changes
- Visual dashboard + JSON API + event log endpoint
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
EVENT_LOG_PATH = BASE_DIR / "events.json"
LOG_FILE = BASE_DIR / "monitor.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("cursor-monitor")

DASHBOARD_URL = "https://cursor.com/dashboard"
MEMBERS_URL = "https://cursor.com/dashboard/members"
INVITE_LINK_API = "https://cursor.com/api/dashboard/get-team-invite-link"
ACCEPT_INVITE_API = "https://cursor.com/api/accept-invite"

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


# ============================================================
# EVENT LOG — records EVERYTHING with ms precision
# ============================================================
event_log = []  # In-memory log, also saved to disk
event_log_lock = threading.Lock()


def log_event(event_type, detail, extra=None):
    """Log an event with millisecond precision timestamp."""
    now = datetime.now()
    event = {
        "time": now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "epoch_ms": int(now.timestamp() * 1000),
        "type": event_type,
        "detail": detail,
    }
    if extra:
        event["extra"] = extra
    with event_log_lock:
        event_log.append(event)
        # Keep last 1000 events in memory
        if len(event_log) > 1000:
            event_log.pop(0)
    # Also write to disk (append)
    try:
        with open(EVENT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
    except:
        pass
    # Console + file log
    ts = now.strftime("%H:%M:%S.%f")[:-3]
    symbol = {"info": "..", "ok": "OK", "warn": "!!", "error": "!!", "critical": "XX",
              "removal": "XX", "rejoin": "OK", "session": "!!", "link_change": ">>",
              "check": ".."}.get(event_type, ">>")
    print(f"{ts} [{symbol}] [{event_type.upper()}] {detail}")
    log.info(f"[{event_type.upper()}] {detail}")


def cprint(symbol, msg):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
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
            "check_interval_seconds": int(os.environ.get("CHECK_INTERVAL", "1")),
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


def parse_netscape_cookies(text):
    """Parse Netscape HTTP Cookie File format into {name: value} dict.
    Format: domain\tinclude_subdomains\tpath\tsecure\texpiry\tname\tvalue
    Lines starting with # are comments (except #HttpOnly_ prefix).
    """
    cookies = {}
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        # #HttpOnly_ prefix means httpOnly cookie — strip prefix and parse
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#"):
            continue  # Skip comment lines

        parts = line.split("\t")
        if len(parts) >= 7:
            name = parts[5]
            value = parts[6]
            cookies[name] = value
    return cookies


def is_netscape_format(text):
    """Detect if text is Netscape cookie file format."""
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Netscape lines are tab-separated with 7 fields
        parts = line.split("\t")
        if len(parts) >= 7:
            return True
        return False
    return False


def normalize_cookies(raw):
    """Accept multiple formats:
    - Cookie-Editor export: [{"name":"x","value":"y",...}, ...]
    - Simple dict: {"name": "value", ...}
    - Netscape HTTP Cookie File format (string)
    Returns simple {name: value} dict.
    """
    if isinstance(raw, str):
        # Try Netscape format
        if is_netscape_format(raw):
            return parse_netscape_cookies(raw)
        return {}
    if isinstance(raw, list):
        # Cookie-Editor / browser extension format
        return {c["name"]: c["value"] for c in raw if "name" in c and "value" in c}
    if isinstance(raw, dict):
        return raw
    return {}


def load_cookies(suffix=""):
    """Load cookies from env var or file. suffix="" for main, "_2", "_3" etc for extra accounts.
    Accepts both Cookie-Editor array format and simple {name:value} dict.
    """
    env_key = f"SESSION_COOKIES{suffix}"
    env_cookies = os.environ.get(env_key, "")
    if env_cookies:
        try:
            raw = json.loads(env_cookies)
            cookies = normalize_cookies(raw)
            cprint("OK", f"Loaded {len(cookies)} cookies from {env_key} (JSON)")
            return cookies
        except json.JSONDecodeError:
            # Try Netscape format
            if is_netscape_format(env_cookies):
                cookies = parse_netscape_cookies(env_cookies)
                if cookies:
                    cprint("OK", f"Loaded {len(cookies)} cookies from {env_key} (Netscape format)")
                    return cookies
            cprint("!!", f"{env_key} env var is not valid JSON or Netscape format")
    if not suffix and COOKIE_PATH.exists():
        with open(COOKIE_PATH, "r") as f:
            content = f.read()
        # Try JSON first, then Netscape
        try:
            raw = json.loads(content)
            cookies = normalize_cookies(raw)
            cprint("OK", f"Loaded {len(cookies)} cookies from cookies.json (JSON)")
            return cookies
        except json.JSONDecodeError:
            if is_netscape_format(content):
                cookies = parse_netscape_cookies(content)
                if cookies:
                    cprint("OK", f"Loaded {len(cookies)} cookies from cookies.json (Netscape format)")
                    return cookies
            cprint("!!", "cookies.json is not valid JSON or Netscape format")
    if not suffix:
        cprint("!!", "No cookies found!")
    return {}


def save_cookies(cookies):
    with open(COOKIE_PATH, "w") as f:
        json.dump(cookies, f, indent=2)


def discover_accounts():
    """Auto-discover accounts from env vars.
    Account 1: SESSION_COOKIES, ACCOUNT_NAME, KNOWN_INVITE_LINK
    Account 2: SESSION_COOKIES_2, ACCOUNT_NAME_2, KNOWN_INVITE_LINK_2
    Account 3: SESSION_COOKIES_3, ACCOUNT_NAME_3, KNOWN_INVITE_LINK_3
    ...up to 10 accounts.
    """
    accounts = []

    # Account 1 (main)
    cookies = load_cookies()
    if cookies:
        accounts.append({
            "name": os.environ.get("ACCOUNT_NAME", "Account 1"),
            "known_invite_link": os.environ.get("KNOWN_INVITE_LINK", ""),
            "cookies": cookies,
            "suffix": "",
            "enabled": True,
        })

    # Accounts 2-10
    for i in range(2, 11):
        suffix = f"_{i}"
        cookies = load_cookies(suffix)
        if cookies:
            accounts.append({
                "name": os.environ.get(f"ACCOUNT_NAME{suffix}", f"Account {i}"),
                "known_invite_link": os.environ.get(f"KNOWN_INVITE_LINK{suffix}", ""),
                "cookies": cookies,
                "suffix": suffix,
                "enabled": True,
            })

    return accounts


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
    def __init__(self, cookies):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.team_id = cookies.get("team_id", "")
        for name, value in cookies.items():
            self.session.cookies.set(name, value, domain=".cursor.com")
        self.valid = True

    def check_session(self):
        """Returns (valid, detail)."""
        t0 = time.time()
        try:
            resp = self.session.get(DASHBOARD_URL, allow_redirects=False, timeout=10)
            ms = int((time.time() - t0) * 1000)
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location", "")
                if "authenticator" in location or "login" in location:
                    self.valid = False
                    log_event("session", f"EXPIRED — redirect to login ({ms}ms)")
                    return False, "session_expired"
                return True, f"redirect ({ms}ms)"
            if resp.status_code == 403:
                self.valid = False
                log_event("session", f"403 Forbidden ({ms}ms)")
                return False, "forbidden"
            if resp.status_code == 200:
                text = resp.text
                if "authenticator" in text and "Sign in" in text:
                    self.valid = False
                    log_event("session", f"EXPIRED — login page in body ({ms}ms)")
                    return False, "session_expired"
                return True, f"ok ({ms}ms)"
            log_event("warn", f"Session check unexpected status {resp.status_code} ({ms}ms)")
            return False, f"status_{resp.status_code}"
        except Exception as e:
            ms = int((time.time() - t0) * 1000)
            log_event("error", f"Session check error: {str(e)[:60]} ({ms}ms)")
            return False, str(e)[:60]

    def get_invite_link_via_api(self):
        """Returns (link, status, response_ms). Status: 'ok', 'unauthorized', 'error'."""
        if not self.team_id:
            return None, "error", 0
        t0 = time.time()
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
                timeout=10,
            )
            ms = int((time.time() - t0) * 1000)
            if resp.status_code == 200:
                data = resp.json()
                link = data.get("inviteLink", "")
                if link:
                    return link, "ok", ms
                return None, "ok", ms
            elif resp.status_code == 401:
                error_msg = ""
                try:
                    error_msg = resp.json().get("error", {}).get("message", "")
                except:
                    pass
                self.valid = False
                return None, "unauthorized", ms
            else:
                return None, "error", ms
        except Exception as e:
            ms = int((time.time() - t0) * 1000)
            return None, "error", ms

    def join_with_invite_link(self, invite_link):
        """Accept invite via POST /api/accept-invite. Returns (success, detail, response_ms)."""
        if not invite_link:
            return False, "no_link", 0

        # Extract invite code from URL
        code = ""
        m = re.search(r'[?&]code=([a-f0-9]+)', invite_link)
        if m:
            code = m.group(1)
        else:
            log_event("error", f"Cannot extract invite code from: {invite_link}")
            return False, "bad_link", 0

        t0 = time.time()
        try:
            # Primary method: POST /api/accept-invite with inviteCode
            resp = self.session.post(
                ACCEPT_INVITE_API,
                json={"inviteCode": code},
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Referer": invite_link,
                    "Origin": "https://cursor.com",
                },
                timeout=10,
            )
            ms = int((time.time() - t0) * 1000)

            if resp.status_code == 200:
                try:
                    data = resp.json()
                    team_id = data.get("teamId", "")
                    log_event("rejoin", f"ACCEPTED via API! teamId={team_id} ({ms}ms)")
                    return True, f"accepted ({ms}ms) teamId={team_id}", ms
                except:
                    log_event("rejoin", f"ACCEPTED via API (non-JSON response) ({ms}ms)")
                    return True, f"accepted ({ms}ms)", ms

            # Parse error
            error_detail = ""
            try:
                err = resp.json()
                error_detail = err.get("error", {}).get("details", [{}])[0].get("details", {}).get("detail", "")
            except:
                error_detail = resp.text[:200]

            log_event("rejoin", f"FAILED HTTP {resp.status_code}: {error_detail} ({ms}ms)")

            if "expired" in error_detail.lower():
                return False, f"invite_code_expired ({ms}ms)", ms
            elif "not found" in error_detail.lower():
                return False, f"invite_code_not_found ({ms}ms)", ms
            else:
                return False, f"http_{resp.status_code}: {error_detail[:80]} ({ms}ms)", ms

        except Exception as e:
            ms = int((time.time() - t0) * 1000)
            log_event("error", f"Join error: {str(e)[:60]} ({ms}ms)")
            return False, str(e)[:60], ms


# ============================================================
# EMAIL
# ============================================================
def send_email(cfg, subject, body):
    email_addr = cfg.get("notification_email", "")
    app_pw = cfg.get("gmail_app_password", "")
    if not email_addr or not app_pw or app_pw == "NEED_APP_PASSWORD":
        log_event("warn", f"Email skip (not configured) | {subject}")
        return False
    try:
        msg = MIMEMultipart()
        msg["From"] = email_addr
        msg["To"] = email_addr
        msg["Subject"] = f"[Cursor Monitor] {subject}"
        msg.attach(MIMEText(body, "html"))
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(email_addr, app_pw)
            server.send_message(msg)
        log_event("ok", f"Email sent: {subject}")
        return True
    except Exception as e:
        log_event("error", f"Email failed: {e}")
        return False


# ============================================================
# HEALTH SERVER + DASHBOARD
# ============================================================
monitor_status = {
    "started": None, "last_check": None, "checks": 0,
    "link_changes": 0, "current_link": "", "status": "starting",
    "errors": 0, "last_error": "", "session_valid": False,
    "removals": 0, "rejoins": 0, "last_response_ms": 0,
}
# Per-account status: account_statuses["Account 1"] = {status dict}
account_statuses = {}
account_statuses_lock = threading.Lock()
link_history_log = []

DASHBOARD_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Cursor Monitor v8</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="5">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0d1117;color:#e6edf3;font-family:'Segoe UI',sans-serif;padding:20px}}
h1{{color:#58a6ff;margin-bottom:20px;font-size:24px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:20px;margin-bottom:16px}}
.card h2{{color:#8b949e;font-size:14px;text-transform:uppercase;margin-bottom:12px}}
.stat-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:12px}}
.stat{{background:#21262d;border-radius:8px;padding:16px;text-align:center}}
.stat .val{{font-size:24px;font-weight:bold;color:#58a6ff}}
.stat .label{{font-size:11px;color:#8b949e;margin-top:4px}}
.stat.ok .val{{color:#3fb950}}
.stat.warn .val{{color:#d29922}}
.stat.err .val{{color:#f85149}}
.link-box{{background:#21262d;border-radius:8px;padding:16px;margin-top:12px;word-break:break-all}}
.link-box a{{color:#58a6ff;text-decoration:none;font-size:14px}}
.link-box .time{{color:#8b949e;font-size:12px;margin-top:4px}}
table{{width:100%;border-collapse:collapse;margin-top:12px}}
th{{text-align:left;color:#8b949e;font-size:11px;padding:6px;border-bottom:1px solid #30363d}}
td{{padding:6px;border-bottom:1px solid #21262d;font-size:12px}}
td a{{color:#58a6ff;text-decoration:none}}
.badge{{display:inline-block;padding:4px 10px;border-radius:12px;font-size:12px;font-weight:600}}
.badge.running{{background:#0d2818;color:#3fb950}}
.badge.starting{{background:#2d2200;color:#d29922}}
.badge.error{{background:#2d0f0f;color:#f85149}}
.badge.expired{{background:#2d0f0f;color:#f85149}}
.badge.rejoining{{background:#2d1800;color:#f0883e}}
.alert{{background:#2d0f0f;border:1px solid #f85149;border-radius:8px;padding:16px;margin-bottom:16px;color:#f85149}}
.event-log{{max-height:400px;overflow-y:auto;background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:8px;font-family:monospace;font-size:11px;line-height:1.6}}
.event-log .e-time{{color:#8b949e}}
.event-log .e-type{{font-weight:bold;padding:0 4px;border-radius:3px}}
.event-log .e-removal{{background:#5c1010;color:#f85149}}
.event-log .e-rejoin{{background:#0d2818;color:#3fb950}}
.event-log .e-session{{background:#2d1800;color:#f0883e}}
.event-log .e-ok{{color:#3fb950}}
.event-log .e-warn{{color:#d29922}}
.event-log .e-error{{color:#f85149}}
.event-log .e-link_change{{color:#58a6ff}}
.event-log .e-info{{color:#8b949e}}
.event-log .e-check{{color:#484f58}}
</style></head><body>
<h1>Cursor Invite Link Monitor v8 — ULTRA-FAST</h1>

{alert_html}

<div class="card">
<h2>Status</h2>
<div class="stat-grid">
<div class="stat ok"><div class="val">{status_badge}</div><div class="label">Status</div></div>
<div class="stat"><div class="val">{checks}</div><div class="label">Checks</div></div>
<div class="stat {changes_class}"><div class="val">{link_changes}</div><div class="label">Link Changes</div></div>
<div class="stat {err_class}"><div class="val">{errors}</div><div class="label">Errors</div></div>
<div class="stat {removal_class}"><div class="val">{removals}</div><div class="label">Removals</div></div>
<div class="stat ok"><div class="val">{rejoins}</div><div class="label">Rejoins</div></div>
<div class="stat"><div class="val">{response_ms}ms</div><div class="label">Last API</div></div>
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
<h2>Event Log (last 50)</h2>
<div class="event-log">{event_log_html}</div>
</div>

<div class="card">
<h2>Link Change History</h2>
{history_html}
</div>

<p style="color:#8b949e;font-size:11px;margin-top:20px;text-align:center">
v8 ULTRA-FAST | 1s checks | Auto-rejoin | <a href="/api" style="color:#58a6ff">JSON API</a> | <a href="/events" style="color:#58a6ff">Full Event Log</a>
</p>
</body></html>"""


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/events":
            # Full event log as JSON
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            with event_log_lock:
                data = list(event_log)
            self.wfile.write(json.dumps(data, indent=2).encode())
        elif self.path in ("/api", "/health", "/ping"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            with account_statuses_lock:
                accs = dict(account_statuses)
            data = {**monitor_status, "accounts": accs,
                    "history": link_history_log[-50:],
                    "recent_events": event_log[-20:]}
            self.wfile.write(json.dumps(data, indent=2, default=str).encode())
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            try:
                s = monitor_status
                st = s["status"]
                if "running" in st:
                    badge = '<span class="badge running">RUNNING</span>'
                elif "rejoin" in st:
                    badge = f'<span class="badge rejoining">{st.upper()}</span>'
                elif "expired" in st:
                    badge = '<span class="badge expired">SESSION EXPIRED</span>'
                elif "error" in st or "failed" in st:
                    badge = f'<span class="badge error">{st.upper()}</span>'
                else:
                    badge = f'<span class="badge starting">{st.upper()}</span>'

                link = s.get("current_link", "")
                link_html = f'<a href="{link}">{link}</a>' if link else '<span style="color:#8b949e">Not yet extracted...</span>'

                last_check = s.get("last_check", "Never")
                if last_check and last_check != "Never":
                    last_check = str(last_check)[:23]

                alert_html = ""
                if not s.get("session_valid", True):
                    alert_html = '<div class="alert"><strong>Session Expired!</strong> Update SESSION_COOKIES env var on Render with fresh cookies.</div>'

                # Event log HTML
                with event_log_lock:
                    recent = list(event_log[-50:])
                ev_lines = []
                for ev in reversed(recent):
                    etype = ev["type"]
                    ev_lines.append(
                        f'<div><span class="e-time">{ev["time"]}</span> '
                        f'<span class="e-type e-{etype}">{etype.upper()}</span> '
                        f'{ev["detail"]}</div>'
                    )
                event_log_html = "\n".join(ev_lines) if ev_lines else '<div style="color:#8b949e">No events yet</div>'

                # Link history
                history = link_history_log[-20:]
                if history:
                    rows = ""
                    for h in reversed(history):
                        ts = h.get("time", "")[:23]
                        old = h.get("old", "")
                        new = h.get("new", "")
                        rows += f'<tr><td>{ts}</td><td><a href="{old}">...{old[-20:]}</a></td><td><a href="{new}">...{new[-20:]}</a></td></tr>'
                    hist_html = f'<table><tr><th>Time</th><th>Old Link</th><th>New Link</th></tr>{rows}</table>'
                else:
                    hist_html = '<p style="color:#8b949e;padding:12px">No changes detected yet</p>'

                html = DASHBOARD_HTML.format(
                    alert_html=alert_html,
                    status_badge=badge,
                    checks=s["checks"],
                    link_changes=s["link_changes"],
                    changes_class="warn" if s["link_changes"] > 0 else "",
                    errors=s["errors"],
                    err_class="err" if s["errors"] > 0 else "",
                    removals=s.get("removals", 0),
                    removal_class="err" if s.get("removals", 0) > 0 else "",
                    rejoins=s.get("rejoins", 0),
                    response_ms=s.get("last_response_ms", 0),
                    current_link_html=link_html,
                    last_check=last_check,
                    event_log_html=event_log_html,
                    history_html=hist_html,
                )
                self.wfile.write(html.encode())
            except Exception as e:
                err_html = f'<html><body style="background:#0d1117;color:#f85149;padding:40px;font-family:monospace"><h1>Dashboard Error</h1><pre>{e}</pre></body></html>'
                self.wfile.write(err_html.encode())

    def do_HEAD(self):
        """Handle HEAD requests (used by UptimeRobot and other monitors)."""
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()

    def log_message(self, *a):
        pass


def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    log_event("info", f"Health server on :{port}")
    threading.Thread(target=server.serve_forever, daemon=True).start()


# ============================================================
# MONITOR LOOP — ULTRA-FAST
# ============================================================
def monitor_account(account, cfg):
    name = account.get("name", "Main")
    cookies = account.get("cookies", {})
    suffix = account.get("suffix", "")
    known_link = account.get("known_invite_link", "")
    interval = cfg.get("check_interval_seconds", 1)
    history = load_history()

    # Per-account status
    acc_status = {
        "name": name, "status": "starting", "checks": 0,
        "current_link": "", "session_valid": False,
        "removals": 0, "rejoins": 0, "errors": 0,
        "last_check": None, "last_error": "", "last_response_ms": 0,
    }
    with account_statuses_lock:
        account_statuses[name] = acc_status

    class DualStatus:
        """Updates both per-account and global status dicts."""
        def __setitem__(self, key, value):
            acc_status[key] = value
            monitor_status[key] = value
        def __getitem__(self, key):
            return acc_status.get(key, monitor_status.get(key))
        def get(self, key, default=None):
            return acc_status.get(key, monitor_status.get(key, default))

    status = DualStatus()

    log_event("info", f"[{name}] Monitor starting | {interval}s interval | cookies_env=SESSION_COOKIES{suffix}")

    http = CursorHTTP(cookies)

    # ── INITIAL SESSION CHECK ──
    status["status"] = "checking_session"
    valid, detail = http.check_session()
    if not valid:
        log_event("session", f"Session invalid on start: {detail}")
        status["status"] = "session_expired"
        status["session_valid"] = False
        status["last_error"] = f"Session invalid: {detail}"
        send_email(cfg, f"SESSION EXPIRED - {name}",
            "<h2>Session Expired on Start!</h2>"
            "<p>Your Cursor session cookies are invalid.</p>"
            "<h3>How to fix:</h3>"
            "<ol>"
            "<li>Open <b>cursor.com</b> in your browser and log in</li>"
            "<li>Use cookie extension (EditThisCookie / Cookie-Editor) → Export as JSON</li>"
            "<li>Go to Render Dashboard → cursor-invite-monitor → Environment</li>"
            f"<li>Update <b>SESSION_COOKIES{suffix}</b> env var with the new JSON</li>"
            "<li>Save → Render auto-redeploys</li>"
            "</ol>")
        while True:
            time.sleep(30)
            new_cookies = load_cookies(suffix)
            if new_cookies and new_cookies != cookies:
                log_event("info", f"[{name}] New cookies detected, retrying...")
                cookies = new_cookies
                http = CursorHTTP(cookies)
                valid, detail = http.check_session()
                if valid:
                    log_event("ok", f"[{name}] Session restored!")
                    status["session_valid"] = True
                    break
    else:
        log_event("ok", f"[{name}] Session valid: {detail}")
        status["session_valid"] = True

    # ── INITIAL INVITE LINK ──
    status["status"] = "extracting_link"
    link, api_status, api_ms = http.get_invite_link_via_api()
    if link:
        log_event("ok", f"Invite link: {link} ({api_ms}ms)")
        if link != known_link:
            known_link = link
            account["known_invite_link"] = link
            save_config(cfg)
    elif known_link:
        log_event("info", f"Using known link from config: ...{known_link[-30:]}")
        link = known_link
    else:
        log_event("warn", "No invite link found — rejoin will not work without one!")

    status["current_link"] = known_link or ""
    status["status"] = "running"
    check_count = 0
    consecutive_errors = 0
    last_session_check = time.time()

    log_event("ok", f"MONITORING STARTED — checking every {interval}s")
    log_event("info", f"Known link: {known_link or 'NONE'}")

    while True:
        time.sleep(interval)
        check_count += 1
        loop_start = time.time()

        try:
            # ── SESSION CHECK (every 2 min) ──
            if time.time() - last_session_check > 120:
                valid, detail = http.check_session()
                last_session_check = time.time()
                if not valid:
                    log_event("session", f"SESSION EXPIRED: {detail}")
                    status["status"] = "session_expired"
                    status["session_valid"] = False
                    status["last_error"] = f"Session expired: {detail}"
                    send_email(cfg, f"SESSION EXPIRED - {name}",
                        "<h2>Session Expired!</h2>"
                        "<p>Your Cursor session cookies have expired.</p>"
                        "<h3>How to fix:</h3>"
                        "<ol>"
                        "<li>Open <b>cursor.com</b> in browser → log in</li>"
                        "<li>Cookie extension → Export all cursor.com cookies as JSON</li>"
                        "<li>Render Dashboard → cursor-invite-monitor → Environment</li>"
                        f"<li>Update <b>SESSION_COOKIES{suffix}</b> with new JSON → Save</li>"
                        "</ol>")
                    while True:
                        time.sleep(30)
                        new_cookies = load_cookies(suffix)
                        if new_cookies and new_cookies != cookies:
                            log_event("info", f"[{name}] New cookies detected...")
                            cookies = new_cookies
                            http = CursorHTTP(new_cookies)
                            valid, _ = http.check_session()
                            if valid:
                                log_event("ok", "Session restored!")
                                status["session_valid"] = True
                                status["status"] = "running"
                                last_session_check = time.time()
                                send_email(cfg, f"SESSION RESTORED - {name}",
                                    "<h2>Session Restored!</h2><p>Monitoring resumed.</p>")
                                break
                    continue

            # ── PRIMARY CHECK: GET INVITE LINK VIA API ──
            new_link, api_status, api_ms = http.get_invite_link_via_api()
            status["last_response_ms"] = api_ms
            status["last_check"] = datetime.now().isoformat()
            status["checks"] = check_count

            # ════════════════════════════════════════════
            # REMOVED FROM TEAM — INSTANT REJOIN
            # ════════════════════════════════════════════
            if api_status == "unauthorized":
                removal_time = datetime.now()
                removal_ts = removal_time.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                removal_epoch = int(removal_time.timestamp() * 1000)

                log_event("removal", f"REMOVED FROM TEAM at {removal_ts} (API responded in {api_ms}ms)")
                status["status"] = "REMOVED_REJOINING"
                status["removals"] = status.get("removals", 0) + 1
                status["last_error"] = f"REMOVED at {removal_ts}"

                # Send email in background so it doesn't block rejoin
                threading.Thread(target=send_email, args=(cfg, f"REMOVED FROM TEAM - {name}",
                    f"<h2 style='color:red'>REMOVED FROM TEAM!</h2>"
                    f"<p>Detected at: <b>{removal_ts}</b></p>"
                    f"<p>API response time: {api_ms}ms</p>"
                    f"<p>Auto-rejoin starting immediately...</p>"
                    f"<p>Known invite link: {known_link}</p>"), daemon=True).start()

                if not known_link:
                    log_event("critical", "NO INVITE LINK — CANNOT REJOIN!")
                    status["status"] = "REMOVED_NO_LINK"
                    send_email(cfg, f"CANNOT REJOIN - NO LINK - {name}",
                        f"<h2 style='color:red'>Cannot rejoin — no invite link!</h2>"
                        f"<p>Set KNOWN_INVITE_LINK env var on Render.</p>")
                    # Keep checking in case link appears
                    continue

                # ── REJOIN LOOP — as fast as possible ──
                for attempt in range(1, 61):  # Up to 60 attempts
                    attempt_start = time.time()
                    log_event("rejoin", f"Attempt #{attempt}...")

                    success, detail, join_ms = http.join_with_invite_link(known_link)

                    if success:
                        rejoin_time = datetime.now()
                        rejoin_ts = rejoin_time.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                        rejoin_epoch = int(rejoin_time.timestamp() * 1000)
                        total_ms = rejoin_epoch - removal_epoch

                        log_event("rejoin",
                            f"REJOINED! at {rejoin_ts} | "
                            f"Total time: {total_ms}ms | "
                            f"Attempt: #{attempt} | "
                            f"Method: {detail}")

                        status["status"] = "running"
                        status["rejoins"] = status.get("rejoins", 0) + 1
                        status["last_error"] = f"Rejoined in {total_ms}ms at {rejoin_ts}"

                        # Log to history
                        rejoin_record = {
                            "type": "rejoin",
                            "removal_time": removal_ts,
                            "rejoin_time": rejoin_ts,
                            "total_ms": total_ms,
                            "attempt": attempt,
                            "method": detail,
                            "link_used": known_link,
                        }
                        history.append(rejoin_record)
                        save_history(history)

                        send_email(cfg, f"REJOINED TEAM in {total_ms}ms - {name}",
                            f"<h2 style='color:green'>Auto-Rejoined Team!</h2>"
                            f"<table border='1' cellpadding='8' style='border-collapse:collapse'>"
                            f"<tr><td><b>Removed at</b></td><td>{removal_ts}</td></tr>"
                            f"<tr><td><b>Rejoined at</b></td><td>{rejoin_ts}</td></tr>"
                            f"<tr><td><b>Total time</b></td><td><b>{total_ms}ms</b></td></tr>"
                            f"<tr><td><b>Attempts</b></td><td>{attempt}</td></tr>"
                            f"<tr><td><b>Method</b></td><td>{detail}</td></tr>"
                            f"<tr><td><b>Link</b></td><td>{known_link}</td></tr>"
                            f"</table>")

                        # Cooldown after rejoin to avoid Cursor rate-limiting
                        log_event("info", "Post-rejoin cooldown: waiting 10s before resuming checks...")
                        time.sleep(10)
                        consecutive_errors = 0

                        # Re-fetch invite link after cooldown to confirm we're back
                        new_link, api_stat, _ = http.get_invite_link_via_api()
                        if api_stat == "ok" and new_link:
                            known_link = new_link
                            account["known_invite_link"] = new_link
                            save_config(cfg)
                            log_event("ok", f"Post-rejoin link confirmed: ...{new_link[-30:]}")
                        break

                    # Don't wait between first few attempts — go as fast as possible
                    if attempt <= 5:
                        time.sleep(0.1)  # 100ms between first 5 attempts
                    elif attempt <= 20:
                        time.sleep(0.5)  # 500ms between next 15
                    else:
                        time.sleep(2)  # 2s after that

                else:
                    # All 60 attempts failed — link is dead
                    log_event("critical", f"REJOIN FAILED after 60 attempts! Link is dead.")
                    dead_link = known_link
                    status["status"] = "REJOIN_FAILED"
                    status["last_error"] = "Rejoin failed after 60 attempts"
                    send_email(cfg, f"REJOIN FAILED - {name}",
                        f"<h2 style='color:red'>Auto-Rejoin Failed!</h2>"
                        f"<p>Removed at: {removal_ts}</p>"
                        f"<p>Tried 60 times. The invite link may have been revoked.</p>"
                        f"<p>Link used: {known_link}</p>"
                        f"<p><b>Action needed:</b> Get a new invite link and update KNOWN_INVITE_LINK on Render.</p>")

                    # Wait for new invite link — don't retry dead link forever
                    log_event("info", "Waiting for new invite link (checking every 30s)...")
                    while True:
                        time.sleep(30)
                        # Check if KNOWN_INVITE_LINK env var was updated
                        env_link = os.environ.get(f"KNOWN_INVITE_LINK{suffix}", "")
                        if env_link and env_link != dead_link:
                            log_event("ok", f"New invite link detected from env: ...{env_link[-30:]}")
                            known_link = env_link
                            account["known_invite_link"] = env_link
                            save_config(cfg)
                            # Try rejoining with new link immediately
                            success, detail, join_ms = http.join_with_invite_link(known_link)
                            if success:
                                log_event("rejoin", f"REJOINED with new link! {detail}")
                                status["status"] = "running"
                                status["rejoins"] = status.get("rejoins", 0) + 1
                                send_email(cfg, f"REJOINED TEAM - {name}",
                                    f"<h2 style='color:green'>Rejoined with new invite link!</h2>"
                                    f"<p>Link: {known_link}</p>")
                            break
                        # Also check if new cookies were provided (redeploy)
                        new_cookies = load_cookies(suffix)
                        if new_cookies and new_cookies != cookies:
                            log_event("info", "New cookies detected during wait, restarting...")
                            cookies = new_cookies
                            http = CursorHTTP(new_cookies)
                            # New cookies might mean we're back on team
                            new_link, api_stat, _ = http.get_invite_link_via_api()
                            if api_stat == "ok" and new_link:
                                log_event("ok", f"Back on team! Link: ...{new_link[-30:]}")
                                known_link = new_link
                                account["known_invite_link"] = new_link
                                save_config(cfg)
                                status["status"] = "running"
                                status["session_valid"] = True
                                break
                            elif api_stat == "unauthorized":
                                log_event("warn", "Still removed with new cookies, continuing wait...")

                continue

            # ── NORMAL CHECK: link retrieved successfully ──
            if new_link:
                consecutive_errors = 0
                status["current_link"] = new_link

                # LINK CHANGED!
                if new_link != known_link and known_link:
                    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                    log_event("link_change",
                        f"LINK CHANGED at {now} | OLD: ...{known_link[-20:]} → NEW: ...{new_link[-20:]}")

                    record = {
                        "type": "link_change",
                        "timestamp": now, "account": name,
                        "old_link": known_link, "new_link": new_link,
                        "check_number": check_count,
                    }
                    history.append(record)
                    save_history(history)
                    status["link_changes"] = acc_status.get("link_changes", 0) + 1
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
                        f"</table>")

                elif not known_link and new_link:
                    known_link = new_link
                    account["known_invite_link"] = new_link
                    save_config(cfg)
                    log_event("ok", f"Initial link saved: {new_link}")

            else:
                consecutive_errors += 1
                if consecutive_errors == 30:
                    log_event("warn", f"30 consecutive API failures, forcing session check")
                    last_session_check = 0
                elif consecutive_errors == 60:
                    log_event("warn", f"60 consecutive failures — possible rate-limit. Slowing to 5s checks for 2 min...")
                    for _ in range(24):  # 24 x 5s = 2 minutes cooldown
                        time.sleep(5)
                        new_link, api_stat, _ = http.get_invite_link_via_api()
                        if api_stat == "ok" and new_link:
                            log_event("ok", f"API recovered after cooldown! Link: ...{new_link[-30:]}")
                            known_link = new_link
                            account["known_invite_link"] = new_link
                            consecutive_errors = 0
                            break
                    else:
                        log_event("warn", "API still failing after 2 min cooldown, resuming normal checks")
                    consecutive_errors = 0

            # Periodic status log (every 500 checks)
            if check_count % 500 == 0:
                log_event("info",
                    f"Status: #{check_count} checks | "
                    f"link=...{(known_link or 'none')[-20:]} | "
                    f"api={api_ms}ms | "
                    f"removals={status.get('removals', 0)} | "
                    f"rejoins={status.get('rejoins', 0)}")

        except KeyboardInterrupt:
            raise
        except Exception as e:
            log_event("error", f"Check #{check_count} error: {str(e)[:100]}")
            status["last_error"] = str(e)[:100]
            status["errors"] = acc_status.get("errors", 0) + 1
            consecutive_errors += 1


def main():
    print(f"\n{'='*60}")
    print(f"  CURSOR INVITE LINK MONITOR v8 — ULTRA-FAST")
    print(f"  1s checks | Instant rejoin | Full event log")
    print(f"  Multi-account support")
    print(f"{'='*60}\n")

    start_health_server()
    monitor_status["started"] = datetime.now().isoformat()

    cfg = load_config()

    # Auto-discover accounts from env vars
    accounts = discover_accounts()

    if not accounts:
        log_event("critical", "NO ACCOUNTS! Set SESSION_COOKIES env var.")
        monitor_status["status"] = "no_cookies"
        monitor_status["last_error"] = "No session cookies for any account"
        while True:
            time.sleep(30)
            accounts = discover_accounts()
            if accounts:
                break

    log_event("info", f"Found {len(accounts)} account(s): {[a['name'] for a in accounts]}")

    if len(accounts) == 1:
        monitor_account(accounts[0], cfg)
    else:
        threads = []
        for acc in accounts:
            t = threading.Thread(target=monitor_account, args=(acc, cfg), daemon=True)
            t.start()
            threads.append(t)
            time.sleep(0.5)
        log_event("info", f"All {len(threads)} account monitors running")
        try:
            for t in threads:
                t.join()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
