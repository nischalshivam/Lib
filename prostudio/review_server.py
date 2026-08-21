#!/usr/bin/env python3
"""Browser review + edit page for a ProStudio job.

Flow:
  1. plan the job (fast) and render a small DRAFT proxy
  2. open http://localhost:PORT in the browser: a storyboard of every shot,
     each with the narration spoken under it + the draft video to watch
  3. the user fixes what doesn't match: Replace a clip with their own file,
     Trim, Delete, Reorder, Move to another scene — Rebuild the draft to
     re-watch
  4. "Export final" renders the full-quality MP4 from the edited plan

Pure stdlib (http.server) — nothing to install. One job at a time.
"""
from __future__ import annotations

import html
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from engine import project as P            # noqa: E402
from engine.audio_sync import duration     # noqa: E402
from prostudio import Job, plan_job, render_from_plan  # noqa: E402


class Review:
    def __init__(self, job: Job, job_index=0):
        self.job = job
        self.logbuf = []
        self.progress = {"stage": "planning", "pct": 0, "done": False,
                         "output": "", "busy": True}
        plan = plan_job(job, job_index, log=self._log)
        self.shots = plan["shots"]
        self.events = plan["events"]
        self.scenes = plan["scenes"]
        self.windows = plan["windows"]
        self.words = plan["words"]
        self.audio_dur = duration(job.audio)
        self.workdir = os.path.join(os.path.dirname(os.path.abspath(job.out_path))
                                    or ".", ".prostudio_review")
        os.makedirs(self.workdir, exist_ok=True)
        self.media_dir = os.path.join(self.workdir, "user_media")
        os.makedirs(self.media_dir, exist_ok=True)
        self.thumb_dir = os.path.join(self.workdir, "thumbs")
        os.makedirs(self.thumb_dir, exist_ok=True)
        self.proxy_path = os.path.splitext(job.out_path)[0] + "_proxy.mp4"
        self.lock = threading.Lock()
        self.progress.update(stage="idle", busy=False)

    # ---- logging / progress ------------------------------------------------
    def _log(self, msg=""):
        self.logbuf.append(str(msg))
        m = re.match(r"\[\s*(\d+)%\]", str(msg))
        if m:
            self.progress["pct"] = int(m.group(1))
            self.progress["stage"] = str(msg).strip()[:80]
        print(msg)

    # ---- narration under each shot ----------------------------------------
    def narration_list(self):
        return P.narration_by_shot(self.shots, self.words, self.scenes,
                                   self.windows)

    # ---- timing: keep every scene's shots tiling its audio window ----------
    def reflow(self, si):
        w0, w1 = self.windows[si]
        sc = [sh for sh in self.shots if sh.scene_i == si]
        if not sc:
            return
        weights = [max(0.5, sh.secs) for sh in sc]
        tot = sum(weights)
        span = max(0.4, w1 - w0)
        t = w0
        for k, sh in enumerate(sc):
            d = (w1 - t) if k == len(sc) - 1 else span * weights[k] / tot
            sh.t0, sh.t1 = t, t + d
            t += d

    # ---- edit operations ---------------------------------------------------
    def replace_media(self, i, path, kind=None):
        sh = self.shots[i]
        sh.path = path
        sh.src_in = 0.0                          # reset in-point for new media
        if kind:
            sh.kind = kind
        else:
            sh.kind = "video" if path.lower().endswith(
                (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v")) else "image"

    def media_duration(self, i):
        return duration(self.shots[i].path)

    def set_in(self, i, src_in):
        """Pick which part of a longer source clip to use (the in-point). The
        shot keeps its slot length; we just start the source later."""
        sh = self.shots[i]
        d = duration(sh.path)
        slot = sh.secs
        sh.src_in = max(0.0, min(float(src_in), max(0.0, d - slot)))

    def delete_shot(self, i):
        si = self.shots[i].scene_i
        # never leave a scene with zero shots (that would desync audio)
        if sum(1 for s in self.shots if s.scene_i == si) <= 1:
            return False
        del self.shots[i]
        self.reflow(si)
        return True

    def move_shot(self, i, direction):
        sc_idx = [k for k, s in enumerate(self.shots)
                  if s.scene_i == self.shots[i].scene_i]
        pos = sc_idx.index(i)
        j = pos + direction
        if 0 <= j < len(sc_idx):
            a, b = i, sc_idx[j]
            self.shots[a], self.shots[b] = self.shots[b], self.shots[a]
            self.reflow(self.shots[a].scene_i)
            return True
        return False

    def trim_shot(self, i, delta):
        sh = self.shots[i]
        si = sh.scene_i
        sc = [s for s in self.shots if s.scene_i == si]
        if len(sc) < 2:
            return False                      # lone shot must fill the window
        newsecs = max(1.0, sh.secs + delta)
        # bump this shot's size (its weight), then reflow re-tiles the scene to
        # keep the audio window exact — neighbours absorb the difference
        sh.t1 = sh.t0 + newsecs
        self.reflow(si)
        return True

    # ---- renders (background) ---------------------------------------------
    def _run_async(self, fn, label):
        def worker():
            self.progress.update(stage=label, pct=0, done=False, busy=True,
                                 output="")
            try:
                rep = fn()
                self.progress.update(done=True, busy=False, pct=100,
                                     output=rep.get("output", ""),
                                     stage="done")
            except Exception as exc:
                self.progress.update(done=True, busy=False,
                                     stage=f"ERROR: {exc}")
        threading.Thread(target=worker, daemon=True).start()

    def build_proxy(self):
        self._run_async(
            lambda: render_from_plan(self.job, self.shots, self.events,
                                     self._log, proxy=True),
            "building draft")

    def export_final(self):
        self._run_async(
            lambda: render_from_plan(self.job, self.shots, self.events,
                                     self._log, proxy=False),
            "exporting final")

    # ---- thumbnails --------------------------------------------------------
    def thumb(self, i):
        out = os.path.join(self.thumb_dir, f"{i}.jpg")
        sh = self.shots[i]
        src = sh.path
        sig = f"{src}:{os.path.getmtime(src) if os.path.isfile(src) else 0}"
        sigf = out + ".sig"
        if os.path.isfile(out) and os.path.isfile(sigf) \
                and open(sigf).read() == sig:
            return out
        seek = []
        if sh.kind == "video":
            d = duration(src)
            if d > 0.3:
                seek = ["-ss", f"{d * 0.5:.2f}"]
        try:
            subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", *seek,
                            "-i", src, "-frames:v", "1",
                            "-vf", "scale=320:180:force_original_aspect_ratio="
                            "increase,crop=320:180", out],
                           capture_output=True, timeout=30)
            open(sigf, "w").write(sig)
        except Exception:
            return None
        return out if os.path.isfile(out) else None

    # ---- project JSON for the page ----------------------------------------
    def project_json(self):
        narr = self.narration_list()
        shots = []
        for i, sh in enumerate(self.shots):
            shots.append({
                "i": i, "scene": sh.scene_i + 1, "kind": sh.kind,
                "t0": round(sh.t0, 2), "t1": round(sh.t1, 2),
                "secs": round(sh.secs, 2),
                "src_in": round(getattr(sh, "src_in", 0.0), 2),
                "name": os.path.basename(sh.path),
                "narration": narr[i] if i < len(narr) else "",
            })
        return {
            "name": os.path.basename(self.job.out_path),
            "format": self.job.format_key, "resolution": self.job.resolution,
            "text_on": self.job.text, "audio_dur": round(self.audio_dur, 1),
            "n_shots": len(self.shots), "n_scenes": len(self.scenes),
            "proxy_ready": os.path.isfile(self.proxy_path),
            "shots": shots,
        }


