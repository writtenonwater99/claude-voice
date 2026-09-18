#!/usr/bin/env python3
"""Codex CLI -> voice hook (WSL side). Thin adapter over speak.py; stdlib only.

Codex has no MessageDisplay event, so nothing streams: the whole turn arrives at
Stop as last_assistant_message. It is fed through speak.py's MessageDisplay path
as one final batch, so the budget, fence stripping and summary tail all behave
exactly as they do for Claude.

Wired in ~/.codex/hooks.json:
  Stop               speaks the turn's last assistant message
  PermissionRequest  priority line, never decides (no stdout -> Codex asks as usual)

Same kill switch as speak.py: touch ~/.claude/tts-off.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import speak  # noqa: E402


def main():
    if os.environ.get(speak.GUARD_ENV) or os.path.exists(speak.OFF):
        return
    try:
        d = json.load(sys.stdin)
    except Exception:
        return
    ev = d.get("hook_event_name")
    if ev == "Stop":
        text = (d.get("last_assistant_message") or "").strip()
        if not text:
            return
        mid = "codex-" + str(d.get("turn_id") or d.get("session_id") or "unknown")
        speak.on_message_display({"message_id": mid, "delta": text, "index": 0, "final": True})
    elif ev == "PermissionRequest":
        speak.enqueue("Codex needs permission.", kind="notify")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        speak.log(f"codex hook: ERROR {e!r}")
    # never block Codex, never decide anything for it
    sys.exit(0)
