#!/usr/bin/env python3
"""VText GUI — queue up to 15 videos, three files each, one Start button."""
import json
import os
import threading
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk

from engine.pipeline import run_job

NICHES = ["MOVIE_ESSAY", "CARTOON_ESSAY", "CLASSIC_MOVIE", "SITCOM_ESSAY",
          "DARK_PSYCHOLOGY", "HISTORY_DOC", "TRUE_CRIME", "SPORTS"]
PACKS = ["auto", "bold_geometric", "modern_clean", "editorial_serif",
         "classic_cinema", "condensed_impact", "playful_premium", "typewriter"]
STAGES = {"probe": "Probing", "parse": "Reading instructions",
          "audio": "Extracting audio", "align": "Aligning script",
          "cues": "Resolving moments", "scenes": "Shot cuts",
          "zones": "Analyzing frames", "style": "Planning style",
          "render": "Rendering text", "compose": "Compositing",
          "qc": "QC", "done": "Done"}


class App:
    def __init__(self, root):
        self.root = root
        root.title("VText — final-touch text overlays")
        root.geometry("760x640")
        self.jobs = []
        frm = ttk.Frame(root, padding=10)
        frm.pack(fill="both", expand=True)

        pick = ttk.LabelFrame(frm, text="Add a video", padding=10)
        pick.pack(fill="x")
        self.v_video = self._file_row(pick, 0, "Final video",
                                      [("Video", "*.mp4 *.mov *.mkv *.m4v")])
        self.v_script = self._file_row(pick, 1, "Clean script (.txt)",
                                       [("Text", "*.txt")])
        self.v_instr = self._file_row(pick, 2, "Instruction file (.txt)",
                                      [("Text", "*.txt")])

        opt = ttk.Frame(pick)
        opt.grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.v_niche = tk.StringVar(value=NICHES[0])
        self.v_pack = tk.StringVar(value="auto")
        self.v_energy = tk.DoubleVar(value=0)
        self.v_scale = tk.StringVar(value="auto")
        self.v_density = tk.StringVar(value="file")
        self.v_accent = tk.StringVar(value="")
        ttk.Label(opt, text="Niche").grid(row=0, column=0, sticky="w")
        ttk.Combobox(opt, textvariable=self.v_niche, values=NICHES, width=17,
                     state="readonly").grid(row=0, column=1, padx=4)
        ttk.Label(opt, text="Typography").grid(row=0, column=2, sticky="w")
        ttk.Combobox(opt, textvariable=self.v_pack, values=PACKS, width=16,
                     state="readonly").grid(row=0, column=3, padx=4)
        ttk.Button(opt, text="Accent color…", command=self._accent
                   ).grid(row=0, column=4, padx=4)
        ttk.Label(opt, text="Motion energy (0 = niche default)"
                  ).grid(row=1, column=0, columnspan=2, sticky="w")
        ttk.Scale(opt, from_=0, to=5, variable=self.v_energy, length=140
                  ).grid(row=1, column=2, columnspan=2, sticky="w")
        ttk.Label(opt, text="Text scale").grid(row=2, column=0, sticky="w")
        ttk.Combobox(opt, textvariable=self.v_scale, width=10,
                     values=["auto", "small", "balanced", "large"],
                     state="readonly").grid(row=2, column=1, padx=4)
        ttk.Label(opt, text="Density").grid(row=2, column=2, sticky="w")
        ttk.Combobox(opt, textvariable=self.v_density, width=10,
                     values=["file", "medium", "light"],
                     state="readonly").grid(row=2, column=3, padx=4)
        ttk.Button(pick, text="+ Add to queue", command=self.add_job
                   ).grid(row=4, column=2, sticky="e", pady=(8, 0))

        qf = ttk.LabelFrame(frm, text="Queue", padding=6)
        qf.pack(fill="both", expand=True, pady=8)
        self.listbox = tk.Listbox(qf, height=8)
        self.listbox.pack(fill="both", expand=True)
        ttk.Button(qf, text="Remove selected", command=self.remove_job
                   ).pack(anchor="e", pady=4)

        run = ttk.Frame(frm)
        run.pack(fill="x")
        self.pbar = ttk.Progressbar(run, maximum=1.0)
        self.pbar.pack(fill="x")
        self.status = ttk.Label(run, text="Ready.")
        self.status.pack(anchor="w")
        self.btn = ttk.Button(run, text="▶ Start Queue", command=self.start)
        self.btn.pack(anchor="e", pady=4)

    def _file_row(self, parent, row, label, types):
        var = tk.StringVar()
        ttk.Label(parent, text=label, width=20).grid(row=row, column=0,
                                                     sticky="w")
        ttk.Entry(parent, textvariable=var, width=52).grid(row=row, column=1)
        ttk.Button(parent, text="Browse", command=lambda: var.set(
            filedialog.askopenfilename(filetypes=types) or var.get())
                   ).grid(row=row, column=2, padx=4)
        return var

    def _accent(self):
        c = colorchooser.askcolor()[1]
        if c:
            self.v_accent.set(c)

    def add_job(self):
        v, s, i = (self.v_video.get(), self.v_script.get(),
                   self.v_instr.get())
        if not (v and s and i):
            messagebox.showerror("VText", "Pick all three files first.")
            return
        if len(self.jobs) >= 15:
            messagebox.showerror("VText", "Queue is full (15 max).")
            return
        opts = {"niche": self.v_niche.get(), "pack": self.v_pack.get(),
                "text_scale": self.v_scale.get(),
                "density": self.v_density.get()}
        if self.v_energy.get() > 0:
            opts["energy"] = round(self.v_energy.get(), 1)
        if self.v_accent.get():
            h = self.v_accent.get().lstrip("#")
            opts["accent"] = tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
        out = os.path.splitext(v)[0] + "_vtext.mp4"
        self.jobs.append({"video": v, "script": s, "instructions": i,
                          "out": out, "opts": opts})
        self.listbox.insert("end",
                            f"{os.path.basename(v)}  →  {os.path.basename(out)}"
                            f"   [{opts['niche']}]")

    def remove_job(self):
        for idx in reversed(self.listbox.curselection()):
            self.listbox.delete(idx)
            del self.jobs[idx]

    def start(self):
        if not self.jobs:
            messagebox.showinfo("VText", "Queue is empty.")
            return
        self.btn.config(state="disabled")
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        for n, job in enumerate(self.jobs, 1):
            name = os.path.basename(job["video"])

            def prog(stage, frac, n=n, name=name):
                self.root.after(0, self._update, n, name, stage, frac)
            try:
                run_job(job["video"], job["script"], job["instructions"],
                        job["out"], opts=job["opts"], progress=prog,
                        log=lambda *a: None)
            except Exception as e:
                err = str(e)
                self.root.after(0, lambda err=err, name=name:
                                messagebox.showerror(
                                    "VText", f"{name} failed:\n{err}"))
        self.root.after(0, self._done)

    def _update(self, n, name, stage, frac):
        self.pbar["value"] = frac
        self.status.config(text=f"[{n}/{len(self.jobs)}] {name} — "
                                f"{STAGES.get(stage, stage)} "
                                f"({frac * 100:.0f}%)")

    def _done(self):
        self.btn.config(state="normal")
        self.status.config(text="Queue finished. Reports saved next to "
                                "each output video.")
        messagebox.showinfo("VText", "All jobs finished.")


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