# ============================ HTTP layer ===================================

STATE: Review | None = None


def _fmt_time(s):
    return f"{int(s)//60}:{int(s)%60:02d}"


PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ProStudio — Review & Edit</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--line:#2a3240;--fg:#e6edf3;--mut:#8b98a9;
 --acc:#3b82f6;--good:#22c55e;--warn:#f59e0b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
 font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif}
header{position:sticky;top:0;z-index:5;background:#0d1117ee;backdrop-filter:blur(6px);
 border-bottom:1px solid var(--line);padding:10px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
h1{font-size:15px;margin:0;font-weight:650}
.pill{color:var(--mut);font-size:12px;border:1px solid var(--line);border-radius:20px;padding:2px 10px}
.grow{flex:1}
button{background:var(--card);color:var(--fg);border:1px solid var(--line);
 border-radius:8px;padding:8px 12px;cursor:pointer;font-size:13px}
button:hover{border-color:var(--acc)}
button.primary{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}
button.go{background:var(--good);border-color:var(--good);color:#04210f;font-weight:700}
button:disabled{opacity:.5;cursor:not-allowed}
main{max-width:1100px;margin:0 auto;padding:16px}
video{width:100%;border-radius:12px;background:#000;border:1px solid var(--line)}
.bar{height:8px;background:var(--card);border-radius:6px;overflow:hidden;border:1px solid var(--line)}
.bar>i{display:block;height:100%;background:var(--acc);width:0}
.status{color:var(--mut);font-size:12px;margin:6px 2px}
.card{display:grid;grid-template-columns:180px 1fr auto;gap:14px;background:var(--card);
 border:1px solid var(--line);border-radius:12px;padding:12px;margin:10px 0;align-items:center}
.card img{width:180px;height:101px;object-fit:cover;border-radius:8px;background:#000}
.meta{min-width:0}
.tag{font-size:11px;color:var(--mut)}
.tag b{color:var(--fg)}
.narr{margin-top:4px;font-size:14px}
.name{font-size:11px;color:var(--mut);margin-top:4px;word-break:break-all}
.ops{display:flex;flex-direction:column;gap:6px;align-items:stretch}
.ops .row{display:flex;gap:6px}
.ops button{padding:6px 8px;font-size:12px}
.scenehdr{margin:18px 2px 2px;color:var(--warn);font-weight:600;font-size:13px}
label.rep{background:var(--card);border:1px solid var(--line);border-radius:8px;
 padding:6px 8px;cursor:pointer;font-size:12px;text-align:center}
label.rep:hover{border-color:var(--acc)}
input[type=file]{display:none}
.hint{color:var(--mut);font-size:12px;margin:2px}
</style></head><body>
<header>
 <h1>ProStudio · Review & Edit</h1>
 <span class="pill" id="pmeta">…</span>
 <span class="grow"></span>
 <button id="rebuild" onclick="rebuild()">↻ Rebuild draft</button>
 <button id="export" class="go" onclick="exportFinal()">✔ Export final</button>
</header>
<main>
 <video id="vid" controls preload="metadata"></video>
 <div class="status" id="status">idle</div>
 <div class="bar"><i id="prog"></i></div>
 <p class="hint">Watch the draft, then fix any shot whose clip doesn't match the
  narration: <b>Replace</b> with your own file, <b>Trim</b>, <b>Delete</b>, or
  reorder. Click <b>Rebuild draft</b> to re-watch, and <b>Export final</b> when happy.</p>
 <div id="list"></div>
</main>
<script>
let P=null, busy=false;
function api(u,o){return fetch(u,o).then(r=>r.json())}
function esc(s){let d=document.createElement('div');d.textContent=s;return d.innerHTML}
function load(){api('/api/project').then(p=>{P=p;render()})}
function render(){
 document.getElementById('pmeta').textContent =
   `${P.name} · ${P.format} · ${P.resolution} · ${P.n_shots} shots · ${P.n_scenes} scenes · ${P.audio_dur}s`;
 let v=document.getElementById('vid');
 if(P.proxy_ready && !v.src){v.src='/proxy.mp4?t='+Date.now()}
 let h='',lastScene=0;
 for(const s of P.shots){
  if(s.scene!==lastScene){h+=`<div class="scenehdr">Scene ${s.scene}</div>`;lastScene=s.scene}
  h+=`<div class="card">
    <img src="/thumb/${s.i}?t=${Date.now()}" loading="lazy">
    <div class="meta">
     <div class="tag"><b>${fmt(s.t0)}–${fmt(s.t1)}</b> · ${s.secs}s · ${s.kind}</div>
     <div class="narr">${esc(s.narration)||'<span class=tag>(no narration here)</span>'}</div>
     <div class="name">${esc(s.name)}</div>
    </div>
    <div class="ops">
     <label class="rep">Replace…<input type="file" accept="video/*,image/*"
        onchange="replaceShot(${s.i},this)"></label>
     ${s.kind==='video'?`<button onclick="openTrim(${s.i})">✂ Pick best part</button>`:''}
     <div class="row">
       <button onclick="edit('trim',${s.i},-0.5)">−0.5s</button>
       <button onclick="edit('trim',${s.i},0.5)">+0.5s</button>
     </div>
     <div class="row">
       <button onclick="edit('up',${s.i})">↑</button>
       <button onclick="edit('down',${s.i})">↓</button>
       <button onclick="edit('delete',${s.i})">🗑</button>
     </div>
    </div></div>`;
 }
 document.getElementById('list').innerHTML=h;
}
function fmt(s){s=Math.round(s);return Math.floor(s/60)+':'+String(s%60).padStart(2,'0')}
function edit(action,i,val){
 if(busy)return;
 api('/api/edit',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({action,shot:i,value:val})}).then(load)
}
function replaceShot(i,inp){
 if(!inp.files.length||busy)return;
 let f=inp.files[0];
 fetch('/api/replace?shot='+i+'&name='+encodeURIComponent(f.name),
   {method:'POST',body:f}).then(r=>r.json()).then(load)
}
function poll(){
 api('/api/progress').then(p=>{
  busy=p.busy;
  document.getElementById('status').textContent=p.stage;
  document.getElementById('prog').style.width=(p.pct||0)+'%';
  document.getElementById('rebuild').disabled=p.busy;
  document.getElementById('export').disabled=p.busy;
  if(!p.busy && p._wasBusy){ // finished
    let v=document.getElementById('vid');v.src='/proxy.mp4?t='+Date.now();load();
  }
  p._wasBusy=p.busy;
 })
}
function rebuild(){api('/api/rebuild',{method:'POST'}).then(()=>{})}
function exportFinal(){if(confirm('Export the full-quality final video now? This can take a while.'))
  api('/api/export',{method:'POST'}).then(()=>{})}

// ---- in-point trimmer: pick which N seconds of a longer clip to use --------
let TRIM={i:-1,dur:0,slot:0};
function openTrim(i){
 api('/api/media_info?shot='+i).then(m=>{
  TRIM={i:i,dur:m.duration,slot:m.slot};
  let mv=document.getElementById('mvid');
  mv.src='/media/'+i+'?t='+Date.now();
  let sl=document.getElementById('mslider');
  let maxIn=Math.max(0,m.duration-m.slot);
  sl.min=0;sl.max=maxIn.toFixed(2);sl.step=0.1;sl.value=Math.min(m.src_in,maxIn);
  document.getElementById('mslot').textContent=m.slot.toFixed(1);
  document.getElementById('mdur').textContent=m.duration.toFixed(1);
  if(m.duration<=m.slot+0.05){
    document.getElementById('mnote').textContent=
      'This clip ('+m.duration.toFixed(1)+'s) is not longer than the '+m.slot.toFixed(1)+'s slot — the whole clip is used.';
    sl.disabled=true;
  } else { sl.disabled=false; document.getElementById('mnote').textContent=''; }
  updTrim();
  document.getElementById('modal').style.display='flex';
 });
}
function updTrim(){
 let sl=document.getElementById('mslider'), inp=parseFloat(sl.value)||0;
 document.getElementById('min').textContent=inp.toFixed(1);
 document.getElementById('mout').textContent=(inp+TRIM.slot).toFixed(1);
 let mv=document.getElementById('mvid');
 if(Math.abs(mv.currentTime-inp)>0.15){try{mv.currentTime=inp}catch(e){}}
}
function playSeg(){let mv=document.getElementById('mvid');mv.currentTime=parseFloat(document.getElementById('mslider').value)||0;mv.play();
 clearTimeout(window._segT);window._segT=setTimeout(()=>mv.pause(),TRIM.slot*1000);}
function closeTrim(){document.getElementById('modal').style.display='none';
 let mv=document.getElementById('mvid');mv.pause();mv.src='';}
function confirmTrim(){
 let inp=parseFloat(document.getElementById('mslider').value)||0;
 api('/api/edit',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({action:'setin',shot:TRIM.i,value:inp})}).then(()=>{
   closeTrim();load();alert('Best part set. Click "Rebuild draft" to see it in the video.');
 });
}
load();setInterval(poll,1000);
</script>
<div id="modal" style="display:none;position:fixed;inset:0;background:#000b;
  z-index:20;align-items:center;justify-content:center" onclick="if(event.target===this)closeTrim()">
 <div style="background:var(--card);border:1px solid var(--line);border-radius:14px;
   padding:16px;max-width:640px;width:92%">
  <h3 style="margin:0 0 4px">Pick the best part of this clip</h3>
  <p class="hint" id="mnote"></p>
  <video id="mvid" style="width:100%;border-radius:10px;background:#000" muted playsinline></video>
  <p class="hint">Clip length <b id="mdur">0</b>s · this slot needs <b id="mslot">0</b>s.
    Drag to choose the start; the tool keeps <b id="mslot2"></b> the slot length and
    uses <b><span id="min">0</span>s → <span id="mout">0</span>s</b>.</p>
  <input type="range" id="mslider" style="width:100%" oninput="updTrim()">
  <div style="display:flex;gap:8px;margin-top:12px">
   <button onclick="playSeg()">▶ Preview selection</button>
   <span class="grow"></span>
   <button onclick="closeTrim()">Cancel</button>
   <button class="go" onclick="confirmTrim()">Use this part</button>
  </div>
 </div>
</div>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj))

    def _file(self, path, ctype=None):
        if not path or not os.path.isfile(path):
            self._send(404, b"not found", "text/plain")
            return
        ctype = ctype or mimetypes.guess_type(path)[0] or "application/octet-stream"
        size = os.path.getsize(path)
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start = int(a) if a else 0
            end = int(b) if b else size - 1
            end = min(end, size - 1)
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(length))
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                self.wfile.write(f.read(length))
        else:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with open(path, "rb") as f:
                self.wfile.write(f.read())

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        if p == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif p == "/api/project":
            self._json(STATE.project_json())
        elif p == "/api/progress":
            self._json(STATE.progress)
        elif p == "/proxy.mp4":
            self._file(STATE.proxy_path, "video/mp4")
        elif p == "/api/media_info":
            i = int(parse_qs(u.query).get("shot", [-1])[0])
            sh = STATE.shots[i]
            self._json({"duration": round(STATE.media_duration(i), 2),
                        "src_in": round(getattr(sh, "src_in", 0.0), 2),
                        "slot": round(sh.secs, 2), "kind": sh.kind})
        elif p.startswith("/media/"):
            try:
                i = int(p.split("/")[2].split("?")[0])
            except ValueError:
                self._send(404, b"", "text/plain"); return
            self._file(STATE.shots[i].path)
        elif p.startswith("/thumb/"):
            try:
                i = int(p.split("/")[2].split("?")[0])
            except ValueError:
                self._send(404, b"", "text/plain"); return
            self._file(STATE.thumb(i), "image/jpeg")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        q = parse_qs(u.query)
        if p == "/api/edit":
            n = int(self.headers.get("Content-Length", 0))
            d = json.loads(self.rfile.read(n) or b"{}")
            act, i = d.get("action"), int(d.get("shot", -1))
            with STATE.lock:
                if act == "delete":
                    STATE.delete_shot(i)
                elif act == "up":
                    STATE.move_shot(i, -1)
                elif act == "down":
                    STATE.move_shot(i, +1)
                elif act == "trim":
                    STATE.trim_shot(i, float(d.get("value", 0)))
                elif act == "setin":
                    STATE.set_in(i, float(d.get("value", 0)))
            self._json({"ok": True})
        elif p == "/api/replace":
            i = int(q.get("shot", [-1])[0])
            name = q.get("name", ["upload"])[0]
            n = int(self.headers.get("Content-Length", 0))
            data = self.rfile.read(n)
            safe = re.sub(r"[^\w.\-]", "_", os.path.basename(name)) or "upload"
            dest = os.path.join(STATE.media_dir, f"{i}_{int(time.time())}_{safe}")
            with open(dest, "wb") as f:
                f.write(data)
            with STATE.lock:
                STATE.replace_media(i, dest)
            self._json({"ok": True, "path": dest})
        elif p == "/api/rebuild":
            STATE.build_proxy()
            self._json({"ok": True})
        elif p == "/api/export":
            STATE.export_final()
            self._json({"ok": True})
        else:
            self._send(404, b"not found", "text/plain")


