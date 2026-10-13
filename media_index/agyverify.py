"""Free, fast clip verification BEFORE the render — via the agy / CLIProxyAPI
Gemini rotation (no paid API).

After makevideo has cut every clip into its scene folder, and before prostudio
spends 10-25 min rendering, each scene is sent as ONE request: its narration, the
show + characters the clue script expects, and one small frame from every clip /
still placed in that scene (labelled with the source the tool took it from).
Gemini answers per frame: is it really from that show, and does it fit what the
narration is saying?

  * a frame from the WRONG show  -> rejected (moved to scene_xxx/_rejected, so
    prostudio never renders it);
  * a frame that clearly contradicts the narration -> rejected;
  * 'partial' / generic B-roll is KEPT (essay narration is abstract; the old
    verify over-rejected good clips into gaps);
  * a scene never loses more than half its media;
  * if too many frames are from the wrong show the whole video FAILS with a clear
    message — that is the wrong-folder / wrong-library safety net.

Scenes run in parallel across the account rotation with a hard time budget
(default 6 min); anything not answered in time is left as-is and reported.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

PROXY = os.environ.get("MODEL_ROUTER_URL", "http://127.0.0.1:8317") + "/v1/chat/completions"
MODEL = os.environ.get("VERIFY_MODEL") or os.environ.get("VP_MODEL", "gemini-3.8-flash-high")  # 3.6 retired 2026-10-13
BUDGET_S = int(os.environ.get("VERIFY_BUDGET_S", "360"))
WORKERS = int(os.environ.get("VERIFY_WORKERS", "12"))
FRAME_W = 384
MAX_FRAMES_PER_CALL = 16
WRONG_SHOW_FAIL = float(os.environ.get("VERIFY_WRONG_SHOW_FAIL", "0.25"))


class VerifyFailed(RuntimeError):
    """Too many clips are from the wrong show — do not render this video."""


RULES = (
    "You are the fact-checker for a TV/film video essay. For ONE scene you get "
    "the narration spoken over it, the show(s) and characters the script expects, "
    "and numbered frames — one per clip placed in that scene, each labelled with "
    "the episode the editor says it came from. Judge every frame:\n"
    "- show_ok: true if the frame plausibly comes from the labelled show (sets, "
    "cast, look). false ONLY when it clearly belongs to a different show/film "
    "(e.g. a medieval fantasy frame labelled as a 2000s family sitcom).\n"
    "- fits: 'yes' if it shows what/who the narration is about; 'partial' for "
    "generic but on-topic footage of the right show; 'no' ONLY if it clearly "
    "contradicts the narration (the wrong character is shown as the one being "
    "discussed, or it is ONLY credits/a title card/black with no scene). Cast or "
    "crew names overlaid on a real scene are FINE — judge the scene under them. "
    "A clip frame may be two pictures side by side (early and late in the clip).\n"
    "Be lenient with 'partial' — essays are abstract. Reply ONLY with a JSON "
    'array: [{"i": <frame number>, "show_ok": true|false, "fits": "yes"|"partial"|"no", '
    '"reason": "<5-12 words>"}]')


def _key():
    k = os.environ.get("MODEL_ROUTER_KEY")
    if k:
        return k.strip()
    try:
        return open(os.path.expanduser("~/.model-router-proxy-key.txt")).read().strip()
    except OSError:
        return ""


def proxy_ready(timeout=5) -> bool:
    try:
        req = urllib.request.Request(PROXY.replace("/chat/completions", "/models"),
                                     headers={"Authorization": f"Bearer {_key()}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _grab(path: str, at: float | None) -> bytes:
    args = ["ffmpeg", "-nostdin", "-v", "error"]
    if at is not None:
        args += ["-ss", f"{max(0.0, at):.2f}"]
    args += ["-i", path, "-frames:v", "1", "-vf", f"scale={FRAME_W}:-2",
             "-q:v", "5", "-f", "image2pipe", "-vcodec", "mjpeg", "-"]
    return subprocess.run(args, capture_output=True, timeout=60).stdout


def _frame_b64(path: str, kind: str, dur: float) -> str:
    """A still as one small JPEG; a clip as TWO frames side by side (25 % and
    75 % in) — a credit overlay ('co-executive producer ...') often covers only
    part of a clip, and one mid frame let most of them through. base64."""
    if kind != "video":
        out = _grab(path, None)
        return base64.b64encode(out).decode() if out else ""
    d = dur or 2.0
    a, b = _grab(path, d * 0.25), _grab(path, d * 0.75)
    if a and b:
        try:
            import io                                        # noqa: PLC0415
            from PIL import Image                            # noqa: PLC0415
            ia, ib = Image.open(io.BytesIO(a)), Image.open(io.BytesIO(b))
            w = Image.new("RGB", (ia.width + ib.width + 6, max(ia.height, ib.height)),
                          "white")
            w.paste(ia, (0, 0))
            w.paste(ib, (ia.width + 6, 0))
            buf = io.BytesIO()
            w.save(buf, "JPEG", quality=80)
            return base64.b64encode(buf.getvalue()).decode()
        except Exception:
            pass
    out = a or b
    return base64.b64encode(out).decode() if out else ""


def _call(content: list, timeout=120) -> list:
    body = json.dumps({"model": MODEL, "temperature": 0, "messages": [
        {"role": "system", "content": RULES},
        {"role": "user", "content": content}]}).encode()
    req = urllib.request.Request(PROXY, data=body, method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {_key()}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        txt = json.loads(r.read())["choices"][0]["message"]["content"]
    a, b = txt.find("["), txt.rfind("]")
    if a < 0 or b <= a:
        raise ValueError("no JSON array in reply")
    return json.loads(txt[a:b + 1])


def _beat_expect(beats: list, idx: int) -> tuple:
    """(shows, characters) the clue script names for beat `idx`."""
    if idx >= len(beats):
        return [], []
    shows, chars = [], []
    for s in beats[idx].get("shots") or []:
        src = re.sub(r"\s*\bs\d{1,2}\s*e\d{1,3}\b.*$", "", str(s.get("source") or ""),
                     flags=re.I).strip()
        if src and src not in shows:
            shows.append(src)
        for c in (s.get("characters") or []) + [s.get("speaker") or ""]:
            c = str(c).strip()
            if c and c.lower() != "unknown" and c not in chars:
                chars.append(c)
    return shows, chars[:10]


def _verify_scene(build_dir: str, sc: dict, shows: list, chars: list) -> list:
    """-> [(item, verdict_or_None)] for one scene."""
    sdir = os.path.join(build_dir, f"scene_{int(sc['scene']):03d}")
    items = [it for it in sc.get("items") or []
             if os.path.isfile(os.path.join(sdir, it.get("file", "")))]
    results = []
    for k in range(0, len(items), MAX_FRAMES_PER_CALL):
        chunk = items[k:k + MAX_FRAMES_PER_CALL]
        content = [{"type": "text", "text": (
            f"NARRATION: {str(sc.get('narration') or '')[:900]}\n"
            f"EXPECTED SHOW(S): {', '.join(shows) or 'as labelled'}\n"
            f"CHARACTERS THE SCRIPT NAMES HERE: {', '.join(chars) or 'n/a'}\n"
            f"{len(chunk)} frames follow.")}]
        for n, it in enumerate(chunk, 1):
            b64 = _frame_b64(os.path.join(sdir, it["file"]), it.get("kind", ""),
                             float(it.get("duration") or 2.0))
            content.append({"type": "text",
                            "text": f"#{n} — labelled: {it.get('source', '?')}"})
            if b64:
                content.append({"type": "image_url",
                                "image_url": {"url": "data:image/jpeg;base64," + b64}})
        try:
            ans = {int(a.get("i")): a for a in _call(content) if isinstance(a, dict)}
        except Exception:
            ans = {}
        for n, it in enumerate(chunk, 1):
            results.append((it, ans.get(n)))
    return results


def verify_build(build_dir: str, clue_path: str, log=print) -> dict:
    """Verify every placed clip of a built (not yet rendered) video. Moves bad
    ones aside; raises VerifyFailed if the video looks like the wrong library."""
    from . import jobs                                        # noqa: PLC0415
    t0 = time.time()
    tl_path = os.path.join(build_dir, "timeline.json")
    tl = json.load(open(tl_path, encoding="utf-8"))
    scenes = tl.get("scenes") or []
    try:
        beats = jobs.read_build_beats(build_dir, clue_path)
    except Exception:
        beats = []
    if not proxy_ready():
        log("  agy verify: proxy (127.0.0.1:8317) not reachable — SKIPPED "
            "(start agy_rotation\\start.bat to enable)")
        return {"skipped": True}
    log(f"  agy verify: {sum(len(s.get('items') or []) for s in scenes)} clips in "
        f"{len(scenes)} scenes, {WORKERS} parallel, budget {BUDGET_S}s, model {MODEL}")

    done = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = {pool.submit(_verify_scene, build_dir, sc, *_beat_expect(beats, i)): i
                for i, sc in enumerate(scenes)}
        pending = set(futs)
        while pending and time.time() - t0 < BUDGET_S:
            fin, pending = wait(pending, timeout=5, return_when=FIRST_COMPLETED)
            for f in fin:
                try:
                    done[futs[f]] = f.result()
                except Exception:
                    done[futs[f]] = []
        for f in pending:
            f.cancel()
        pool.shutdown(wait=False, cancel_futures=True)

    checked = wrong_show = rejected = unverified = 0
    report = []
    for i, sc in enumerate(scenes):
        res = done.get(i)
        if res is None:
            unverified += len(sc.get("items") or [])
            continue
        sdir = os.path.join(build_dir, f"scene_{int(sc['scene']):03d}")
        bad = []
        for it, v in res:
            if not v:
                unverified += 1
                continue
            checked += 1
            is_wrong_show = v.get("show_ok") is False
            wrong_show += is_wrong_show
            # overlaid credits/cast names are fine (user, 2026-09-27) — only the
            # show and the match with the narration decide
            if is_wrong_show or str(v.get("fits")).lower() == "no":
                bad.append((0 if is_wrong_show else 1, it, v))
        # never strip a scene below half its media — wrong-show frames go first
        bad.sort(key=lambda x: x[0])
        cap = max(1, len(res) // 2)
        for _prio, it, v in bad[:cap]:
            src = os.path.join(sdir, it["file"])
            dst_dir = os.path.join(sdir, "_rejected")
            os.makedirs(dst_dir, exist_ok=True)
            try:
                shutil.move(src, os.path.join(dst_dir, it["file"]))
                rejected += 1
                report.append({"scene": sc["scene"], "file": it["file"],
                               "source": it.get("source"), "show_ok": v.get("show_ok"),
                               "fits": v.get("fits"), "reason": v.get("reason")})
            except OSError:
                pass
    secs = time.time() - t0
    rate = wrong_show / checked if checked else 0.0
    summary = {"checked": checked, "rejected": rejected, "wrong_show": wrong_show,
               "unverified": unverified, "seconds": round(secs, 1),
               "wrong_show_rate": round(rate, 3), "rejections": report}
    with open(os.path.join(build_dir, "verify_report.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1, ensure_ascii=False)
    log(f"  agy verify: {checked} checked in {secs:.0f}s - {rejected} rejected "
        f"({wrong_show} wrong-show), {unverified} unverified")
    for r in report[:8]:
        log(f"      x scene {r['scene']} {r['file']} ({r['source']}): {r['reason']}")
    if checked >= 10 and rate > WRONG_SHOW_FAIL:
        raise VerifyFailed(
            f"{rate:.0%} of checked clips are NOT from the expected show — the "
            "library/folder for this script looks wrong. Not rendering.")
    return summary
