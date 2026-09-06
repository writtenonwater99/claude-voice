#!/usr/bin/env bash
# install.sh -- WSL side. Wires the three Claude Code hooks into ~/.claude/settings.json.
# Idempotent: existing hooks (e.g. memory sweeps) are kept; the voice entries are added once.
# Run AFTER install.ps1 on the Windows side.  Usage: bash install.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK="$HERE/hook/speak.py"
SETTINGS="$HOME/.claude/settings.json"

case "$HERE" in /mnt/*) ;; *) echo "ERROR: repo must live on the Windows filesystem (/mnt/c/...), got $HERE"; exit 1;; esac
command -v python3 >/dev/null || { echo "ERROR: python3 missing in WSL"; exit 1; }
command -v powershell.exe >/dev/null || { echo "ERROR: powershell.exe not on PATH (WSL interop)"; exit 1; }
[ -f "$HERE/heartbeat" ] || echo "WARN: no heartbeat file -- run install.ps1 on Windows first (the hook will still try to spawn the service)"

mkdir -p "$HOME/.claude"
[ -f "$SETTINGS" ] || echo '{}' > "$SETTINGS"
cp "$SETTINGS" "$SETTINGS.bak-pre-voice.$(date +%Y%m%d%H%M%S)"

HOOK="$HOOK" SETTINGS="$SETTINGS" python3 - <<'PY'
import json, os
p, hook = os.environ["SETTINGS"], os.environ["HOOK"]
d = json.load(open(p))
h = d.setdefault("hooks", {})
cmd = f"python3 {hook} 2>/dev/null || true"
def entry(timeout, **extra):
    e = {"type": "command", "command": cmd, "timeout": timeout}; e.update(extra); return e
def has(groups):
    return any("hook/speak.py" in x.get("command", "") for g in groups for x in g.get("hooks", []))
if not has(h.get("MessageDisplay", [])):
    h.setdefault("MessageDisplay", []).append({"hooks": [entry(5)]})
if not has(h.get("Notification", [])):
    h.setdefault("Notification", []).append({"matcher": "permission_prompt|idle_prompt",
                                             "hooks": [entry(10, **{"async": True})]})
stop = h.setdefault("Stop", [])
if not has(stop):
    (stop[0] if stop else stop.append({"hooks": []}) or stop[0]).setdefault("hooks", []).append(entry(10, **{"async": True}))
json.dump(d, open(p, "w"), indent=2)
print("hooks wired:", ", ".join(k for k in ("MessageDisplay", "Notification", "Stop") if has(h.get(k, []))))
PY

echo '{"hook_event_name":"MessageDisplay","message_id":"install-check","index":0,"final":true,"delta":"Hook installed. Claude Code will talk after you restart the session.\n"}' | python3 "$HOOK"
echo "Sent a test line through the hook (listen). Restart Claude Code -- hook config is read at session start."
echo "Mute anytime: touch ~/.claude/tts-off"
