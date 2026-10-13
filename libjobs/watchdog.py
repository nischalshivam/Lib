"""Watchdog (called by supervisor every 10 min). Catches what per-error rules miss:
  1. LOOP GUARD: any video started > 4 times in the last 6 h is parked as a failure, whatever the error.
  2. THROUGHPUT: if workers keep starting jobs but almost none finish in the last 2 h -> ALERT.
  3. Rates + ETA into status (returned text); ALERT also written to Desktop\\INDEXING ALERT.txt."""
import collections, datetime as dt, glob, json, os, re
import common as C

ALERT_FILE = os.path.join(os.path.expanduser("~"), "Desktop", "INDEXING ALERT.txt")
LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (.*)$")


def events(hours):
    since = dt.datetime.now() - dt.timedelta(hours=hours)
    out = []
    for f in glob.glob(os.path.join(C.LOGS, "worker_*.log")):
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                fh.seek(max(0, os.path.getsize(f) - 3_000_000))      # tail only
                for ln in fh:
                    m = LINE.match(ln.rstrip())
                    if m:
                        t = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                        if t >= since:
                            out.append((t, m.group(2)))
        except OSError:
            pass
    return out


def counted_starts(ev):
    """starts per video, minus the ones the worker itself declared 'not counted' (Drive restart, outage, quota, GPU
    busy): those retries are deliberate and must not make the loop guard fail a healthy file. Lines of one worker log
    are consecutive, so the outcome line belongs to the start just before it."""
    c, cur = collections.Counter(), None
    for t, e in ev:
        m = re.match(r"\[(index|refine|dialogue)\] (.*)$", e)
        if m:
            cur = m.group(2)
            c[cur] += 1
        elif cur and "not counted" in e:
            c[cur] -= 1
            cur = None
    return c


def run():
    ev6 = events(6)
    starts = counted_starts(ev6)
    parked = []
    def done(v):
        return C.is_done(v)
    for v, n in starts.items():
        if n > 4 and not done(v) and C.fails(v)["n"] < C.MAX_FAILS:
            since = C.reset_time(v)               # starts before a manual / file-replaced reset do not count
            if since and counted_starts([(t, e) for t, e in ev6 if t.timestamp() >= since]).get(v, 0) <= 4:
                continue
            for _ in range(C.MAX_FAILS - C.fails(v)["n"]):
                C.add_fail(v, f"watchdog: started {n}x in 6 h without finishing (loop)")
            parked.append(v)
    ev2 = [(t, e) for t, e in ev6 if t >= dt.datetime.now() - dt.timedelta(hours=2)]
    # only starts older than 45 min can be 'never finished' (one job takes ~20-25 min; right after a (re)start all 8
    # jobs are simply still running - that raised a false ALERT on 2026-10-10)
    st2 = counted_starts([(t, e) for t, e in ev2 if t <= dt.datetime.now() - dt.timedelta(minutes=45)])
    s2 = sum(n for v, n in st2.items() if not done(v))           # starts that never got finished
    ok2 = sum(1 for t, e in ev2 if e.strip().startswith("ok ("))
    ok6 = sum(1 for t, e in ev6 if e.strip().startswith("ok ("))
    alerts = []
    if parked:
        alerts.append(f"loop guard parked {len(parked)} video(s): " + "; ".join(os.path.basename(p) for p in parked[:5]))
    # stall: workers are wanted, there is work left, but NOTHING finished in the last 60 min
    hour_ago = dt.datetime.now() - dt.timedelta(hours=1)
    ok1 = sum(1 for t, e in ev6 if t >= hour_ago and e.strip().startswith("ok ("))
    pending = any(not C.is_done(v) for v in list(starts)[:50]) or bool(starts)
    workers_up = int(C.cfg().get("workers", 0)) > 0
    started_long_ago = any(t < hour_ago for t, e in ev6)          # don't alert in the first hour after a (re)start
    if workers_up and pending and ok1 == 0 and started_long_ago:
        alerts.append("nothing finished in the last 60 min (workers stuck or failing)")
    if s2 >= 6 and ok2 < 0.5 * s2:
        alerts.append(f"{s2} job starts in 2 h never finished (only {ok2} finished) - something is wrong")
    # ETA
    q = json.load(open(C.QUEUE, encoding="utf-8"))
    left = [v for v in q if not C.is_done(v) and not C.held(v) and C.fails(v)["n"] < C.MAX_FAILS]
    rate = max(ok6 / 6.0, ok2 / 2.0)               # right after a (re)start the 6-h window understates the speed
    eta = f"{len(left) / rate:.0f} h (~{len(left) / rate / 24:.1f} days)" if rate > 0 else "unknown (nothing finished in 6 h)"
    text = (f"speed: {ok2} finished in last 2 h, {ok6} in last 6 h ({rate:.1f}/h)\n"
            f"left (not held, not failed; includes shows waiting for Cast): {len(left)} -> ETA at this speed {eta}\n")
    if not alerts and os.path.exists(ALERT_FILE):      # problem gone: no stale alert left on the Desktop
        try:
            os.remove(ALERT_FILE)
        except OSError:
            pass
    if alerts:
        text = "!!! ALERT: " + " | ".join(alerts) + "\n" + text
        try:
            with open(ALERT_FILE, "w", encoding="utf-8") as f:
                f.write(dt.datetime.now().strftime("%Y-%m-%d %H:%M  ") + "\n".join(alerts) +
                        "\n\nIndexing needs a look. Details: D:\\Lib\\libjobs\\status.txt\n")
        except OSError:
            pass
    return text
