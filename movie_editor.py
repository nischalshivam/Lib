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
        return out

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

    def stop(self):
        # Set the flag AND actually kill the running child. The flag alone only
        # lands BETWEEN stages (studio polls should_stop there), so a 40-80 min
        # makevideo/prostudio would ignore Stop until it finished — which is why
        # Stop "did nothing". Kill the whole process TREE so the ffmpeg workers
        # spawned underneath die with it. The half-done build folder is kept, so
        # the next Run resumes (makevideo reuses cut clips, prostudio the shots).
        self._stop = True
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
                    voice_ref=s.get("voice_ref", ""), index=i))
            ok = 0
            for n, job in enumerate(jobs, 1):
                if self._stop:
                    self._log("\n■ stopped by you.")
                    break
                self._log(f"\n{'='*54}\nVIDEO {n}/{len(jobs)}\n{'='*54}")
                studio.build(job, log=self._log, on_proc=self._set_proc,
                             should_stop=lambda: self._stop)
                if job.status == "done":
                    ok += 1
                    self._log(f"\n  ✓ {job.message}")
                else:
                    self._log(f"\n  ! {job.status}: {job.message}")
            self._status(f"done — {ok}/{len(jobs)} ok" if not self._stop
                         else "stopped", "#3ecf8e")
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
