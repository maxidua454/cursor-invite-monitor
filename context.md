# Cursor Invite Link Monitor — Full Context

**Last updated:** 2026-04-14

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
  - **Web dashboard** — HTML dashboard at service URL with auto-refresh every 5s
  - **JSON API** — `/api` endpoint returns full status as JSON
  - **Event log** — `/events` endpoint, all events logged with ms precision
  - **Health endpoint** — `/health` for UptimeRobot (supports HEAD requests)
  - **Multi-account** — up to 10 accounts via `SESSION_COOKIES`, `SESSION_COOKIES_2`, etc.
  - **3 cookie formats supported** — JSON dict, Cookie-Editor JSON array, Netscape HTTP Cookie File
  - **Self-healing** — auto-detects session expiry, waits for new cookies, resumes monitoring
  - **Link change detection** — detects when invite link rotates, auto-updates and saves

### Key Code Locations in monitor.py
- **Lines 165-210:** Cookie parsing — `parse_netscape_cookies()`, `is_netscape_format()`, `normalize_cookies()`
- **Lines 230-260:** `load_cookies()` — loads from env var or file, tries JSON then Netscape
- **Lines 320-400:** `CursorHTTP` class — session check, invite link API, join with invite link
- **Lines 404-424:** `send_email()` — Gmail SMTP alerts
- **Lines 700-780:** Initial session validation + invite link extraction on startup
- **Lines 840-943:** Main monitoring loop — removal detection + rejoin logic
- **Lines 945-990:** Normal check — link change detection + error handling

### Supporting Files
- `Dockerfile` — Python 3.11 slim, installs requirements, runs monitor.py
- `render.yaml` — Render.com deployment config (free plan, Docker runtime)
- `requirements.txt` — Only dependency: `requests`
- `config.example.json` — Template config for local development
- `.gitignore` — Excludes config.json, cookies.json, logs, events.json, __pycache__

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
- **Trigger deploy:** `curl -X POST "https://api.render.com/v1/services/srv-d700cr75gffc73dja0k0/deploys" -H "Authorization: Bearer rnd_g0ymr9RLAiMslKkNUaWvIT4Wg7u7" -H "Accept: application/json"`
- **List env vars:** `curl "https://api.render.com/v1/services/srv-d700cr75gffc73dja0k0/env-vars" -H "Authorization: Bearer rnd_g0ymr9RLAiMslKkNUaWvIT4Wg7u7" -H "Accept: application/json"`
- **Update env var:** `curl -X PUT "https://api.render.com/v1/services/srv-d700cr75gffc73dja0k0/env-vars/KEY_NAME" -H "Authorization: Bearer rnd_g0ymr9RLAiMslKkNUaWvIT4Wg7u7" -H "Accept: application/json" -H "Content-Type: application/json" -d '{"value": "NEW_VALUE"}'`
- **Add env var:** `curl -X POST "https://api.render.com/v1/services/srv-d700cr75gffc73dja0k0/env-vars" -H "Authorization: Bearer rnd_g0ymr9RLAiMslKkNUaWvIT4Wg7u7" -H "Content-Type: application/json" -d '[{"key":"KEY","value":"VAL"}]'`

### Environment Variables on Render
| Variable | Description | Current Value |
|---|---|---|
| `SESSION_COOKIES` | Account 1 cookies (JSON or Netscape) | Set (user_01KFD8FZX10GWJECRFFD9JG0FM) |
| `ACCOUNT_NAME` | Display name for account 1 | `Adrian Max - Aiston Team` |
| `CHECK_INTERVAL` | Seconds between checks | `1` |
| `NOTIFICATION_EMAIL` | Email for alerts | `maxadrian321@gmail.com` |
| `GMAIL_APP_PASSWORD` | Gmail app password for SMTP | Set (working — emails confirmed) |
| `PORT` | Web server port | `10000` |
| `PYTHONUNBUFFERED` | Force unbuffered output | `1` |

