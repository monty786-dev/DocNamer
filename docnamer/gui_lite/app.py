"""DocNamer Lite - Tkinter GUI for Winlator / Wine (touch-friendly, no GPU / DirectX / .NET needed)."""
from __future__ import annotations

import multiprocessing
import os
import sys
import tkinter as tk
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, simpledialog, ttk

from PIL import Image, ImageTk

from ..core import gui_common as gc
from ..core.env import data_dir as get_data_dir, drives, is_wine
from ..core.i18n import t
from ..core.logsetup import setup_logging
from ..core.report import undo_report
from ..core.review import Decision
from ..core.runner import Runner
from ..core.settings import Anchor, Settings, Template, Zone
from ..core.teach import TeachSession
from ..core.textutil import DEVANAGARI_KEYBOARD

BIG = ("Segoe UI", 13)
BIGB = ("Segoe UI", 13, "bold")
STEP_KEYS = ["step_folder", "step_orient", "step_zones", "step_pages", "step_naming", "step_preview", "step_run"]


# ============================================================================== widgets
class FolderBrowser(tk.Toplevel):
    """In-app folder chooser: big buttons, drive letters (Z:/D: = Android storage), typed path."""

    def __init__(self, parent, start: str = ""):
        super().__init__(parent)
        self.title("Choose folder")
        self.result = None
        self.geometry("720x560")
        self.transient(parent)
        top = ttk.Frame(self)
        top.pack(fill="x", padx=6, pady=6)
        self.path = tk.StringVar(value=start if os.path.isdir(start or "") else (drives()[0]))
        ttk.Entry(top, textvariable=self.path, font=BIG).pack(side="left", fill="x", expand=True)
        ttk.Button(top, text="Go", command=lambda: self._go(self.path.get())).pack(side="left", padx=4, ipadx=10, ipady=6)
        dr = ttk.Frame(self)
        dr.pack(fill="x", padx=6)
        for d in drives():
            ttk.Button(dr, text=d, command=lambda d=d: self._go(d)).pack(side="left", padx=2, ipadx=8, ipady=6)
        fr = ttk.Frame(self)
        fr.pack(fill="both", expand=True, padx=6, pady=6)
        sb = ttk.Scrollbar(fr, orient="vertical", width=26)
        self.lb = tk.Listbox(fr, font=BIG, activestyle="none", yscrollcommand=sb.set)
        sb.config(command=self.lb.yview)
        sb.pack(side="right", fill="y")
        self.lb.pack(side="left", fill="both", expand=True)
        self.lb.bind("<Double-Button-1>", lambda e: self._open())
        bt = ttk.Frame(self)
        bt.pack(fill="x", padx=6, pady=6)
        ttk.Button(bt, text="..  Up", command=self._up).pack(side="left", ipadx=12, ipady=10)
        ttk.Button(bt, text="Open", command=self._open).pack(side="left", padx=6, ipadx=12, ipady=10)
        ttk.Button(bt, text="Cancel", command=self.destroy).pack(side="right", ipadx=12, ipady=10)
        ttk.Button(bt, text="Select this folder", command=self._select).pack(side="right", padx=6, ipadx=12, ipady=10)
        self._go(self.path.get())
        self.grab_set()
        self.wait_window()

    def _go(self, p: str):
        p = p.strip().strip('"')
        if not os.path.isdir(p):
            messagebox.showwarning("Folder", f"Not a folder:\n{p}", parent=self)
            return
        self.path.set(os.path.abspath(p))
        self.lb.delete(0, "end")
        try:
            for n in sorted(os.listdir(p), key=str.lower):
                if os.path.isdir(os.path.join(p, n)):
                    self.lb.insert("end", n)
        except OSError as e:
            messagebox.showwarning("Folder", str(e), parent=self)

    def _open(self):
        sel = self.lb.curselection()
        if sel:
            self._go(os.path.join(self.path.get(), self.lb.get(sel[0])))

    def _up(self):
        parent = os.path.dirname(self.path.get().rstrip("\\/"))
        if parent and os.path.isdir(parent):
            self._go(parent)

    def _select(self):
        self.result = self.path.get()
        self.destroy()


class ScrollFrame(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.c = tk.Canvas(self, highlightthickness=0)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.c.yview, width=26)
        self.inner = ttk.Frame(self.c)
        self.inner.bind("<Configure>", lambda e: self.c.configure(scrollregion=self.c.bbox("all")))
        self.win = self.c.create_window((0, 0), window=self.inner, anchor="nw")
        self.c.bind("<Configure>", lambda e: self.c.itemconfigure(self.win, width=e.width))
        self.c.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.c.pack(side="left", fill="both", expand=True)
        self.bind("<Enter>", lambda e: self.c.bind_all("<MouseWheel>", self._wheel))
        self.bind("<Leave>", lambda e: self.c.unbind_all("<MouseWheel>"))

    def _wheel(self, e):
        self.c.yview_scroll(int(-e.delta / 120) or (-1 if e.delta > 0 else 1), "units")


