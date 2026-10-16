r"""Install the user's FTWD subtitles (Desktop\Subtitles\Fear the Walking Dead + the seasons 2-4 zips, stream-extracted
to the session scratchpad) next to the SSD-2 episodes, with libjobs/subs_install's sync check. 2026-10-17: FTWD only
has PGS (image) subs, so every episode went to GPU whisper and 15 of 22 workers queued 30+ min for a GPU slot."""
import glob, os, sys, traceback
sys.path.insert(0, r"D:\Lib\libjobs"); sys.path.insert(0, r"D:\Lib")
import subs_install as S
# GPU whisper when the indexing workers are paused; set SUBS_CPU=1 to run the sync check on the CPU (while 4
# indexing whisper slots keep the GPU at 100 %)
if os.environ.get("SUBS_CPU") == "1":
    from faster_whisper import WhisperModel
    S.DF._model = WhisperModel(S.DF.MODEL, device="cpu", compute_type="int8", cpu_threads=12)

ROOT = r"F:\Movies\Fear the Walking Dead\Fear the Walking Dead (2015) S01-S08"
SRC = [r"C:\Users\Dell\Desktop\Subtitles\Fear the Walking Dead", sys.argv[1]]
rep = __import__("json").load(open(S.REPORT, encoding="utf-8")) if os.path.exists(S.REPORT) else {}
cands = {}
for d in SRC:
    for p in glob.glob(os.path.join(d, "**", "*.srt"), recursive=True):
        e = S.ep_of(os.path.basename(p))
        if e:
            cands.setdefault(e, []).append(p)
vids = sorted(v for v in glob.glob(os.path.join(ROOT, "**", "*"), recursive=True)
              if v.lower().endswith(S.VID) and S.ep_of(os.path.basename(v)))
S.log(f"== FTWD (SSD 2): {len(vids)} episodes, {sum(len(x) for x in cands.values())} srt")
for v in vids:
    try:
        S.one(v, rep, cands, [])
    except Exception:
        rep[v] = dict(status="error", why=traceback.format_exc().splitlines()[-1][:120])
    S.save_report(rep)
S.log("== FTWD done")
