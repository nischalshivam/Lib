"""Command line for the dialogue index.

    python -m media_index build  D:/Media --db library.db
    python -m media_index find   "I never wanted the harvest" --db library.db
    python -m media_index stats  --db library.db
    python -m media_index resolve script.json --db library.db
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import (align, contact, cutter, doctor, embed, frames, gpu as gpu_mod,
               jobs as jobs_mod, library, lockfile, narration, probe, render,
               runner, search, sources, subs, subtitles, sync, term, timeline,
               transcribe, visual)
from .probe import ProbeError


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} GB"


def cmd_build(a):
    res = library.build(a.media_dir, a.db, verify_sync=a.verify_sync,
                        sync_seconds=a.sync_seconds)
    print("")
    print(f"  added {res.added} · updated {res.updated} · skipped {res.skipped}")
    print(f"  {res.cues:,} dialogue lines indexed in {res.seconds:.1f}s")
    if res.no_subs:
        print(f"\n  ⚠ {len(res.no_subs)} file(s) with no usable subtitles:")
        for p, why in res.no_subs[:20]:
            print(f"      {os.path.basename(p)}  —  {why}")
    if res.desynced:
        print(f"\n  {len(res.desynced)} file(s) had their subtitles shifted:")
        for p, why in res.desynced[:20]:
            print(f"      {os.path.basename(p)}  —  {why}")
    st = library.stats(a.db)
    print(f"\n  library.db = {_fmt_bytes(st['db_bytes'])}")
    return 0


def cmd_stats(a):
    st = library.stats(a.db)
    print(f"files          {st['media_files']}  "
          f"({st['with_subs']} with subs, {st['without_subs']} without)")
    print(f"titles         {st['shows']}")
    print(f"dialogue lines {st['dialogue_lines']:,}")
    print(f"db size        {_fmt_bytes(st['db_bytes'])}")
    print("")
    for t in st["titles"]:
        seasons = ""
        if t["kind"] == "episode" and t["s_min"] is not None:
            seasons = (f"  S{t['s_min']:02d}"
                       + (f"-S{t['s_max']:02d}" if t["s_max"] != t["s_min"] else ""))
        print(f"  {t['show']:<40} {t['files']:>3} file(s){seasons}"
              f"   {t['lines'] or 0:>6,} lines")
    return 0


def cmd_find(a):
    hits = search.find(a.db, a.quote, show=a.show, season=a.season,
                       episode=a.episode, limit=a.limit)
    if not hits:
        print("no match")
        return 1
    print(f'query: "{a.quote}"\n')
    for i, h in enumerate(hits, 1):
        mark = {"high": term.sym("yes"), "medium": term.sym("maybe"),
                "low": term.sym("no")}[h.confidence]
        a0, b0 = h.cut_window()
        print(f"{mark} {i}. {h.label}   {h.timecode}   "
              f"score {h.score:.0f}  cov {h.coverage:.0%}  [{h.confidence}]")
        print(f'      "{h.matched_text}"')
        print(f"      cut {a0/1000:.1f}s → {b0/1000:.1f}s   {os.path.basename(h.path)}")
    return 0


def cmd_resolve(a):
    beats = jobs_mod.read_beats(a.script)
    rows = search.resolve_script(a.db, beats)

    icon = {"resolved": term.sym("ok"), "ambiguous": term.sym("warn"),
            "weak": term.sym("warn"), "not_found": term.sym("fail"),
            "no_query": term.sym("blank")}
    counts = {}
    print(f"{'beat':>5} {'':3} {'where':<34} {'time':>12}  detail")
    print("-" * 92)
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
        where = r.hit.label if r.hit else "—"
        when = r.hit.timecode if r.hit else "—"
        extra = r.note or (f"score {r.hit.score:.0f}" if r.hit else "")
        print(f"{r.beat:>5}.{r.shot} {icon[r.status]} {where:<34} {when:>12}  {extra}")

    total = len(rows)
    ok = counts.get("resolved", 0)
    print("-" * 92)
    print(f"{ok}/{total} shots resolved exactly "
          f"({ok/total*100:.0f}%)" if total else "nothing to resolve")
    for k in ("ambiguous", "weak", "not_found", "no_query"):
        if counts.get(k):
            print(f"  {icon[k].strip()} {k:<10} {counts[k]}")
    if a.out:
        payload = [{"beat": r.beat, "shot": r.shot, "status": r.status,
                    "query": r.query, "note": r.note,
                    "hit": r.hit.as_dict() if r.hit else None,
                    "others": [h.as_dict() for h in r.others]} for r in rows]
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"\nwrote {a.out}")
    return 0 if counts.get("not_found", 0) == 0 else 1


def cmd_sync(a):
    """Check one file's subtitles against its audio."""
    kind, sub_path, cues = subtitles.load_for_video(a.video)
    if not cues:
        print("no subtitles found for this file")
        return 1
    print(f"{len(cues)} cues from {kind}"
          + (f" ({os.path.basename(sub_path)})" if sub_path else ""))
    r = sync.detect(a.video, cues, try_framerates=not a.no_framerate,
                    max_seconds=a.seconds,
                    log=(lambda m: print(m)) if a.verbose else (lambda *x: None))
    print(f"\n  {r.describe()}")
    if r.confidence == "low":
        print("  → these subtitles may belong to a different release")
        return 1
    if not r.in_sync:
        print(f"  → apply {r.offset_ms:+d} ms"
              + (f" and scale {r.scale:.5f} ({r.scale_name})" if r.scale != 1 else ""))
    return 0


def cmd_cut(a):
    """Find a line and write the clip in one step."""
    hits = search.find(a.db, a.quote, show=a.show, season=a.season,
                       episode=a.episode, limit=1)
    if not hits:
        print("no match — nothing cut")
        return 1
    h = hits[0]
    print(f"{h.label}  {h.timecode}  [{h.confidence}]")
    print(f'  "{h.matched_text}"')
    if h.confidence == "low" and not a.force:
        print("  refusing to cut a low-confidence match (use --force)")
        return 1

    if a.window:
        # Measurement mode. A 5-second clip can only answer yes or no, and
        # when the answer is no it does not say by how much — which is the
        # one number needed to fix anything. A window puts the claimed
        # position in the middle and lets the ear read the error off it.
        half = a.window / 2.0
        start = max(0.0, h.start_ms / 1000.0 - half)
        mark = h.start_ms / 1000.0 - start
        cutter.cut_clip(h.path, start, start + a.window, a.out,
                        mode=a.mode, height=a.height, with_audio=True)
        print(f"  wrote {a.out}  ({a.window:.0f}s window)")
        print()
        print(f"  The tool thinks this line is at {int(mark // 60)}:"
              f"{mark % 60:04.1f} into this clip.")
        print("  Play it. If you hear the line somewhere else, note that")
        print("  time — the difference is exactly how far out this episode is.")
        return 0

    cut = cutter.clip_for_hit(h, a.out, target_seconds=a.seconds,
                              mode=a.mode, height=a.height,
                              cover_full_line=a.full_line,
                              with_audio=a.audio, log=print)
    print(f"  wrote {a.out}  ({cut.duration:.2f}s)")
    if a.still:
        mid = (cut.start + cut.end) / 2
        cutter.extract_frame(h.path, mid, a.still, width=a.still_width)
        print(f"  wrote {a.still}")
    return 0


