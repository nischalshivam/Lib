"""Refine A/B: re-run catalog_refine on an episode's ORIGINAL (pre-refine) catalogue with another Gemini model, writing
to tools/_refine_ab/ - the library catalogue, its backup, the cast cache and the strips are not touched.
usage (models via env, e.g. VERIFY_MODELS=gemini-3-flash CLUE_MODELS=gemini-3-flash):
    py tools/refine_ab.py <video> <tag>"""
import json, os, sys, time
sys.path.insert(0, r"D:\Lib")
sys.path.insert(0, r"D:\Lib\libjobs")
import common as C                                   # libjobs: cast + show name the worker way
from media_index import catalog_refine as CR
from editv2 import common as E

video, tag = sys.argv[1], sys.argv[2]
cat = os.path.splitext(video)[0] + ".catalog.json"
bak = cat + ".v1.bak"
assert os.path.exists(bak), "no .v1.bak (original catalogue)"
assert "refined" not in json.load(open(bak, encoding="utf-8")), ".v1.bak is not the original"
out_dir = os.path.join(r"D:\Lib\tools\_refine_ab", tag)
os.makedirs(out_dir, exist_ok=True)
out = os.path.join(out_dir, os.path.basename(cat))
CR.CASTFREQ_DIR = os.path.join(out_dir, "castfreq")        # own cast answers (this model), not the library's cache
CR.STRIP_DIR = os.path.join(out_dir, "strips")
os.makedirs(CR.STRIP_DIR, exist_ok=True)
_orig = CR._lp
CR._lp = lambda p: _orig(bak if os.path.normcase(str(p)) == os.path.normcase(cat) else p)
print(f"{time.strftime('%H:%M:%S')} {tag}: vision {E.VISION_MODEL}, text {E.text_models()} -> {out}", flush=True)
r = CR.refine(video, C.cast_of(video), C.show_name(video), out=out,
              progress=lambda s: print(time.strftime('%H:%M:%S'), s, flush=True))
print("RESULT", json.dumps(r), flush=True)