class Form(ttk.Frame):
    """Settings form generated from gui_common schema rows."""

    def __init__(self, parent, fields, app):
        super().__init__(parent)
        self.fields, self.app, self.vars = fields, app, {}
        self.columnconfigure(1, weight=1)
        for r, f in enumerate(fields):
            ttk.Label(self, text=f.label, font=BIG, wraplength=330).grid(row=r, column=0, sticky="w", padx=6, pady=5)
            if f.kind == "bool":
                v = tk.IntVar()
                w = ttk.Checkbutton(self, variable=v)
            elif f.kind in ("choice", "choiceedit"):
                v = tk.StringVar()
                w = ttk.Combobox(self, textvariable=v, values=f.extra, font=BIG,
                                 state="readonly" if f.kind == "choice" else "normal")
            elif f.kind == "dir":
                v = tk.StringVar()
                w = ttk.Frame(self)
                ttk.Entry(w, textvariable=v, font=BIG).pack(side="left", fill="x", expand=True)
                ttk.Button(w, text=t("browse"), command=lambda v=v: self._browse(v)).pack(side="left", padx=4, ipady=6)
            elif f.kind == "color":
                v = tk.StringVar()
                w = ttk.Frame(self)
                ttk.Entry(w, textvariable=v, font=BIG, width=10).pack(side="left")
                ttk.Button(w, text="Pick", command=lambda v=v: self._color(v)).pack(side="left", padx=4, ipady=6)
            else:
                v = tk.StringVar()
                w = ttk.Entry(self, textvariable=v, font=BIG)
            w.grid(row=r, column=1, sticky="ew", padx=6, pady=5)
            if f.tip:
                ttk.Label(self, text=f.tip, foreground="#666", wraplength=520).grid(row=r, column=2, sticky="w", padx=6)
            self.vars[f.path] = v

    def _browse(self, v):
        d = FolderBrowser(self, v.get()).result
        if d:
            v.set(d)

    def _color(self, v):
        c = colorchooser.askcolor(color=v.get() or "#E53935", parent=self)[1]
        if c:
            v.set(c)

    def load(self, st: Settings):
        for f in self.fields:
            self.vars[f.path].set(gc.to_text(gc.get_path(st, f.path), f.kind) if f.kind != "bool"
                                  else int(bool(gc.get_path(st, f.path))))

    def store(self, st: Settings):
        for f in self.fields:
            old = gc.get_path(st, f.path)
            raw = self.vars[f.path].get()
            gc.set_path(st, f.path, bool(raw) if f.kind == "bool" else gc.from_text(str(raw), f.kind, old))


class ReviewDialog(tk.Toplevel):
    """'Can't read this' dialog."""

    def __init__(self, parent, item):
        super().__init__(parent)
        self.title(t("review_title"))
        self.item, self.result = item, None
        self.transient(parent)
        ttk.Label(self, font=BIGB, wraplength=700,
                  text=f"{Path(item.path).name}   page {item.page + 1}   field: {item.field}\n({item.reason}; read as {item.text!r}, {item.conf:.0%})"
                  ).pack(padx=10, pady=8)
        if item.crop_path and os.path.exists(item.crop_path):
            with Image.open(item.crop_path) as _im:
                im = _im.copy()
            k = min(1.0, 760 / im.size[0]) if item.kind == "field" else min(1.0, 600 / max(im.size))
            if item.kind == "field" and im.size[0] < 380:
                k = 380 / im.size[0]
            im = im.resize((max(1, int(im.size[0] * k)), max(1, int(im.size[1] * k))))
            self._img = ImageTk.PhotoImage(im)
            tk.Label(self, image=self._img, bd=2, relief="groove").pack(padx=10, pady=4)
        if item.kind == "orientation":
            ttk.Label(self, text="Rotate the page clockwise by:", font=BIG).pack(pady=4)
            row = ttk.Frame(self)
            row.pack(pady=6)
            for a in ("0", "90", "180", "270"):
                ttk.Button(row, text=f"{a}\u00b0", command=lambda a=a: self._done(Decision("edit", a))
                           ).pack(side="left", padx=6, ipadx=16, ipady=14)
            ttk.Label(self, text=f"suggested: {item.text}\u00b0", foreground="#666").pack()
        else:
            cf = ttk.Frame(self)
            cf.pack(pady=4)
            for c in item.candidates:
                ttk.Button(cf, text=f"{c[0]}   ({c[2]} {c[1]:.0%})", command=lambda c=c: self._done(Decision(
                    "edit", c[0], self.sim.get(), self.lib.get(), self.kind.get(), self.prof.get()))
                ).pack(fill="x", padx=8, pady=2, ipady=8)
            self.txt = tk.StringVar(value=item.text)
            ttk.Entry(self, textvariable=self.txt, font=BIGB, width=36).pack(padx=10, pady=8, ipady=6)
            opt = ttk.Frame(self)
            opt.pack()
            self.sim, self.lib = tk.BooleanVar(), tk.BooleanVar()
            self.kind, self.prof = tk.StringVar(value="handwriting"), tk.StringVar(value="default")
            ttk.Checkbutton(opt, text=t("apply_similar"), variable=self.sim).grid(row=0, column=0, sticky="w", padx=6)
            ttk.Checkbutton(opt, text=t("save_library"), variable=self.lib).grid(row=0, column=1, sticky="w", padx=6)
            ttk.Combobox(opt, textvariable=self.kind, values=["handwriting", "font"], width=12, state="readonly").grid(row=1, column=0, padx=6, pady=4)
            ttk.Entry(opt, textvariable=self.prof, width=16).grid(row=1, column=1, padx=6)
            bt = ttk.Frame(self)
            bt.pack(pady=10)
            mk = lambda txt, cmd: ttk.Button(bt, text=txt, command=cmd).pack(side="left", padx=4, ipadx=8, ipady=10)
            mk(t("accept"), lambda: self._done(Decision("accept")))
            mk(t("edit"), lambda: self._done(Decision("edit", self.txt.get(), self.sim.get(), self.lib.get(), self.kind.get(), self.prof.get())))
            mk(t("skip_field"), lambda: self._done(Decision("skip")))
            mk(t("use_fallback"), lambda: self._done(Decision("fallback")))
            mk(t("needs_manual"), lambda: self._done(Decision("manual")))
        self.protocol("WM_DELETE_WINDOW", lambda: self._done(None))
        self.grab_set()
        self.wait_window()

    def _done(self, d):
        self.result = d
        self.destroy()



