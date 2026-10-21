"""Final audio: narration (sound-up spans removed / inserted), the original moments
at voice level, a ducked music bed (varies per video), SFX on designed moments and
kinetic words, then compressor + loudnorm to -14 LUFS / -1.5 dBTP, muxed on the video."""
from __future__ import annotations

import glob, hashlib, json, os, re, subprocess

import numpy as np
from scipy.io import wavfile
from scipy.signal import butter, sosfilt

SR = 48000
BGM = r"C:\Users\Dell\Desktop\ProStudio\BGM"
# music bed level under the narration (RMS dB). 7 until 2026-10-22; the user wanted it a little quieter -> 9
BGM_UNDER_DB = float(os.environ.get("EDITV2_BGM_UNDER_DB", "9"))
SFXD = r"D:\Lib\hyperframes_lab\arya_demo\assets"


def load(path, ss=0, t=None):
    cmd = ["ffmpeg", "-v", "error", "-ss", str(ss)] + (["-t", str(t)] if t else []) + \
          ["-i", path, "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
    return np.frombuffer(subprocess.run(cmd, capture_output=True).stdout, np.float32).copy()


def _loop(tr, need, xf=2.5):
    """repeat a music track up to `need` samples with an equal-power crossfade at every join (the old plain
    concatenate made an audible jump each time a short track wrapped - ~5 per 23-min video)."""
    out, n = tr.copy(), int(xf * SR)
    n = min(n, len(tr) // 4)
    fade = np.linspace(0, np.pi / 2, n, dtype=np.float32)
    while len(out) < need:
        head = tr.copy()
        out[-n:] = out[-n:] * np.cos(fade) + head[:n] * np.sin(fade)
        out = np.concatenate([out, head[n:]])
    return out


def rms_db(x):
    m = np.abs(x) > 1e-3
    x = x[m] if m.any() else x
    return 20 * np.log10(np.sqrt((x ** 2).mean()) + 1e-9)


def lufs(p):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", p, "-af", "ebur128=peak=true", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    i = re.findall(r"I:\s+(-?[0-9.]+) LUFS", r.stderr)
    pk = re.findall(r"Peak:\s+(-?[0-9.]+) dBFS", r.stderr)
    return (float(i[-1]) if i else None), (float(pk[-1]) if pk else None)


def run(ctx, silent, out_mp4, log=print):
    B = ctx["build"]
    st = json.load(open(os.path.join(B, "state.json"), encoding="utf-8"))
    look = json.load(open(os.path.join(B, "look.json"), encoding="utf-8"))
    TOTAL, N = st["total"], int(st["total"] * SR)
    narr = load(os.path.join(B, "audio.wav"))
    voice_db = rms_db(narr)
    voice = np.zeros(N, np.float32)
    # narration pieces between the replaced spans
    cur_n, shift = 0.0, 0.0
    for a, b, d, _ in sorted(st["repl"], key=lambda r: r[0]):
        seg = narr[int(cur_n * SR): int(a * SR)]
        s0 = int((cur_n + shift) * SR)
        voice[s0: s0 + len(seg)] = seg[: max(0, N - s0)]
        shift += d - (b - a)
        cur_n = b
    seg = narr[int(cur_n * SR):]
    s0 = int((cur_n + shift) * SR)
    voice[s0: s0 + len(seg)] = seg[: max(0, N - s0)]
    for k, it in enumerate(st["items"]):
        if it["kind"] == "SU":
            w = os.path.join(B, "assets", f"c{k:03d}.wav")
            if os.path.exists(w):
                su = load(w)
                su *= 10 ** ((voice_db - 1.0 - rms_db(su)) / 20)
                s0 = int(it["o0"] * SR)
                voice[s0: s0 + len(su)] += su[: max(0, N - s0)]
    # music bed: a NEW track every ~4 min (changes at a punch-in when one is near), 3-s crossfades,
    # order seeded by the script so every video gets its own playlist; -7 dB under the voice
    beds = sorted(glob.glob(os.path.join(BGM, "*.mp3")) + glob.glob(os.path.join(BGM, "*.wav")))
    h = int(hashlib.md5(ctx["clean"].encode()).hexdigest(), 16)
    order = beds[h % len(beds):] + beds[:h % len(beds)]
    sus = [it["o0"] for it in st["items"] if it["kind"] == "SU"]
    bounds, t = [0.0], 240.0
    while t < TOTAL - 90:
        near = [x for x in sus if abs(x - t) < 60]
        t = min(near, key=lambda x: abs(x - t)) if near else t
        bounds.append(t)
        t += 240.0
    bounds.append(TOTAL)
    bed = np.zeros(N, np.float32)
    x = int(3 * SR)
    used = []
    for j, (a, b) in enumerate(zip(bounds, bounds[1:])):
        need = int((b - a) * SR) + x
        tr, bp = None, None
        for k in range(len(order)):               # a track that does not decode (0 samples) used to loop forever
            bp = order[(j + k) % len(order)]
            tr = load(bp, ss=12)
            if len(tr) < 10 * SR:                 # shorter than the 12-s skip / broken: try the whole file, else next
                tr = load(bp)
            if len(tr) >= 10 * SR:
                break
            log(f"  mix: skipped unusable music track {os.path.basename(bp)}")
            tr = None
        if tr is None:
            raise RuntimeError(f"no usable background music in {BGM}")
        used.append(os.path.basename(bp))
        tr = _loop(tr, need)
        tr = tr[:need] * 10 ** ((voice_db - BGM_UNDER_DB - rms_db(tr[:need])) / 20)
        s0 = int(a * SR)
        seg = tr[: max(0, min(need, N - s0))].copy()
        if j > 0:
            seg[:x] *= np.linspace(0, 1, x)[: len(seg[:x])]
        if s0 + len(seg) < N:
            seg[-x:] *= np.linspace(1, 0, x)[-len(seg[-x:]):]
        bed[s0: s0 + len(seg)] += seg
    bed_p = " > ".join(used)
    # punch-ins: the music almost drops out while the show speaks (a beat before and after too)
    drop = np.ones(N, np.float32)
    for it in st["items"]:
        if it["kind"] == "SU":
            a, b = int(max(0, it["o0"] - 0.25) * SR), int(min(TOTAL, it["o1"] + 0.2) * SR)
            drop[a:b] = 10 ** (-16 / 20)
    k = int(0.25 * SR)                                   # 0.25-s ramps (running mean via cumsum: O(N))
    cs = np.concatenate([[0.0], np.cumsum(drop, dtype=np.float64)])
    lo = np.clip(np.arange(N) - k // 2, 0, N); hi = np.clip(np.arange(N) + k // 2, 0, N)
    drop = ((cs[hi] - cs[lo]) / np.maximum(1, hi - lo)).astype(np.float32)
    bed *= drop
    win = int(0.05 * SR)
    env = np.convolve(np.abs(voice), np.ones(win) / win, mode="same")
    target = np.where(env > 10 ** ((voice_db - 22) / 20), 10 ** (-6 / 20), 1.0)
    g = np.empty(N, np.float32); cur = 1.0
    att, rel = 1 - np.exp(-1 / (0.04 * SR)), 1 - np.exp(-1 / (0.35 * SR))
    for i in range(0, N, 64):
        t = target[i]
        cur += (t - cur) * (att if t < cur else rel) * 64
        cur = min(1.0, max(10 ** (-6 / 20), cur))
        g[i:i + 64] = cur
    bed *= g
    bed = sosfilt(butter(2, [1500, 4000], btype="bandstop", fs=SR, output="sos"), bed) * 0.4 + bed * 0.6
    f = int(2 * SR); bed[:f] *= np.linspace(0, 1, f); bed[-int(3 * SR):] *= np.linspace(1, 0, int(3 * SR))
    # sfx
    fxt = np.zeros(N, np.float32)

    def place(x, at, db):
        x = x * 10 ** ((voice_db + db - rms_db(x)) / 20)
        s = int(at * SR)
        if 0 <= s < N:
            fxt[s: s + len(x)] += x[: N - s]
    t = np.arange(int(0.09 * SR)) / SR
    pop = (np.sin(2 * np.pi * (900 - 4000 * t) * t) * np.exp(-t * 55)).astype(np.float32)
    wh, who = load(os.path.join(SFXD, "sfx_whoosh.wav")), load(os.path.join(SFXD, "sfx_whoosh_out.wav"))
    for kind, a, b in look["designed"]:
        place(wh, a - 0.7, -17); place(who, b - 0.57, -24)
    for m in re.finditer(r'tl\.fromTo\("#pp\d+".*?,([0-9.]+)\);', open(os.path.join(B, "comp", "index.html"), encoding="utf-8").read()):
        place(who, float(m.group(1)) - 0.1, -27)
    html = open(os.path.join(B, "comp", "index.html"), encoding="utf-8").read()
    for m in re.finditer(r'tl\.fromTo\("#kw\d+_\d+".*?,([0-9.]+)\);', html):
        place(pop, float(m.group(1)) + 0.02, -24)
    for m in re.finditer(r'tl\.fromTo\("#af\d+".*?,([0-9.]+)\);', html):    # annotation freeze: shutter
        place(load(os.path.join(SFXD, "sfx_shutter.wav")), float(m.group(1)), -20)
    place(load(os.path.join(SFXD, "sfx_boom.wav")), 0.0, -16)
    pre = os.path.join(B, "mix_pre.wav")
    wavfile.write(pre, SR, (voice + bed + fxt).astype(np.float32))
    # one-pass loudnorm alone overshot after AAC (Reacher 01: true peak -0.8 dBFS, target -1.5): a brick-wall limiter
    # follows it, and if the encoded file still peaks above -1.0 the limit is lowered and the audio encoded again
    limit_db = -2.0
    for _ in range(3):
        af = ("acompressor=threshold=-22dB:ratio=3:attack=5:release=120:makeup=3,loudnorm=I=-14:TP=-1.5:LRA=9,"
              f"aresample=48000,alimiter=limit={10 ** (limit_db / 20):.4f}:attack=3:release=60:level=disabled")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", silent, "-i", pre, "-af", af, "-ar", "48000",
                        "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "256k", "-shortest", out_mp4],
                       check=True)
        li, pk = lufs(out_mp4)
        if pk is None or pk <= -1.0:
            break
        limit_db -= (pk + 1.0) + 0.3
    log(f"  mix: music {bed_p}, {li} LUFS, peak {pk} dBFS")
    return out_mp4
