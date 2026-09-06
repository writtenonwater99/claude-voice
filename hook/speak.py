#!/usr/bin/env python3
"""Claude Code -> voice hook (WSL side). Stdlib only, runs under /usr/bin/python3.

Wired to three events (settings.json):
  MessageDisplay  each batch of displayed assistant lines -> spoken as it lands
  Stop            fallback only: speaks last_assistant_message if nothing was
                  spoken for this session in the last STOP_FALLBACK_S seconds
  Notification    permission_prompt|idle_prompt -> priority line

It never synthesizes. It writes one small JSON job into the Windows-side spool
(VOICE_DIR/spool) and exits in a few ms; speaker.py on Windows does the rest.
If the speaker's heartbeat is stale it is respawned (rate-limited).

Per-message budget: the first BUDGET chars of each assistant message are spoken
verbatim as they stream. If the message runs longer, the remainder is handed at
`final` to a detached summarizer (`claude -p` on Haiku, recursion-guarded) and
the 1-3 sentence summary is spoken as the tail -- the old opener-overlap trick,
now with the opener streamed instead of scraped from the transcript.

Kill switch: touch ~/.claude/tts-off (delete to re-enable).
"""
import json
import os
import re
import subprocess
import sys
import time

# The voice dir is wherever this file's repo lives (hook/ is one level down). It must be
# on the Windows filesystem (/mnt/c/...) because speaker.py runs there.
VOICE_DIR = os.environ.get("CLAUDE_VOICE_DIR") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
SPOOL = os.path.join(VOICE_DIR, "spool")
HEARTBEAT = os.path.join(VOICE_DIR, "heartbeat")
STATE = os.path.expanduser("~/.claude/tts-state")
LOG = os.path.expanduser("~/.claude/tts.log")
OFF = os.path.expanduser("~/.claude/tts-off")
GUARD_ENV = "CLAUDE_TTS_SUMMARIZER"

BUDGET = 480              # verbatim chars per assistant message
SUMMARY_MIN = 200         # remainder shorter than this is just spoken, not summarized
SUMMARY_MODEL = os.environ.get("CLAUDE_TTS_SUMMARY_MODEL", "claude-haiku-4-5-20251001")
SUMMARY_TIMEOUT_S = 45
STOP_FALLBACK_S = 20      # Stop speaks last_assistant_message only if silent this long
SPAWN_COOLDOWN_S = 30
HEARTBEAT_STALE_S = 6

NOTIFY = {"permission": "Claude needs permission.", "idle": "Claude is waiting for input."}
FENCE_RE = re.compile(r"^\s*(```|~~~)")


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + msg + "\n")
    except Exception:
        pass


