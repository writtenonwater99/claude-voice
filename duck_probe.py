import os, time
from pycaw.pycaw import AudioUtilities
print("sessions:")
for s in AudioUtilities.GetAllSessions():
    pid = s.Process.pid if s.Process else 0
    name = s.Process.name() if s.Process else "(system)"
    v = s.SimpleAudioVolume
    print(f"  pid={pid:<6} {name:<28} vol={v.GetMasterVolume():.2f} mute={v.GetMute()} state={s.State}")
