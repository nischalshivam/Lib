"""Pre-rendered 'wow' effects on a real clip (deterministic MP4 out):
  depth_text      — a giant word placed BEHIND the person (person matted per frame)
  color_isolate   — background goes grey/duotone, the person stays in colour
Masks: rembg person matting per frame, temporally smoothed (no edge flicker).
Runs on GPU when the matte venv has CUDA, else CPU."""
import glob, os, subprocess, sys, tempfile

FPS = int(os.environ.get("EDITV2_FPS", "24"))     # = editv2.common.FPS
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

MODEL = os.environ.get("MATTE_MODEL", "u2net_human_seg")


def _session():
    from rembg import new_session
    prov = None
    try:
        import onnxruntime as o
        try:
            o.preload_dlls()
        except Exception:
            pass
        if "CUDAExecutionProvider" in o.get_available_providers():
            prov = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    except Exception:
        pass
    return new_session(MODEL, providers=prov) if prov else new_session(MODEL)


def frames(clip, w=1920, fps=FPS):
    tmp = tempfile.mkdtemp(prefix="fx_")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", clip, "-vf", f"fps={fps},scale={w}:-2",
                    os.path.join(tmp, "%04d.png")], check=True)
    return tmp, sorted(glob.glob(os.path.join(tmp, "*.png")))


def masks(paths, sess=None, small=640):
    from rembg import remove
    sess = sess or _session()
    out, prev = [], None
    for p in paths:
        im = Image.open(p).convert("RGB")
        sm = im.resize((small, int(im.height * small / im.width)))
        m = np.asarray(remove(sm, session=sess, only_mask=True).resize(im.size, Image.BILINEAR)).astype(np.float32) / 255
        if prev is not None:
            m = 0.6 * m + 0.4 * prev          # temporal smoothing: kills edge shimmer
        prev = m
        out.append(m)
    return out


def _encode(frames_rgb, out, fps=FPS):
    h, w = frames_rgb[0].shape[:2]
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
                          "-r", str(fps), "-i", "-", "-c:v", "libx264", "-crf", "17", "-preset", "fast", "-g", str(fps),
                          "-pix_fmt", "yuv420p", out], stdin=subprocess.PIPE)
    for f in frames_rgb:
        p.stdin.write(f.astype(np.uint8).tobytes())
    p.stdin.close(); p.wait()


def _ease(x):
    x = min(1.0, max(0.0, x))
    return 1 - (1 - x) ** 3


def depth_text(clip, text, font, out, color=(255, 255, 255), stroke=None, fps=FPS,
               y_frac=0.36, height_frac=0.42, appear=0.15, sess=None, darken=0.15):
    """Word BEHIND the person. The word scales 1.12->1.0 and fades in over 0.35 s."""
    tmp, paths = frames(clip, fps=fps)
    M = masks(paths, sess)
    W, H = Image.open(paths[0]).size
    # fit the word to ~92% width at the requested cap height
    size = int(H * height_frac)
    f = ImageFont.truetype(font, size)
    while f.getbbox(text)[2] - f.getbbox(text)[0] > W * 0.92 and size > 40:
        size -= 8; f = ImageFont.truetype(font, size)
    bb = f.getbbox(text)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    layer = Image.new("RGBA", (tw + 40, th + 40), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    if stroke:
        d.text((20 - bb[0], 20 - bb[1]), text, font=f, fill=color + (255,), stroke_width=max(4, size // 28), stroke_fill=stroke + (255,))
    else:
        d.text((20 - bb[0], 20 - bb[1]), text, font=f, fill=color + (255,))
    shadow = layer.split()[3].filter(ImageFilter.GaussianBlur(size // 18))
    out_frames = []
    for k, (p, m) in enumerate(zip(paths, M)):
        t = k / fps
        a = _ease((t - appear) / 0.35)
        bg = np.asarray(Image.open(p).convert("RGB")).astype(np.float32)
        bg_d = bg * (1 - darken * a)
        sc = 1.12 - 0.12 * a
        lw, lh = int(layer.width * sc), int(layer.height * sc)
        L = layer.resize((lw, lh), Image.LANCZOS)
        S = shadow.resize((lw, lh))
        canvas = Image.fromarray(bg_d.astype(np.uint8)).convert("RGBA")
        x, y = (W - lw) // 2, int(H * y_frac - lh / 2)
        sh = Image.new("RGBA", (lw, lh), (0, 0, 0, 0)); sh.putalpha(S.point(lambda v: int(v * 0.55 * a)))
        canvas.alpha_composite(sh, (x + size // 30, y + size // 25))
        L.putalpha(L.split()[3].point(lambda v: int(v * a)))
        canvas.alpha_composite(L, (x, y))
        comp = np.asarray(canvas.convert("RGB")).astype(np.float32)
        mm = m[..., None]
        out_frames.append(bg * mm + comp * (1 - mm))      # person back on top
    _encode(out_frames, out, fps)
    return out


def color_isolate(clip, out, tint=(150, 110, 200), fps=FPS, sess=None, ramp=0.4):
    """Background desaturates to a duotone tint over `ramp` s; the person stays in colour."""
    tmp, paths = frames(clip, fps=fps)
    M = masks(paths, sess)
    tint = np.array(tint, np.float32) / 255
    res = []
    for k, (p, m) in enumerate(zip(paths, M)):
        a = _ease(k / fps / ramp)
        im = np.asarray(Image.open(p).convert("RGB")).astype(np.float32)
        lum = (0.3 * im[..., 0] + 0.59 * im[..., 1] + 0.11 * im[..., 2])[..., None]
        duo = (lum / 255) ** 1.1 * (0.35 + 0.65 * tint) * 255 * 0.85
        bg = im * (1 - a) + duo * a
        mm = m[..., None]
        res.append(im * mm + bg * (1 - mm))
    _encode(res, out, fps)
    return out


if __name__ == "__main__":
    import time
    t = time.time()
    clip = sys.argv[1]
    depth_text(clip, "LEADER", r"D:\Lib\hyperframes_lab\fx\fonts\ZillaSlab-Bold.ttf", "test_depth.mp4", color=(242, 177, 30))
    print("depth", round(time.time() - t, 1), "s")
    t = time.time()
    color_isolate(clip, "test_iso.mp4")
    print("isolate", round(time.time() - t, 1), "s")