def enqueue(text, kind="speak", **meta):
    if not text or not text.strip():
        return
    os.makedirs(SPOOL, exist_ok=True)
    prio = 0 if kind == "notify" else 1
    name = f"{prio}-{time.time_ns()}-{os.getpid()}.json"
    job = {"kind": kind, "text": text, "ts": time.time(), **meta}
    tmp = os.path.join(SPOOL, name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(job, f)
    os.replace(tmp, os.path.join(SPOOL, name))
    _touch(os.path.join(STATE, "last-enqueue"))
    _ensure_speaker()


def _touch(path):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def _age(path):
    try:
        return time.time() - os.path.getmtime(path)
    except OSError:
        return 1e9


def _ensure_speaker():
    """Respawn speaker.py on Windows if its heartbeat is stale. Rate-limited."""
    if _age(HEARTBEAT) < HEARTBEAT_STALE_S:
        return
    marker = os.path.join(STATE, "spawned-at")
    if _age(marker) < SPAWN_COOLDOWN_S:
        return
    _touch(marker)
    try:
        win = subprocess.run(["wslpath", "-w", VOICE_DIR], capture_output=True, text=True,
                             timeout=5).stdout.strip()
        py = win + "\\.venv\\Scripts\\python.exe"
        subprocess.Popen(["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command",
                          f"& '{py}' '{win}\\speaker.py' start"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        log("speaker: heartbeat stale, spawn requested")
    except Exception as e:
        log(f"speaker: spawn failed {e!r}")


# ------------------------------------------------------------ MessageDisplay

def _state_path(message_id):
    return os.path.join(STATE, f"msg-{message_id}.json")


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"spoken": 0, "rest": "", "in_fence": False, "head": ""}


def _save(path, st):
    os.makedirs(STATE, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, path)


def _strip_fences(delta, in_fence):
    """Drop fenced code blocks (they may span batches); return (prose, in_fence)."""
    keep = []
    for line in delta.splitlines(keepends=True):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            keep.append(line)
    return "".join(keep), in_fence


_SENT_END_RE = re.compile(r"[.!?][\"')\]]?(?=\s)")


def _split_at_budget(prose, room):
    """Speak up to `room` chars, cut at the last sentence end before the limit
    (allowing a modest overrun to finish a sentence). Rest goes to the summary."""
    if len(prose) <= room + 80:
        return prose, ""
    cut = None
    for m in _SENT_END_RE.finditer(prose):
        if m.end() <= room + 80:
            cut = m.end()
        else:
            break
    if cut is None or cut < 40:
        cut = room
        sp = prose.rfind(" ", 0, room)
        if sp > 40:
            cut = sp
    return prose[:cut], prose[cut:]


def on_message_display(d):
    mid = d.get("message_id") or "unknown"
    path = _state_path(mid)
    st = _load(path)
    prose, st["in_fence"] = _strip_fences(d.get("delta") or "", st["in_fence"])
    if prose.strip():
        room = BUDGET - st["spoken"]
        if room > 0:
            head, tail = _split_at_budget(prose, room)
            enqueue(head, message_id=mid, index=d.get("index"))
            st["spoken"] += len(head)
            st["head"] = (st["head"] + head)[-600:]
            st["rest"] += tail
        else:
            st["rest"] += prose
    if d.get("final"):
        rest = st["rest"].strip()
        try:
            os.remove(path)
        except OSError:
            pass
        if rest:
            if len(rest) < SUMMARY_MIN:
                enqueue(rest, message_id=mid, index="tail")
            else:
                _spawn_summarizer(mid, st["head"], rest)
        return
    _save(path, st)


def _spawn_summarizer(mid, head, rest):
    os.makedirs(STATE, exist_ok=True)
    payload = os.path.join(STATE, f"sum-{mid}.json")
    with open(payload, "w", encoding="utf-8") as f:
        json.dump({"head": head, "rest": rest, "message_id": mid}, f)
    env = dict(os.environ, **{GUARD_ENV: "1"})
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "--summarize", payload],
                     env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    log(f"summarizer: spawned for {mid[:8]} rest={len(rest)} chars")


def _extract_fallback(rest):
    out = []
    for para in re.split(r"\n\s*\n", rest):
        para = " ".join(para.split())
        if not para:
            continue
        m = re.match(r"(.+?[.!?])(\s|$)", para)
        out.append(m.group(1) if m else para[:160])
        if len(" ".join(out)) > 400:
            break
    return " ".join(out)


def summarize(payload):
    with open(payload, encoding="utf-8") as f:
        p = json.load(f)
    try:
        os.remove(payload)
    except OSError:
        pass
    prompt = ("You are voicing the rest of a message that a listener has ALREADY heard the "
              "beginning of. Summarize ONLY the remainder in at most three short spoken "
              "sentences. Do not repeat the beginning. No markdown, no lists, no code.\n\n"
              f"BEGINNING (already heard):\n{p['head']}\n\nREMAINDER (summarize this):\n{p['rest']}")
    text = ""
    try:
        r = subprocess.run(["claude", "-p", "--model", SUMMARY_MODEL, "--safe-mode", prompt],
                           capture_output=True, text=True, timeout=SUMMARY_TIMEOUT_S,
                           env=dict(os.environ, **{GUARD_ENV: "1"}))
        text = (r.stdout or "").strip()
        if r.returncode != 0 or not text:
            log(f"summarizer: claude rc={r.returncode} stderr={(r.stderr or '')[:120]!r}")
            text = ""
    except Exception as e:
        log(f"summarizer: {e!r}")
    if not text:
        text = _extract_fallback(p["rest"])
    enqueue(text[:700], message_id=p["message_id"], index="summary")
    log(f"summarizer: spoke {len(text)} chars for {p['message_id'][:8]}")


# ------------------------------------------------------------ Stop / Notification

def on_stop(d):
    if _age(os.path.join(STATE, "last-enqueue")) < STOP_FALLBACK_S:
        return
    text = (d.get("last_assistant_message") or "").strip()
    if not text:
        return
    prose, _ = _strip_fences(text, False)
    enqueue(prose[:BUDGET + 200], index="stop-fallback")
    log("stop: fallback spoke last_assistant_message")


def on_notification(d):
    typ = (d.get("notification_type") or "").lower()
    msg = (d.get("notification_message") or d.get("message") or "").lower()
    if "permission" in typ or "permission" in msg:
        kind = "permission"
    elif "idle" in typ or "waiting" in msg or "idle" in msg:
        kind = "idle"
    else:
        return
    enqueue(NOTIFY[kind], kind="notify")


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--summarize":
        return summarize(sys.argv[2])
    if os.environ.get(GUARD_ENV) or os.path.exists(OFF):
        return
    try:
        d = json.load(sys.stdin)
    except Exception:
        return
    if d.get("agent_id"):          # subagent chatter is not for the ear
        return
    ev = d.get("hook_event_name")
    if ev == "MessageDisplay":
        on_message_display(d)
    elif ev == "Stop":
        on_stop(d)
    elif ev == "Notification":
        on_notification(d)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"hook: ERROR {e!r}")
    # never block the harness; never emit JSON (original text is displayed)
    sys.exit(0)
