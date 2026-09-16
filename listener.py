#!/usr/bin/env python3
"""Voice INPUT for Claude Code (Windows side). The mirror image of speaker.py.

Shape, and why it is this shape: all audio stays on Windows, only text crosses
into WSL. Capture through WSLg's PulseAudio bridge needs the audio-in RDP channel
to have negotiated at WSLg session start, which it frequently has not (see the
claude-code-voice-wsl gotcha: a dead default mic at boot leaves `audin` down and
every capture hangs forever with zero frames). Recording here sidesteps that
whole class of failure exactly as playback does.

    mic -> ring buffer -> Silero VAD (streaming) -> utterance -> Whisper -> inbox/*.json

inbox/ is watched by hook/inject.py on the WSL side, which types the text into the
tmux pane running Claude Code. Nothing here knows about tmux; nothing there knows
about audio.

No button anywhere: an utterance opens on speech and closes on silence.

Operate:
    .venv\\Scripts\\python.exe listener.py start|stop|status|devices|once
It is never started automatically -- no hook spawns it, no logon entry. You start
it, and "stop listening" (said as a whole sentence) stops it.

Off switches:
    "stop listening"                 spoken; exits the process
    listen-off (next to this file)   pause listening, keep the process
    ~/.claude/tts-off                the shared voice kill switch, honoured too
Self-hearing: speaker.py holds a `speaking` flag for the length of each spoken
burst; audio captured while it is held is discarded, so Bella is never
transcribed back into the session.
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
INBOX = os.path.join(HERE, "inbox")
LOG = os.path.join(HERE, "listener.log")
PIDFILE = os.path.join(HERE, "listener.pid")
HEARTBEAT = os.path.join(HERE, "listener-heartbeat")
STOPFLAG = os.path.join(HERE, "listener-stop")
OFF = os.path.join(HERE, "listen-off")
SPEAKING = os.path.join(HERE, "speaking")
BARGE = os.path.join(HERE, "barge")
CONFIG = os.path.join(HERE, "config.json")
MUTEX_NAME = "Local\\ClaudeVoiceListener"

SR = 16000          # Silero and Whisper both want 16k mono
FRAME = 512         # Silero v6 consumes 512 new samples per step (32 ms at 16k)
CONTEXT = 64        # ...prepended with the previous 64, hence the 576-wide input
SPEAKING_STALE_S = 180   # a crashed speaker must not deafen the listener forever

DEFAULTS = dict(
    listener_device=None,        # None = Windows default capture device
    vad_threshold=0.5,           # Silero speech probability
    vad_start_frames=3,          # ~96 ms of speech opens an utterance
    vad_silence_ms=700,          # ...this much silence closes it
    min_utterance_ms=350,        # shorter than this is a cough, not a sentence
    max_utterance_s=45,          # hard cap so one long monologue still lands
    speak_gate_tail_ms=400,      # keep ignoring the mic after Bella stops
    barge_in=True,               # talking over Bella cuts her off instead of being discarded
    barge_threshold=0.65,        # stricter than vad_threshold: her own bleed must not trigger it
    barge_frames=5,              # ~160 ms of sustained speech, so a cough does not interrupt
    barge_preroll_s=4.0,         # audio kept while gated, so a slow trigger loses nothing
    barge_grace_s=2.0,           # ignore the speaking flag this long after a barge
    stt_model="small.en",
    stt_compute="int8",
    stt_beam=1,
    stt_lang="en",
    min_chars=2,
    stt_vocab=True,              # bias recognition toward the names this rig already knows
    stt_prompt_extra="",         # anything else you say often; comma-separated
    keep_audio=True,             # keep each utterance as a wav, so models can be A/B'd offline
    stop_phrases=["stop listening", "stop the listener", "stop listening now",
                  "microphone off", "mic off", "voice off"],
)

# Terms the speaker's glossary teaches it to PRONOUNCE are the same terms the
# listener needs to RECOGNISE -- a project codename came back as a common word on the first
# live test. Keys only: the values are pronunciation hints and are private.
GLOSSARIES = ("glossary.json", "glossary.local.json")
AUDIO_DIR = os.path.join(INBOX, "audio")
SPOOL = os.path.join(HERE, "spool")        # speaker.py's queue, for spoken confirmations
LEAD_INS = ("okay", "ok", "alright", "hey", "please", "claude", "and", "so", "um", "uh")
BASE_VOCAB = "Claude, Codex, tmux, WSL, repo, commit, PR, JSON, Python, artifact"
PROMPT_CAP = 700             # whisper truncates a long initial_prompt anyway

# Whisper emits these from silence and room tone. They are never worth injecting.
HALLUCINATIONS = {
    "you", "thank you", "thanks for watching", "thank you for watching", "bye",
    "okay", "ok", "oh", "hmm", "mm", "mhm", "uh", "um", "so", "yeah", ".", "...",
    "please subscribe", "subtitles by the amara.org community", "transcription by castingwords",
}


def cfg():
    c = dict(DEFAULTS)
    try:
        with open(CONFIG, encoding="utf-8") as f:
            c.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    except Exception:
        pass
    return c


def vocab_prompt(c):
    """A comma list of proper nouns, handed to Whisper as initial_prompt."""
    if not c["stt_vocab"]:
        return None
    terms = []
    for name in GLOSSARIES:
        try:
            with open(os.path.join(HERE, name), encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        for section in ("codenames", "glossary"):
            if isinstance(d.get(section), dict):
                terms += [k for k in d[section] if not k.startswith("_")]
    extra = [t.strip() for t in (c["stt_prompt_extra"] or "").split(",") if t.strip()]
    seen, out = set(), []
    for t in terms + extra + [t.strip() for t in BASE_VOCAB.split(",")]:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    if not out:
        return None
    prompt = "Vocabulary: " + ", ".join(out) + "."
    return prompt[:PROMPT_CAP]


def heard_as():
    """{misheard -> correct} from the gitignored local glossary. Whisper hears
    unusual names as ordinary words ("Codex" -> "codecs"); a prompt does not
    reliably fix that, and these are exactly the words that matter most."""
    out = {}
    for name in GLOSSARIES:
        try:
            with open(os.path.join(HERE, name), encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        section = d.get("heard_as")
        if isinstance(section, dict):
            for correct, variants in section.items():
                for v in ([variants] if isinstance(variants, str) else variants):
                    out[v.lower()] = correct
    return out


def apply_heard_as(text, table):
    if not table:
        return text
    import re as _re
    def sub(m):
        w = m.group(0)
        fix = table.get(w.lower())
        return fix if fix else w
    return _re.sub(r"[A-Za-z][A-Za-z'\-]*", sub, text)


def normalise(text):
    """Lowercase, strip punctuation and conversational lead-ins, collapse spaces."""
    import re as _re
    t = _re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    words = [w for w in t.split() if w]
    while words and words[0] in LEAD_INS:
        words.pop(0)
    return " ".join(words)


def say(text):
    """Drop a priority job in the speaker's spool so a voice command is answered
    out loud -- there is no screen to look at when your hands are elsewhere."""
    try:
        os.makedirs(SPOOL, exist_ok=True)
        name = f"0-{time.time_ns()}-{os.getpid()}.json"
        tmp = os.path.join(SPOOL, name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"kind": "notify", "text": text, "ts": time.time()}, f)
        os.replace(tmp, os.path.join(SPOOL, name))
    except Exception as e:
        log(f"say failed: {e!r}")


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    if sys.stdout and sys.stdout.isatty():
        print(line, flush=True)


def start_heartbeat():
    """Beat from before the model load, for the same reason speaker.py does:
    the Whisper load is slow enough that a single pre-load write would look
    like death to anything watching."""
    def beat():
        while True:
            try:
                with open(HEARTBEAT, "w") as f:
                    f.write(str(time.time()))
            except Exception:
                pass
            time.sleep(1.0)
    threading.Thread(target=beat, daemon=True).start()


def _muted():
    if os.path.exists(OFF):
        return True
    return os.path.exists(os.path.expanduser("~/.claude/tts-off"))


def _speaker_busy():
    try:
        if time.time() - os.path.getmtime(SPEAKING) > SPEAKING_STALE_S:
            return False        # stale flag from a killed speaker; do not go deaf over it
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------- VAD

class Vad:
    """Streaming Silero. The ONNX ships inside faster-whisper, so there is no
    extra download and no torch.hub at runtime."""

    def __init__(self, threshold):
        import numpy as np
        import onnxruntime as ort
        import faster_whisper
        self.np = np
        path = os.path.join(os.path.dirname(faster_whisper.__file__), "assets", "silero_vad_v6.onnx")
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1      # one frame at a time; threads only add latency
        opts.intra_op_num_threads = 1
        self.sess = ort.InferenceSession(path, opts, providers=["CPUExecutionProvider"])
        self.threshold = threshold
        self.reset()

    def reset(self):
        np = self.np
        self.h = np.zeros((1, 1, 128), dtype=np.float32)
        self.c = np.zeros((1, 1, 128), dtype=np.float32)
        self.context = np.zeros(CONTEXT, dtype=np.float32)

    def prob(self, frame):
        np = self.np
        x = np.concatenate([self.context, frame]).astype(np.float32).reshape(1, CONTEXT + FRAME)
        p, self.h, self.c = self.sess.run(None, {"input": x, "h": self.h, "c": self.c})
        self.context = frame[-CONTEXT:].copy()
        return float(np.asarray(p).reshape(-1)[-1])


# ----------------------------------------------------------------- listener

class Listener:
    def __init__(self):
        self.c = cfg()
        import numpy as np
        import sounddevice as sd
        self.np, self.sd = np, sd
        t0 = time.perf_counter()
        self.vad = Vad(self.c["vad_threshold"])
        t1 = time.perf_counter()
        from faster_whisper import WhisperModel
        self.stt = WhisperModel(self.c["stt_model"], device="cpu",
                                compute_type=self.c["stt_compute"])
        t2 = time.perf_counter()
        seg, _ = self.stt.transcribe(np.zeros(SR, dtype=np.float32), beam_size=1)
        list(seg)                                  # first inference is slow; pay it now
        self.jobs = queue.Queue()
        self.prompt = vocab_prompt(self.c)
        self.heard_as = heard_as()
        self.stop_set = {normalise(p) for p in self.c["stop_phrases"]}
        self.silence_frames = int(self.c["vad_silence_ms"] / 1000 * SR / FRAME)
        self.min_frames = int(self.c["min_utterance_ms"] / 1000 * SR / FRAME)
        self.max_frames = int(self.c["max_utterance_s"] * SR / FRAME)
        self.reset_segment()
        log(f"vocab: {len(self.prompt)} chars of bias" if self.prompt else "vocab: off")
        log(f"ready: vad {t1-t0:.2f}s  whisper {self.c['stt_model']}/{self.c['stt_compute']} "
            f"{t2-t1:.2f}s  warmup {time.perf_counter()-t2:.2f}s  "
            f"in={sd.query_devices(kind='input')['name']!r}")

    # -- segmentation: the whole no-button trick lives here
    def reset_segment(self):
        self.buf, self.run_speech, self.run_silence, self.in_utt = [], 0, 0, False
        self.vad.reset()

    def feed(self, frame):
        """One 512-sample frame in; a finished utterance out, or None.

        Opening needs `vad_start_frames` consecutive speech frames so a door
        slam cannot start a turn; closing needs `vad_silence_ms` of quiet so a
        mid-sentence breath does not end one.
        """
        c = self.c
        speech = self.vad.prob(frame) >= c["vad_threshold"]
        if not self.in_utt:
            self.buf.append(frame)
            if len(self.buf) > c["vad_start_frames"] * 4:
                self.buf.pop(0)          # short pre-roll so the first word is never clipped
            self.run_speech = self.run_speech + 1 if speech else 0
            if self.run_speech >= c["vad_start_frames"]:
                self.in_utt, self.run_silence = True, 0
            return None

        self.buf.append(frame)
        self.run_silence = 0 if speech else self.run_silence + 1
        if self.run_silence < self.silence_frames and len(self.buf) < self.max_frames:
            return None

        capped = len(self.buf) >= self.max_frames
        voiced = len(self.buf) - self.run_silence
        utt = self.np.concatenate(self.buf).astype(self.np.float32) if voiced >= self.min_frames else None
        if utt is None:
            log("drop: too short")
        elif capped:
            log("utterance hit the length cap; flushed")
        self.reset_segment()
        return utt

    # -- transcription runs off the capture path so a slow decode never drops audio
    def _worker(self):
        while True:
            audio = self.jobs.get()
            if audio is None:
                return
            try:
                self._transcribe(audio)
            except Exception as e:
                log(f"stt: ERROR {e!r}")

    def _transcribe(self, audio):
        t0 = time.perf_counter()
        segs, _ = self.stt.transcribe(audio, language=self.c["stt_lang"],
                                      beam_size=self.c["stt_beam"],
                                      initial_prompt=self.prompt,
                                      condition_on_previous_text=False)
        text = " ".join(s.text.strip() for s in segs).strip()
        fixed = apply_heard_as(text, self.heard_as)
        if fixed != text:
            log(f"heard-as: {text[:60]!r} -> {fixed[:60]!r}")
            text = fixed
        dur = len(audio) / SR
        if not text or len(text) < self.c["min_chars"]:
            log(f"drop: empty ({dur:.1f}s audio, {time.perf_counter()-t0:.2f}s decode)")
            return
        if text.strip(" .,!?").lower() in HALLUCINATIONS:
            log(f"drop: filler {text!r} ({dur:.1f}s)")
            return
        # A command must be the WHOLE utterance. Matching on "contains" would end
        # the session the moment he says "...and then stop listening for changes".
        if normalise(text) in self.stop_set:
            log(f"voice command: stop listening ({text!r})")
            say("Listener stopping. Start it again from the terminal.")
            try:
                with open(STOPFLAG, "w") as f:
                    f.write(str(time.time()))
            except Exception as e:
                log(f"stop flag failed: {e!r}")
            return
        self._emit(text, dur, time.perf_counter() - t0, audio)

    def _emit(self, text, dur, decode_s, audio=None):
        os.makedirs(INBOX, exist_ok=True)
        name = f"{time.time_ns()}-{os.getpid()}.json"
        if audio is not None and self.c["keep_audio"]:
            try:
                import soundfile as sf
                os.makedirs(AUDIO_DIR, exist_ok=True)
                sf.write(os.path.join(AUDIO_DIR, name.replace(".json", ".wav")),
                         audio, SR, subtype="PCM_16")
            except Exception as e:
                log(f"keep_audio failed: {e!r}")
        tmp = os.path.join(INBOX, name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"text": text, "ts": time.time(), "audio_s": round(dur, 2),
                       "decode_s": round(decode_s, 2)}, f)
        os.replace(tmp, os.path.join(INBOX, name))   # atomic: inject.py never sees a partial file
        log(f"heard {dur:.1f}s -> {len(text)}ch in {decode_s:.2f}s: {text[:90]!r}")

    def run(self):
        os.makedirs(INBOX, exist_ok=True)
        with open(PIDFILE, "w") as f:
            f.write(str(os.getpid()))
        atexit.register(self.shutdown)
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, lambda *_: sys.exit(0))
            except Exception:
                pass
        try:
            os.remove(STOPFLAG)
        except OSError:
            pass
        threading.Thread(target=self._worker, daemon=True).start()

        frames = queue.Queue()

        def on_audio(indata, n, t, status):
            if status:
                log(f"audio: {status}")
            frames.put(indata[:, 0].copy())

        dev = self.c["listener_device"]
        stream = self.sd.InputStream(samplerate=SR, channels=1, dtype="float32",
                                     blocksize=FRAME, device=dev, callback=on_audio)
        tail_s = self.c["speak_gate_tail_ms"] / 1000
        gate_until = barge_grace_until = 0.0
        gated_frames = gated_speech = barge_run = 0
        preroll = []
        preroll_cap = int(self.c["barge_preroll_s"] * SR / FRAME)
        self.reset_segment()
        with stream:
            log(f"run: pid={os.getpid()} listening, inbox {INBOX}")
            while True:
                if os.path.exists(STOPFLAG):
                    log("stop: flag seen, exiting cleanly")
                    try:
                        os.remove(STOPFLAG)
                    except OSError:
                        pass
                    break
                try:
                    frame = frames.get(timeout=0.5)
                except queue.Empty:
                    continue

                now = time.time()
                if _muted():
                    gated_frames = gated_speech = 0
                    self.reset_segment()
                    continue

                # Bella is talking. Either cut her off or ignore the mic -- but never
                # discard a whole minute of speech in silence, which is what the first
                # live test did.
                if _speaker_busy() and now >= barge_grace_until:
                    gate_until = now + tail_s
                    gated_frames += 1
                    if not self.c["barge_in"]:
                        self.reset_segment()
                        continue
                    prob = self.vad.prob(frame)
                    if prob >= self.c["vad_threshold"]:
                        gated_speech += 1          # how much of the gate was actually you talking
                    barge_run = barge_run + 1 if prob >= self.c["barge_threshold"] else 0
                    preroll.append(frame)
                    if len(preroll) > preroll_cap:
                        preroll.pop(0)
                    if barge_run >= self.c["barge_frames"]:
                        try:
                            with open(BARGE, "w") as f:
                                f.write(str(now))
                        except Exception:
                            pass
                        log(f"barge: cutting playback after {gated_frames * FRAME / SR:.1f}s gated "
                            f"({gated_speech * FRAME / SR:.1f}s of it speech); "
                            f"recovering {len(preroll) * FRAME / SR:.1f}s of pre-roll")
                        barge_grace_until = now + self.c["barge_grace_s"]
                        gate_until, barge_run, gated_frames, gated_speech = 0.0, 0, 0, 0
                        self.reset_segment()
                        for f_ in preroll:            # the barge itself is the start of the turn
                            self.feed(f_)
                        preroll = []
                    continue

                if now < gate_until:
                    gated_frames += 1
                    self.reset_segment()
                    continue
                if gated_frames:
                    log(f"gate: ignored {gated_frames * FRAME / SR:.1f}s while speaking, "
                        f"{gated_speech * FRAME / SR:.1f}s of it speech-like")
                    gated_frames = gated_speech = barge_run = 0
                    preroll = []

                utt = self.feed(frame)
                if utt is not None:
                    self.jobs.put(utt)
        self.shutdown()
        os._exit(0)      # onnx/ctranslate2 worker threads can stall teardown while the mutex is held

    def shutdown(self):
        for p in (PIDFILE,):
            try:
                os.remove(p)
            except OSError:
                pass


# ------------------------------------------------------------------ control

def status():
    try:
        age = time.time() - os.path.getmtime(HEARTBEAT)
        alive = age < 5 and os.path.exists(PIDFILE)
    except OSError:
        age, alive = None, False
    print(f"listener: {'alive' if alive else 'DOWN'}"
          + (f" (heartbeat {age:.1f}s ago)" if age is not None else " (no heartbeat)"))
    pending = len([f for f in os.listdir(INBOX) if f.endswith(".json")]) if os.path.isdir(INBOX) else 0
    print(f"inbox: {pending} waiting   off-switch: {'ON' if _muted() else 'off'}"
          f"   speaker: {'speaking' if _speaker_busy() else 'quiet'}")
    return 0 if alive else 1


def devices():
    import sounddevice as sd
    default = sd.default.device[0]
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            print(f"{'*' if i == default else ' '} [{i}] {d['name']}  "
                  f"({d['max_input_channels']}ch @ {int(d['default_samplerate'])})")
    print("\n* = current default. Pin one with \"listener_device\": <index> in config.json")
    return 0


def _single_instance():
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.CreateMutexW(None, True, MUTEX_NAME)
        if k32.GetLastError() == 183:      # ERROR_ALREADY_EXISTS
            return None
        return h
    except Exception as e:
        log(f"mutex: unavailable {e!r}; continuing without single-instance guard")
        return True


_MUTEX = None


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "run"
    if cmd == "run":
        global _MUTEX
        for _ in range(40):                # a restart's old instance may still be exiting
            _MUTEX = _single_instance()
            if _MUTEX is not None:
                break
            time.sleep(0.25)
        if _MUTEX is None:
            log("run: another listener holds the mutex; exiting")
            return 0
        start_heartbeat()
        Listener().run()
    elif cmd == "once":                    # one utterance, printed -- for testing the mic end
        start_heartbeat()
        lst = Listener()
        lst._emit = lambda text, dur, d, audio=None: print(f"\n[{dur:.1f}s audio, {d:.2f}s decode] {text}")
        lst.run()
    elif cmd == "file":                    # run a wav through the real pipeline, no mic needed
        if len(argv) < 3:
            print("usage: listener.py file <path.wav>")
            return 2
        import numpy as np
        import soundfile as sf
        audio, sr = sf.read(argv[2], dtype="float32", always_2d=True)
        audio = audio.mean(axis=1)
        if sr != SR:                       # Silero and Whisper are both 16k-only
            from scipy.signal import resample_poly
            from math import gcd
            g = gcd(int(sr), SR)
            audio = resample_poly(audio, SR // g, int(sr) // g).astype(np.float32)
        lst = Listener()
        pad = np.zeros(SR, dtype=np.float32)          # silence either side so the VAD must segment
        audio = np.concatenate([pad, audio, pad])
        found = 0
        for i in range(0, len(audio) - FRAME, FRAME):
            utt = lst.feed(audio[i:i + FRAME])
            if utt is not None:
                found += 1
                lst._transcribe(utt)
        print(f"\nsegmented {found} utterance(s) from {len(audio)/SR:.1f}s")
        return 0 if found else 1
    elif cmd == "start":
        subprocess.Popen([os.path.join(HERE, ".venv", "Scripts", "pythonw.exe"),
                          os.path.join(HERE, "listener.py"), "run"],
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                         | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                         close_fds=True)
        print("started")
    elif cmd == "stop":
        if not os.path.exists(PIDFILE):
            print("not running")
            return 0
        with open(STOPFLAG, "w") as f:
            f.write(str(time.time()))
        for _ in range(60):
            time.sleep(0.25)
            if not os.path.exists(PIDFILE):
                try:
                    os.remove(STOPFLAG)
                except OSError:
                    pass
                print("stopped")
                return 0
        try:
            with open(PIDFILE) as f:
                os.kill(int(f.read().strip()), signal.SIGTERM)
            print("killed (was not responding)")
        except Exception as e:
            print("stop failed", e)
        return 1
    elif cmd == "status":
        return status()
    elif cmd == "devices":
        return devices()
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv) or 0)
