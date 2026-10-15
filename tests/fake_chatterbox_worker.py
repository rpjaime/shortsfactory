"""Imitación del worker de Chatterbox para las pruebas (sin modelos ni torch)."""
import json
import math
import shutil
import struct
import sys
import wave

print("loaded PerthNet (Implicit) at step 250,000", flush=True)   # ruido real de la librería
print(json.dumps({"ready": True}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    if req["cmd"] == "quit":
        break
    if req["cmd"] == "prepare_ref":
        shutil.copyfile(req["src"], req["out"])
        with wave.open(req["out"]) as w:
            dur = w.getnframes() / w.getframerate()
        print(json.dumps({"ok": True, "duration": dur}), flush=True)
    elif req["cmd"] == "synth":
        print("mensaje suelto de una librería", flush=True)
        if "FALLA" in req["text"]:
            print(json.dumps({"ok": False, "error": "fallo simulado"}), flush=True)
            continue
        sr = 24000
        n = int(sr * 0.05 * len(req["text"]))
        with wave.open(req["out"], "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
            w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 200 * i / sr))) for i in range(n)))
        print(json.dumps({"ok": True, "out": req["out"], "duration": n / sr, "seconds": 0.01, "sr": sr}), flush=True)
