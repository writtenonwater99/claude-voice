#!/usr/bin/env python3
"""speaker.py -- resident Windows-side voice for Claude Code talkback.

    python speaker.py run            foreground service (pythonw.exe for detached)
    python speaker.py say "text"     drop a job into the spool (smoke test)
    python speaker.py notify "text"  same, priority lane
    python speaker.py status         heartbeat age + queue depth

Design (2026-09-06 rebuild): the WSL hooks only write small JSON jobs into
./spool/. This process holds Kokoro loaded, watches the spool, cleans the text
(speakability.py), splits it into sentences, synthesizes each one while the
previous one plays, and streams PCM straight into WASAPI through sounddevice.
No PowerShell, no wav files, no seams. Priority-0 jobs (notify) jump the queue
at the next sentence boundary; nothing is dropped.

Ducking: every other audio session is lowered to duck_factor while we speak
and restored duck_release_ms after the queue drains. A ledger file records what
was lowered so a crash mid-duck is repaired on the next start.

Kill switch: a file named `off` in this directory drains the spool silently.
Heartbeat: `heartbeat` is touched every loop; the hook respawns us when stale.
"""
import atexit
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
SPOOL = os.path.join(HERE, "spool")
HEARTBEAT = os.path.join(HERE, "heartbeat")
OFF = os.path.join(HERE, "off")
LOG = os.path.join(HERE, "speaker.log")
LEDGER = os.path.join(HERE, "duck_ledger.json")
PIDFILE = os.path.join(HERE, "speaker.pid")
STOPFLAG = os.path.join(HERE, "stop")
MUTEX_NAME = "Local\\ClaudeVoiceSpeaker"
CONFIG = os.path.join(HERE, "config.json")
MODEL = os.path.join(HERE, "models", "kokoro-v1.0.onnx")
VOICES = os.path.join(HERE, "models", "voices-v1.0.bin")
SR = 24000

DEFAULTS = dict(engine="pocket", pocket_voice="voices/bella.safetensors", pocket_threads=4,
                voice="af_bella", speed=1.15, lang="en-us", sentence_pause_ms=120,
                duck_factor=0.35, duck_release_ms=600, poll_ms=60, max_sentence_chars=300)


def cfg():
    c = dict(DEFAULTS)
    try:
        with open(CONFIG, encoding="utf-8") as f:
            c.update(json.load(f))
    except Exception:
        pass
    return c


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n"
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > 512_000:
            with open(LOG, encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-800:]
            with open(LOG, "w", encoding="utf-8") as f:
                f.writelines(tail)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    if sys.stdout and sys.stdout.isatty():
        print(line, end="", flush=True)


# ------------------------------------------------------------------ spool

