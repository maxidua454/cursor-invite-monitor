#!/bin/bash
# Start Xvfb virtual display (so Chrome runs in "headed" mode = bypasses Cloudflare)
echo "[*] Starting Xvfb virtual display..."
Xvfb :99 -screen 0 1280x720x24 -nolisten tcp &
sleep 1
export DISPLAY=:99
echo "[*] Xvfb ready on :99"

# Start monitor
echo "[*] Starting Cursor Invite Monitor..."
exec python monitor.py
