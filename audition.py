"""Ear gate: same paragraph through Kokoro af_bella and several Pocket-TTS voices.
Writes wavs to ./audition/ and prints load / time-to-first-audio / RTF per engine."""
import os, sys, time
import numpy as np
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "audition"); os.makedirs(OUT, exist_ok=True)
TEXT = ("The build finished and all forty-two tests pass. I changed two files, "
        "and the deploy is waiting on your go. Nothing else is blocked.")
POCKET_VOICES = sys.argv[1:] or ["alba", "jane", "eve", "vera", "cosette"]

def report(name, load_s, tfa_s, total_s, audio, sr):
    dur = len(audio) / sr
    print(f"{name:22s} load {load_s:5.2f}s  first-audio {tfa_s:5.2f}s  synth {total_s:5.2f}s  "
          f"audio {dur:4.1f}s  RTF {total_s/dur:.2f}")
    sf.write(os.path.join(OUT, f"{name}.wav"), audio, sr, subtype="PCM_16")

# ---- Kokoro (incumbent) -------------------------------------------------
t0 = time.perf_counter()
from kokoro_onnx import Kokoro
kok = Kokoro(os.path.join(HERE, "models", "kokoro-v1.0.onnx"), os.path.join(HERE, "models", "voices-v1.0.bin"))
kload = time.perf_counter() - t0
t0 = time.perf_counter(); first = None; parts = []
# sentence-split ourselves (what the old rig did) for a fair first-audio number
import re
sents = re.split(r"(?<=[.!?])\s+", TEXT)
for s in sents:
    a, sr = kok.create(s, voice="af_bella", speed=1.15, lang="en-us")
    if first is None: first = time.perf_counter() - t0
    parts.append(a)
audio = np.concatenate(parts)
report("kokoro_af_bella", kload, first, time.perf_counter() - t0, audio, sr)

# ---- Pocket-TTS ----------------------------------------------------------
t0 = time.perf_counter()
from pocket_tts import TTSModel
import torch
tts = TTSModel.load_model()
pload = time.perf_counter() - t0
sr = tts.sample_rate
def synth_pocket(name, prompt):
    t0 = time.perf_counter()
    state = tts.get_state_for_audio_prompt(prompt)
    vload = time.perf_counter() - t0
    t0 = time.perf_counter(); first = None; parts = []
    for chunk in tts.generate_audio_stream(state, TEXT):
        if first is None: first = time.perf_counter() - t0
        parts.append(chunk.detach().cpu().numpy() if hasattr(chunk, "detach") else np.asarray(chunk))
    audio = np.concatenate([p.reshape(-1) for p in parts]).astype(np.float32)
    report(f"pocket_{name}", vload, first, time.perf_counter() - t0, audio, sr)
print(f"pocket model load {pload:.2f}s, sample rate {sr}, threads {torch.get_num_threads()}")
for v in POCKET_VOICES:
    synth_pocket(v, v)
# clone: hand Pocket the Kokoro af_bella sample so the same voice rides the new engine
# (needs the gated cloning weights: accept terms at hf.co/kyutai/pocket-tts + `hf auth login`)
if os.environ.get("POCKET_CLONE"):
    synth_pocket("clone_of_af_bella", os.path.join(OUT, "kokoro_af_bella.wav"))
print("wavs in", OUT)