def serve(job: Job, job_index=0, port=8750, open_browser=True, log=print):
    global STATE
    log("Planning + preparing review ...")
    STATE = Review(job, job_index)
    log("Building the first draft proxy (fast) ...")
    STATE.build_proxy()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    log(f"Review page: {url}  (Ctrl+C to stop)")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", help="jobs.json (first job is reviewed)")
    ap.add_argument("--scenes"); ap.add_argument("--audio")
    ap.add_argument("--out", default="ProStudio.mp4")
    ap.add_argument("--script", default=""); ap.add_argument("--instructor", default="")
    ap.add_argument("--format", default="auto"); ap.add_argument("--language", default="en")
    ap.add_argument("--niche", default="Movie Essay")
    ap.add_argument("--resolution", default="4K")
    ap.add_argument("--text", action="store_true")
    ap.add_argument("--no-text", action="store_true")
    ap.add_argument("--no-keyword-colors", action="store_true")
    ap.add_argument("--whisper-model", default="base")
    ap.add_argument("--port", type=int, default=8750)
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()
    if a.queue:
        d = json.load(open(a.queue, encoding="utf-8"))
        j = d["jobs"][0]
        job = Job(scenes_dir=j["scenes"], audio=j["audio"], out_path=j["out"],
                  script=j.get("script", ""), instructor=j.get("instructor", ""),
                  format_choice=j.get("format", "auto"),
                  language=j.get("language", "en"),
                  niche=j.get("niche", "Movie Essay"),
                  keyword_colors=j.get("keyword_colors", True),
                  text=j.get("text", False),
                  resolution=j.get("resolution", d.get("resolution", "4K")),
                  whisper_model=j.get("whisper_model", "base"))
    else:
        job = Job(scenes_dir=a.scenes, audio=a.audio, out_path=a.out,
                  script=a.script, instructor=a.instructor,
                  format_choice=a.format, language=a.language, niche=a.niche,
                  keyword_colors=not a.no_keyword_colors,
                  text=a.text and not a.no_text,
                  resolution=a.resolution, whisper_model=a.whisper_model)
    serve(job, port=a.port, open_browser=not a.no_open)