def cmd_handoff(a):
    """Export a built project as the ResearchCut Automate handoff JSON."""
    from . import handoff                                   # noqa: PLC0415
    out = handoff.export(a.build, a.out or "")
    import json as _json                                   # noqa: PLC0415
    data = _json.load(open(out, encoding="utf-8"))
    print(f"wrote {out}\n  {len(data['beats'])} beats, "
          f"{data['project']['duration']}s, schema {data['schema']}")
    return 0


def cmd_libcheck(a):
    """Does the library exist for this script? The launcher's pre-build gate."""
    from . import libcheck                                  # noqa: PLC0415
    try:
        res = libcheck.check(a.script, a.movies)
    except libcheck.ScriptUnreadable as exc:
        print(f"SCRIPT ERROR — {exc}")
        return 2
    print(libcheck.format_report(res))
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(res, f, indent=2, default=lambda o: sorted(o)
                      if isinstance(o, set) else str(o))
        print(f"\nwrote {a.out}")
    return 0 if res["ready"] else 1


def cmd_sources(a):
    """Which titles does this script need, and are they in the library?"""
    beats = jobs_mod.read_beats(a.script)
    reqs = sources.check(a.db, beats, resolve_dialogue=not a.fast)
    print(sources.format_report(reqs))
    if a.out:
        payload = [{"title": r.title, "shots": r.shots, "status": r.status,
                    "note": r.note, "beats": r.beats,
                    "library_titles": r.library_titles,
                    "episodes_needed": sorted(
                        f"S{s:02d}E{e:02d}" for s, e in
                        (r.episodes_declared | r.episodes_resolved) if e),
                    "episodes_missing": sorted(
                        f"S{s:02d}E{e:02d}" for s, e in r.missing_episodes if e)}
                   for r in reqs]
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"\nwrote {a.out}")
    return 1 if any(r.status == "missing" for r in reqs) else 0


def cmd_align(a):
    """Place shots that carry no dialogue, by walking the scene in order."""
    beats = jobs_mod.read_beats(a.script)
    places = align.align(a.db, beats, log=print)
    print()
    print(f"{'beat':>5} {'method':<14} {'time':>13} {'conf':<8} note")
    print("-" * 92)
    for p in places:
        print(f"{p.beat:>5}.{p.shot} {p.method:<13} "
              f"{(p.timecode if p.ok else '-'):>13} {p.confidence:<8} {p.note}")
    print("-" * 92)
    print(align.summarise(places))
    return 0


def cmd_subs(a):
    """Attach a downloaded subtitle pack to the right videos."""
    matches = subs.link(a.media_dir, a.subs, verify=not a.no_verify,
                        overwrite=a.overwrite, log=print)
    print(subs.format_results(matches))
    return 1 if any(m.status == "none" for m in matches) else 0


def cmd_stills(a):
    """Pull many distinct, good-quality stills out of a file or a range."""
    cands = frames.scan(a.video, a.start, a.end)
    best = frames.pick(cands, a.count)
    print(f"  {frames.describe(cands, best)}")
    out = a.out or os.path.join(os.path.dirname(a.video) or ".", "stills")
    written = frames.extract_stills(a.video, out, a.count, a.start, a.end,
                                    width=a.width, log=print)
    print(f"  wrote {len(written)} image(s) to {out}")
    return 0 if written else 1


def _gpu_report(rep):
    """Print the measurement. Every line is something that was read, not assumed."""
    print(f"  python       {rep.python}  ({rep.executable})")
    if not rep.torch:
        print("  torch        install nahi hai — picture index chalega hi nahi")
        return
    print(f"  torch        {rep.torch}")
    print(f"  CUDA build   {rep.cuda_build or 'NAHI — ye CPU-only wheel hai'}")
    if rep.arch_list:
        print(f"  banaya gaya  {', '.join(rep.arch_list)}")
    if rep.device_name:
        print(f"  GPU          {rep.device_name}  (compute {rep.compute} = {rep.sm})")
        print(f"  VRAM         {rep.vram_gb:.1f} GB, {rep.free_gb:.1f} GB free")

    if rep.usable:
        print("\n  GPU par 64x64 multiply chal gaya. Ye sach me kaam karega.")
    elif rep.wrong_arch:
        # The failure worth spelling out, because its own error message
        # ("no kernel image is available") names nothing that caused it.
        print(f"\n  Card dikh raha hai par ye torch uske liye nahi bana.")
        print(f"  Card {rep.sm} hai; wheel {', '.join(rep.arch_list)} ke liye hai.")
        print("  Aise torch par indexing 40 minute chal ke beech me marti hai,")
        print("  isliye tool khud CPU chunega — ye safe hai, bas dhima.")
    else:
        print(f"\n  GPU use nahi hoga — {rep.fault}")


def cmd_gpu(a):
    """What this machine can actually run the models on, measured — and fixed.

    Written because "GPU hai to use karo" is one sentence and the answer is
    not. The driver seeing a card, torch having CUDA at all, the wheel being
    built for *this* card, and a real multiply coming back correct are four
    different facts, and a person deciding whether to spend 2.5 GB of
    download deserves all four rather than a guess.

    `--install` exists because the alternative was a version number typed
    into a batch file, and a typed version is a claim about somebody else's
    computer. Here the index is asked what it has for this interpreter, and
    only an answer is installed.
    """
    rep = gpu_mod.probe()
    _gpu_report(rep)

    if not a.install:
        if not rep.usable and rep.torch:
            print("\n  Theek karne ke liye:  mi gpu --install")
        return 0 if rep.usable else 1

    if rep.usable:
        print("\n  Pehle se chal raha hai — kuch install karne ki zarurat nahi.")
        return 0

    print("\n  ================================================================")
    print("    Index se puch rahe hain ki is Python ke liye kya maujood hai")
    print("  ================================================================\n")
    found = gpu_mod.candidates(log=print)
    if not found:
        # Not a network failure and not worth retrying: PyTorch ships CPU
        # wheels for a new Python months before the CUDA ones, so this is a
        # calendar problem with a one-line answer.
        print(f"\n  Python {rep.python} ke liye kisi bhi CUDA channel par torch nahi hai.")
        print("  (CPU wala hai — isiliye tool chal raha hai. CUDA wala abhi nahi bana.)")
        print("\n  Iska ek hi seedha hal hai: Python 3.12 alag se install karo")
        print("  (python.org se, 'Add to PATH' tick karke), phir usme setup.bat")
        print("  chalao. Purana Python hataana nahi hai — dono saath rehte hain.")
        return 1

    pick = found[0]
    print(f"\n  Chuna gaya: torch {pick.version} ({pick.channel})")
    if rep.capability and rep.capability < (7, 0):
        # Pascal and older. Newer CUDA wheels drop these, and the whole
        # point of arch_list is that we find out now rather than at minute
        # forty of an index.
        print(f"  Dhyan do: tumhara card {rep.sm} hai, jo purana hai. Install ke")
        print("  baad dobara jaanch hogi — agar wheel me ye arch nahi hai to")
        print("  tool CPU par hi rahega aur ye saaf bata dega.")
    if not gpu_mod.install(pick, log=print):
        print("\n  Install nahi hua. Torch waisa hi hai jaisa pehle tha.")
        return 1

    print("\n  ================================================================")
    print("    Dobara jaanch — kya sach me badla?")
    print("  ================================================================\n")
    after = gpu_mod.probe()
    _gpu_report(after)
    if after.torch and pick.version not in after.torch:
        # pip printed "Successfully installed torch-2.13.0+cu130" and the
        # very next probe read 2.13.0+cpu. Saying so is the whole value of
        # measuring twice; without this line the install looks like it
        # worked and the tool looks like it is lying.
        print(f"\n  Dhyan do: pip ne {pick.version} lagaya bola, par Python "
              f"abhi bhi {after.torch} utha raha hai.")
        print("  Matlab purana torch poori tarah hata nahi — Windows use hone")
        print("  wali DLL delete nahi karne deta. SAB Python/tool windows band")
        print("  karo aur gpu.bat dobara chalao; ab wo purana hissa khud saaf")
        print("  karta hai.")
        return 1
    return 0 if after.usable else 1


