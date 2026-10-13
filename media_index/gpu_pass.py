"""Child process for catalog_refine's GPU pass (language id, subtitle word timing, whisper).

A CUDA context with cuBLAS/cuDNN and CTranslate2's allocator cache stays in VRAM (~0.9 GB) for as long as the process
lives, even after the whisper model is released. 16 indexing workers that had each run whisper once held ~14 GB of a
12 GB GPU -> WDDM paged to system RAM, the GPU sat at '100 %' / 58 W and whisper ran 20x slower (2026-10-14 04:40,
12 workers queued 40 min for a slot). Run in a short-lived child, the GPU work gives all of its VRAM back on exit.

    gpu_pass.py <args.pkl> <result.pkl>      progress lines on stdout start with 'P '
"""
import os, pickle, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from media_index import catalog_refine as R  # noqa: E402


def main():
    a = pickle.load(open(sys.argv[1], "rb"))

    def prog(s):
        print("P " + str(s), flush=True)
    try:
        res = R._sync_dialogue_gpu(a["video"], a["data"], a["work"], a["dur"], a["tagged"], prog, a["wav"], a["checked"])
        out = ("r", res)
    except BaseException as e:                      # the parent re-raises it with the same text (error routing)
        out = ("e", f"{type(e).__name__}: {e}")
    tmp = sys.argv[2] + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump(out, f)
    os.replace(tmp, sys.argv[2])


if __name__ == "__main__":
    main()
