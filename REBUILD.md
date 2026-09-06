# REBUILD.md — for the AI agent rebuilding this on a new machine

You are reading this because someone pointed you at this repo and said "make Claude Code
talk again." Everything you need is here. Read this whole file before running anything.

## What this is

Claude Code (running in WSL2) speaks its replies out loud through the Windows audio device,
in one specific voice — **Bella** — with ~0.2 s latency, and lowers other apps' audio while
speaking. The author consumes a lot of Claude's output by ear; this is not a novelty.

## Target environment (verify before you start)

| Requirement | How to check | Notes |
|---|---|---|
| Windows 10/11 + WSL2 with interop | `powershell.exe -c 'echo ok'` from WSL prints `ok` | the hook spawns the Windows service through `powershell.exe` |
| Claude Code ≥ 2.1.26x in WSL | `claude --version` | needs the `MessageDisplay` hook event (2026) |
| Python 3 in WSL (stdlib only) | `python3 --version` | the hook uses nothing outside stdlib |
| `winget` on Windows | `winget --version` in PowerShell | installs `uv`; if absent, install uv by hand |
| **No** system Python needed on Windows | — | `uv` fetches its own 3.12. The Store `python.exe` alias is a stub, ignore it |
| ~1 GB disk, ~1.5 GB RAM resident | — | Kokoro 340 MB + Pocket ~400 MB + torch-cpu |
| A default Windows output device | `Get-CimInstance Win32_SoundDevice` | the service opens the *default* device per utterance |

The repo **must be cloned onto the Windows filesystem** (e.g. `C:\Users\<you>\voice`,
which WSL sees as `/mnt/c/Users/<you>/voice`). Both sides read it; the hook derives every
path from its own location, so no path editing is needed.

## Build steps (in this order)

1. **Windows, PowerShell, in the repo:** `powershell -ExecutionPolicy Bypass -File .\install.ps1`
   Expected tail: `54 passed`, `service: alive`, a spoken line, and a log line like
   `spoke speak pieces=2 first-audio=0.2s`. If it says `pocket: load failed` → step 3.
2. **WSL, in the repo:** `bash install.sh` — merges the three hooks into `~/.claude/settings.json`
   (keeps whatever hooks exist there), sends one line through the hook (you should hear it).
   Then **restart Claude Code** — hook config is snapshotted at session start.
3. **Fast engine (optional, and the default config):** Pocket-TTS's voice-cloning weights are gated.
   The person you are working for must accept terms at https://huggingface.co/kyutai/pocket-tts and provide a
   Hugging Face token **with the "read gated repos" scope** (fine-grained tokens lack it by
   default). Then on Windows: `.venv\Scripts\hf.exe auth login`, restart the service
   (`speaker.py stop` / `start`). The cloned voice state `voices/bella.safetensors` is committed,
   so no re-cloning is needed. Without this step the service runs Kokoro — same voice, ~0.4 s slower.
