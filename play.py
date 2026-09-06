import os, sys, time
import sounddevice as sd, soundfile as sf
HERE = os.path.dirname(os.path.abspath(__file__))
names = sys.argv[1:] or ["kokoro_af_bella", "pocket_alba", "pocket_jane", "pocket_eve", "pocket_vera", "pocket_cosette"]
for i, n in enumerate(names, 1):
    p = os.path.join(HERE, "audition", n + ".wav")
    a, sr = sf.read(p, dtype="float32")
    print(i, n, f"{len(a)/sr:.1f}s", flush=True)
    sd.play(a, sr); sd.wait(); time.sleep(1.0)
