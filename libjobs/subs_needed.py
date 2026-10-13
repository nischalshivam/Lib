"""Which videos still need an English subtitle file? A video is fine if it has a sidecar .srt (incl. ones we
installed), an embedded TEXT subtitle track in English, or a catalogue whose dialogue came from subtitles.
Non-English audio without English subs is flagged as critical (whisper cannot give good English dialogue)."""
import json, os, re, subprocess, sys, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from media_index import subtitles
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
SKIP_SUPPLIED = ("Andor", "Brooklyn Nine-Nine", "\\House\\", "Reacher", "Supernatural", "Two and a Half Men")
q = json.load(open(C.QUEUE, encoding="utf-8"))


def streams(v):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name:stream_tags=language",
                        "-of", "json", C.lp(v)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        return json.loads(r.stdout).get("streams", [])
    except Exception:
        return []


need = collections.defaultdict(list)
crit = collections.defaultdict(list)
for v in q:
    if any(k in v for k in SKIP_SUPPLIED):
        continue
    if subtitles.find_sidecar(v):
        continue
    st = streams(v)
    txt = [s for s in st if s.get("codec_type") == "subtitle" and s.get("codec_name") in ("subrip", "ass", "ssa", "mov_text", "webvtt", "text")]
    eng_sub = [s for s in txt if (s.get("tags") or {}).get("language", "eng").lower() in ("eng", "en", "english", "und")]
    if eng_sub:
        continue
    auds = [(s.get("tags") or {}).get("language", "und").lower() for s in st if s.get("codec_type") == "audio"]
    show = C.show_name(v)
    m = re.search(r"[Ss](\d{1,2})[Ee](\d{1,3})", os.path.basename(v))
    tag = f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}" if m else os.path.basename(v)[:60]
    if auds and not any(a in ("eng", "en", "english") for a in auds) and any(a not in ("und",) for a in auds):
        crit[show].append(f"{tag} (audio: {','.join(auds)})")
    else:
        need[show].append(tag)
out = {"critical_non_english": crit, "would_use_whisper": need}
json.dump(out, open(os.path.join(C.HERE, "subs_needed.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("CRITICAL (non-English audio, no English subs):")
for k, v in sorted(crit.items()):
    print(f"  {k}: {len(v)} -> {', '.join(v[:8])}{' ...' if len(v) > 8 else ''}")
print("No subtitles (whisper will be used, subtitles would be better):")
for k, v in sorted(need.items(), key=lambda x: -len(x[1])):
    print(f"  {k}: {len(v)}")