def enqueue(text, kind="speak", meta=None):
    """Atomic drop into the spool. Used by `say`/`notify` and mirrored by the WSL hook."""
    os.makedirs(SPOOL, exist_ok=True)
    prio = 0 if kind == "notify" else 1
    name = f"{prio}-{time.time_ns()}-{os.getpid()}.json"
    job = {"kind": kind, "text": text, "ts": time.time()}
    if meta:
        job.update(meta)
    tmp = os.path.join(SPOOL, name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(job, f)
    os.replace(tmp, os.path.join(SPOOL, name))
    return name


def _pending(prio_only=None):
    try:
        names = sorted(n for n in os.listdir(SPOOL) if n.endswith(".json"))
    except FileNotFoundError:
        return []
    if prio_only is not None:
        names = [n for n in names if n.startswith(f"{prio_only}-")]
    return names


def _take(name):
    path = os.path.join(SPOOL, name)
    try:
        with open(path, encoding="utf-8") as f:
            job = json.load(f)
    except Exception as e:
        log(f"spool: unreadable {name}: {e!r}")
        job = None
    try:
        os.remove(path)
    except OSError:
        pass
    return job


# ------------------------------------------------------------------ ducking

class Ducker:
    """Lower every other Windows audio session while speaking. Best-effort:
    any failure is logged and speech proceeds undimmed."""

    def __init__(self, factor):
        self.factor = factor
        self.held = {}      # pid -> original master volume
        self.ok = False
        try:
            from pycaw.pycaw import AudioUtilities  # noqa: F401
            self.ok = True
        except Exception as e:
            log(f"duck: pycaw unavailable ({e!r}); ducking disabled")
        self._repair()

    def _sessions(self):
        from pycaw.pycaw import AudioUtilities
        return AudioUtilities.GetAllSessions()

    def _repair(self):
        """A previous run died mid-duck: restore whatever the ledger says."""
        if not self.ok or not os.path.exists(LEDGER):
            return
        try:
            with open(LEDGER, encoding="utf-8") as f:
                stale = json.load(f)
            restored = 0
            for s in self._sessions():
                pid = s.Process.pid if s.Process else 0
                if str(pid) in stale:
                    s.SimpleAudioVolume.SetMasterVolume(float(stale[str(pid)]), None)
                    restored += 1
            os.remove(LEDGER)
            log(f"duck: repaired {restored}/{len(stale)} sessions from ledger")
        except Exception as e:
            log(f"duck: repair failed {e!r}")

    def hold(self):
        if not self.ok or self.held:
            return
        try:
            me = os.getpid()
            names = {}
            for s in self._sessions():
                pid = s.Process.pid if s.Process else 0
                if pid in (0, me):
                    continue
                try:
                    names[pid] = s.Process.name()
                except Exception:
                    pass
                v = s.SimpleAudioVolume
                cur = v.GetMasterVolume()
                if cur <= 0.001 or v.GetMute():
                    continue
                self.held[pid] = cur
                v.SetMasterVolume(max(0.0, cur * self.factor), None)
            if self.held:
                with open(LEDGER, "w", encoding="utf-8") as f:
                    json.dump({str(k): v for k, v in self.held.items()}, f)
            log(f"duck: lowered {len(self.held)} session(s) to x{self.factor}: "
                + ", ".join(f"{names.get(p, p)}@{v:.2f}" for p, v in self.held.items()))
        except Exception as e:
            log(f"duck: hold failed {e!r}")
            self.held = {}

    def release(self):
        if not self.held:
            return
        try:
            live = {(s.Process.pid if s.Process else 0): s for s in self._sessions()}
            for pid, orig in self.held.items():
                s = live.get(pid)
                if s is not None:
                    s.SimpleAudioVolume.SetMasterVolume(float(orig), None)
        except Exception as e:
            log(f"duck: release failed {e!r}")
        self.held = {}
        try:
            os.remove(LEDGER)
        except OSError:
            pass


# ------------------------------------------------------------------ speaker

class Speaker:
    def __init__(self):
        self.c = cfg()
        import numpy as np
        import sounddevice as sd
        self.np, self.sd = np, sd
        import speakability
        self.spk = speakability
        self.duck = Ducker(self.c["duck_factor"])
        self.stream = None
        self.pause = np.zeros(int(SR * self.c["sentence_pause_ms"] / 1000), dtype=np.float32)
        t0 = time.perf_counter()
        self.engine = None
        if self.c["engine"] == "pocket":
            try:
                self._load_pocket()
            except Exception as e:
                log(f"pocket: load failed {e!r}; falling back to kokoro")
        if self.engine is None:
            self._load_kokoro()
        t1 = time.perf_counter()
        for _ in self._synth_stream("Ready."):   # first inference is slow; pay it now
            pass
        log(f"ready: {self.engine} loaded in {t1-t0:.2f}s warmup {time.perf_counter()-t1:.2f}s "
            f"voice={self.c['pocket_voice'] if self.engine == 'pocket' else self.c['voice']} "
            f"out={sd.query_devices(kind='output')['name']!r}")

    def _load_pocket(self):
        import torch
        from pocket_tts import TTSModel
        torch.set_num_threads(int(self.c["pocket_threads"]))
        self.pocket = TTSModel.load_model()
        path = os.path.join(HERE, self.c["pocket_voice"])
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        self.pocket_state = self.pocket.get_state_for_audio_prompt(path)
        if getattr(self.pocket, "sample_rate", SR) != SR:
            raise RuntimeError(f"pocket sample rate {self.pocket.sample_rate} != {SR}")
        self.engine = "pocket"

    def _load_kokoro(self):
        from kokoro_onnx import Kokoro
        self.kokoro = Kokoro(MODEL, VOICES)
        self.engine = "kokoro"

    # -- audio device
    def _open(self):
        if self.stream is None:
            self.stream = self.sd.OutputStream(samplerate=SR, channels=1, dtype="float32")
            self.stream.start()

    def _close(self):
        if self.stream is not None:
            try:
                self.stream.stop(); self.stream.close()
            except Exception:
                pass
            self.stream = None

    # -- synthesis: a generator of float32 chunks per sentence (pocket streams
    #    sub-sentence chunks; kokoro yields the sentence whole)
    def _synth_stream(self, sentence):
        if self.engine == "pocket":
            try:
                for chunk in self.pocket.generate_audio_stream(self.pocket_state, sentence):
                    a = chunk.detach().cpu().numpy() if hasattr(chunk, "detach") else self.np.asarray(chunk)
                    yield a.reshape(-1).astype(self.np.float32)
                return
            except Exception as e:
                log(f"pocket: synth failed {e!r}; kokoro for this piece")
                if not hasattr(self, "kokoro"):
                    from kokoro_onnx import Kokoro
                    self.kokoro = Kokoro(MODEL, VOICES)
        a = self._synth(sentence)
        if a is not None:
            yield a

    def _synth(self, sentence):
        try:
            a, sr = self.kokoro.create(sentence, voice=self.c["voice"], speed=self.c["speed"],
                                       lang=self.c["lang"])
            return a.astype(self.np.float32)
        except Exception as e:
            # kokoro-onnx's known IndexError zone: retry on halves, then give up on this piece
            if len(sentence) > 40:
                mid = sentence.rfind(" ", 0, len(sentence) // 2 + 20)
                if mid > 0:
                    log(f"synth: retry split after {e!r}")
                    parts = [self._synth(sentence[:mid]), self._synth(sentence[mid:].strip())]
                    parts = [p for p in parts if p is not None]
                    return self.np.concatenate(parts) if parts else None
            log(f"synth: FAILED {e!r} on {sentence[:60]!r}")
            return None

    def _pieces(self, text, kind):
        clean = self.spk.phrase(text) if kind == "notify" else self.spk.speakable(text)
        clean = self.spk.transport_safe(clean)
        out = []
        for s in self.spk.sentences(clean):
            while len(s) > self.c["max_sentence_chars"]:
                cut = s.rfind(" ", 0, self.c["max_sentence_chars"])
                if cut <= 0:
                    break
                out.append(s[:cut]); s = s[cut:].strip()
            if s:
                out.append(s)
        return out

    # -- one job: producer thread synthesizes ahead, main thread streams
    def speak(self, job):
        pieces = self._pieces(job.get("text", ""), job.get("kind", "speak"))
        if not pieces:
            return
        q = queue.Queue(maxsize=16)
        END = object()

        def produce():          # chunks as they come; END marks a sentence boundary
            for p in pieces:
                for a in self._synth_stream(p):
                    q.put(a)
                q.put(END)
            q.put(None)

        threading.Thread(target=produce, daemon=True).start()
        t0 = time.perf_counter()
        first = lag = None
        self.duck.hold()
        self._open()
        n = 0
        while True:
            a = q.get()
            if a is None:
                break
            if a is END:
                self.stream.write(self.pause)
                n += 1
                if os.path.exists(STOPFLAG):
                    log("stop: flag seen mid-utterance, finishing at sentence boundary")
                    break
                # a notify may jump in between sentences of a long utterance
                if job.get("kind") != "notify":
                    for name in _pending(prio_only=0):
                        j = _take(name)
                        if j:
                            self._speak_inline(j)
                continue
            if first is None:
                first = time.perf_counter() - t0
                lag = time.time() - job.get("ts", time.time())
            self.stream.write(a)
        log(f"spoke {job.get('kind','speak')} pieces={n} first-audio={first or 0:.2f}s "
            f"enqueue-to-first={lag or 0:.2f}s chars={len(job.get('text',''))}")

    def _speak_inline(self, job):
        for p in self._pieces(job.get("text", ""), "notify"):
            for a in self._synth_stream(p):
                self.stream.write(a)
            self.stream.write(self.pause)
        log(f"spoke notify (inline) chars={len(job.get('text',''))}")

    # -- main loop
    def run(self):
        os.makedirs(SPOOL, exist_ok=True)
        with open(PIDFILE, "w") as f:
            f.write(str(os.getpid()))
        atexit.register(self.shutdown)
        try:
            os.remove(STOPFLAG)
        except OSError:
            pass
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, lambda *_: sys.exit(0))
            except Exception:
                pass
        poll = self.c["poll_ms"] / 1000
        release_at = None

        def beat():                      # own thread: a 40-second utterance must not look like death
            while True:
                try:
                    with open(HEARTBEAT, "w") as f:
                        f.write(str(time.time()))
                except Exception:
                    pass
                time.sleep(1.0)
        threading.Thread(target=beat, daemon=True).start()
        log(f"run: pid={os.getpid()} watching {SPOOL}")
        while True:
            now = time.time()
            if os.path.exists(STOPFLAG):
                log("stop: flag seen, exiting cleanly")
                try:
                    os.remove(STOPFLAG)
                except OSError:
                    pass
                self.shutdown()
                os._exit(0)        # torch/onnx worker threads can stall interpreter teardown
                                   # for many seconds while the mutex stays held; hard-exit after cleanup
            names = _pending()
            if names:
                if os.path.exists(OFF):
                    for n in names:
                        _take(n)
                    log(f"off: drained {len(names)} job(s) silently")
                    continue
                job = _take(names[0])
                if job:
                    try:
                        self.speak(job)
                    except Exception as e:
                        log(f"speak: ERROR {e!r}")
                release_at = time.time() + self.c["duck_release_ms"] / 1000
                continue
            if release_at and now >= release_at:
                self.duck.release()
                self._close()
                release_at = None
            time.sleep(poll)

    def shutdown(self):
        try:
            self.duck.release()
            self._close()
            os.remove(PIDFILE)
        except Exception:
            pass


# ------------------------------------------------------------------ cli

def status():
    try:
        age = time.time() - os.path.getmtime(HEARTBEAT)
        alive = age < 5 and os.path.exists(PIDFILE)
    except OSError:
        age, alive = None, False
    print(f"service: {'alive' if alive else 'DOWN'}"
          + (f" (heartbeat {age:.1f}s ago)" if age is not None else " (no heartbeat)"))
    print(f"queue: {len(_pending())} job(s)   off-switch: {'ON' if os.path.exists(OFF) else 'off'}")
    return 0 if alive else 1


def _single_instance():
    """Windows named mutex: a second `run` exits immediately instead of racing the spool."""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.CreateMutexW(None, True, MUTEX_NAME)
        if k32.GetLastError() == 183:      # ERROR_ALREADY_EXISTS
            return None
        return h                           # keep the handle alive for the process lifetime
    except Exception as e:
        log(f"mutex: unavailable {e!r}; continuing without single-instance guard")
        return True


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "run"
    if cmd == "run":
        global _MUTEX
        for _ in range(40):                    # a restart's old instance may still be exiting
            _MUTEX = _single_instance()
            if _MUTEX is not None:
                break
            time.sleep(0.25)
        if _MUTEX is None:
            log("run: another speaker holds the mutex; exiting")
            return 0
        try:                                   # heartbeat before the model loads: closes the
            with open(HEARTBEAT, "w") as f:    # respawn window while kokoro is still loading
                f.write(str(time.time()))
        except Exception:
            pass
        Speaker().run()
    elif cmd in ("say", "notify"):
        text = " ".join(argv[2:]) or "If you can hear this, the new voice path works."
        print("queued", enqueue(text, kind="notify" if cmd == "notify" else "speak"))
    elif cmd == "status":
        return status()
    elif cmd == "start":
        # detached background start, used by the WSL hook and by hand
        subprocess.Popen([os.path.join(HERE, ".venv", "Scripts", "pythonw.exe"),
                          os.path.join(HERE, "speaker.py"), "run"],
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                         | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                         close_fds=True)
        print("started")
    elif cmd == "stop":
        # cooperative stop: the loop sees the flag, releases ducking, closes the device, exits.
        # (os.kill on Windows is TerminateProcess -- no cleanup -- so it is only the fallback.)
        if not os.path.exists(PIDFILE):
            print("not running")
            return 0
        with open(STOPFLAG, "w") as f:
            f.write(str(time.time()))
        for _ in range(120):
            time.sleep(0.25)
            if not os.path.exists(PIDFILE):   # removed by shutdown(), i.e. fully exited
                try:
                    os.remove(STOPFLAG)
                except OSError:
                    pass
                print("stopped")
                return 0
        try:
            with open(PIDFILE) as f:
                pid = int(f.read().strip())
            os.kill(pid, signal.SIGTERM)
            print("killed (was not responding)", pid)
        except Exception as e:
            print("not running?", e)
        try:
            os.remove(STOPFLAG)
        except OSError:
            pass
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv) or 0)