### Adding More Accounts
Add `SESSION_COOKIES_2`, `ACCOUNT_NAME_2` (and optionally `KNOWN_INVITE_LINK_2`) as env vars on the **service** (not environment group). Up to 10 accounts supported (`_2` through `_10`). Each account needs its own cookies from a **different** Cursor user — same cookies = same account, no point adding twice.

---

## Current Account

- **User:** Adrian Max (`maxadrian321@gmail.com`)
- **Team:** Aiston (Team ID: `18905505`)
- **workos_id:** `user_01KFD8FZX10GWJECRFFD9JG0FM`
- **Session token expires:** ~2026-06-24 (JWT exp: 1777226560)
- **Current invite link:** `https://cursor.com/team/accept-invite?code=1eed6596a8ad96c8dbd3ecaca0b5db4ee56761463949bdeb`

### Previous Team
- **Team:** Hanwha (Team ID: `19393905`) — no longer active, switched to Aiston on 2026-04-14

---

## How Cookie Update Works

When session expires:
1. Monitor detects expiry and sends email alert with step-by-step instructions
2. Log into cursor.com in browser
3. Use Cookie-Editor extension → Export cookies (any format: JSON, Netscape, Cookie-Editor array — all supported)
4. Update `SESSION_COOKIES` on Render — either via dashboard or API:
   ```
   curl -X PUT "https://api.render.com/v1/services/srv-d700cr75gffc73dja0k0/env-vars/SESSION_COOKIES" \
     -H "Authorization: Bearer rnd_g0ymr9RLAiMslKkNUaWvIT4Wg7u7" \
     -H "Content-Type: application/json" \
     -d '{"value": "PASTE_COOKIES_HERE"}'
   ```
5. **Important:** Make sure `team_id` in cookies matches your current team
6. Save → auto-redeploys → monitor auto-extracts invite link and resumes
7. No need to set `KNOWN_INVITE_LINK` manually — the monitor extracts it via API automatically

---

## Scenarios — What Happens When

### Scenario 1: Team owner removes your account
```
Second 0.000: Monitor calls get-team-invite-link API
Second 0.150: API returns 401 → REMOVAL DETECTED
Second 0.150: Email sent in background thread (non-blocking)
Second 0.150: POST /api/accept-invite with saved invite code
Second 0.300: API responds 200 → REJOINED
Second 1.300: Next check → captures current invite link
Result: Removed and rejoined within ~300ms, all automatic
```

### Scenario 2: Team owner changes invite link (no removal)
```
Second 0: API returns new link ≠ saved link
Second 0: LINK CHANGED detected
Second 0: New link saved to known_link + config
Second 0: Email alert sent
Result: New link captured within 1 second, no manual action needed
```

### Scenario 3: Owner changes link, then removes you later
```
- Link change detected in 1s → new link saved
- Later removal detected in 1s → rejoin uses NEW saved link
Result: Fully automatic, no intervention needed
```

### Scenario 4: Owner removes you AND changes link simultaneously
```
- This is the only dangerous scenario
- If removal + link change happen within the same 1-second check window:
  - Monitor tries to rejoin with OLD link → fails (expired/not found)
  - 60 attempts all fail → sends REJOIN FAILED email
  - You must manually get new invite link and update KNOWN_INVITE_LINK on Render
- Practically impossible for a human to do both within 1 second
```

### Scenario 5: Session cookies expire
```
- Monitor detects session expired (redirect to login or 403)
- Sends SESSION EXPIRED email with fix instructions
- Enters wait loop, checking every 30s for new cookies
- When you update SESSION_COOKIES on Render → auto-redeploy → resumes
```

---

## How Rejoin Works (Detailed Flow)

