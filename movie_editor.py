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
import sys
import threading

import webview                                        # pip install pywebview

import studio

HERE = os.path.dirname(os.path.abspath(__file__))
UI = os.path.join(HERE, "movie_editor_ui.html")


class Api:
    def __init__(self):
        self.window = None
        self._stop = False
        self._running = False

    # ---- native file / folder pickers ---------------------------------- #
    def pick_file(self, kind=""):
        types = {
            "clean": ("Script (*.txt;*.md)", "*.txt;*.md"),
            "clue": ("Clue (*.json;*.jsonl;*.txt)", "*.json;*.jsonl;*.txt"),
            "audio": ("Audio (*.wav;*.mp3;*.m4a)", "*.wav;*.mp3;*.m4a"),
            "text_file": ("Text instructions (*.txt)", "*.txt"),
        }.get(kind, ("All files (*.*)", "*.*"))
        res = self.window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False,
            file_types=(f"{types[0]}", "All files (*.*)"))
        return res[0] if res else ""

    def pick_folder(self):
        res = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        return res[0] if res else ""

    # ---- native file drag-and-drop ------------------------------------- #
    # pywebview 6 only puts the dropped file's real path on the event that
    # reaches a Python-side DOM 'drop' handler (as file['pywebviewFullPath']);
    # a plain JS drop listener never sees it. So the UI asks us to bind each
    # droppable row here, and we push the resolved path back into the page.
    def bind_drop(self, row_id):
        try:
            el = self.window.dom.get_element("#" + row_id)
            if el is None:
                return False
            el.on("drop", lambda e, rid=row_id: self._on_drop(e, rid))
            return True
        except Exception as exc:                                # noqa: BLE001
            self._log(f"[drop-bind err] {row_id}: {type(exc).__name__}: {exc}")
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
            self._log(f"[drop err] {type(exc).__name__}: {exc}")

    def copy(self, text):
        try:
            import tkinter as tk
            r = tk.Tk(); r.withdraw(); r.clipboard_clear()
            r.clipboard_append(text or ""); r.update(); r.destroy()
        except Exception:
            pass
        return True

    def stop(self):
        self._stop = True
        return True

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
                    text_file=s.get("text_file", ""), index=i))
            ok = 0
            for n, job in enumerate(jobs, 1):
                if self._stop:
                    self._log("\n■ stopped by you.")
                    break
                self._log(f"\n{'='*54}\nVIDEO {n}/{len(jobs)}\n{'='*54}")
                studio.build(job, log=self._log,
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
