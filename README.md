# claude-voice — Claude Code talks back

Local, offline voice output for [Claude Code](https://claude.com/claude-code) on **WSL2**.
Claude speaks each reply as it appears on screen, says *"Claude needs permission"* when it's
blocked on you, and turns other apps' audio down while it talks — so you can walk away from
the screen. First audio lands **~0.2 s** after a line is displayed.

Everything runs on your machine. No API keys, no cloud TTS. (One optional call: replies longer
than ~480 characters get a 3-sentence spoken summary from Haiku through your existing `claude` CLI.)

🔊 Hear it: [`samples/bella-pocket-clone.wav`](samples/bella-pocket-clone.wav) (the default voice) ·
[`samples/bella-kokoro.wav`](samples/bella-kokoro.wav) (the fallback engine, same voice)

## Why this exists

WSL2 can't play audio well (WSLg playback stutters), Claude Code has no built-in speech, and
the hooks it does have were designed for text. This repo is the small amount of glue that makes
those three things add up to a good listening experience:

| Problem | What this does about it |
|---|---|
| Audio from inside WSL crackles | Synthesis and playback run **on the Windows side**; WSL only ships text |
| Speech starts late | Streams sentence-by-sentence while the reply is still being written (`MessageDisplay` hook); Pocket-TTS streams sub-sentence chunks |
| Markdown sounds terrible read aloud | `speakability.py` turns tables, bullets, paths, hashes, dates, money, emoji and jargon into prose (54 golden tests) |
| Long replies drone on | First 480 chars verbatim, the rest summarized in ≤3 sentences and spoken as the tail |
| Music drowns it out | Other audio sessions are lowered to 40 % while speaking (CoreAudio via `pycaw`), restored after |
| It talks over itself | One priority queue; a permission alert jumps in at the next sentence boundary; nothing is dropped or repeated |
| The service dies | The hook notices a stale heartbeat and respawns it; single-instance guard; crash-safe volume restore |

## How it works

```
Claude Code (WSL)                                    Windows
 MessageDisplay ─┐   hook/speak.py (stdlib, ~100 ms)    speaker.py (resident)
 Notification ───┼─▶ one small JSON job ──▶ spool/ ──▶ clean text ─▶ split sentences
 Stop (fallback) ┘   exits immediately                ─▶ Pocket-TTS (CPU, streaming) or Kokoro
                                                      ─▶ sounddevice ─▶ your default output device
                                                      ─▶ pycaw ducks everything else
```

The voice is **Bella** (Kokoro's `af_bella`), cloned onto [Kyutai Pocket-TTS](https://github.com/kyutai-labs/pocket-tts)
for streaming and ~3× lower first-audio latency. Kokoro itself stays as the automatic fallback.
Any Kokoro voice, any Pocket catalog voice, or a clone of any 10-second clip works — see *Changing the voice*.

## Install

Requirements: Windows 10/11 with WSL2, Claude Code (2026 builds with the `MessageDisplay` hook),
`python3` inside WSL, `winget` on Windows. No Python needed on Windows — `uv` brings its own.

```powershell
# 1. Windows (PowerShell). Clone onto the Windows filesystem, not inside WSL's ext4.
git clone https://github.com/writtenonwater99/claude-voice C:\Users\<you>\voice
cd C:\Users\<you>\voice
powershell -ExecutionPolicy Bypass -File .\install.ps1     # uv, venv, models, tests, starts the service, speaks
```
```bash
# 2. WSL
cd /mnt/c/Users/<you>/voice && bash install.sh              # wires the hooks into ~/.claude/settings.json
```
3. Restart Claude Code (hook config is read at session start). Done.

**Fast engine (optional):** Pocket-TTS's voice-cloning weights are gated. Accept the terms at
[hf.co/kyutai/pocket-tts](https://huggingface.co/kyutai/pocket-tts), then on Windows
`.venv\Scripts\hf.exe auth login` with a token that has *read access to gated repos*, and restart
the service. Without this you get the same voice on Kokoro, ~0.4 s slower to start.

**Mic input** is Claude Code's own `/voice` and needs `sudo apt install sox libsox-fmt-pulse` in WSL.

## Daily use

| | |
|---|---|
| Mute / unmute | `touch ~/.claude/tts-off` / `rm ~/.claude/tts-off` |
| Service | `.venv\Scripts\python.exe speaker.py start \| stop \| status` (Windows, in the repo) |
| Say something | `.venv\Scripts\python.exe speaker.py say "hello"` |
| Logs | `speaker.log` (Windows side) · `~/.claude/tts.log` (hook side) |
| Ducking depth | `duck_factor` in `config.json` (0.4 = others at 40 %), then restart the service |
| Private jargon | `glossary.local.json` (gitignored) — ALL-CAPS project names and words the voice mangles |

## Changing the voice

- **Another Kokoro voice:** `"engine": "kokoro", "voice": "af_heart"` in `config.json` (54 voices; `speed` applies).
- **A Pocket catalog voice:** `python export_voice.py alba alba` → `"pocket_voice": "voices/alba.safetensors"`.
- **Clone a voice:** `python export_voice.py path\to\clip.wav myvoice` (clean 5–10 s sample) → point `pocket_voice` at it.
- **Audition:** `python audition.py` renders the same paragraph through Kokoro and several Pocket voices; `python play.py` plays them back to back.

## Rebuilding / handing to an AI agent

`REBUILD.md` is written for an agent: environment checks, build order, verification commands with
expected output, the architecture with its reasons, and every gotcha met while building it.

## Credits and licenses

MIT. Voices: [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) (Apache-2.0, via
[kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx), MIT); [Pocket-TTS](https://github.com/kyutai-labs/pocket-tts)
by Kyutai (code MIT; model weights under Kyutai's terms, voice cloning gated). `voices/bella.safetensors`
is a Pocket voice state derived from a clip synthesized with Kokoro's Apache-2.0 `af_bella`.
Ducking via [pycaw](https://github.com/AndreMiras/pycaw). Predecessor: [claude-code-talkback](https://github.com/writtenonwater99/claude-code-talkback).