```
Every 1s: POST /api/dashboard/get-team-invite-link with team_id
  ├── 200 OK + link returned → still on team
  │   ├── Link same as saved → all good, continue
  │   └── Link different → LINK CHANGED! Save new link, email alert
  ├── 401 Unauthorized → REMOVED FROM TEAM!
  │   ├── Send removal email (background thread, non-blocking)
  │   ├── Check if known_link exists
  │   │   ├── No link → CRITICAL: cannot rejoin, email alert, continue monitoring
  │   │   └── Has link → start rejoin loop:
  │   │       ├── POST /api/accept-invite with inviteCode
  │   │       ├── Attempts 1-5: 100ms apart (ultra-fast)
  │   │       ├── Attempts 6-20: 500ms apart
  │   │       ├── Attempts 21-60: 2s apart
  │   │       ├── On success → REJOINED! Log, email, resume monitoring
  │   │       └── All 60 fail → REJOIN FAILED email, keep monitoring
  └── Error → increment error counter, retry next cycle
      └── 30 consecutive errors → force session recheck
```

---

## Dashboard Endpoints

| Endpoint | Description |
|---|---|
| `/` | HTML dashboard with auto-refresh (5s) — shows status, invite link, event log, link history |
| `/api` | JSON API — full status, account details, recent events |
| `/events` | Full event log as JSON array |
| `/health` | Health check — returns "ok" (supports GET and HEAD for UptimeRobot) |

---

## GitHub

- **Repo:** https://github.com/maxidua454/cursor-invite-monitor
- **User:** maxidua454
- **Branch:** master (only branch)
- **Latest commit:** `c7f16da` — Cleanup: remove obsolete files, add context.md, update team name

---

## Version History (Key Commits)

| Commit | Description |
|---|---|
| `c7f16da` | Cleanup: remove obsolete files, add context.md, update team name |
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

1. **Render free plan** sleeps after inactivity — use UptimeRobot to ping `/health` endpoint to keep it alive
2. **Invite link must exist** — if the team admin revokes the invite link AND removes you before the monitor picks up the new link, rejoin fails. Monitor emails you about this. Practically impossible within 1 second.
3. **Same session cookies = same account** — don't add the same account as both SESSION_COOKIES and SESSION_COOKIES_2. Each account needs cookies from a different Cursor user.
4. **Environment Group on Render** — there's an env group called "cursor" (`evg-d7evi177f7vs73dej2ug`) but it's NOT linked to the service. All env vars are set directly on the service. Ignore the env group.
5. **config.json on Render** — doesn't exist on Render (Docker build doesn't copy it). All config comes from env vars. The monitor auto-creates config.json from env vars on startup.
6. **`self.valid` flag in CursorHTTP** — gets set to False on 401 but is never checked before API calls. Not a bug — the rejoin and subsequent checks work fine regardless.
7. **Email alerts working** — confirmed working as of 2026-04-09 (session expiry email received). Uses Gmail SMTP with app password.
8. **Typical API response time** — ~130-170ms for invite link check, ~150-200ms for rejoin

---

## Quick Reference — Common Tasks

### Update session cookies
```bash
curl -X PUT "https://api.render.com/v1/services/srv-d700cr75gffc73dja0k0/env-vars/SESSION_COOKIES" \
  -H "Authorization: Bearer rnd_g0ymr9RLAiMslKkNUaWvIT4Wg7u7" \
  -H "Content-Type: application/json" \
  -d '{"value": "PASTE_COOKIES_HERE"}'
```

### Trigger manual deploy
```bash
curl -X POST "https://api.render.com/v1/services/srv-d700cr75gffc73dja0k0/deploys" \
  -H "Authorization: Bearer rnd_g0ymr9RLAiMslKkNUaWvIT4Wg7u7" \
  -H "Accept: application/json"
```

### Check current status
- Dashboard: https://cursor-invite-monitor.onrender.com
- JSON API: https://cursor-invite-monitor.onrender.com/api
- Events: https://cursor-invite-monitor.onrender.com/events

### Add second account
```bash
curl -X POST "https://api.render.com/v1/services/srv-d700cr75gffc73dja0k0/env-vars" \
  -H "Authorization: Bearer rnd_g0ymr9RLAiMslKkNUaWvIT4Wg7u7" \
  -H "Content-Type: application/json" \
  -d '[{"key":"SESSION_COOKIES_2","value":"PASTE_COOKIES"},{"key":"ACCOUNT_NAME_2","value":"Account Name"}]'
```
