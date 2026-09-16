#!/usr/bin/env python3
"""inbox/ -> the tmux pane running Claude Code (WSL side). Stdlib only.

listener.py (Windows) drops one JSON per utterance into VOICE_DIR/inbox. This
watches that directory and types the text into a tmux pane, optionally pressing
Enter. It is the only half that knows what a session is; listener.py is the only
half that knows what a microphone is.

tmux is the injection path because it is the one that works: the desktop app has
no text-injection surface at all, and OS-level SendKeys types into whatever window
happens to be focused -- fine until you alt-tab mid-sentence.

Run it in any WSL tab (it outlives individual Claude sessions):
    python3 hook/inject.py run
Pick a target explicitly when more than one Claude is open:
    python3 hook/inject.py panes
    python3 hook/inject.py pin %3
Config (VOICE_DIR/config.json): inject_auto_submit, inject_target, inject_poll_ms.
"""
import json
import os
import subprocess
import sys
import time

VOICE_DIR = os.environ.get("CLAUDE_VOICE_DIR") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
INBOX = os.path.join(VOICE_DIR, "inbox")
UNDELIVERED = os.path.join(INBOX, "undelivered")
CONFIG = os.path.join(VOICE_DIR, "config.json")
PIN = os.path.expanduser("~/.claude/voice-target")
LOG = os.path.expanduser("~/.claude/voice-in.log")

DEFAULTS = dict(inject_auto_submit=True, inject_target=None, inject_poll_ms=150)
DELIVERY_GRACE_S = 120      # no pane for this long -> park the text rather than spin on it


def cfg():
    c = dict(DEFAULTS)
    try:
        with open(CONFIG, encoding="utf-8") as f:
            c.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    except Exception:
        pass
    return c


def log(msg):
    line = time.strftime("%H:%M:%S ") + msg
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


def _tmux(*args, timeout=5):
    return subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=timeout)


def panes():
    """Every pane, newest-active first, with a guess at which is Claude Code."""
    fmt = "#{pane_id}\t#{pane_current_command}\t#{window_name}\t#{pane_active}\t#{window_activity}"
    r = _tmux("list-panes", "-a", "-F", fmt)
    if r.returncode != 0:
        return []
    out = []
    for line in r.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        pid, cmd, win, active, activity = parts[:5]
        out.append(dict(id=pid, cmd=cmd, window=win, active=active == "1",
                        activity=int(activity or 0),
                        claude=cmd in ("claude", "node", "claude-code")))
    out.sort(key=lambda p: (p["claude"], p["activity"]), reverse=True)
    return out


def target(c):
    """Pinned pane wins, then config, then the most recently active claude pane."""
    for pinned in (_read(PIN), c["inject_target"]):
        if pinned:
            if any(p["id"] == pinned for p in panes()):
                return pinned
            log(f"target {pinned} is gone; falling back to auto-detect")
    for p in panes():
        if p["claude"]:
            return p["id"]
    return None


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def send(pane, text, submit):
    """-l sends the text literally, so a transcript containing 'Enter' or ';'
    is typed rather than interpreted as key names."""
    r = _tmux("send-keys", "-t", pane, "-l", "--", text)
    if r.returncode != 0:
        log(f"send failed for {pane}: {r.stderr.strip()}")
        return False
    if submit:
        time.sleep(0.05)       # the TUI needs the paste to land before the newline
        r = _tmux("send-keys", "-t", pane, "Enter")
        if r.returncode != 0:
            log(f"submit failed for {pane}: {r.stderr.strip()}")
            return False
    return True


def run():
    c = cfg()
    os.makedirs(INBOX, exist_ok=True)
    poll = c["inject_poll_ms"] / 1000
    log(f"inject: watching {INBOX}  auto_submit={c['inject_auto_submit']}")
    first_failure = {}
    while True:
        try:
            names = sorted(f for f in os.listdir(INBOX) if f.endswith(".json"))
        except OSError:
            names = []
        for name in names:
            path = os.path.join(INBOX, name)
            try:
                with open(path, encoding="utf-8") as f:
                    job = json.load(f)
            except Exception:
                continue                       # still being written; next pass gets it
            text = (job.get("text") or "").strip()
            if not text:
                os.remove(path)
                continue
            pane = target(c)
            if pane and send(pane, text, c["inject_auto_submit"]):
                os.remove(path)
                first_failure.pop(name, None)
                log(f"-> {pane}: {text[:80]!r}")
                continue
            t0 = first_failure.setdefault(name, time.time())
            if time.time() - t0 > DELIVERY_GRACE_S:
                os.makedirs(UNDELIVERED, exist_ok=True)
                os.replace(path, os.path.join(UNDELIVERED, name))
                first_failure.pop(name, None)
                log(f"parked (no claude pane for {DELIVERY_GRACE_S}s): {text[:60]!r}")
            break                              # keep order: do not skip past a stuck utterance
        time.sleep(poll)


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "run"
    if cmd == "run":
        try:
            run()
        except KeyboardInterrupt:
            print()
        return 0
    if cmd == "panes":
        cur = target(cfg())
        for p in panes():
            print(f"{'*' if p['id'] == cur else ' '} {p['id']:<6} {p['cmd']:<12} "
                  f"{p['window']}{'  <- claude?' if p['claude'] else ''}")
        print(f"\n* = current target. Pin one: python3 hook/inject.py pin <pane_id>")
        return 0
    if cmd == "pin":
        if len(argv) < 3:
            print("usage: inject.py pin %3   (or 'pin auto' to clear)")
            return 2
        if argv[2] == "auto":
            try:
                os.remove(PIN)
            except OSError:
                pass
            print("target: auto-detect")
            return 0
        with open(PIN, "w") as f:
            f.write(argv[2])
        print("pinned", argv[2])
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv) or 0)
