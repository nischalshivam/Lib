#!/usr/bin/env python3
"""ProStudio Launcher — the window.

One card per video: pick Clean script, Clue script, Voiceover audio, a couple of
options. "+ Add Video" for a batch (20+), "Run" processes them one at a time in
a background thread while a live log streams every stage. The pipeline itself
lives in studio.py (build/run_queue) — this file is only the window, so the same
pipeline runs head-less overnight too.
"""
from __future__ import annotations

import os
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, ttk

import studio

# ---- palette (matches prostudio's GUI) ------------------------------------
BG, PANEL, FIELD = "#0f1420", "#171d2b", "#1f2838"
LINE, FG, MUT = "#2a3446", "#e7edf6", "#94a3b8"
ACCENT, GO, WARN, BAD = "#4f8cff", "#22c55e", "#f4b740", "#ef4444"

RES = ["1080p", "4K"]
FMTS = ["auto"] + studio._FORMATS + ["No Filter"]   # No Filter = raw clips only (long videos)


class Card(ttk.Frame):
    """One video's three inputs + options."""
    n = 0

    def __init__(self, master, on_remove):
        super().__init__(master, style="Card.TFrame", padding=12)
        Card.n += 1
        self.idx = Card.n
        self._on_remove = on_remove
        self.clean = tk.StringVar(); self.clue = tk.StringVar()
        self.audio = tk.StringVar(); self.save_dir = tk.StringVar()
        self.fmt = tk.StringVar(value="auto")
        self.res = tk.StringVar(value="1080p")
        self.text = tk.BooleanVar(value=False)
        self.verify = tk.BooleanVar(value=False)   # OFF = free (library is accurate)
        self.status = tk.StringVar(value="ready")
        self._build()

    def _row(self, r, label, var, kinds):
        ttk.Label(self, text=label, style="Mut.TLabel").grid(
            row=r, column=0, sticky="w", pady=3)
        e = ttk.Entry(self, textvariable=var, width=52)
        e.grid(row=r, column=1, sticky="we", padx=6)
        ttk.Button(self, text="Browse", style="Ghost.TButton",
                   command=lambda: self._pick(var, kinds)).grid(row=r, column=2)

    def _pick(self, var, kinds):
        p = filedialog.askopenfilename(filetypes=kinds)
        if p:
            var.set(p)

    def _dir_row(self, r, label, var):
        ttk.Label(self, text=label, style="Mut.TLabel").grid(
            row=r, column=0, sticky="w", pady=3)
        ttk.Entry(self, textvariable=var, width=52).grid(
            row=r, column=1, sticky="we", padx=6)

        def pick():
            d = filedialog.askdirectory(title="Where should the finished video be saved?")
            if d:
                var.set(d)
        ttk.Button(self, text="Choose folder", style="Ghost.TButton",
                   command=pick).grid(row=r, column=2)

    def _build(self):
        self.columnconfigure(1, weight=1)
        hdr = ttk.Label(self, text=f"Video {self.idx}", style="H.TLabel")
        hdr.grid(row=0, column=0, sticky="w", columnspan=2)
        ttk.Button(self, text="✕ Remove", style="Ghost.TButton",
                   command=lambda: self._on_remove(self)).grid(row=0, column=2, sticky="e")
        self._row(1, "Clean script (.txt)", self.clean,
                  [("Script", "*.txt *.md"), ("All", "*.*")])
        self._row(2, "Clue script (.json)", self.clue,
                  [("Clue", "*.json *.jsonl *.txt"), ("All", "*.*")])
        self._row(3, "Voiceover audio", self.audio,
                  [("Audio", "*.wav *.mp3 *.m4a"), ("All", "*.*")])
        self._dir_row(4, "Save finished video to", self.save_dir)

        opt = ttk.Frame(self, style="Card.TFrame")
        opt.grid(row=5, column=0, columnspan=3, sticky="we", pady=(8, 0))
        ttk.Label(opt, text="Format", style="Mut.TLabel").pack(side="left")
        ttk.Combobox(opt, textvariable=self.fmt, values=FMTS, width=14,
                     state="readonly").pack(side="left", padx=(4, 14))
        ttk.Label(opt, text="Quality", style="Mut.TLabel").pack(side="left")
        ttk.Combobox(opt, textvariable=self.res, values=RES, width=7,
                     state="readonly").pack(side="left", padx=(4, 14))
        ttk.Checkbutton(opt, text="On-screen text", variable=self.text).pack(side="left")
        ttk.Checkbutton(opt, text="Verify clips (Gemini · costs API)",
                        variable=self.verify).pack(side="left", padx=(14, 0))
        ttk.Label(opt, textvariable=self.status, style="Mut.TLabel").pack(side="right")

    def to_job(self, index) -> studio.Job:
        return studio.Job(clean=self.clean.get().strip(), clue=self.clue.get().strip(),
                          audio=self.audio.get().strip(),
                          save_dir=self.save_dir.get().strip(),
                          fmt=self.fmt.get(), resolution=self.res.get(),
                          text=self.text.get(), verify=self.verify.get(), index=index)

    def set_status(self, s):
        self.status.set(s)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ProStudio Launcher — Clean + Clue + Audio → finished video")
        self.geometry("980x760")
        self.configure(bg=BG)
        self._theme()
        self.cards: list[Card] = []
        self.log_q: queue.Queue = queue.Queue()
        self.running = False
        self.stopping = False
        self.current_proc = None          # the child (makevideo/prostudio) now running
        self._layout()
        self.add_card()
        self.after(80, self._drain_log)

    # ---- styling ----
    def _theme(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=BG, foreground=FG, fieldbackground=FIELD,
                     bordercolor=LINE, font=("Segoe UI", 10))
        st.configure("Card.TFrame", background=PANEL, relief="flat")
        st.configure("TFrame", background=BG)
        st.configure("TLabel", background=PANEL, foreground=FG)
        st.configure("Mut.TLabel", background=PANEL, foreground=MUT)
        st.configure("H.TLabel", background=PANEL, foreground=FG,
                     font=("Segoe UI Semibold", 13))
        st.configure("Title.TLabel", background=BG, foreground=FG,
                     font=("Segoe UI Semibold", 16))
        st.configure("TEntry", fieldbackground=FIELD, foreground=FG)
        st.configure("TCombobox", fieldbackground=FIELD, foreground=FG)
        st.configure("TCheckbutton", background=PANEL, foreground=FG)
        st.configure("Ghost.TButton", background=FIELD, foreground=FG, borderwidth=0)
        st.configure("Go.TButton", background=GO, foreground="#04210f",
                     font=("Segoe UI Semibold", 11), borderwidth=0)
        st.configure("Add.TButton", background=ACCENT, foreground="white", borderwidth=0)
        st.configure("Stop.TButton", background=BAD, foreground="white",
                     font=("Segoe UI Semibold", 11), borderwidth=0)
        st.map("Go.TButton", background=[("active", "#16a34a")])
        st.map("Add.TButton", background=[("active", "#3b76e0")])
        st.map("Stop.TButton", background=[("active", "#c0392b")])

    def _layout(self):
        top = ttk.Frame(self); top.pack(fill="x", padx=16, pady=(14, 6))
        ttk.Label(top, text="ProStudio Launcher", style="Title.TLabel").pack(side="left")
        ttk.Label(top, text="  library found by SSD label — drive letter can change freely",
                  background=BG, foreground=MUT).pack(side="left")

        # scrollable card area
        mid = ttk.Frame(self); mid.pack(fill="both", expand=True, padx=16)
        self.canvas = tk.Canvas(mid, bg=BG, highlightthickness=0)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.canvas.yview)
        self.holder = ttk.Frame(self.canvas)
        self.holder.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.holder, anchor="nw", width=920)
        self.canvas.configure(yscrollcommand=sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        bar = ttk.Frame(self); bar.pack(fill="x", padx=16, pady=8)
        ttk.Button(bar, text="＋ Add Video", style="Add.TButton",
                   command=self.add_card).pack(side="left")
        self.run_btn = ttk.Button(bar, text="▶  Run", style="Go.TButton",
                                  command=self.run)
        self.run_btn.pack(side="right")
        self.stop_btn = ttk.Button(bar, text="⏹  Stop", style="Stop.TButton",
                                   command=self.stop, state="disabled")
        self.stop_btn.pack(side="right", padx=(0, 8))

        # log
        lf = ttk.Frame(self); lf.pack(fill="both", expand=False, padx=16, pady=(0, 12))
        self.log = tk.Text(lf, height=12, bg="#0b0f18", fg="#cbd5e1",
                           insertbackground=FG, relief="flat", wrap="word",
                           font=("Cascadia Mono", 9))
        self.log.pack(fill="both", expand=True)
        self.log.insert("end", "Ready. Add a video (Clean + Clue + Audio) and press Run.\n")
        self.log.configure(state="disabled")

    # ---- cards ----
    def add_card(self):
        c = Card(self.holder, self.remove_card)
        c.pack(fill="x", pady=8)
        self.cards.append(c)

    def remove_card(self, c):
        if len(self.cards) <= 1:
            return
        c.destroy(); self.cards.remove(c)

    # ---- run ----
    def _log(self, msg):
        self.log_q.put(msg)

    def _drain_log(self):
        try:
            while True:
                msg = self.log_q.get_nowait()
                self.log.configure(state="normal")
                self.log.insert("end", msg + "\n")
                self.log.see("end")
                self.log.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(80, self._drain_log)

    def run(self):
        if self.running:
            return
        jobs = []
        for i, c in enumerate(self.cards):
            j = c.to_job(i)
            if not (j.clean and j.clue and j.audio):
                c.set_status("needs all 3 files")
                continue
            jobs.append((c, j))
        if not jobs:
            self._log("✗ nothing to run — every video needs Clean + Clue + Audio.")
            return
        self.running = True
        self.stopping = False
        self.run_btn.configure(text="running…", state="disabled")
        self.stop_btn.configure(state="normal")
        threading.Thread(target=self._worker, args=(jobs,), daemon=True).start()

    def _set_proc(self, proc):
        self.current_proc = proc

    def stop(self):
        """Stop the queue and kill whatever child is running. The half-done
        video is kept, so the next Run resumes it from where it stopped."""
        if not self.running:
            return
        self.stopping = True
        self.stop_btn.configure(state="disabled")
        self._log("\n⏹  STOPPING — finishing the current step's kill, "
                  "progress is saved. Press Run to resume from here.")
        proc = self.current_proc
        if proc is not None:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True)     # kill the whole tree (ffmpeg too)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _worker(self, jobs):
        for i, (card, job) in enumerate(jobs):
            if self.stopping:
                card.set_status("stopped (re-Run to resume)")
                continue
            job.index = i
            card.set_status("running…")
            self._log(f"\n{'='*60}\nVIDEO {i+1}/{len(jobs)}: {os.path.basename(job.clean)}\n{'='*60}")
            studio.build(job, self._log, on_proc=self._set_proc,
                         should_stop=lambda: self.stopping)
            tag = {"done": "✓ done", "blocked": "⚠ library not ready",
                   "error": "✗ error", "stopped": "⏸ stopped"}.get(job.status, job.status)
            card.set_status(f"{tag} — {job.message}")
            self._log(f"→ {tag}: {job.message}")
            if self.stopping:
                break
        self.current_proc = None
        self._log("\nSTOPPED — press Run to resume." if self.stopping else "\nALL DONE.")
        self.running = False
        self.stopping = False
        self.run_btn.configure(text="▶  Run", state="normal")
        self.stop_btn.configure(state="disabled")


def main():
    App().mainloop()
    return 0


if __name__ == "__main__":
    main()