4. **Mic input** (Claude Code's own `/voice`, separate from this repo): in a plain WSL tab,
   `sudo apt update && sudo apt install -y sox libsox-fmt-pulse`. If capture hangs, the Windows
   default *microphone* is probably a dead endpoint (webcam) — switch it in Sound settings.

## Verify (done means all of these)

```
# Windows
.venv\Scripts\python.exe speaker.py status          -> service: alive, queue: 0
.venv\Scripts\python.exe speaker.py say "test"      -> heard; speaker.log shows first-audio < 0.5s
.venv\Scripts\python.exe duck_probe.py              -> lists other apps' audio sessions (ducking targets)
# WSL
echo '{"hook_event_name":"Notification","notification_type":"permission_prompt"}' | python3 hook/speak.py
                                                    -> "Claude needs permission." is heard within ~1 s
time (echo '{"hook_event_name":"MessageDisplay","message_id":"x","index":0,"final":true,"delta":"hi\n"}' | python3 hook/speak.py)
                                                    -> real < 0.2s (this hook blocks the screen)
# Then a real Claude Code session: replies are spoken as they display; a permission prompt is announced.
```

## Architecture and the reasons (read before changing anything)

```
Claude Code (WSL)                                  Windows
 MessageDisplay ─┐  hook/speak.py (stdlib, ~100 ms)   speaker.py (resident, pythonw, single-instance mutex)
 Notification ───┼──► writes 1 JSON job ──► spool/ ──► clean (speakability.py) ─► sentence split
 Stop (fallback) ┘    atomic rename                    ─► Pocket-TTS streams chunks (Kokoro fallback)
                      touches state, exits             ─► sounddevice → WASAPI default device
                      respawns service if heartbeat stale   ─► pycaw lowers other sessions to duck_factor
```

- **Why synthesis is on Windows:** WSLg audio playback stutters; the previous rig (13k lines,
  repo `voice-stack`) generated audio in WSL and shipped wav files to a PowerShell/NAudio player,
  which required seam-stitching, a wire protocol, a fallback ladder and PID-based ducking.
  Moving synthesis to Windows deleted ~60 % of that. Do not move it back.
- **Why a file spool and not TCP:** WSL→Windows localhost is NAT-mode dependent and trips the
  firewall; a spool on the shared filesystem needs nothing, is visible for debugging, and survives
  a service restart. Poll interval 60 ms. Priority is the filename prefix (`0-` notify, `1-` speech).
- **Why `MessageDisplay`:** it delivers completed lines with `message_id`/`index`/`final` while
  Claude streams, which replaced transcript scraping, a quiescence gate and per-transcript
  counters. It **blocks the screen until the hook returns** — the hook must only write a file.
- **Per-message budget (480 chars):** first part verbatim as it streams; if the message runs
  longer, the remainder goes at `final` to a detached `claude -p` (Haiku, recursion-guarded by
  `CLAUDE_TTS_SUMMARIZER=1`) and the 1–3-sentence summary is spoken as the tail. Fenced code is
  never spoken. `Stop` only speaks if nothing was enqueued in the last 20 s (safety net).
- **Engine of record: Pocket-TTS with a clone of Kokoro `af_bella`.** The author A/B'd all 11
  Pocket catalog female voices against Bella (Bella won), then the clone against the original
  (judged equal) — first audio 0.17 s vs 0.5 s. Pocket needs its gated weights at load; if
  missing, Kokoro loads instead. `config.json: engine` forces either.
- **Ducking:** `pycaw` `SimpleAudioVolume` on every other session, restored `duck_release_ms`
  after the queue drains. A ledger file is written while ducked; on start the service repairs
  from it (Windows *persists* per-app volume, so a hard kill mid-duck would otherwise leave apps
  quiet forever). Default depth: 0.4.
- **Lifecycle:** heartbeat file touched every 1 s by its own thread (a 40-s utterance must not
  look like death). The hook respawns the service when the heartbeat is > 6 s old, at most once
  per 30 s. A Windows named mutex prevents two instances; `run` waits up to 10 s for it during a
  restart. `stop` is a flag file honored at the next sentence boundary; after cleanup the process
  hard-exits (`os._exit`) because torch teardown can otherwise hold the mutex for many seconds.
- **`os.kill` on Windows is TerminateProcess** — no cleanup runs. Never use it as the primary stop.

## Knobs

`config.json`: `engine` (`pocket`|`kokoro`), `pocket_voice`, `pocket_threads`, `voice` (Kokoro id),
`speed` (Kokoro only), `duck_factor`, `duck_release_ms`, `sentence_pause_ms`, `poll_ms`.
Restart the service after edits. Hook constants (budget, timeouts) are at the top of `hook/speak.py`.
Mute: `touch ~/.claude/tts-off` (WSL) or an `off` file beside `speaker.py`.
Jargon: `glossary.json` (shipped examples) + `glossary.local.json` (gitignored, private project names) —
both loaded by `speakability.py`; ALL-CAPS tokens in `codenames` are spoken as their value.

## Gotchas met during the first build (2026-09-06), so you don't meet them again

- winget's `uv` is at `%LOCALAPPDATA%\Microsoft\WinGet\Packages\astral-sh.uv_*\uv.exe`, not the
  WindowsApps alias path — `install.ps1` searches both.
- `hf auth login --add-to-git-credential` fails when Windows has no git; the token file is
  simply `%USERPROFILE%\.cache\huggingface\token` containing the bare token.
- A 403 on the gated repo with a valid token = terms not accepted **or** fine-grained token
  without gated-repo scope. A 404 on `config.json` is normal (file doesn't exist); check by loading.
- Hugging Face downloads sometimes fail transiently unauthenticated (rate limit); retry.
- The venv `pythonw.exe` is a launcher: each service shows as **two** `pythonw.exe` processes
  (launcher + interpreter). Count pairs. Find them with
  `Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" | ? CommandLine -like '*speaker.py*'`.
- Windows and WSL clocks were in sync on this machine; if `enqueue-to-first` in the log looks
  absurd, compare `date` and `Get-Date` before debugging.
- The old test file expected the module under `~/.claude/tts`; it now defaults to the sibling
  file. `SPEAKABILITY_PATH` still overrides.

## What was deliberately left out

Logon autostart (respawn covers it; ~3 s on the first line after a reboot) · GPU (Pocket is CPU
by design) · tmux fleet talkback (`voice-companion` repo, parked) ·
the v1 chunk-profile env pins (no chunk files → no seams → nothing to pin).