class TeachDialog(tk.Toplevel):
    """Asks, piece by piece, which shabd (word) / akshar (letter) the handwriting contains."""

    def __init__(self, parent, lib, img, source_doc: str = "", page: int = 0, hint_text: str = ""):
        super().__init__(parent)
        self.title(t("teach_title"))
        self.transient(parent)
        self.lib = lib
        self.sess = TeachSession(lib, img, source_doc, page, "default", "handwriting", "words", hint_text)
        self.saved = 0
        self.head = ttk.Label(self, font=BIGB)
        self.head.pack(padx=10, pady=(8, 2))
        self.pic = tk.Label(self, bd=2, relief="groove", bg="white")
        self.pic.pack(padx=10, pady=4)
        ttk.Label(self, text=t("teach_ask"), font=BIG).pack()
        self.var = tk.StringVar()
        self.entry = ttk.Entry(self, textvariable=self.var, font=("Nirmala UI", 20), width=26)
        self.entry.pack(padx=10, pady=6, ipady=6)
        self.hint = ttk.Label(self, text="", foreground="#666")
        self.hint.pack()
        nb = ttk.Notebook(self)
        nb.pack(fill="x", padx=10, pady=4)
        for title, keys in DEVANAGARI_KEYBOARD.items():
            fr = ttk.Frame(nb)
            nb.add(fr, text=title)
            for i, k in enumerate(keys):
                ttk.Button(fr, text=k, width=4, command=lambda k=k: self._ins(k)).grid(row=i // 10, column=i % 10, padx=1, pady=1, ipady=4)
        kb = ttk.Frame(self)
        kb.pack()
        ttk.Button(kb, text="\u232b", width=6, command=lambda: self.var.set(self.var.get()[:-1])).pack(side="left", padx=2, ipady=4)
        ttk.Button(kb, text="space", width=8, command=lambda: self._ins(" ")).pack(side="left", padx=2, ipady=4)
        ttk.Button(kb, text="clear", width=6, command=lambda: self.var.set("")).pack(side="left", padx=2, ipady=4)
        opt = ttk.Frame(self)
        opt.pack(pady=6)
        ttk.Label(opt, text=t("teach_profile")).grid(row=0, column=0, padx=4)
        self.prof = ttk.Combobox(opt, values=[p["name"] for p in lib.profiles()] or ["default"], width=16)
        self.prof.set("default")
        self.prof.grid(row=0, column=1, padx=4)
        ttk.Label(opt, text=t("teach_kind")).grid(row=0, column=2, padx=4)
        self.kind = ttk.Combobox(opt, values=["handwriting", "font"], state="readonly", width=12)
        self.kind.set("handwriting")
        self.kind.grid(row=0, column=3, padx=4)
        self.also = tk.IntVar(value=1)
        ttk.Checkbutton(self, text=t("teach_also"), variable=self.also).pack()
        self.status = ttk.Label(self, text="", foreground="#0a58ca", wraplength=640)
        self.status.pack(pady=2)
        bt = ttk.Frame(self)
        bt.pack(pady=8)
        self.letters_b = ttk.Button(bt, text=t("teach_letters"), command=self._letters)
        for txt, cmd in ((t("teach_save"), self._save), (t("teach_skip"), self._skip), (t("back"), self._back)):
            ttk.Button(bt, text=txt, command=cmd).pack(side="left", padx=4, ipadx=8, ipady=10)
        self.letters_b.pack(side="left", padx=4, ipadx=8, ipady=10)
        ttk.Button(bt, text=t("teach_finish"), command=self.destroy).pack(side="left", padx=4, ipadx=8, ipady=10)
        self.bind("<Return>", lambda e: self._save())
        self._show()
        self.grab_set()
        self.wait_window()

    def _ins(self, ch: str):
        self.entry.insert(self.entry.index(tk.INSERT), ch)
        self.entry.focus_set()

    def _show(self):
        seg = self.sess.current()
        if seg is None:
            messagebox.showinfo(t("teach_title"), f"{self.saved} sample(s) saved to the library.", parent=self)
            self.destroy()
            return
        k = max(1.0, 90 / max(seg.crop.size[1], 1))
        k = min(k, 700 / max(seg.crop.size[0], 1)) if seg.unit == "word" else min(k, 4.0)
        im = seg.crop.resize((max(1, int(seg.crop.size[0] * k)), max(1, int(seg.crop.size[1] * k))))
        self._ph = ImageTk.PhotoImage(im)
        self.pic.configure(image=self._ph)
        kind = t("teach_word") if seg.unit == "word" else t("teach_letter")
        self.head.configure(text=f"{self.sess.pos + 1} / {self.sess.total}    {kind}")
        self.var.set(seg.guess)
        self.hint.configure(text=(f"guess: {seg.guess}  ({seg.guess_conf:.0%})" if seg.guess else "no guess yet - please type it"))
        self.letters_b.state(["!disabled" if seg.unit == "word" else "disabled"])
        self.entry.focus_set()
        self.entry.select_range(0, "end")

    def _save(self):
        label = self.var.get().strip()
        if not label:
            self.status.configure(text="Type what it says, or press Skip.")
            return
        self.sess.profile, self.sess.kind = self.prof.get().strip() or "default", self.kind.get()
        info = self.sess.save(label, bool(self.also.get()))
        self.saved += info["saved"] + info["letters_saved"]
        msg = f"saved '{label}'" + (f" + {info['letters_saved']} letter(s)" if info["letters_saved"] else "")
        if info["letter_mismatch"]:
            msg += f"  (letters found: {info['letter_mismatch'][0]}, you typed {info['letter_mismatch'][1]} - use 'Teach this word's letters' to label them one by one)"
        self.status.configure(text=msg)
        self._show()

    def _skip(self):
        self.sess.skip()
        self._show()

    def _back(self):
        self.sess.back()
        self._show()

    def _letters(self):
        n = self.sess.ask_letters_next()
        self.status.configure(text=f"{n} letter piece(s) will be asked right after this word." if n else "No separate letters were found in this word.")


# ============================================================================== main window
class App(tk.Tk):
    def __init__(self, data: Path):
        super().__init__()
        self.title(f"{t('app')} Lite")
        self.geometry("1000x720")
        self.data = data
        self.settings_path = data / "settings.json"
        self.st = Settings.load(self.settings_path)
        self.st.apply_lite_defaults()      # this build always uses the low-memory profile
        self.option_add("*Font", BIG)
        ttk.Style(self).configure("TButton", font=BIG, padding=6)
        ttk.Style(self).configure("Treeview", rowheight=34, font=BIG)
        ttk.Style(self).configure("Treeview.Heading", font=BIGB)
        self.runner = Runner(self.st, data)
        self.step = 0
        self.sample_path = ""
        self.sample_img = None
        self.scale = 1.0
        self.drag = None
        self.results = []
        self._build_shell()
        self._build_steps()
        self._show(0)
        self.protocol("WM_DELETE_WINDOW", self._quit)
        self.after(150, self._poll)

    # ---- shell ---------------------------------------------------------------------------
    def _build_shell(self):
        self.head = ttk.Label(self, font=("Segoe UI", 16, "bold"))
        self.head.pack(fill="x", padx=10, pady=(8, 2))
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=10)
        self.crumbs = [ttk.Label(bar, text=t(k), foreground="#888") for k in STEP_KEYS]
        for c in self.crumbs:
            c.pack(side="left", padx=5)
        self.body = ttk.Frame(self)
        self.body.pack(fill="both", expand=True, padx=8, pady=6)
        nav = ttk.Frame(self)
        nav.pack(fill="x", padx=10, pady=8)
        self.back_b = ttk.Button(nav, text=t("back"), command=lambda: self._show(self.step - 1))
        self.back_b.pack(side="left", ipadx=18, ipady=10)
        ttk.Button(nav, text="Library...", command=self._library).pack(side="left", padx=10, ipady=10)
        self.next_b = ttk.Button(nav, text=t("next"), command=lambda: self._show(self.step + 1))
        self.next_b.pack(side="right", ipadx=18, ipady=10)

    def _build_steps(self):
        S = gc.steps()
        self.forms, sf = {}, {}
        for key in ("folder", "orientation", "pages", "naming"):
            holder = ScrollFrame(self.body)
            form = Form(holder.inner, S[key], self)
            form.pack(fill="both", expand=True)
            self.forms[key] = form
            sf[key] = holder
        self.frames = [sf["folder"], sf["orientation"], self._zones_frame(), sf["pages"], sf["naming"],
                       self._preview_frame(), self._run_frame()]

    def _show(self, n: int):
        if n < 0 or n >= len(self.frames):
            return
        self._store_forms()
        if n <= 4 and self.results:                       # settings may change: the old preview is stale
            self.results, self.runner.results = [], []
        for f in self.frames:
            f.pack_forget()
        self.step = n
        self.frames[n].pack(fill="both", expand=True)
        for i, c in enumerate(self.crumbs):
            c.configure(foreground="#0a58ca" if i == n else "#888", font=BIGB if i == n else BIG)
        self.head.configure(text=t(STEP_KEYS[n]))
        for form in self.forms.values():
            form.load(self.st)
        self.back_b.state(["!disabled" if n else "disabled"])
        self.next_b.state(["!disabled" if n < len(self.frames) - 1 else "disabled"])
        self._sync_mode()

    def _store_forms(self):
        for form in self.forms.values():
            try:
                form.store(self.st)
            except tk.TclError:
                pass
        self.st.save(self.settings_path)
        pl = self.runner.pipeline
        pl.st = self.st
        o = self.st.ocr
        sig = (o.engine, o.languages, o.handwriting, o.handwriting_model, o.secondary, o.threads)
        if sig != getattr(self, "_ocr_sig", sig):
            from ..core.ocr import Engines
            pl.engines = Engines(o, self.data, library=pl.lib)          # engine choice changed: rebuild lazily
        self._ocr_sig = sig

    # ---- step 3: zones / anchors ------------------------------------------------------------
    def _zones_frame(self):
        f = ttk.Frame(self.body)
        top = ttk.Frame(f)
        top.pack(fill="x")
        self.mode = tk.StringVar(value=self.st.mode)
        for txt, val in ((t("mode_zones"), "zones"), (t("mode_anchors"), "anchors"), (t("mode_full"), "full")):
            ttk.Radiobutton(top, text=txt, value=val, variable=self.mode, command=self._sync_mode).pack(side="left", padx=8, pady=4)
        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Button(row, text="Pick sample document...", command=self._pick_sample).pack(side="left", ipady=8, padx=4)
        ttk.Button(row, text="Test on sample", command=self._test_sample).pack(side="left", ipady=8, padx=4)
        ttk.Button(row, text=t("teach_btn"), command=self._teach_zone).pack(side="left", ipady=8, padx=4)
        self.tpl_name = tk.StringVar()
        ttk.Entry(row, textvariable=self.tpl_name, width=14).pack(side="left", padx=4, ipady=6)
        ttk.Button(row, text="Save template", command=self._save_tpl).pack(side="left", ipady=8)
        self.tpl_pick = ttk.Combobox(row, values=self.runner.pipeline.templates.names(), width=14, state="readonly")
        self.tpl_pick.pack(side="left", padx=4, ipady=6)
        ttk.Button(row, text="Load", command=self._load_tpl).pack(side="left", ipady=8)
        self.auto_tpl = tk.IntVar(value=int(self.st.auto_template))
        ttk.Checkbutton(row, text="auto-match", variable=self.auto_tpl).pack(side="left", padx=6)
        mid = ttk.Frame(f)
        mid.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(mid, bg="#ddd", width=420, height=520, highlightthickness=1)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self._down)
        self.canvas.bind("<B1-Motion>", self._move)
        self.canvas.bind("<ButtonRelease-1>", self._up)
        right = ttk.Frame(mid)
        right.pack(side="left", fill="y", padx=6)
        self.tree = ttk.Treeview(right, columns=("n", "k", "x", "y", "w", "h"), show="headings", height=7)
        for c, w in (("n", 110), ("k", 80), ("x", 45), ("y", 45), ("w", 45), ("h", 45)):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="x")
        self.tree.bind("<<TreeviewSelect>>", self._select_spec)
        self.ev = {k: tk.StringVar() for k in ("name", "kind", "fmt", "regex", "whitelist", "pages", "x", "y", "w", "h", "keyword", "direction")}
        self.ev_req = tk.IntVar(value=1)
        g = ttk.Frame(right)
        g.pack(fill="x", pady=4)
        rows = [("name", None), ("kind", ["auto", "printed", "handwritten"]), ("fmt", ["any", "date", "number", "letters"]),
                ("regex", None), ("whitelist", None), ("pages", None)]
        self.zone_only = []
        for i, (k, ch) in enumerate(rows):
            ttk.Label(g, text=k).grid(row=i, column=0, sticky="w")
            (ttk.Combobox(g, textvariable=self.ev[k], values=ch, state="readonly", width=14) if ch
             else ttk.Entry(g, textvariable=self.ev[k], width=16)).grid(row=i, column=1, sticky="ew", pady=1)
        r0 = len(rows)
        self.geo = ttk.Frame(g)
        self.geo.grid(row=r0, column=0, columnspan=2, sticky="w")
        for i, k in enumerate(("x", "y", "w", "h")):
            ttk.Label(self.geo, text=k + "%").grid(row=0, column=2 * i)
            ttk.Entry(self.geo, textvariable=self.ev[k], width=5).grid(row=0, column=2 * i + 1, padx=2)
        self.anc = ttk.Frame(g)
        self.anc.grid(row=r0 + 1, column=0, columnspan=2, sticky="ew")
        ttk.Label(self.anc, text="keyword").grid(row=0, column=0)
        ttk.Entry(self.anc, textvariable=self.ev["keyword"], width=14).grid(row=0, column=1)
        ttk.Combobox(self.anc, textvariable=self.ev["direction"], values=["right", "below", "offset"], state="readonly", width=8).grid(row=0, column=2, padx=3)
        ttk.Checkbutton(g, text="required", variable=self.ev_req).grid(row=r0 + 2, column=0, sticky="w")
        bt = ttk.Frame(right)
        bt.pack(fill="x")
        ttk.Button(bt, text="Add / update", command=self._upsert).pack(side="left", ipady=8, padx=2)
        ttk.Button(bt, text="Delete", command=self._delete_spec).pack(side="left", ipady=8, padx=2)
        self.test_out = tk.Text(right, height=6, width=34, font=("Consolas", 10))
        self.test_out.pack(fill="both", expand=True, pady=4)
        self._refresh_specs()
        return f

    def _sync_mode(self):
        if not hasattr(self, "mode"):
            return
        self.st.mode = self.mode.get()
        self.st.auto_template = bool(self.auto_tpl.get())
        zones = self.st.mode == "zones"
        (self.geo.grid if zones else self.geo.grid_remove)()
        (self.anc.grid_remove if zones else self.anc.grid)()
        self._refresh_specs()

    def _specs(self):
        return self.st.zones if self.st.mode == "zones" else self.st.anchors

    def _refresh_specs(self):
        self.tree.delete(*self.tree.get_children())
        if self.st.mode == "full":
            return
        for i, s in enumerate(self._specs()):
            if isinstance(s, Zone):
                v = (s.name, s.kind, round(s.x * 100), round(s.y * 100), round(s.w * 100), round(s.h * 100))
            else:
                v = (s.name, s.kind, s.keyword, s.direction, "", "")
            self.tree.insert("", "end", iid=str(i), values=v)
        self._redraw()

    def _select_spec(self, _e=None):
        sel = self.tree.selection()
        if not sel:
            return
        s = self._specs()[int(sel[0])]
        for k in ("name", "kind", "fmt", "regex", "whitelist", "pages"):
            self.ev[k].set(getattr(s, k))
        self.ev_req.set(int(s.required))
        if isinstance(s, Zone):
            for k in "xywh":
                self.ev[k].set(str(round(getattr(s, k) * 100, 1)))
        else:
            self.ev["keyword"].set(s.keyword)
            self.ev["direction"].set(s.direction)
        self._redraw()

    def _upsert(self):
        e = {k: v.get().strip() for k, v in self.ev.items()}
        if not e["name"]:
            messagebox.showinfo("Field", "Give the field a name (it becomes {name} in the pattern).")
            return
        common = dict(name=e["name"], kind=e["kind"] or "auto", fmt=e["fmt"] or "any", regex=e["regex"],
                      whitelist=e["whitelist"], pages=e["pages"], required=bool(self.ev_req.get()))
        if self.st.mode == "zones":
            try:
                x, y, w, h = (float(e[k]) / 100 for k in "xywh")
            except ValueError:
                messagebox.showinfo("Zone", "Draw the zone on the page, or type x y w h in percent.")
                return
            spec = Zone(x=x, y=y, w=w, h=h, **common)
            lst = self.st.zones
        else:
            if not e["keyword"]:
                messagebox.showinfo("Anchor", "Type the keyword to look for, e.g. Invoice No")
                return
            spec = Anchor(keyword=e["keyword"], direction=e["direction"] or "right", **common)
            lst = self.st.anchors
        sel = self.tree.selection()
        idx = next((i for i, s in enumerate(lst) if s.name == spec.name), None)
        if idx is None and sel and lst[int(sel[0])].name == spec.name:
            idx = int(sel[0])
        if idx is None:
            lst.append(spec)
        else:
            lst[idx] = spec
        self._refresh_specs()

    def _delete_spec(self):
        sel = self.tree.selection()
        if sel:
            del self._specs()[int(sel[0])]
            self._refresh_specs()

    # ---- sample preview + drawing -----------------------------------------------------------------
    def _pick_sample(self):
        p = filedialog.askopenfilename(initialdir=self.st.output.input_dir or None, title="Sample document",
                                       filetypes=[("PDF / images", "*.pdf *.jpg *.jpeg *.png *.bmp *.tif *.tiff *.webp *.gif"), ("All", "*.*")])
        if not p:
            return
        self._store_forms()
        self.config(cursor="watch")
        self.update_idletasks()
        try:
            self.sample_img = self.runner.pipeline.preview_image(p)
            self.sample_path = p
        except Exception as e:
            messagebox.showerror(t("error"), str(e))
        finally:
            self.config(cursor="")
        self._redraw()

    def _redraw(self, boxes=None):
        c = self.canvas
        c.delete("all")
        if self.sample_img is None:
            c.create_text(210, 40, text="Pick a sample document, then drag a rectangle\nover the text you want to read.", font=BIG)
            return
        W = max(c.winfo_width(), 300)
        H = max(c.winfo_height(), 300)
        iw, ih = self.sample_img.size
        self.scale = min(W / iw, H / ih)
        im = self.sample_img.resize((int(iw * self.scale), int(ih * self.scale)))
        self._photo = ImageTk.PhotoImage(im)
        c.create_image(0, 0, image=self._photo, anchor="nw")
        if self.st.mode == "zones":
            sel = self.tree.selection()
            for i, z in enumerate(self.st.zones):
                col = "#e53935" if sel and int(sel[0]) == i else "#1e88e5"
                c.create_rectangle(z.x * iw * self.scale, z.y * ih * self.scale, (z.x + z.w) * iw * self.scale,
                                   (z.y + z.h) * ih * self.scale, outline=col, width=3)
                c.create_text(z.x * iw * self.scale + 3, z.y * ih * self.scale - 8, text=z.name, anchor="w", fill=col, font=BIGB)
        for b in boxes or []:
            c.create_rectangle(b[0] * self.scale, b[1] * self.scale, b[2] * self.scale, b[3] * self.scale, outline="#2e7d32", width=3)

    def _down(self, e):
        self.drag = (e.x, e.y, None)

    def _move(self, e):
        if self.drag and self.sample_img is not None and self.st.mode == "zones":
            if self.drag[2]:
                self.canvas.delete(self.drag[2])
            r = self.canvas.create_rectangle(self.drag[0], self.drag[1], e.x, e.y, outline="#e53935", width=3, dash=(4, 3))
            self.drag = (self.drag[0], self.drag[1], r)

    def _up(self, e):
        if not self.drag or self.sample_img is None or self.st.mode != "zones":
            self.drag = None
            return
        x0, y0 = self.drag[0], self.drag[1]
        self.drag = None
        if abs(e.x - x0) < 8 or abs(e.y - y0) < 8:
            return
        iw, ih = self.sample_img.size
        k = self.scale
        x1, x2 = sorted((x0, e.x))
        y1, y2 = sorted((y0, e.y))
        self.ev["x"].set(f"{x1 / (iw * k) * 100:.1f}")
        self.ev["y"].set(f"{y1 / (ih * k) * 100:.1f}")
        self.ev["w"].set(f"{(x2 - x1) / (iw * k) * 100:.1f}")
        self.ev["h"].set(f"{(y2 - y1) / (ih * k) * 100:.1f}")
        is_new = not self.ev["name"].get().strip()
        if is_new:
            self.ev["name"].set(simpledialog.askstring("Zone name", "Name for this field (e.g. invoice):", parent=self) or f"field{len(self.st.zones) + 1}")
        self._upsert()
        if is_new:                       # next drag starts a NEW zone instead of overwriting this one
            for k in ("name", "regex", "whitelist", "pages"):
                self.ev[k].set("")

    def _test_sample(self):
        if not self.sample_path:
            messagebox.showinfo("Test", "Pick a sample document first.")
            return
        self._store_forms()
        self.config(cursor="watch")
        self.update_idletasks()
        try:
            out = self.runner.pipeline.try_extract(self.sample_path)
        except Exception as e:
            messagebox.showerror(t("error"), str(e))
            return
        finally:
            self.config(cursor="")
        self.test_out.delete("1.0", "end")
        boxes = []
        for n, fv in out.items():
            self.test_out.insert("end", f"{n}: {fv.text!r}  conf {fv.conf:.0%}  {'OK' if fv.valid and fv.found else 'CHECK'}\n")
            boxes += fv.boxes or ([fv.box] if fv.box else [])
        self._redraw(boxes)

    def _save_tpl(self):
        self._store_forms()
        name = self.tpl_name.get().strip()
        if not name:
            messagebox.showinfo("Template", "Type a template name first.")
            return
        kw = simpledialog.askstring("Auto-match", "Keywords that identify this document type (comma separated, optional):", parent=self) or ""
        sig = []
        if self.sample_img is not None:
            from ..core.templates import layout_signature
            sig = layout_signature(self.sample_img)
        self.runner.pipeline.templates.save(Template(
            name=name, mode=self.st.mode, zones=self.st.zones, anchors=self.st.anchors, pattern=self.st.naming.pattern,
            fallback=self.st.naming.fallback, rules=self.st.rules.__dict__,
            match_keywords=[k.strip() for k in kw.split(",") if k.strip()], layout_sig=[] if kw.strip() else sig))
        self.tpl_pick.configure(values=self.runner.pipeline.templates.names())
        messagebox.showinfo("Template", f"Saved '{name}'.")

    def _load_tpl(self):
        n = self.tpl_pick.get()
        if n:
            self.st = self.runner.pipeline.templates.load(n).apply_to(self.st)
            self.runner.pipeline.st = self.st
            self.mode.set(self.st.mode)
            self._sync_mode()
            for form in self.forms.values():
                form.load(self.st)

    # ---- step 6: preview -------------------------------------------------------------------------------
    def _preview_frame(self):
        f = ttk.Frame(self.body)
        top = ttk.Frame(f)
        top.pack(fill="x")
        ttk.Button(top, text="Analyse now (nothing is copied)", command=lambda: self._start("preview")).pack(side="left", ipady=10, padx=4)
        self.pv_info = ttk.Label(top, text="")
        self.pv_info.pack(side="left", padx=10)
        cols = ("file", "new", "status", "msg")
        self.pv = ttk.Treeview(f, columns=cols, show="headings")
        for c, w, h in (("file", 300, "Original"), ("new", 300, "New name (double-tap to edit)"), ("status", 90, "Status"), ("msg", 240, "Note")):
            self.pv.heading(c, text=h)
            self.pv.column(c, width=w)
        self.pv.tag_configure("review", background="#fff3cd")
        self.pv.tag_configure("error", background="#f8d7da")
        sb = ttk.Scrollbar(f, orient="vertical", command=self.pv.yview, width=26)
        self.pv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.pv.pack(fill="both", expand=True, pady=6)
        self.pv.bind("<Double-Button-1>", self._edit_name)
        return f

    def _fill_preview(self):
        self.pv.delete(*self.pv.get_children())
        for i, r in enumerate(self.results):
            tag = "review" if r.status == "review" else ("error" if r.status in ("error", "encrypted") else "")
            seg = f" [part {r.segment}]" if r.segment > 0 else ""
            self.pv.insert("", "end", iid=str(i), tags=(tag,),
                           values=(Path(r.path).name + seg, r.new_name, r.status, r.message))
        n_rev = sum(1 for r in self.results if r.status == "review")
        self.pv_info.configure(text=f"{len(self.results)} file(s), {n_rev} need review")

    def _edit_name(self, e):
        sel = self.pv.selection()
        if not sel:
            return
        r = self.results[int(sel[0])]
        if not r.new_name:
            return
        new = simpledialog.askstring("New name", "File name:", initialvalue=r.new_name, parent=self)
        if new:
            r.new_name, r.name_locked = new, True
            self._fill_preview()

    # ---- step 7: run ------------------------------------------------------------------------------------------
    def _run_frame(self):
        f = ttk.Frame(self.body)
        bt = ttk.Frame(f)
        bt.pack(fill="x")
        self.start_b = ttk.Button(bt, text=t("start"), command=self._start_run)
        self.start_b.pack(side="left", ipady=12, ipadx=16, padx=4)
        self.pause_b = ttk.Button(bt, text=t("pause"), command=self._pause)
        self.pause_b.pack(side="left", ipady=12, padx=4)
        ttk.Button(bt, text=t("cancel"), command=lambda: self.runner.cancel()).pack(side="left", ipady=12, padx=4)
        ttk.Button(bt, text=t("undo"), command=self._undo).pack(side="right", ipady=12, padx=4)
        self.pb = ttk.Progressbar(f, maximum=100)
        self.pb.pack(fill="x", pady=8)
        self.pb_l = ttk.Label(f, text="")
        self.pb_l.pack(anchor="w")
        self.log = tk.Text(f, font=("Consolas", 10), height=16)
        self.log.pack(fill="both", expand=True, pady=4)
        return f

    def _start(self, job: str):
        self._store_forms()
        if not self.st.output.input_dir:
            messagebox.showinfo("Folder", "Choose the input folder in step 1.")
            return
        self.runner.pipeline.st = self.st
        self.runner.start(job)

    def _start_run(self):
        if self.runner.busy:
            return
        if self.results:
            self._after_analysis()
        else:
            self._start("analyze")

    def _pause(self):
        if self.pause_b.cget("text") == t("pause"):
            self.runner.pause()
            self.pause_b.configure(text=t("resume"))
        else:
            self.runner.resume()
            self.pause_b.configure(text=t("pause"))

    def _after_analysis(self):
        self.runner.results = self.results
        for r, it in self.runner.pending_review():
            d = ReviewDialog(self, it).result
            if d:
                self.runner.pipeline.apply_review(r, it, d, self.results)
        self._run_teach_queue()
        self._fill_preview()
        self.runner.start("finish")

    # ---- teaching handwriting -------------------------------------------------------------------------
    def _teach_zone(self):
        sel = self.tree.selection()
        if self.sample_img is None or self.st.mode != "zones" or not sel:
            messagebox.showinfo(t("teach_title"), t("teach_pick_zone"))
            return
        z = self.st.zones[int(sel[0])]
        W, H = self.sample_img.size
        crop = self.sample_img.crop((int(z.x * W), int(z.y * H), int((z.x + z.w) * W), int((z.y + z.h) * H)))
        TeachDialog(self, self.runner.pipeline.lib, crop, self.sample_path, 0)

    def _run_teach_queue(self):
        """Handwritten crops waiting to be taught (from 'teach handwriting' option or unmatched word counts)."""
        items = self.runner.pending_teach()
        if not items or not messagebox.askyesno(t("teach_title"), f"{len(items)} handwritten crop(s) can be added to the library so "
                                                f"future documents are read automatically.\nTeach them now?"):
            return
        for r, it in items:
            if it.crop_path and os.path.exists(it.crop_path):
                with Image.open(it.crop_path) as im:
                    TeachDialog(self, self.runner.pipeline.lib, im.copy(), r.path, it.page, it.text)
            r.teach = [x for x in r.teach if x.id != it.id]

    def _undo(self):
        out = self.runner.pipeline.out_dir()
        rep = out / "report.csv"
        if rep.exists() and messagebox.askyesno(t("undo"), f"Delete everything created in\n{out}\n(originals are never touched)?"):
            messagebox.showinfo(t("undo"), str(undo_report(rep, out)))
            self.results = []

    def _poll(self):
        try:
            while True:
                ev = self.runner.events.get_nowait()
                k = ev[0]
                if k == "log":
                    self.log.insert("end", ev[1] + "\n")
                    self.log.see("end")
                elif k == "progress":
                    self.pb.configure(value=100.0 * ev[1] / max(ev[2], 1))
                    self.pb_l.configure(text=f"{ev[1]}/{ev[2]}  {ev[3]}")
                elif k == "analyzed":
                    self.results = ev[1]
                    self._fill_preview()
                    if self.step == 6:
                        self._after_analysis()
                elif k == "finished":
                    self.results = ev[1]
                    self._fill_preview()
                    self.runner.results = self.results
                    self._run_teach_queue()
                    done = sum(1 for r in self.results if r.status == "done")
                    messagebox.showinfo(t("done"), f"{done} file(s) saved to\n{self.runner.pipeline.out_dir()}")
                    self.results = []
                elif k == "review":
                    ev[3]["decision"] = ReviewDialog(self, ev[1]).result
                    ev[2].set()
                elif k == "password":
                    ev[3]["password"] = simpledialog.askstring("Password", f"Password for {Path(ev[1]).name} (cancel = skip)", show="*", parent=self)
                    ev[2].set()
                elif k == "error":
                    messagebox.showerror(t("error"), ev[1])
                elif k == "cancelled":
                    self.log.insert("end", "cancelled\n")
        except Exception as e:      # queue.Empty or UI errors: never kill the poll loop
            if type(e).__name__ != "Empty":
                self.log.insert("end", f"UI error: {e}\n")
        self.after(150, self._poll)

    def _library(self):
        win = tk.Toplevel(self)
        win.title("Handwriting & font library")
        lib = self.runner.pipeline.lib
        tv = ttk.Treeview(win, columns=("id", "unit", "kind", "profile", "label"), show="headings", height=14)
        for c, w in (("id", 50), ("unit", 60), ("kind", 100), ("profile", 130), ("label", 260)):
            tv.heading(c, text=c)
            tv.column(c, width=w)
        tv.pack(fill="both", expand=True, padx=6, pady=6)

        def fill():
            tv.delete(*tv.get_children())
            for s in lib.list_samples(limit=500):
                tv.insert("", "end", values=(s["id"], s["unit"], s["kind"], s["profile"], s["label"]))
        fill()
        row = ttk.Frame(win)
        row.pack(fill="x", padx=6, pady=6)

        def delete():
            for i in tv.selection():
                lib.delete_sample(int(tv.item(i)["values"][0]))
            fill()

        def edit():
            for i in tv.selection():
                sid = int(tv.item(i)["values"][0])
                n = simpledialog.askstring("Label", "Correct label:", initialvalue=str(tv.item(i)["values"][4]), parent=win)
                if n:
                    lib.update_label(sid, n)
            fill()

        def export():
            p = filedialog.asksaveasfilename(defaultextension=".zip", parent=win)
            if p:
                lib.export_zip(p)

        def imp():
            p = filedialog.askopenfilename(filetypes=[("zip", "*.zip")], parent=win)
            if p:
                messagebox.showinfo("Library", f"Imported {lib.import_zip(p)} samples", parent=win)
                fill()

        def teach_file():
            p = filedialog.askopenfilename(filetypes=[("PDF / images", "*.pdf *.jpg *.jpeg *.png *.bmp *.tif *.tiff *.webp"), ("All", "*.*")], parent=win)
            if p:
                try:
                    img = self.runner.pipeline.preview_image(p)
                except Exception as e:
                    messagebox.showerror(t("error"), str(e), parent=win)
                    return
                TeachDialog(win, lib, img, p, 0)
                fill()

        def dataset():
            d = FolderBrowser(win, "").result
            if d:
                messagebox.showinfo("Library", f"{lib.export_dataset(d, 'paddle')} samples exported (PaddleOCR format)", parent=win)

        for txt, cmd in ((t("teach_btn"), teach_file), ("Edit label", edit), ("Delete", delete), ("Export zip", export), ("Import zip", imp), ("Training set", dataset)):
            ttk.Button(row, text=txt, command=cmd).pack(side="left", padx=3, ipady=8)

    def _quit(self):
        self._store_forms()
        self.runner.cancel()
        self.destroy()


def main() -> int:
    multiprocessing.freeze_support()
    data = get_data_dir(None)
    setup_logging(data, console=False)
    App(data).mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
