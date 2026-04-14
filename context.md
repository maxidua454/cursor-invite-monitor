# Cursor Invite Link Monitor — Full Context

## What This Project Does

Automatically monitors a Cursor.com team and **instantly rejoins** if you get removed. Runs 24/7 on Render as a Docker web service. Checks every 1 second, detects removal via API, and auto-rejoins using the team's invite link — all within milliseconds.

---

## Architecture (v8 — Pure HTTP, No Browser)

### Core File: `monitor.py` (~1000 lines)
- Pure HTTP-based monitor using `requests` library
- No browser/Selenium needed — uses session cookies for authentication
- Calls Cursor's API endpoints directly:
  - `GET https://cursor.com/dashboard` — session validation
  - `POST https://cursor.com/api/dashboard/get-team-invite-link` — fetch invite link (uses `team_id`)
  - `POST https://cursor.com/api/accept-invite` — rejoin team (uses `inviteCode`)
- Features:
  - **1s check interval** — ultra-fast removal detection
  - **Auto-rejoin** — up to 60 attempts (100ms between first 5, 500ms next 15, 2s after)
  - **Email alerts** — removal, rejoin, session expiry, link changes (via Gmail SMTP)
  - **Web dashboard** — HTML dashboard at service URL with auto-refresh
  - **JSON API** — `/api` endpoint returns full status as JSON
  - **Event log** — `/events` endpoint, all events logged with ms precision
  - **Health endpoint** — `/health` for UptimeRobot (supports HEAD requests)
  - **Multi-account** — up to 10 accounts via `SESSION_COOKIES`, `SESSION_COOKIES_2`, etc.
  - **3 cookie formats supported** — JSON dict, Cookie-Editor JSON array, Netscape HTTP Cookie File
  - **Self-healing** — auto-detects session expiry, waits for new cookies, resumes monitoring
  - **Link change detection** — detects when invite link rotates, auto-updates

### Supporting Files
- `Dockerfile` — Python 3.11 slim, installs requirements, runs monitor.py
- `render.yaml` — Render.com deployment config (free plan, Docker runtime)
- `requirements.txt` — Only dependency: `requests`
- `config.example.json` — Template config for local development
- `.gitignore` — Excludes config.json, cookies.json, logs, __pycache__

### Local-Only Files (not in git)
- `config.json` — Local account config (email, password, invite link)
- `cookies.json` — Session cookies for local testing

---

## Deployment — Render.com

### Service Info
- **Service name:** `cursor-invite-monitor`
- **Service ID:** `srv-d700cr75gffc73dja0k0`
- **URL:** https://cursor-invite-monitor.onrender.com
- **GitHub repo:** https://github.com/maxidua454/cursor-invite-monitor
- **Branch:** `master`
- **Auto-deploy:** Yes — pushes to master trigger automatic redeploy
- **Runtime:** Docker (free plan)

### Render API
- **API key:** `rnd_g0ymr9RLAiMslKkNUaWvIT4Wg7u7`
- **Trigger deploy:** `curl -X POST "https://api.render.com/v1/services/srv-d700cr75gffc73dja0k0/deploys" -H "Authorization: Bearer rnd_g0ymr9RLAiMslKkNUaWvIT4Wg7u7"`
- **List env vars:** `curl "https://api.render.com/v1/services/srv-d700cr75gffc73dja0k0/env-vars" -H "Authorization: Bearer rnd_g0ymr9RLAiMslKkNUaWvIT4Wg7u7"`
- **Update env var:** `curl -X PUT ".../env-vars/KEY_NAME" -H "Authorization: ..." -d '{"value": "..."}'`

### Environment Variables on Render
| Variable | Description | Current Value |
|---|---|---|
| `SESSION_COOKIES` | Account 1 cookies (JSON or Netscape) | Set (user_01KFD8FZX10GWJECRFFD9JG0FM) |
| `ACCOUNT_NAME` | Display name for account 1 | `Adrian Max - Aiston Team` |
| `CHECK_INTERVAL` | Seconds between checks | `1` |
| `NOTIFICATION_EMAIL` | Email for alerts | `maxadrian321@gmail.com` |
| `GMAIL_APP_PASSWORD` | Gmail app password for SMTP | Set |
| `PORT` | Web server port | `10000` |
| `PYTHONUNBUFFERED` | Force unbuffered output | `1` |

### Adding More Accounts
Add `SESSION_COOKIES_2`, `ACCOUNT_NAME_2` (and optionally `KNOWN_INVITE_LINK_2`) as env vars. Up to 10 accounts supported (`_2` through `_10`).

---

## Current Account

- **User:** Adrian Max (`maxadrian321@gmail.com`)
- **Team:** Aiston (Team ID: `18905505`)
- **workos_id:** `user_01KFD8FZX10GWJECRFFD9JG0FM`
- **Session token expires:** ~2026-06-24 (JWT exp: 1777226560)

---

## How Cookie Update Works

When session expires:
1. Monitor sends email alert with instructions
2. Log into cursor.com in browser
3. Use Cookie-Editor extension → Export cookies (any format: JSON, Netscape, Cookie-Editor array)
4. Go to Render → Environment → Update `SESSION_COOKIES` with exported cookies
5. **Important:** Make sure `team_id` in cookies matches your current team
6. Save → auto-redeploys → monitor auto-extracts invite link and resumes

---

## How Rejoin Works (Flow)

```
Every 1s: POST /api/dashboard/get-team-invite-link with team_id
  ├── 200 OK → still on team, save invite link
  ├── 401 Unauthorized → REMOVED FROM TEAM!
  │   ├── Send removal email (background thread, non-blocking)
  │   ├── POST /api/accept-invite with inviteCode from saved link
  │   ├── Retry up to 60 times (100ms → 500ms → 2s spacing)
  │   ├── On success → log, email, resume monitoring
  │   └── On failure → email alert, keep monitoring
  └── Error → increment error counter, retry next cycle
```

---

## GitHub

- **Repo:** https://github.com/maxidua454/cursor-invite-monitor
- **User:** maxidua454
- **Branch:** master (only branch)
- **Latest commit:** `018dff9` — Support Netscape HTTP Cookie File format for SESSION_COOKIES

---

## Version History (Key Commits)

| Commit | Description |
|---|---|
| `018dff9` | Support Netscape HTTP Cookie File format for SESSION_COOKIES |
| `dc82972` | Fix rejoin: use POST /api/accept-invite with inviteCode |
| `5facb69` | Handle HEAD requests for UptimeRobot compatibility |
| `21b5e10` | Accept Cookie-Editor export format directly |
| `fbe2d8d` | Multi-account support — monitor multiple teams simultaneously |
| `fd264b5` | v8: Ultra-fast monitor — 1s checks, instant rejoin, full event log |
| `188f464` | Auto-rejoin team instantly when removed |
| `84878fc` | v7: Pure HTTP monitor with cookie-based auth |

---

## Known Issues / Notes

1. **Render free plan** sleeps after inactivity — use UptimeRobot to ping the health endpoint to keep it alive
2. **Invite link must exist** — if the team admin revokes/rotates the invite link AND you get removed before the monitor picks up the new link, rejoin fails. Monitor emails you about this.
3. **Same session cookies = same account** — don't add the same account as both SESSION_COOKIES and SESSION_COOKIES_2
4. **Environment Group on Render** — there's an env group called "cursor" but it's NOT linked to the service. All env vars are set directly on the service.
5. **config.json on Render** — doesn't exist on Render (Docker build doesn't copy it). All config comes from env vars. The monitor auto-creates config.json from env vars on startup.
