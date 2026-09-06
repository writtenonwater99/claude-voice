"""One-time: turn a voice sample into a Pocket-TTS voice state (fast to load).
    python export_voice.py <sample.wav> <name>      -> voices/<name>.safetensors
Default: the Kokoro af_bella audition clip -> voices/bella.safetensors"""
import os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "audition", "kokoro_af_bella.wav")
name = sys.argv[2] if len(sys.argv) > 2 else "bella"
os.makedirs(os.path.join(HERE, "voices"), exist_ok=True)
from pocket_tts import TTSModel, export_model_state
t0 = time.perf_counter()
m = TTSModel.load_model()
st = m.get_state_for_audio_prompt(src)
out = os.path.join(HERE, "voices", name + ".safetensors")
export_model_state(st, out)
t1 = time.perf_counter()
st2 = m.get_state_for_audio_prompt(out)
print(f"exported {out} ({os.path.getsize(out)//1024} KB) in {t1-t0:.1f}s; reload {time.perf_counter()-t1:.2f}s")
