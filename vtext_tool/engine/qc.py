"""QC report written next to every output video."""
from __future__ import annotations

import json


def write_report(path, header, events, plans, coverage, problems, info):
    by_num = {pl["num"]: pl for pl in plans}
    ev_rows = []
    for ev in events:
        row = {"event": ev["num"], "type": ev["EVENT_TYPE"],
               "cue": ev["NARRATION_CUE"]}
        pl = by_num.get(ev["num"])
        if pl:
            row.update(status="rendered", start=pl["t0"], end=pl["t1"],
                       zone=f"r{pl['zone'][0]}c{pl['zone'][1]}",
                       animation=pl["family"])
        elif ev["EVENT_TYPE"] == "BREATHING_MOMENT" and "t_start" in ev:
            row.update(status="breathing (kept clean)",
                       start=round(ev["t_start"], 2))
        else:
            row.update(status="skipped",
                       reason=ev.get("skip_reason", "unknown"))
        ev_rows.append(row)

    warnings = list(problems)
    if coverage < 0.5:
        warnings.append(
            f"Only {coverage:.0%} of the script matched the audio — check "
            "that the right script was uploaded for this video.")
    rendered = sum(1 for r in ev_rows if r["status"] == "rendered")
    report = {
        "video": info, "title": header.get("VIDEO_TITLE", ""),
        "niche": header.get("NICHE", ""),
        "script_audio_match": round(coverage, 3),
        "events_total": len(events), "events_rendered": rendered,
        "warnings": warnings, "events": ev_rows,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    return report