def cmd_gold(a):
    """Turn a build into a labelling sheet, or score a filled one.

    The whole point is that the number at the end was written by a person
    watching the video, not produced by the same solver it is meant to
    judge. Two commands, one file between them:

        mi gold --template output/manifest.json   # makes gold.csv
        ... fill the 'verdict' column: exact / ok / wrong / none ...
        mi gold --score gold.csv                  # prints real accuracy
    """
    from . import gold                                     # noqa: PLC0415

    if a.template:
        if not os.path.isfile(a.template):
            print(f"  manifest nahi mila: {a.template}")
            return 1
        with open(a.template, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        rows = gold.rows_from_manifest(manifest)
        if not rows:
            print("  is manifest me koi scene nahi mila")
            return 1
        with open(a.out, "w", encoding="utf-8", newline="") as f:
            f.write(gold.write_template(rows))
        print(f"  {len(rows)} scene ki sheet bani: {a.out}")
        print("  ab video dekho aur har row ki 'verdict' me likho:")
        print("      exact  = sahi moment    ok = sahi scene, thoda idhar-udhar")
        print("      wrong  = galat footage  none = card/khaali")
        print(f"  phir chalao:  mi gold --score {a.out}")
        return 0

    if a.score:
        if not os.path.isfile(a.score):
            print(f"  sheet nahi mili: {a.score}")
            return 1
        with open(a.score, "r", encoding="utf-8") as f:
            rows = gold.read_labels(f.read())
        print(gold.score(rows).summary())
        return 0

    print("  --template <manifest.json>  ya  --score <gold.csv>  do")
    return 1


def cmd_gemini(a):
    """Is the vision verifier configured, and can it be reached?

    Written so the answer to "did I set the key right" is one command, not a
    forty-minute build that silently skips the step. It sends the smallest
    possible real request and reports exactly what came back, without ever
    printing the key.
    """
    from . import gemini                                   # noqa: PLC0415

    cfg = gemini.config()
    # Show the key MASKED (first/last 4 + length) and where it came from. A
    # valid key that still 401s almost always means the tool is sending a
    # DIFFERENT string than the one on the dashboard — most often a stale
    # GEMINI_API_KEY environment variable silently overriding settings.txt.
    # The mask lets the user compare length + ends against the provider without
    # ever printing the secret.
    env_key = os.environ.get("GEMINI_API_KEY")
    ksrc = gemini.key_source() or "kahin nahi"

    def _mask(k):
        return f"{k[:4]}…{k[-4:]}  ({len(k)} chars)" if k else \
            "NAHI — settings.txt me gemini_key daalo"
    print(f"  key      {_mask(cfg.key)}   [{ksrc}]")
    print(f"  endpoint {cfg.base or 'NAHI — settings.txt me gemini_base daalo'}")
    print(f"  model    {cfg.model}")
    # settings.txt now wins over the environment, so a stale env var can no
    # longer break a good file. Still worth a word so the picture is clear.
    if ksrc == "environment":
        print("\n  ℹ️  Key environment variable se aa rahi hai (settings.txt me "
              "gemini_key nahi hai). Behtar: settings.txt me daalo.")
    elif env_key:
        print(f"\n  ℹ️  Ek GEMINI_API_KEY env var bhi set hai ({_mask(env_key)}) "
              "par ab settings.txt jeet raha hai — sahi. Wo purana env var "
              "chahо to hata sakte ho:  setx GEMINI_API_KEY \"\"")
    ok, why = gemini.available()
    if not ok:
        print(f"\n  {why}")
        print("  settings.txt me ye do line daalo (tool ke folder me):")
        print("      gemini_key=<tumhari key>")
        print("      gemini_base=<endpoint URL, jaise https://.../v1>")
        return 1

    # Two steps, and the order matters. A text ping proves the key and the
    # endpoint; only then does an image ping test the multimodal path the
    # tool actually uses. If text works and image does not, the fault is the
    # image request, which is a different fix from a bad key — and the old
    # check, which said only "koi jawab nahi aaya", could not tell them apart.
    print("\n  1/2  text test bhej rahe hain...")
    ok, detail = gemini.ping(cfg, with_image=False)
    if not ok:
        print(f"       ✗  {detail}")
        low = detail.lower()
        # A 401 / "invalid token" means the endpoint answered and REJECTED the
        # key — the base is fine, so telling the user to try other bases (the
        # old advice) sends them the wrong way. An auth failure is a key
        # problem: re-copy it, check it has balance, check it covers this model.
        auth = ("401" in detail or "invalid" in low or "token" in low
                or "令牌" in detail or "无效" in detail or "unauthor" in low)
        if auth:
            print("\n  Endpoint to chal raha hai — usne jawab diya. Problem KEY "
                  "ki hai, base ki nahi (isliye base mat badlo).")
            print("  '令牌/invalid token' = key reject hui. Ye check karo:")
            print("   1. Key poori aur sahi copy hui? (aage-peeche space/enter "
                  "na ho, poori key ho)")
            print("   2. Us account/key me balance/credit hai? (khaali key 401 "
                  "deti hai — provider ke dashboard me top-up/activate karo)")
            print("   3. Ye key 'gemini-2.5-flash' (chat+vision) ke liye allowed "
                  "hai? (sirf image wali key chat pe kaam nahi karegi)")
            print("   4. Provider ke docs me jo EXACT base likha hai wahi daalo.")
            print("\n  Ya koi aur Gemini-capable base use karo:")
            print("   Google official: https://generativelanguage.googleapis.com/v1beta/openai")
            print("                    (key AIza... , model gemini-2.5-flash)")
            print("   yunwu:           https://yunwu.ai/v1")
        else:
            print("\n  Endpoint tak baat nahi pahunchi — base URL galat lag raha "
                  "hai. Ye try karo:")
            print("      https://yunwu.ai/v1")
            print("      https://generativelanguage.googleapis.com/v1beta/openai")
        return 1
    print(f"       ✓  jawab: {detail}")

    print("  2/2  image test bhej rahe hain (tool isi ka use karta hai)...")
    ok, detail = gemini.ping(cfg, with_image=True)
    if not ok:
        print(f"       ✗  {detail}")
        print("\n  Text chala par image nahi. Key sahi hai; ye model/endpoint "
              "shayad image (vision) support nahi karta.")
        print("  gemini_model=gemini-2.5-flash rakho (ye vision karta hai), "
              "ya wahi base rakho jispe text chala.")
        return 1
    print(f"       ✓  jawab: {detail}")
    print("\n  Sab sahi ✓  — vision model chaalu hai, build me apne aap "
          "lag jayega.")
    return 0


def cmd_catalog(a):
    """Tag a whole film/episode into a searchable shot library (catalog.json).

    The one-time, best-of-best pass: break the video into shots, have the
    vision model describe each (who / what / shot type / clean?), store it
    beside the exact subtitle timing. Every future video reuses it. Start with
    `--minutes 15` to sanity-check the descriptions cheaply before paying to
    tag a whole two-hour film.
    """
    from . import catalog                                  # noqa: PLC0415

    is_folder = os.path.isdir(a.video)
    if not is_folder and not os.path.isfile(a.video):
        print(f"  video/folder nahi mila: {a.video}")
        return 1
    try:
        minutes = float(str(a.minutes).strip())
    except ValueError:
        print(f"  --minutes ke liye sirf number chahiye, ye mila: {a.minutes!r}")
        print("  (sirf number likho, jaise: 15 — koi shabd nahi)")
        return 1

    # Character list: a file (one person per line, aliases after '=') or an
    # inline ';'-separated list. Forces one name per person instead of the
    # actor/persona/full-name mix the model gives on its own.
    people = []
    raw_chars = (a.characters or "").strip()
    if raw_chars:
        if os.path.isfile(raw_chars):
            with open(raw_chars, "r", encoding="utf-8-sig") as f:
                people = [ln.strip() for ln in f if ln.strip()]
        else:
            people = [p.strip() for p in raw_chars.split(";") if p.strip()]

    cast_dir = (getattr(a, "cast", "") or "").strip()
    if cast_dir and not os.path.isdir(cast_dir):
        print(f"  --cast folder nahi mila: {cast_dir}")
        print("  (har character ka subfolder + 5-8 photos: cast\\Victor\\1.jpg)")
        return 1

    # Season-cast subset: only these characters' reference photos are sent on
    # every shot. On a big-cast show (The Wire = 58) this is the main cost lever.
    ref_names = None
    raw_rn = (getattr(a, "ref_names", "") or "").strip()
    if raw_rn:
        if os.path.isfile(raw_rn):
            with open(raw_rn, "r", encoding="utf-8-sig") as f:
                ref_names = [ln.strip() for ln in f if ln.strip()]
        else:
            ref_names = [p.strip() for p in raw_rn.split(";") if p.strip()]
        print(f"  ref-names: {len(ref_names)} character(s) — sirf inke refs bhejenge (sasta build)")

    try:
        if is_folder:
            # A whole series/season: every episode into its own catalog.json.
            counts = catalog.run_folder(a.video, known_characters=people or None,
                                        max_minutes=minutes, cast_dir=cast_dir,
                                        ref_names=ref_names, log=print)
            done = sum(1 for n in counts.values() if n)
            print(f"\n  {done}/{len(counts)} episode(s) catalogued — "
                  f"{sum(counts.values())} shots total")
            print("  poori series ki library ban gayi — har video reuse karega.")
            return 0
        lib = catalog.run(a.video, out_json=a.out or "",
                          known_characters=people or None,
                          max_minutes=minutes, cast_dir=cast_dir,
                          ref_names=ref_names, log=print)
    except RuntimeError as exc:
        print(f"  {exc}")
        print("  pehle chalao:  mi gemini   (key + endpoint check)")
        return 1
    tagged = sum(1 for s in lib.values() if s.description)
    out = a.out or (os.path.splitext(a.video)[0] + ".catalog.json")
    print(f"\n  {tagged}/{len(lib)} shots described  →  {out}")
    named = sorted({c for s in lib.values() for c in s.characters})
    if named:
        print(f"  characters seen: {', '.join(named[:20])}")
    print("  ye library har video me reuse hogi — dobara tag nahi karna.")
    return 0


def cmd_plan(a):
    """Match a script against a catalogue and print the shot list (Stage 2).

    For each shot the script wants, show which catalogued moment it picked and
    why — a dialogue anchor (exact line), a description+character match, or an
    honest NEEDS VISUAL gap. This is the retrieval step made visible before any
    footage is cut.
    """
    from . import jobs, catalog, plan                       # noqa: PLC0415

    if not os.path.isfile(a.script):
        print(f"  script nahi mila: {a.script}")
        return 1
    # A catalogue is a single catalog.json OR a whole-series folder.
    if not (os.path.isfile(a.catalog) or os.path.isdir(a.catalog)):
        print(f"  catalogue nahi mila: {a.catalog}")
        print("  ek catalog.json do, ya poori series ka folder "
              "(jaise E:\\Movies\\Breaking Bad)")
        return 1
    library = catalog.load_library(a.catalog)
    if not library:
        print(f"  is jagah koi catalog.json nahi mili: {a.catalog}")
        print("  pehle chalao:  catalog.bat   (library banane ke liye)")
        return 1

    # A visual (genspark) script parses as JSON beats; a clean narration is
    # plain prose. Try the rich one first, fall back to sentence-per-line text
    # so a narration script works too.
    try:
        source = jobs.read_beats(a.script)
        kind = "visual script"
    except Exception:
        with open(a.script, "r", encoding="utf-8-sig") as f:
            source = f.read()
        kind = "narration (prose)"
    scope = (a.scope or "").strip()
    if scope:
        print(f"  scope: sirf {scope} ke shots use honge")
    print(f"  script: {kind}  ·  catalogue: {len(library)} shots")
    pairs, stats = plan.plan(source, library, scope=scope)
    icon = {"dialogue": "🗣", "description": "🎬", "none": "▢"}
    for req, m in pairs:
        tag = icon.get(m.method, "?")
        src = os.path.basename(m.shot.file) if m.placed else ""
        where = f"{m.shot.start:.0f}-{m.shot.end:.0f}s" if m.placed else "—"
        print(f"  b{req.beat:<3} {tag} {where:<12} {m.why}")
        if m.placed:
            print(f"        from : {m.shot.source}  ({src})")
        print(f"        script: {req.visual[:70]}")
    print("\n  " + stats.summary())
    print(f"  {len(library)} shots in the catalogue")

    # A file to hand back — a long script's shot list does not fit a terminal,
    # and the JSON is what a review or the next (assembly) step reads.
    if a.out:
        rows = [{
            "beat": req.beat, "method": m.method, "why": m.why,
            "script_visual": req.visual, "script_dialogue": req.dialogue,
            "script_characters": req.characters,
            "picked": None if not m.placed else {
                "source": m.shot.source, "file": m.shot.file,
                "start": m.shot.start, "end": m.shot.end,
                "description": m.shot.description,
                "characters": m.shot.characters, "dialogue": m.shot.dialogue},
        } for req, m in pairs]
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump({"summary": stats.summary(), "shots": rows}, f,
                      ensure_ascii=False, indent=1)
        print(f"  shot-list saved: {a.out}")
    return 0


def cmd_makevideo(a):
    """Stage 3: script + catalogue + voiceover -> a finished video.

    Cuts the matched shots out of the source episodes, times them to the
    voiceover, and renders the mp4 — reusing the existing timeline + render
    pipeline. Runs where the footage lives (needs ffmpeg + the source files).
    """
    from . import jobs, catalog, assemble                  # noqa: PLC0415

    if not os.path.isfile(a.script):
        print(f"  script nahi mila: {a.script}")
        return 1
    if not (os.path.isfile(a.catalog) or os.path.isdir(a.catalog)):
        print(f"  catalogue nahi mila: {a.catalog}")
        return 1
    if not os.path.isfile(a.audio):
        print(f"  voiceover (audio) nahi mila: {a.audio}")
        return 1
    library = catalog.load_library(a.catalog)
    if not library:
        print(f"  is jagah koi catalog.json nahi mili: {a.catalog}")
        return 1
    try:
        beats = jobs.read_beats(a.script)
    except Exception:
        print("  ye ek genspark (visual) script honi chahiye — clean narration "
              "nahi. Stage 3 shots + timings ke liye genspark chahiye.")
        return 1

    clean = ""
    if a.narration and os.path.isfile(a.narration):
        from . import narration as narr
        clean = narr.read_clean(a.narration)
    elif a.narration:
        print(f"  (narration file nahi mili, timing estimate se hogi: {a.narration})")

    out_dir = a.out or os.path.join(os.path.dirname(os.path.abspath(a.audio)),
                                    "video_build")
    print(f"  build folder: {out_dir}")
    video = assemble.make_video(beats, library, a.audio, out_dir,
                                scope=a.scope, pace=a.pace, clean=clean,
                                verify=not a.no_verify, cast_dir=a.cast,
                                verify_until=getattr(a,'verify_until',0.0),
                                language=getattr(a,'language','en'),
                                intro_punch=getattr(a,'intro_punch',False),
                                intro_punch_seconds=getattr(a,'intro_punch_seconds',180.0),
                                cold_open=getattr(a,'cold_open',False),
                                ken_burns=getattr(a,'ken_burns',False),
                                log=print)
    if os.path.isfile(video):
        print(f"\n  ✓ video ban gaya:  {video}")
    else:
        print("\n  video nahi bana — upar ka render report dekho")
    return 0


def cmd_look(a):
    """Index what the footage LOOKS like, so shots can be checked, not guessed.

    Slow and one-time, exactly like building the dialogue index — and for the
    same reason. Every script written about these episodes afterwards asks
    this index questions for free.
    """
    ok, why = embed.available()
    if not ok:
        print(f"  The picture model is not installed — {why}")
        print("\n  Install it with:")
        print("      pip install torch transformers sentencepiece")
        print(f"\n  The first run then downloads ~1 GB into {embed.models_dir()}")
        print("  After that it works with no internet at all.")
        return 1

    only = None
    if a.script:
        if not os.path.isfile(a.script):
            print(f"  No such script: {a.script}")
            return 1
        beats = jobs_mod.read_beats(a.script)
        only = visual.files_for_script(a.db, beats)
        if not only:
            print("  That script names no episode that is in the library.")
            return 1
        print(f"  this script needs {len(only)} file(s):")
        for path in only[:20]:
            print(f"      {os.path.basename(path)}")

    done, total = visual.coverage(a.db)
    print(f"  {done} of {total} file(s) already have their pictures indexed")
    if only is None and done >= total and total and not a.force:
        print("  nothing to do — add --force to redo them")
        return 0

    try:
        res = visual.build(a.db, only=only, fps=a.fps, force=a.force, log=print)
    except lockfile.Busy as exc:
        # The only thing anybody can do about this is close the other
        # window, so the sentence has to arrive whole rather than as a
        # traceback from four frames down.
        print(f"\n  {exc}\n")
        return 1
    print("")
    print(f"  looked at {res.indexed} file(s) {term.sym('dot')} "
          f"skipped {res.skipped} {term.sym('dot')} "
          f"{res.frames:,} frames in {res.seconds / 60:.0f} min")
    if res.failed:
        print(f"\n  {len(res.failed)} file(s) could not be read:")
        for path, why in res.failed[:20]:
            print(f"      {os.path.basename(path)}  —  {why}")
    done, total = visual.coverage(a.db)
    print(f"\n  {done} of {total} file(s) can now be checked by picture")
    return 0 if not res.failed else 1


def cmd_see(a):
    """Describe a picture; get the real frames back. The eye's own proof.

    The same idea as cutting a clip and listening to it. A build reports
    numbers, and numbers can be healthy while the footage is wrong — that has
    happened here more than once. This asks for one picture, in words, and
    puts the frames on screen. Either the tool can see or it cannot, and it
    takes ten seconds to find out.
    """
    ok, why = embed.available()
    if not ok:
        print(f"  The picture model is not installed — {why}")
        return 1
    done, total = visual.coverage(a.db)
    if not done:
        print("  No footage has been looked at yet — run 'Look at the "
              "footage' first.")
        return 1

    backend = embed.load(log=print)
    vec = backend.encode_texts([a.text])[0]

    con = library.connect(a.db)
    try:
        rows = con.execute(
            "SELECT v.path AS path, m.show AS show, m.season AS season, "
            "       m.episode AS episode "
            "  FROM visual v LEFT JOIN media m ON m.path = v.path").fetchall()
        hits = []
        for row in rows:
            index = visual.load(con, a.db, row["path"])
            if index is None:
                continue
            match = visual.best_in(index, vec)
            if match.searched:
                hits.append((match, row))
    finally:
        con.close()

    if not hits:
        print("  nothing indexed could be searched")
        return 1
    hits.sort(key=lambda h: -h[0].lift)

    print(f'\n  "{a.text}"\n')
    os.makedirs(a.out, exist_ok=True)
    written = []
    for i, (match, row) in enumerate(hits[:a.limit], 1):
        label = os.path.basename(row["path"])
        if row["show"] and row["season"] is not None:
            label = f"{row['show']} S{row['season']:02d}E{row['episode']:02d}"
        mark = {"high": term.sym("yes"), "medium": term.sym("maybe"),
                "low": term.sym("no")}[match.confidence]
        mins, secs = divmod(int(match.time), 60)
        print(f"  {mark} {i}. {label}   {mins}:{secs:02d}   "
              f"lift {match.lift:.1f}  [{match.confidence}]")
        out = os.path.join(a.out, f"see_{i:02d}.jpg")
        try:
            cutter.extract_frame(row["path"], match.time, out, width=1920)
            written.append(out)
        except Exception as exc:                # one bad file is not the end
            print(f"        could not extract the frame — {exc}")

    print(f"\n  {len(written)} frame(s) written to {a.out}")
    print("  Open them. If the top one is not the picture you described,")
    print("  the model is not seeing this footage and nothing built on it")
    print("  will be right either.")
    return 0 if written else 1


def cmd_check(a):
    """Inspect a media folder and say whether it will work."""
    reports = doctor.inspect_folder(
        a.media_dir, log=(lambda m: print(m)) if a.verbose else (lambda *x: None))
    print(doctor.format_report(reports, a.media_dir))
    bad = [r for r in reports if r.verdict != doctor.VERDICT_OK]
    return 1 if bad else 0


def cmd_transcribe(a):
    """Make subtitles from the audio when a file has none."""
    if not transcribe.available():
        print("faster-whisper is not installed.\n"
              "  Install it with:  pip install faster-whisper\n"
              "  The first run then downloads the model (a few hundred MB).")
        return 1
    target = a.target
    try:
        if os.path.isdir(target):
            results = transcribe.transcribe_folder(
                target, model_name=a.model, overwrite=a.overwrite)
        else:
            results = [transcribe.transcribe_file(
                target, model_name=a.model, overwrite=a.overwrite, log=print)]
    except transcribe.TranscribeUnavailable as exc:
        print(f"\n  {exc}")
        return 1
    print(transcribe.format_results(results))
    return 1 if any(r.status == "failed" for r in results) else 0


def cmd_web(a):
    """Serve the pages until the window is closed."""
    from . import web
    print()
    web.serve(db_path=a.db, out=a.out, port=a.port,
              open_browser=not a.no_browser, libraries_root=a.libraries)
    return 0


def cmd_preflight(a):
    """Check every queued job without building anything."""
    queue = jobs_mod.load_jobs(a.jobs)
    reports = jobs_mod.preflight_all(queue, log=lambda m: print("  " + str(m)))
    print(jobs_mod.format_reports(reports))
    return 1 if any(r.status == "BLOCKED" for r in reports) else 0


def cmd_make(a):
    """Build one video from one script, without writing a job file.

    The queue exists for twenty-five videos overnight. Testing a single
    script should not require authoring JSON about JSON first.
    """
    job = jobs_mod.Job(name=a.name or os.path.splitext(
        os.path.basename(a.script))[0],
        script=os.path.abspath(a.script),
        out=os.path.abspath(a.out), db=os.path.abspath(a.db),
        clip_seconds=a.seconds, height=a.height,
        stills_per_scene=a.stills)
    report = jobs_mod.preflight(job, log=lambda m: print("  " + str(m)))
    print(jobs_mod.format_reports([report]))
    # Printed in full only here. The queue builds twenty-five videos and this
    # would bury its summary; a single script is being tested, and the whole
    # point of testing one is to find out what to change in it.
    if report.quotes and report.quotes.advice():
        print("\n  ABOUT THE QUOTED LINES")
        print(f"  {report.quotes.detail()}\n")
        for line in report.quotes.advice():
            print(f"      {line}")
    if report.blocked and not a.force:
        print("\n  blocked — nothing built (use --force to try anyway)")
        return 1
    result = runner.run_job(job, report, log=print)
    print(f"\n  {result.clips} clip(s), {result.stills} still(s) "
          f"in {result.seconds:.0f}s")
    print(f"  {result.gaps} scene(s) with nothing")
    print(f"  -> {job.out}")
    return 0


def cmd_timeline(a):
    """Decide how long every shot holds, and when — then write it down.

    Separate from the build on purpose. Re-timing is seconds and re-cutting
    is an hour, so the rhythm can be argued with as many times as it takes
    without touching a frame of footage.
    """
    try:
        manifest = timeline.load_manifest(a.folder)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  No manifest in {a.folder} — build it first.  ({exc})")
        return 1
    beats = jobs_mod.read_beats(a.script)

    total, spans = 0.0, None
    if a.audio:
        try:
            total = probe.probe(a.audio).duration
            print(f"  narration is {total / 60:.1f} min")
        except ProbeError as exc:
            print(f"  could not read {a.audio} — {exc}")
        if not a.no_listen:
            # Listening to the recording beats estimating from word counts,
            # and by enough to be worth the minutes: an even read is an
            # assumption, and where it fails it fails locally — every visual
            # after a long pause under the wrong sentence.
            heard = narration.align_audio(beats, a.audio, total_seconds=total,
                                          log=print)
            print(heard.summary())
            if heard.ok:
                spans = heard.spans
                if heard.weak:
                    print(f"      {len(heard.weak)} beat(s) had no matched "
                          "word nearby and were interpolated: "
                          + ", ".join(str(b) for b in heard.weak[:12]))
    if spans is None:
        print("  falling back on the script's estimate of 150 words a minute")

    tl = timeline.plan(beats, manifest, total_seconds=total, pace=a.pace,
                       spans=spans,
                       audio=os.path.abspath(a.audio) if a.audio else "")
    path = timeline.write(tl, a.folder)
    print(tl.summary())
    short = tl.uncovered()
    if short:
        print(f"\n  {len(short)} beat(s) have less footage than narration:")
        for s in short[:10]:
            print(f"      scene {s.index:>3}  {s.gap:.1f}s short  {s.note}")
        print("      More shots in those beats is the fix, not longer ones.")
    print(f"\n  -> {path}")
    return 0


def cmd_render(a):
    """Make the video. The first step whose output can simply be watched."""
    res = render.render_folder(a.folder, out_name=a.out, audio=a.audio,
                               motion=not a.no_motion,
                               resume=not a.restart, log=print)
    print("")
    print(render.describe(res))
    if res.failed:
        print(f"\n  {len(res.failed)} problem(s):")
        for what, why in res.failed[:10]:
            print(f"      {what}  —  {why}")
    if res.ok:
        print(f"\n  -> {res.path}")
    return 0 if res.ok else 1


def cmd_sheet(a):
    """One page of every still, so a hundred can be judged at a glance."""
    made = contact.build(a.folder, a.out, columns=a.columns, log=print)
    if not made:
        print("  no images found under " + a.folder)
        return 1
    print(f"  wrote {made}")
    return 0


def cmd_run(a):
    if os.path.isdir(a.jobs):
        print(f"  {a.jobs} is a folder.\n"
              "  This step wants a job FILE (jobs.json) listing the videos to\n"
              "  build. To point the tool at a folder of episodes, use "
              "'Set the media folder'.")
        return 1
    if not os.path.isfile(a.jobs):
        print(f"  No such job file: {a.jobs}")
        return 1
    """Pre-flight the whole queue, then build what passed."""
    results = runner.run_queue(a.jobs, dry_run=a.dry_run,
                               allow_gaps=not a.strict)
    return 1 if any(r.status in ("failed", "skipped") for r in results) else 0


def main(argv=None):
    term.enable_utf8()          # never let a code page raise
    # --db is shared by every subcommand, and works on either side of it
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default="library.db",
                        help="index file (default library.db)")

    p = argparse.ArgumentParser(prog="media_index", parents=[common],
                                description="Dialogue index for owned media.")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", parents=[common],
                       help="scan a media folder into the index")
    b.add_argument("media_dir")
    b.add_argument("--verify-sync", action="store_true",
                   help="check every subtitle against the audio and correct drift")
    b.add_argument("--sync-seconds", type=float,
                   help="only analyse the first N seconds when checking sync")
    b.set_defaults(func=cmd_build)

    s = sub.add_parser("stats", parents=[common], help="what is in the index")
    s.set_defaults(func=cmd_stats)

    f = sub.add_parser("find", parents=[common], help="locate a spoken line")
    f.add_argument("quote")
    f.add_argument("--show")
    f.add_argument("--season", type=int)
    f.add_argument("--episode", type=int)
    f.add_argument("--limit", type=int, default=5)
    f.set_defaults(func=cmd_find)

    r = sub.add_parser("resolve", parents=[common], help="pre-flight a whole visual script")
    r.add_argument("script", help="JSON from the visual-script prompt")
    r.add_argument("--out", help="write the full report as JSON")
    r.set_defaults(func=cmd_resolve)

    y = sub.add_parser("sync", parents=[common],
                       help="check one file's subtitle timing against its audio")
    y.add_argument("video")
    y.add_argument("--seconds", type=float, help="analyse only the first N seconds")
    y.add_argument("--no-framerate", action="store_true",
                   help="skip the framerate-conversion search")
    y.add_argument("-v", "--verbose", action="store_true")
    y.set_defaults(func=cmd_sync)

    c = sub.add_parser("cut", parents=[common],
                       help="find a line and write the clip")
    c.add_argument("quote")
    c.add_argument("--out", required=True, help="output clip path")
    c.add_argument("--seconds", type=float, default=4.0, help="clip length")
    c.add_argument("--full-line", action="store_true",
                   help="cover the whole spoken line instead of --seconds")
    c.add_argument("--mode", choices=("accurate", "fast"), default="accurate")
    c.add_argument("--height", type=int, help="scale output to this height")
    c.add_argument("--window", type=float, default=0.0,
                   help="cut this many seconds AROUND the line instead of a "
                        "clip, to measure how far out the subtitles are")
    c.add_argument("--audio", action="store_true",
                   help="keep the original sound (use when you will watch it)")
    c.add_argument("--still", help="also write a still frame here")
    c.add_argument("--still-width", type=int, default=1920)
    c.add_argument("--show")
    c.add_argument("--season", type=int)
    c.add_argument("--episode", type=int)
    c.add_argument("--force", action="store_true",
                   help="cut even a low-confidence match")
    c.set_defaults(func=cmd_cut)

    o = sub.add_parser("sources", parents=[common],
                       help="what titles this script needs, and what is missing")
    o.add_argument("script", help="JSON from the visual-script prompt")
    o.add_argument("--out", help="write the report as JSON")
    o.add_argument("--fast", action="store_true",
                   help="skip dialogue resolution (titles only, no episodes)")
    o.set_defaults(func=cmd_sources)

    lc = sub.add_parser("libcheck", parents=[common],
                        help="does the library exist for this script? (reads "
                             "catalog.json on disk; the launcher's pre-build gate)")
    lc.add_argument("script", help="JSON visual/clue script")
    lc.add_argument("movies", help="movies root, e.g. E:\\Movies")
    lc.add_argument("--out", help="write the JSON report here")
    lc.set_defaults(func=cmd_libcheck)

    hx = sub.add_parser("handoff", parents=[common],
                        help="export a built project as the ResearchCut Automate "
                             "handoff JSON (researchcut-automation-beats-v1)")
    hx.add_argument("build", help="the makevideo build folder (has timeline.json)")
    hx.add_argument("--out", help="output path (default: <build>/researchcut_beats.json)")
    hx.set_defaults(func=cmd_handoff)

    u = sub.add_parser("subs", parents=[common],
                       help="attach a downloaded subtitle pack to the videos")
    u.add_argument("media_dir")
    u.add_argument("--subs", help="folder holding the .srt files "
                                  "(default: the media folder itself)")
    u.add_argument("--no-verify", action="store_true",
                   help="skip playing each version against the audio")
    u.add_argument("--overwrite", action="store_true")
    u.set_defaults(func=cmd_subs)

    i = sub.add_parser("stills", parents=[common],
                       help="pull many distinct stills out of a video")
    i.add_argument("video")
    i.add_argument("--count", type=int, default=20)
    i.add_argument("--start", type=float, default=0.0)
    i.add_argument("--end", type=float)
    i.add_argument("--width", type=int, default=1920)
    i.add_argument("--out", help="output folder (default <video folder>/stills)")
    i.set_defaults(func=cmd_stills)

    g = sub.add_parser("align", parents=[common],
                       help="place shots that have no dialogue, along the scene")
    g.add_argument("script", help="JSON from the visual-script prompt")
    g.set_defaults(func=cmd_align)

    d = sub.add_parser("check", parents=[common],
                       help="inspect a media folder before indexing it")
    d.add_argument("media_dir")
    d.add_argument("-v", "--verbose", action="store_true")
    d.set_defaults(func=cmd_check)

    t = sub.add_parser("transcribe", parents=[common],
                       help="make subtitles from the audio (files with none)")
    t.add_argument("target", help="a video file, or a folder of them")
    t.add_argument("--model", default=transcribe.DEFAULT_MODEL,
                   help=f"whisper model (default {transcribe.DEFAULT_MODEL}; "
                        "small.en is slower and more accurate)")
    t.add_argument("--overwrite", action="store_true",
                   help="redo files that already have a subtitle")
    t.set_defaults(func=cmd_transcribe)

    mk = sub.add_parser("make", parents=[common],
                       help="build one video from one script")
    mk.add_argument("script")
    mk.add_argument("--out", required=True, help="output folder")
    mk.add_argument("--name", default="")
    mk.add_argument("--seconds", type=float, default=4.0)
    mk.add_argument("--stills", type=int, default=2,
                    help="stills to take per shot")
    mk.add_argument("--height", type=int)
    mk.add_argument("--force", action="store_true")
    mk.set_defaults(func=cmd_make)

    tm = sub.add_parser("timeline", parents=[common],
                        help="decide how long every shot holds, and when")
    tm.add_argument("folder", help="a built job output folder")
    tm.add_argument("script", help="the visual script it was built from")
    tm.add_argument("--audio", default="",
                    help="the narration recording, so the plan matches it")
    tm.add_argument("--no-listen", action="store_true",
                    help="do not transcribe the voiceover; estimate instead")
    tm.add_argument("--pace", default="normal",
                    choices=sorted(timeline.PACES),
                    help="how often the picture changes (default normal)")
    tm.set_defaults(func=cmd_timeline)

    rn = sub.add_parser("render", parents=[common],
                        help="turn a planned timeline into a video file")
    rn.add_argument("folder", help="a built job output folder")
    rn.add_argument("--out", default="video.mp4")
    rn.add_argument("--audio", default="",
                    help="narration; taken from timeline.json if omitted")
    rn.add_argument("--no-motion", action="store_true",
                    help="hold stills dead still instead of drifting")
    rn.add_argument("--restart", action="store_true",
                    help="re-render every segment from scratch")
    rn.set_defaults(func=cmd_render)

    sh = sub.add_parser("sheet", parents=[common],
                        help="contact sheet of every still that was made")
    sh.add_argument("folder", help="a job output folder")
    sh.add_argument("--out", default="contact_sheet.jpg")
    sh.add_argument("--columns", type=int, default=8)
    sh.set_defaults(func=cmd_sheet)

    lk = sub.add_parser("look", parents=[common],
                        help="index what the footage looks like (slow, once)")
    lk.add_argument("--fps", type=float, default=visual.DEFAULT_FPS,
                    help=f"frames sampled per second (default {visual.DEFAULT_FPS})")
    lk.add_argument("--script",
                    help="index only the episodes this visual script needs")
    lk.add_argument("--force", action="store_true",
                    help="redo files that are already done")
    lk.set_defaults(func=cmd_look)

    gp = sub.add_parser("gpu", parents=[common],
                        help="kya models GPU par chal sakte hain")
    gp.add_argument("--install", action="store_true",
                    help="is Python ke liye jo CUDA torch maujood hai wo lagao")
    gp.set_defaults(func=cmd_gpu)

    ge = sub.add_parser("gemini", parents=[common],
                        help="kya vision model (silent shots ke liye) set hai")
    ge.set_defaults(func=cmd_gemini)

    ct = sub.add_parser("catalog", parents=[common],
                        help="movie/episode/poori-series ko tag karke searchable library banao")
    ct.add_argument("video", help="ek video file, YA poori series/season ka folder")
    ct.add_argument("--out", default="",
                    help="library kahan likhni hai (default: video ke paas .catalog.json)")
    # str, not type=float: a stray word after the number ("15 minutes" typed
    # into the batch prompt) must produce ONE clear Hindi line from
    # cmd_catalog, not argparse's generic English "invalid float value".
    ct.add_argument("--minutes", default="0",
                    help="sirf pehle N minute (sasta test); 0 = poori video")
    ct.add_argument("--characters", default="",
                    help="character naam consistent karne ke liye: file path "
                         "(ek line ek banda, aliases '=' ke baad) ya inline "
                         "'Arthur = Arthur Fleck, Joker; Murray = Murray Franklin'")
    ct.add_argument("--cast", default="",
                    help="cast folder: har character ka subfolder + 5-8 "
                         "reference photos (cast\\Victor\\1.jpg). Isse model "
                         "catalog banate waqt sahi character pehchanta hai — "
                         "library ki foundation isi se bharosemand banti hai.")
    ct.add_argument("--ref-names", dest="ref_names", default="",
                    help="sirf in characters ke refs bhejo: file (ek line ek "
                         "naam) ya 'A; B; C'. Bade cast (The Wire) me season-wise "
                         "subset se build cost ~⅓ ho jaati hai. cast/characters "
                         "ke naam se match (case-insensitive).")
    ct.set_defaults(func=cmd_catalog)

    pl = sub.add_parser("plan", parents=[common],
                        help="script ko catalog se match karke shot-list dikhao")
    pl.add_argument("script", help="visual/genspark script (beats + shots)")
    pl.add_argument("catalog", help="catalog.json, YA poori series ka folder "
                                    "(saari catalog.json merge ho jayengi)")
    pl.add_argument("--out", default="", help="shot-list JSON kahan likhni hai")
    pl.add_argument("--scope", default="",
                    help="poore script ko ek episode/title tak seemit karo "
                         "(jaise S04E01) — single-scene essay ke liye")
    pl.set_defaults(func=cmd_plan)

    mv = sub.add_parser("makevideo", parents=[common],
                        help="Stage 3: script + catalog + voiceover se video banao")
    mv.add_argument("script", help="genspark (visual) script")
    mv.add_argument("catalog", help="catalog.json ya series folder")
    mv.add_argument("audio", help="voiceover / narration audio (mp3/wav)")
    mv.add_argument("--narration", default="",
                    help="clean narration (poori) script — accurate timing ke liye")
    mv.add_argument("--out", default="", help="build folder (default: audio ke paas)")
    mv.add_argument("--scope", default="", help="ek episode tak seemit (jaise S04E01)")
    mv.add_argument("--pace", default="normal", help="normal | fast | cinematic")
    mv.add_argument("--no-verify", action="store_true",
                    help="Gemini se har clip verify mat karo (tez, par kam accurate)")
    mv.add_argument("--verify-until", type=float, default=0.0,
                    help="Gemini verify only the first N SECONDS (the intro), then off — cheap accuracy for long videos")
    mv.add_argument("--cast", default="",
                    help="cast folder — har character ka subfolder + reference "
                         "photos (identity reliably verify karne ke liye)")
    mv.add_argument("--language", "--lang", default="en", dest="language",
                    help="script/voiceover language: en (default), pt, fr, es, "
                         "de, ... or 'auto'. Library stays English; non-English "
                         "just switches whisper to the multilingual model.")
    mv.add_argument("--intro-punch-ins", dest="intro_punch", action="store_true",
                    help="intro hook: pehle 3 min me famous dialogues pe narration "
                         "ruk ke ORIGINAL show ki awaaz bajti hai (halka pause "
                         "dono taraf), phir narration resume. Engagement 10x.")
    mv.add_argument("--intro-punch-seconds", dest="intro_punch_seconds",
                    type=float, default=180.0,
                    help="kitne shuruaati seconds me punch-ins dhoondhe (default 180)")
    mv.add_argument("--cold-open", dest="cold_open", action="store_true",
                    help="cold-open hook: video ki shuruaat me hi script ki pehli "
                         "famous line ORIGINAL awaaz ke saath (5-8s, length dialogue "
                         "ke hisaab se), phir narration. Loudness auto-balanced.")
    mv.add_argument("--ken-burns", dest="ken_burns", action="store_true",
                    help="har still pe slow zoom/pan motion (default OFF = static "
                         "stills). Direction/distance har still pe alag.")
    mv.set_defaults(func=cmd_makevideo)

    go = sub.add_parser("gold", parents=[common],
                        help="ek build ko haath se label karke asli accuracy naapo")
    go.add_argument("--template",
                    help="is build ki manifest.json se labelling sheet banao")
    go.add_argument("--score",
                    help="bhari hui sheet (.csv) se asli accuracy nikaalo")
    go.add_argument("--out", default="gold.csv",
                    help="sheet kahan likhni hai (default gold.csv)")
    go.set_defaults(func=cmd_gold)

    se = sub.add_parser("see", parents=[common],
                        help="describe a picture, get the real frames back")
    se.add_argument("text", help='e.g. "a man in a red hazmat suit"')
    se.add_argument("--limit", type=int, default=5)
    se.add_argument("--out", default="proof", help="where to write the frames")
    se.set_defaults(func=cmd_see)

    q = sub.add_parser("preflight", parents=[common],
                       help="check a queue of jobs without building anything")
    q.add_argument("jobs", help="job file (JSON)")
    q.set_defaults(func=cmd_preflight)

    n = sub.add_parser("run", parents=[common],
                       help="pre-flight a queue, then build every job that passed")
    n.add_argument("jobs", help="job file (JSON)")
    n.add_argument("--dry-run", action="store_true",
                   help="pre-flight only, build nothing")
    n.add_argument("--strict", action="store_true",
                   help="build only jobs with no gaps at all")
    n.set_defaults(func=cmd_run)

    wb = sub.add_parser("web", parents=[common],
                        help="open the tool in a browser")
    wb.add_argument("--out", default="",
                    help="an output folder to open straight away")
    wb.add_argument("--port", type=int, default=0)
    wb.add_argument("--libraries", default="",
                    help="folder holding one library per title")
    wb.add_argument("--no-browser", action="store_true",
                    help="print the address instead of opening a window")
    wb.set_defaults(func=cmd_web)

    a = p.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
