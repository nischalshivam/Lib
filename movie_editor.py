"""Movie Editor — modern desktop UI for the clean+clue+audio -> finished video
pipeline. Same engine as the old Tkinter launcher (studio.build); only the face
is new. The window is a local HTML/CSS/JS view (pywebview); a tiny js_api bridges
the buttons to the Python backend, streaming the process log live to the UI.

The old studio_gui.py is kept as a fallback — nothing in the render pipeline
changed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import webview                                        # pip install pywebview

import studio

for _s in (sys.stdout, sys.stderr):      # UTF-8 console: never crash on a "→" path
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
UI = os.path.join(HERE, "movie_editor_ui.html")


class Api:
    def __init__(self):
        self.window = None
        self._stop = False
        self._running = False
        self._proc = None            # the child (makevideo/prostudio) now running
        self._drop_broken = False    # this webview can't do DOM drag-drop -> stop trying

    # ---- native file / folder pickers ---------------------------------- #
    def pick_file(self, kind=""):
        types = {
            "clean": ("Script (*.txt;*.md)", "*.txt;*.md"),
            "clue": ("Clue (*.json;*.jsonl;*.txt)", "*.json;*.jsonl;*.txt"),
            "audio": ("Audio (*.wav;*.mp3;*.m4a)", "*.wav;*.mp3;*.m4a"),
            "text_file": ("Text instructions (*.txt)", "*.txt"),
            "voice_ref": ("Voice sample (*.wav;*.mp3;*.m4a)", "*.wav;*.mp3;*.m4a"),
        }.get(kind, ("All files (*.*)", "*.*"))
        res = self.window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False,
            file_types=(f"{types[0]}", "All files (*.*)"))
        return res[0] if res else ""

    def pick_folder(self):
        res = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        return res[0] if res else ""

    def scan_folder(self, folder):
        """Look inside ONE video's folder and guess each input file: the clean
        (narration) script, the clue script, the voiceover audio, and the
        optional kinetic-text file. Returns {clean, clue, audio, text_file} of
        full paths (blank where nothing matched). Lets the user drop/choose a
        whole folder instead of adding four files one by one."""
        out = {"clean": "", "clue": "", "audio": "", "text_file": ""}
        try:
            if not folder or not os.path.isdir(folder):
                return out
            files = [os.path.join(folder, n) for n in sorted(os.listdir(folder))]
            files = [f for f in files if os.path.isfile(f)]
        except OSError:
            return out

        A_EXT = (".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus")

        def nm(f):
            return os.path.basename(f).lower()

        # voiceover: any audio file, preferring one named audio/voice/vo/narration
        auds = [f for f in files if nm(f).endswith(A_EXT)]
        if auds:
            auds.sort(key=lambda f: (0 if any(k in nm(f) for k in
                      ("audio", "voice", "vo", "narrat")) else 1, len(nm(f))))
            out["audio"] = auds[0]

        # clue: a .json/.jsonl, preferring one whose name says 'clue'
        js = [f for f in files if nm(f).endswith((".json", ".jsonl"))]
        if js:
            js.sort(key=lambda f: (0 if "clue" in nm(f) else 1, len(nm(f))))
            out["clue"] = js[0]

        # clean vs kinetic-text: both are .txt. The kinetic-text file is the
        # VText instruction file — spot it by name or by its header/markers;
        # whatever .txt is left (and isn't the clue) is the clean script.
        def is_textfile(f):
            n = nm(f)
            if any(k in n for k in ("text_instruction", "text-instruction",
                    "textinstruction", "vtext", "kinetic", "caption",
                    "onscreen", "on-screen", "_texts")):
                return True
            try:
                head = open(f, encoding="utf-8-sig", errors="ignore").read(600).upper()
            except OSError:
                return False
            return ("VTEXT INSTRUCTION" in head or "EVENT_TYPE" in head
                    or "DISPLAY_TEXT" in head or "NARRATION_CUE" in head)

        txts = [f for f in files if nm(f).endswith((".txt", ".md"))]
        tf = [f for f in txts if is_textfile(f)]
        if tf:
            out["text_file"] = tf[0]
        if not out["clue"]:                       # a .txt clue (rare) as a fallback
            clue_txt = [f for f in txts if "clue" in nm(f) and f not in tf]
            if clue_txt:
                out["clue"] = clue_txt[0]
        cleans = [f for f in txts if f not in tf and f != out["clue"]
                  and "clue" not in nm(f)]
        if cleans:
            cleans.sort(key=lambda f: (0 if any(k in nm(f) for k in
                        ("clean", "narrat", "script")) else 1, len(nm(f))))
            out["clean"] = cleans[0]
        # auto-detect the narration language so a single-folder drop also fills
        # the language (the bulk parent-folder path already does this)
        out["lang"] = self._detect_lang(out.get("clean"))
        return out

    # ---- movie library: where is the footage, and is all of it there? ---- #
    _lib_lock = threading.Lock()

    def check_library(self, clue):
        """Import-time check for one video: every show + episode its clue pins,
        across ALL library locations. {ready, summary}. Serialised, so twenty
        cards imported at once share one drive scan (cached ~2 min)."""
        try:
            from media_index import locator
            with Api._lib_lock:
                r = locator.locate(clue)
            return {"ready": bool(r.get("ready")), "summary": r.get("summary", "")}
        except Exception as exc:
            return {"ready": False, "summary": f"library check failed: {exc}"}

    def add_library_location(self):
        """User points at a folder of shows (or one show) the tool didn't find."""
        p = self.pick_folder()
        if not p:
            return {"roots": []}
        try:
            from media_index import locator
            return {"roots": locator.add_root(p)}
        except Exception as exc:
            return {"roots": [], "error": str(exc)}

    def scan_parent(self, folder):
        """Every immediate subfolder that holds one video's inputs, scanned.
        Returns [{name, folder, clean, clue, audio, text_file, lang}] so the UI
        can drop a PARENT folder of topic folders and get one auto-filled video
        card per topic — script, clue, audio, text and detected language all set.
        """
        out = []
        try:
            subs = [os.path.join(folder, n) for n in sorted(os.listdir(folder))
                    if os.path.isdir(os.path.join(folder, n))]
        except OSError:
            return out
        for sub in subs:
            r = self.scan_folder(sub)
            if r.get("clean") or r.get("clue"):        # a real topic folder
                r["name"] = os.path.basename(sub)
                r["folder"] = sub
                r["lang"] = self._detect_lang(r.get("clean"))
                out.append(r)
        return out

    @staticmethod
    def _detect_lang(clean_path):
        """Guess the narration language from the clean script — so the user never
        has to set it. Stop-word + accent heuristic over fr/es/de/it/pt/en;
        'auto' if unsure (the pipeline then detects at build time)."""
        if not clean_path:
            return "auto"
        try:
            t = open(clean_path, encoding="utf-8-sig", errors="ignore").read(4000).lower()
        except OSError:
            return "auto"
        import re as _re
        # Devanagari (Hindi written in its own script) — catch before word stats
        if sum(1 for ch in t if "ऀ" <= ch <= "ॿ") > 15:
            return "hi"
        words = _re.findall(r"[a-zà-ÿœñ¿¡ß]+", t)
        if not words:
            return "auto"
        stop = {
            "fr": "le la les de des du un une et que qui pour dans est ne pas plus avec sur au aux ce cette elle nous vous mais",
            "es": "el la los las de un una y que para con por en su se lo como mas pero este esta cuando porque no muy",
            "de": "der die das und ist ein eine zu mit fur den dem des nicht auch aber auf sich von im war sind",
            "it": "il la lo le di un una che per con non si come piu ma questo questa quando perche degli sono nel",
            "pt": "o a os as de um uma que para com por em seu se como mais mas este esta quando porque nao muito",
            "en": "the a an and of to in is for on with as at by this that it he she we you they not but are was",
            # romanized Hindi / Hinglish — tokens that are distinctively Hindi
            # and rare in English, so an English essay won't score on them
            "hi": "hai nahi nahin kya ka ki ke ko mein aur yeh yah woh wah kyun kyunki tha thi the karta karti karte "
                  "karna raha rahi rahe hota hoti hue liye jab tab phir bhi apne apna uska uske unka sirf lekin magar "
                  "kaise kuch bahut theek matlab samajh baat wala wali",
        }
        score = {k: sum(1 for w in words if w in set(v.split()))
                 for k, v in stop.items()}
        score["fr"] += sum(t.count(c) for c in "àâçéèêëîïôûù") // 20
        score["es"] += (t.count("ñ") + t.count("¿") + t.count("¡")) // 3
        score["de"] += (t.count("ß") + t.count("ä") + t.count("ö") + t.count("ü")) // 10
        best = max(score, key=score.get)
        return best if score[best] > 0 else "auto"

    # ---- native file drag-and-drop ------------------------------------- #
    # pywebview 6 only puts the dropped file's real path on the event that
    # reaches a Python-side DOM 'drop' handler (as file['pywebviewFullPath']);
    # a plain JS drop listener never sees it. So the UI asks us to bind each
    # droppable row here, and we push the resolved path back into the page.
    def bind_drop(self, row_id):
        # DOM drag-drop is DISABLED by default. On this pywebview 6 / WebView2
        # build, `window.dom.get_element(...).on(...)` makes pythonnet recurse
        # forever over a .NET object's properties (SyncRoot / Empty), and the
        # flood happens INSIDE pywebview before we can catch it — it fills the
        # console and freezes the window ("not responding") for a minute+ on
        # every launch. We never touch window.dom, so that code path never runs.
        # Browse and "Choose folder" (native dialogs, no DOM) do the same job —
        # folder auto-fill still works via its button. Re-enable drag-drop only
        # on a webview where it's fixed:  set  MOVIE_EDITOR_DND=1 .
        if os.environ.get("MOVIE_EDITOR_DND", "").strip().lower() not in (
                "1", "true", "yes", "on"):
            return False
        if self._drop_broken:
            return False
        try:
            el = self.window.dom.get_element("#" + row_id)
            if el is None:
                return False
            el.on("drop", lambda e, rid=row_id: self._on_drop(e, rid))
            return True
        except Exception:                                       # noqa: BLE001
            self._drop_broken = True
            return False

    def _on_drop(self, event, row_id):
        try:
            files = ((event or {}).get("dataTransfer", {}) or {}).get("files", []) or []
            if not files:
                return
            f0 = files[0]
            path = f0.get("pywebviewFullPath") or ""
            name = f0.get("name") or ""
            self.window.evaluate_js(
                f"applyDrop({json.dumps(row_id)},{json.dumps(path)},{json.dumps(name)})")
        except Exception as exc:                                # noqa: BLE001
            try:                                # never str(exc): a .NET error can
                self._log(f"[drop err] {type(exc).__name__}")  # recurse on SyncRoot
            except Exception:
                pass

    def copy(self, text):
        try:
            import tkinter as tk
            r = tk.Tk(); r.withdraw(); r.clipboard_clear()
            r.clipboard_append(text or ""); r.update(); r.destroy()
        except Exception:
            pass
        return True

    def prepare_clues(self):
        """Pick a folder of clean scripts -> grounded clue for each (agy Claude),
        in the background. Later builds of those scripts reuse the clue."""
        folder = self.pick_folder()
        if not folder or self._running:
            return False
        self._running, self._stop = True, False

        def go():
            try:
                self._log(f"\n=== Prepare clues: {folder}")
                proc = subprocess.Popen([sys.executable, "-u", "-m", "editv2", "clues", folder],
                                        cwd=os.path.dirname(os.path.abspath(__file__)),
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                        encoding="utf-8", errors="replace",
                                        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
                self._proc = proc
                for line in proc.stdout:
                    if line.strip() and "warn" not in line.lower():
                        self._log(line.rstrip())
                proc.wait()
                self._status("clues ready" if proc.returncode == 0 else "clues: error", "#3ecf8e")
            finally:
                self._proc, self._running = None, False
        threading.Thread(target=go, daemon=True).start()
        return True

    def stop(self):
        # Set the flag AND actually kill the running child. The flag alone only
        # lands BETWEEN stages (studio polls should_stop there), so a 40-80 min
        # makevideo/prostudio would ignore Stop until it finished — which is why
        # Stop "did nothing". Kill the whole process TREE so the ffmpeg workers
        # spawned underneath die with it. The half-done build folder is kept, so
        # the next Run resumes (makevideo reuses cut clips, prostudio the shots).
        self._stop = True
        try:                      # a voiceover killed mid-way must not keep the indexing workers off the GPU
            os.remove(r"D:\Lib\libjobs\gpu\reserved")
        except OSError:
            pass
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                   capture_output=True)
                else:
                    proc.terminate()
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        return True

    # ---- batch memory: resume after a crash / shutdown -------------------- #
    def _save_batch(self, specs, states):
        try:
            p = studio.BATCH_FILE
            old = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}
            if old.get("specs") != specs:
                old = {"specs": specs, "state": {}, "saved": time.strftime("%Y-%m-%d %H:%M")}
            old["state"].update({str(k): v for k, v in states.items()})
            os.makedirs(os.path.dirname(p), exist_ok=True)
            json.dump(old, open(p + ".tmp", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            os.replace(p + ".tmp", p)
        except Exception:
            pass

    def last_batch(self):
        """-> {pending, total, saved} of the last batch if it has unfinished videos, else {} (for the Resume button)."""
        try:
            b = json.load(open(studio.BATCH_FILE, encoding="utf-8"))
            left = [i for i in range(len(b["specs"])) if b["state"].get(str(i)) != "done"]
            return {"pending": len(left), "total": len(b["specs"]), "saved": b.get("saved", "")} if left else {}
        except Exception:
            return {}

    def resume_batch(self):
        """run the unfinished videos of the last batch again - every build folder continues from its last saved stage."""
        try:
            b = json.load(open(studio.BATCH_FILE, encoding="utf-8"))
        except Exception:
            return False
        left = [s for i, s in enumerate(b["specs"]) if b["state"].get(str(i)) != "done"]
        return self.run(json.dumps(left)) if left else False

    def _set_proc(self, proc):
        # studio.build hands us each child process (and None when it ends) so a
        # Stop click can reach in and kill whatever is running right now.
        self._proc = proc

    # ---- run the queue -------------------------------------------------- #
    def run(self, jobs_json):
        if self._running:
            return False
        self._running = True
        self._stop = False
        threading.Thread(target=self._run, args=(jobs_json,), daemon=True).start()
        return True

    def _log(self, *parts):
        line = " ".join(str(p) for p in parts)
        try:
            self.window.evaluate_js("appendLog(" + json.dumps(line) + ")")
        except Exception:
            print(line)

    def _status(self, text, color):
        try:
            self.window.evaluate_js(
                f"setStatus({json.dumps(text)},{json.dumps(color)})")
        except Exception:
            pass

    def _video_status(self, index, status):
        """Push one video's state (pending|building|done|failed) to its card so
        the UI can badge it and keep a live done/total count."""
        try:
            self.window.evaluate_js(
                f"setVideoStatus({int(index)},{json.dumps(status)})")
        except Exception:
            pass

    def _run(self, jobs_json):
        try:
            specs = json.loads(jobs_json)
            jobs = []
            for i, s in enumerate(specs):
                jobs.append(studio.Job(
                    clean=s.get("clean", ""), clue=s.get("clue", ""),
                    audio=s.get("audio", ""), save_dir=s.get("save_dir", ""),
                    fmt=s.get("fmt", "auto"), resolution=s.get("res", "1080p"),
                    language=s.get("lang", "en"),
                    text=bool(s.get("text")),
                    verify=bool(s.get("verify")),
                    verify_intro_min=int(s.get("verify_min", 0) or 0),
                    cold_open=bool(s.get("cold_open")),
                    intro_punch=bool(s.get("intro_punch")),
                    ken_burns=bool(s.get("ken_burns")),
                    frame=bool(s.get("frame")),
                    kinetic_text=bool(s.get("kinetic_text")),
                    text_file=s.get("text_file", ""),
                    auto_voice=bool(s.get("auto_voice")),
                    voice_ref=s.get("voice_ref", ""), index=i,
                    engine=s.get("engine", "classic"), look=s.get("look", "auto")))
            for j in range(len(jobs)):           # everything starts as pending
                self._video_status(j, "pending")
            # remember the batch: after a crash / shutdown / closed window the next start offers "Resume last batch"
            self._save_batch(specs, {})

            def on_status(job, st):
                if job is None and st == "green":
                    self._status("🟢 internet work done for all videos - the rest needs NO internet", "#3ecf8e")
                    return
                self._video_status(job.index, st)
                if st in ("done", "failed", "online"):
                    self._save_batch(specs, {job.index: st})
            # two phases (studio.build_batch): all internet work first (2 videos at a time, outages waited out),
            # green signal, then render / mix / QA offline. Each build folder resumes stage by stage.
            studio.build_batch(jobs, log=self._log, on_proc=self._set_proc, should_stop=lambda: self._stop,
                               on_status=on_status)
            ok = sum(1 for j in jobs if j.status == "done")
            if self._stop:
                self._log("\n■ stopped by you - Run again (or 'Resume last batch') continues where it stopped.")
            self._status(f"done — {ok}/{len(jobs)} ok" if not self._stop else "stopped", "#3ecf8e")
        except Exception as exc:
            self._log(f"\n! error: {type(exc).__name__}: {exc}")
            self._status("error", "#f25555")
        finally:
            self._running = False


def main():
    if not os.path.isfile(UI):
        print("UI file missing:", UI); sys.exit(1)
    api = Api()
    win = webview.create_window(
        "Movie Editor", UI, js_api=api, width=1020, height=800,
        min_size=(820, 620), background_color="#0f1115")
    api.window = win
    webview.start()


if __name__ == "__main__":
    main()
