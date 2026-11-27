"""DocNamer (Full) - PySide6 desktop GUI for Windows 10/11 x64."""
from __future__ import annotations

import multiprocessing
import sys
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QImage, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QColorDialog, QComboBox, QDialog,
                               QDoubleSpinBox, QFileDialog, QFormLayout, QGraphicsRectItem, QGridLayout, QTabWidget, QGraphicsScene,
                               QGraphicsTextItem, QGraphicsView, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
                               QLineEdit, QListWidget, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
                               QPushButton, QRadioButton, QScrollArea, QSpinBox, QStackedWidget, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from ..core import gui_common as gc
from ..core.env import data_dir as get_data_dir
from ..core.i18n import t
from ..core.logsetup import setup_logging
from ..core.report import undo_report
from ..core.review import Decision
from ..core.runner import Runner
from ..core.settings import Anchor, Settings, Template, Zone
from ..core.teach import TeachSession
from ..core.textutil import DEVANAGARI_KEYBOARD

STEP_KEYS = ["step_folder", "step_orient", "step_zones", "step_pages", "step_naming", "step_preview", "step_run"]


def pil2pix(im: Image.Image) -> QPixmap:
    im = im.convert("RGBA")
    buf = im.tobytes("raw", "RGBA")          # QImage does NOT copy: keep `buf` alive until after .copy()
    qi = QImage(buf, im.width, im.height, 4 * im.width, QImage.Format_RGBA8888)
    pm = QPixmap.fromImage(qi.copy())
    del buf
    return pm


def apply_theme(app: QApplication, dark: bool) -> None:
    app.setStyle("Fusion")
    p = QPalette()
    if dark:
        p.setColor(QPalette.Window, QColor(37, 37, 38))
        p.setColor(QPalette.WindowText, Qt.white)
        p.setColor(QPalette.Base, QColor(30, 30, 30))
        p.setColor(QPalette.AlternateBase, QColor(45, 45, 48))
        p.setColor(QPalette.Text, Qt.white)
        p.setColor(QPalette.Button, QColor(51, 51, 55))
        p.setColor(QPalette.ButtonText, Qt.white)
        p.setColor(QPalette.Highlight, QColor(10, 88, 202))
        p.setColor(QPalette.HighlightedText, Qt.white)
        p.setColor(QPalette.ToolTipBase, QColor(51, 51, 55))
        p.setColor(QPalette.ToolTipText, Qt.white)
    app.setPalette(p if dark else app.style().standardPalette())


# ============================================================================== schema-driven forms
class FormPage(QWidget):
    def __init__(self, fields):
        super().__init__()
        self.fields, self.get, self.set = fields, {}, {}
        lay = QFormLayout(self)
        lay.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        for f in fields:
            w, getter, setter = self._make(f)
            if f.tip:
                w.setToolTip(f.tip)
            box = QWidget()
            hb = QHBoxLayout(box)
            hb.setContentsMargins(0, 0, 0, 0)
            hb.addWidget(w, 1)
            if f.tip:
                tip = QLabel(f.tip)
                tip.setStyleSheet("color: gray;")
                tip.setWordWrap(True)
                hb.addWidget(tip, 1)
            lay.addRow(f.label, box)
            self.get[f.path], self.set[f.path] = getter, setter

    def _make(self, f):
        k = f.kind
        if k == "bool":
            w = QCheckBox()
            return w, w.isChecked, lambda v: w.setChecked(bool(v))
        if k in ("choice", "choiceedit"):
            w = QComboBox()
            w.setEditable(k == "choiceedit")
            w.addItems(f.extra)
            return w, w.currentText, lambda v: w.setCurrentText(str(v))
        if k == "int":
            w = QSpinBox()
            w.setRange(*f.extra)
            return w, w.value, lambda v: w.setValue(int(v))
        if k == "float":
            w = QDoubleSpinBox()
            w.setRange(*f.extra)
            w.setSingleStep(0.05)
            w.setDecimals(2)
            return w, w.value, lambda v: w.setValue(float(v))
        if k in ("dir", "color"):
            host = QWidget()
            hb = QHBoxLayout(host)
            hb.setContentsMargins(0, 0, 0, 0)
            le = QLineEdit()
            btn = QPushButton("Browse..." if k == "dir" else "Pick...")
            hb.addWidget(le, 1)
            hb.addWidget(btn)
            if k == "dir":
                btn.clicked.connect(lambda: le.setText(QFileDialog.getExistingDirectory(self, "Folder", le.text()) or le.text()))
            else:
                btn.clicked.connect(lambda: le.setText(QColorDialog.getColor(QColor(le.text() or "#E53935"), self).name().upper()))
            host.text = le.text
            return host, le.text, le.setText
        w = QLineEdit()
        return w, w.text, w.setText

    def load(self, st: Settings):
        for f in self.fields:
            v = gc.get_path(st, f.path)
            self.set[f.path](gc.to_text(v, "list") if f.kind == "list" else (v if f.kind in ("bool", "int", "float") else gc.to_text(v, f.kind)))

    def store(self, st: Settings):
        for f in self.fields:
            old = gc.get_path(st, f.path)
            raw = self.get[f.path]()
            gc.set_path(st, f.path, raw if f.kind in ("bool", "int", "float") else gc.from_text(str(raw), f.kind, old))


# ============================================================================== zone designer
class ZoneView(QGraphicsView):
    zoneDrawn = Signal(float, float, float, float)

    def __init__(self):
        super().__init__()
        self.sc = QGraphicsScene(self)
        self.setScene(self.sc)
        self.pix = None
        self.rubber = None
        self.origin = None
        self.overlays = []
        self.can_draw = True
        self.setBackgroundBrush(QBrush(QColor(90, 90, 90)))

    def set_image(self, im: Image.Image):
        self.sc.clear()
        self.overlays, self.rubber = [], None
        self.pix = self.sc.addPixmap(pil2pix(im))
        self.setSceneRect(self.pix.boundingRect())
        self.fitInView(self.pix, Qt.KeepAspectRatio)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.pix:
            self.fitInView(self.pix, Qt.KeepAspectRatio)

    def wheelEvent(self, e):
        if e.modifiers() & Qt.ControlModifier:
            self.scale(1.15 if e.angleDelta().y() > 0 else 1 / 1.15, 1.15 if e.angleDelta().y() > 0 else 1 / 1.15)
        else:
            super().wheelEvent(e)

    def mousePressEvent(self, e):
        if self.pix and self.can_draw and e.button() == Qt.LeftButton:
            self.origin = self.mapToScene(e.position().toPoint())
            self.rubber = self.sc.addRect(QRectF(self.origin, self.origin), QPen(QColor("#E53935"), 3, Qt.DashLine))
        else:
            super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self.rubber is not None:
            self.rubber.setRect(QRectF(self.origin, self.mapToScene(e.position().toPoint())).normalized())
        else:
            super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self.rubber is not None:
            r = self.rubber.rect()
            self.sc.removeItem(self.rubber)
            self.rubber = None
            W, H = self.pix.boundingRect().width(), self.pix.boundingRect().height()
            if r.width() > 6 and r.height() > 6:
                self.zoneDrawn.emit(max(0, r.x() / W), max(0, r.y() / H), min(1, r.width() / W), min(1, r.height() / H))
        else:
            super().mouseReleaseEvent(e)

    def show_overlays(self, zones, selected: int, boxes):
        for it in self.overlays:
            self.sc.removeItem(it)
        self.overlays = []
        if not self.pix:
            return
        W, H = self.pix.boundingRect().width(), self.pix.boundingRect().height()
        for i, z in enumerate(zones):
            col = QColor("#E53935" if i == selected else "#1E88E5")
            r = self.sc.addRect(QRectF(z.x * W, z.y * H, z.w * W, z.h * H), QPen(col, 3))
            lab = self.sc.addText(z.name)
            lab.setDefaultTextColor(col)
            lab.setPos(z.x * W, max(0, z.y * H - 24))
            self.overlays += [r, lab]
        for b in boxes:
            self.overlays.append(self.sc.addRect(QRectF(b[0], b[1], b[2] - b[0], b[3] - b[1]), QPen(QColor("#2E7D32"), 3)))


# ============================================================================== review dialog
class ReviewDialog(QDialog):
    def __init__(self, parent, item):
        super().__init__(parent)
        self.setWindowTitle(t("review_title"))
        self.result_decision = None
        self.item = item
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"<b>{Path(item.path).name}</b> &nbsp; page {item.page + 1} &nbsp; field: <b>{item.field}</b><br>"
                             f"{item.reason} &mdash; read as <i>{item.text!r}</i> ({item.conf:.0%})"))
        if item.crop_path and Path(item.crop_path).exists():
            with Image.open(item.crop_path) as im:
                im = im.copy()
            if item.kind == "field" and im.width < 420:
                k = 420 / im.width
                im = im.resize((int(im.width * k), int(im.height * k)))
            lab = QLabel()
            lab.setPixmap(pil2pix(im))
            lab.setAlignment(Qt.AlignCenter)
            sa = QScrollArea()
            sa.setWidget(lab)
            sa.setMinimumHeight(min(420, im.height + 20))
            lay.addWidget(sa)
        if item.kind == "orientation":
            row = QHBoxLayout()
            row.addWidget(QLabel("Rotate clockwise by:"))
            for a in ("0", "90", "180", "270"):
                b = QPushButton(f"{a}\u00b0")
                b.clicked.connect(lambda _=False, a=a: self._done(Decision("edit", a)))
                row.addWidget(b)
            lay.addLayout(row)
            lay.addWidget(QLabel(f"Suggested: {item.text}\u00b0"))
        else:
            for c in item.candidates:
                b = QPushButton(f"{c[0]}    ({c[2]} {c[1]:.0%})")
                b.clicked.connect(lambda _=False, c=c: self._done(self._edit(c[0])))
                lay.addWidget(b)
            self.txt = QLineEdit(item.text)
            self.txt.setStyleSheet("font-size: 16px;")
            lay.addWidget(self.txt)
            opt = QHBoxLayout()
            self.sim = QCheckBox(t("apply_similar"))
            self.lib = QCheckBox(t("save_library"))
            self.kind = QComboBox()
            self.kind.addItems(["handwriting", "font"])
            self.prof = QLineEdit("default")
            for w in (self.sim, self.lib, self.kind, self.prof):
                opt.addWidget(w)
            lay.addLayout(opt)
            row = QHBoxLayout()
            for txt, fn in ((t("accept"), lambda: Decision("accept")), (t("edit"), lambda: self._edit(self.txt.text())),
                            (t("skip_field"), lambda: Decision("skip")), (t("use_fallback"), lambda: Decision("fallback")),
                            (t("needs_manual"), lambda: Decision("manual"))):
                b = QPushButton(txt)
                b.clicked.connect(lambda _=False, fn=fn: self._done(fn()))
                row.addWidget(b)
            lay.addLayout(row)

    def _edit(self, text):
        return Decision("edit", text, self.sim.isChecked(), self.lib.isChecked(), self.kind.currentText(), self.prof.text())

    def _done(self, d):
        self.result_decision = d
        self.accept()

    @staticmethod
    def ask(parent, item):
        d = ReviewDialog(parent, item)
        d.exec()
        return d.result_decision



class TeachDialog(QDialog):
    """Asks, piece by piece, which shabd (word) / akshar (letter) the handwriting contains."""

    def __init__(self, parent, lib, img, source_doc: str = "", page: int = 0, hint_text: str = ""):
        super().__init__(parent)
        self.setWindowTitle(t("teach_title"))
        self.resize(760, 640)
        self.lib = lib
        self.sess = TeachSession(lib, img, source_doc, page, "default", "handwriting", "words", hint_text)
        self.saved = 0
        lay = QVBoxLayout(self)
        self.head = QLabel()
        self.head.setStyleSheet("font-weight: bold; font-size: 15px;")
        self.pic = QLabel()
        self.pic.setAlignment(Qt.AlignCenter)
        self.pic.setStyleSheet("background: white; border: 1px solid gray;")
        self.pic.setMinimumHeight(130)
        lay.addWidget(self.head)
        lay.addWidget(self.pic)
        lay.addWidget(QLabel(t("teach_ask")))
        self.edit = QLineEdit()
        self.edit.setStyleSheet("font-size: 22px;")
        self.edit.returnPressed.connect(self._save)
        lay.addWidget(self.edit)
        self.hint = QLabel()
        self.hint.setStyleSheet("color: gray;")
        lay.addWidget(self.hint)
        tabs = QTabWidget()
        for title, keys in DEVANAGARI_KEYBOARD.items():
            page_w = QWidget()
            grid = QGridLayout(page_w)
            for i, k in enumerate(keys):
                b = QPushButton(k)
                b.setMinimumHeight(34)
                b.clicked.connect(lambda _=False, k=k: (self.edit.insert(k), self.edit.setFocus()))
                grid.addWidget(b, i // 10, i % 10)
            tabs.addTab(page_w, title)
        lay.addWidget(tabs)
        opt = QHBoxLayout()
        self.prof = QComboBox()
        self.prof.setEditable(True)
        self.prof.addItems([p["name"] for p in lib.profiles()] or ["default"])
        self.prof.setCurrentText("default")
        self.kind = QComboBox()
        self.kind.addItems(["handwriting", "font"])
        for lab, w in ((t("teach_profile"), self.prof), (t("teach_kind"), self.kind)):
            opt.addWidget(QLabel(lab))
            opt.addWidget(w)
        lay.addLayout(opt)
        self.also = QCheckBox(t("teach_also"))
        self.also.setChecked(True)
        lay.addWidget(self.also)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #0a58ca;")
        lay.addWidget(self.status)
        row = QHBoxLayout()
        self.letters_b = QPushButton(t("teach_letters"))
        for txt, fn in ((t("teach_save"), self._save), (t("teach_skip"), self._skip), (t("back"), self._back),
                        (t("teach_letters"), self._letters), (t("teach_finish"), self.accept)):
            b = QPushButton(txt)
            b.clicked.connect(lambda _=False, fn=fn: fn())
            row.addWidget(b)
            if fn == self._letters:
                self.letters_b = b
        lay.addLayout(row)
        self._show()

    def _show(self):
        seg = self.sess.current()
        if seg is None:
            QMessageBox.information(self, t("teach_title"), f"{self.saved} sample(s) saved to the library.")
            self.accept()
            return
        k = max(1.0, 90 / max(seg.crop.size[1], 1))
        k = min(k, 700 / max(seg.crop.size[0], 1)) if seg.unit == "word" else min(k, 4.0)
        im = seg.crop.resize((max(1, int(seg.crop.size[0] * k)), max(1, int(seg.crop.size[1] * k))))
        self.pic.setPixmap(pil2pix(im))
        kind = t("teach_word") if seg.unit == "word" else t("teach_letter")
        self.head.setText(f"{self.sess.pos + 1} / {self.sess.total}    {kind}")
        self.edit.setText(seg.guess)
        self.hint.setText(f"guess: {seg.guess}  ({seg.guess_conf:.0%})" if seg.guess else "no guess yet - please type it")
        self.letters_b.setEnabled(seg.unit == "word")
        self.edit.setFocus()
        self.edit.selectAll()

    def _save(self):
        label = self.edit.text().strip()
        if not label:
            self.status.setText("Type what it says, or press Skip.")
            return
        self.sess.profile, self.sess.kind = self.prof.currentText().strip() or "default", self.kind.currentText()
        info = self.sess.save(label, self.also.isChecked())
        self.saved += info["saved"] + info["letters_saved"]
        msg = f"saved '{label}'" + (f" + {info['letters_saved']} letter(s)" if info["letters_saved"] else "")
        if info["letter_mismatch"]:
            msg += f"  (letters found: {info['letter_mismatch'][0]}, you typed {info['letter_mismatch'][1]} - use 'Teach this word's letters')"
        self.status.setText(msg)
        self._show()

    def _skip(self):
        self.sess.skip()
        self._show()

    def _back(self):
        self.sess.back()
        self._show()

    def _letters(self):
        n = self.sess.ask_letters_next()
        self.status.setText(f"{n} letter piece(s) will be asked right after this word." if n else "No separate letters were found in this word.")


# ============================================================================== main window
class MainWindow(QMainWindow):
    def __init__(self, data: Path, app: QApplication):
        super().__init__()
        self.setWindowTitle(t("app"))
        self.resize(1250, 800)
        self.app, self.data = app, data
        self.settings_path = data / "settings.json"
        self.st = Settings.load(self.settings_path)
        self.runner = Runner(self.st, data)
        self.sample_path, self.results, self.dark = "", [], False
        self._filling = False
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        self.steps_list = QListWidget()
        self.steps_list.setFixedWidth(170)
        for k in STEP_KEYS:
            self.steps_list.addItem(t(k))
        root.addWidget(self.steps_list)
        right = QVBoxLayout()
        root.addLayout(right, 1)
        self.stack = QStackedWidget()
        right.addWidget(self.stack, 1)
        S = gc.steps()
        self.forms = {k: FormPage(S[k]) for k in S}
        scroll = lambda w: (lambda sa: (sa.setWidget(w), sa.setWidgetResizable(True), sa)[2])(QScrollArea())
        for k, page in (("folder", 0), ("orientation", 1)):
            self.stack.addWidget(scroll(self.forms[k]))
        self.stack.addWidget(self._zones_page())
        for k in ("pages", "naming"):
            self.stack.addWidget(scroll(self.forms[k]))
        self.stack.addWidget(self._preview_page())
        self.stack.addWidget(self._run_page())
        nav = QHBoxLayout()
        self.back_b, self.next_b = QPushButton(t("back")), QPushButton(t("next"))
        lib_b, theme_b = QPushButton("Library..."), QPushButton("Dark / light")
        self.back_b.clicked.connect(lambda: self.go(self.stack.currentIndex() - 1))
        self.next_b.clicked.connect(lambda: self.go(self.stack.currentIndex() + 1))
        lib_b.clicked.connect(self._library)
        theme_b.clicked.connect(self._theme)
        for b in (self.back_b, lib_b, theme_b):
            nav.addWidget(b)
        nav.addStretch(1)
        nav.addWidget(self.next_b)
        right.addLayout(nav)
        self.steps_list.currentRowChanged.connect(self.go)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(150)
        self.go(0)

    # ---- navigation ------------------------------------------------------------------------------
    def go(self, n: int):
        if n < 0 or n >= self.stack.count():
            return
        self._store()
        if n <= 4 and self.results:
            self.results, self.runner.results = [], []
        self.stack.setCurrentIndex(n)
        self.steps_list.blockSignals(True)
        self.steps_list.setCurrentRow(n)
        self.steps_list.blockSignals(False)
        for f in self.forms.values():
            f.load(self.st)
        self.mode_btns[self.st.mode].setChecked(True)
        self._refresh_specs()
        self.back_b.setEnabled(n > 0)
        self.next_b.setEnabled(n < self.stack.count() - 1)

    def _store(self):
        for f in self.forms.values():
            f.store(self.st)
        self.st.auto_template = self.auto_tpl.isChecked()
        self.st.save(self.settings_path)
        pl = self.runner.pipeline
        pl.st = self.st
        o = self.st.ocr
        sig = (o.engine, o.languages, o.handwriting, o.handwriting_model, o.secondary, o.threads)
        if sig != getattr(self, "_sig", sig):
            from ..core.ocr import Engines
            pl.engines = Engines(o, self.data, library=pl.lib)
        self._sig = sig

    def _theme(self):
        self.dark = not self.dark
        apply_theme(self.app, self.dark)

    # ---- step 3 ---------------------------------------------------------------------------------------
    def _zones_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        top = QHBoxLayout()
        lay.addLayout(top)
        self.mode_btns = {}
        for txt, val in ((t("mode_zones"), "zones"), (t("mode_anchors"), "anchors"), (t("mode_full"), "full")):
            rb = QRadioButton(txt)
            rb.toggled.connect(lambda on, v=val: on and self._set_mode(v))
            self.mode_btns[val] = rb
            top.addWidget(rb)
        top.addStretch(1)
        row = QHBoxLayout()
        lay.addLayout(row)
        for txt, fn in (("Pick sample document...", self._pick_sample), ("Test on sample", self._test_sample),
                        (t("teach_btn"), self._teach_zone)):
            b = QPushButton(txt)
            b.clicked.connect(fn)
            row.addWidget(b)
        self.tpl_name = QLineEdit()
        self.tpl_name.setPlaceholderText("template name")
        row.addWidget(self.tpl_name)
        b = QPushButton("Save template")
        b.clicked.connect(self._save_tpl)
        row.addWidget(b)
        self.tpl_pick = QComboBox()
        self.tpl_pick.addItems(self.runner.pipeline.templates.names())
        row.addWidget(self.tpl_pick)
        b = QPushButton("Load")
        b.clicked.connect(self._load_tpl)
        row.addWidget(b)
        self.auto_tpl = QCheckBox("auto-match templates")
        self.auto_tpl.setChecked(self.st.auto_template)
        row.addWidget(self.auto_tpl)
        mid = QHBoxLayout()
        lay.addLayout(mid, 1)
        self.view = ZoneView()
        self.view.zoneDrawn.connect(self._zone_drawn)
        mid.addWidget(self.view, 3)
        side = QVBoxLayout()
        mid.addLayout(side, 2)
        self.spec_table = QTableWidget(0, 5)
        self.spec_table.setHorizontalHeaderLabels(["name", "kind", "fmt", "pos / keyword", ""])
        self.spec_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.spec_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.spec_table.itemSelectionChanged.connect(self._select_spec)
        side.addWidget(self.spec_table, 2)
        form = QFormLayout()
        side.addLayout(form)
        self.e = {k: QLineEdit() for k in ("name", "regex", "whitelist", "pages", "x", "y", "w", "h", "keyword")}
        self.e_kind, self.e_fmt, self.e_dir = QComboBox(), QComboBox(), QComboBox()
        self.e_kind.addItems(["auto", "printed", "handwritten"])
        self.e_fmt.addItems(["any", "date", "number", "letters"])
        self.e_dir.addItems(["right", "below", "offset"])
        self.e_req = QCheckBox("required")
        self.e_req.setChecked(True)
        for lab, w in (("name", self.e["name"]), ("kind", self.e_kind), ("format", self.e_fmt), ("regex", self.e["regex"]),
                       ("allowed chars", self.e["whitelist"]), ("pages (1,3-5)", self.e["pages"])):
            form.addRow(lab, w)
        self.geo_row = QWidget()
        g = QHBoxLayout(self.geo_row)
        g.setContentsMargins(0, 0, 0, 0)
        for k in "xywh":
            g.addWidget(QLabel(k + "%"))
            self.e[k].setMaximumWidth(60)
            g.addWidget(self.e[k])
        form.addRow("area %", self.geo_row)
        self.anc_row = QWidget()
        a = QHBoxLayout(self.anc_row)
        a.setContentsMargins(0, 0, 0, 0)
        a.addWidget(self.e["keyword"])
        a.addWidget(self.e_dir)
        form.addRow("keyword", self.anc_row)
        form.addRow("", self.e_req)
        bt = QHBoxLayout()
        side.addLayout(bt)
        for txt, fn in (("Add / update", self._upsert), ("Delete", self._delete_spec)):
            b = QPushButton(txt)
            b.clicked.connect(fn)
            bt.addWidget(b)
        self.test_out = QPlainTextEdit()
        self.test_out.setReadOnly(True)
        side.addWidget(self.test_out, 1)
        return page

    def _set_mode(self, m: str):
        self.st.mode = m
        zones = m == "zones"
        self.geo_row.setVisible(zones)
        self.anc_row.setVisible(m == "anchors")
        self.view.can_draw = zones
        self._refresh_specs()

    def _specs(self):
        return self.st.zones if self.st.mode == "zones" else self.st.anchors

    def _refresh_specs(self):
        self._filling = True
        self.spec_table.setRowCount(0)
        if self.st.mode != "full":
            for s in self._specs():
                r = self.spec_table.rowCount()
                self.spec_table.insertRow(r)
                pos = (f"{s.x:.0%},{s.y:.0%} {s.w:.0%}x{s.h:.0%}" if isinstance(s, Zone) else f"{s.keyword} \u2192 {s.direction}")
                for c, v in enumerate((s.name, s.kind, s.fmt, pos)):
                    self.spec_table.setItem(r, c, QTableWidgetItem(v))
        self._filling = False
        self.view.show_overlays(self.st.zones if self.st.mode == "zones" else [], self._sel_row(), [])

    def _sel_row(self) -> int:
        rows = self.spec_table.selectionModel().selectedRows() if self.spec_table.selectionModel() else []
        return rows[0].row() if rows else -1

    def _select_spec(self):
        if self._filling or self._sel_row() < 0:
            return
        s = self._specs()[self._sel_row()]
        for k in ("name", "regex", "whitelist", "pages"):
            self.e[k].setText(getattr(s, k))
        self.e_kind.setCurrentText(s.kind)
        self.e_fmt.setCurrentText(s.fmt)
        self.e_req.setChecked(s.required)
        if isinstance(s, Zone):
            for k in "xywh":
                self.e[k].setText(f"{getattr(s, k) * 100:.1f}")
        else:
            self.e["keyword"].setText(s.keyword)
            self.e_dir.setCurrentText(s.direction)
        self.view.show_overlays(self.st.zones if self.st.mode == "zones" else [], self._sel_row(), [])

    def _zone_drawn(self, x, y, w, h):
        for k, v in zip("xywh", (x, y, w, h)):
            self.e[k].setText(f"{v * 100:.1f}")
        is_new = not self.e["name"].text().strip()
        if is_new:
            name, ok = QInputDialog.getText(self, "Zone name", "Name for this field (e.g. invoice):")
            self.e["name"].setText(name if ok and name else f"field{len(self.st.zones) + 1}")
        self._upsert()
        if is_new:
            for k in ("name", "regex", "whitelist", "pages"):
                self.e[k].clear()

    def _upsert(self):
        name = self.e["name"].text().strip()
        if not name:
            QMessageBox.information(self, "Field", "Give the field a name (it becomes {name} in the pattern).")
            return
        common = dict(name=name, kind=self.e_kind.currentText(), fmt=self.e_fmt.currentText(), regex=self.e["regex"].text(),
                      whitelist=self.e["whitelist"].text(), pages=self.e["pages"].text(), required=self.e_req.isChecked())
        if self.st.mode == "zones":
            try:
                x, y, w, h = (float(self.e[k].text()) / 100 for k in "xywh")
            except ValueError:
                QMessageBox.information(self, "Zone", "Drag a rectangle on the page, or type x y w h in percent.")
                return
            spec, lst = Zone(x=x, y=y, w=w, h=h, **common), self.st.zones
        else:
            if not self.e["keyword"].text().strip():
                QMessageBox.information(self, "Anchor", "Type the keyword to look for, e.g. Invoice No")
                return
            spec, lst = Anchor(keyword=self.e["keyword"].text().strip(), direction=self.e_dir.currentText(), **common), self.st.anchors
        idx = next((i for i, s in enumerate(lst) if s.name == name), None)
        lst.append(spec) if idx is None else lst.__setitem__(idx, spec)
        self._refresh_specs()

    def _delete_spec(self):
        if self._sel_row() >= 0:
            del self._specs()[self._sel_row()]
            self._refresh_specs()

    def _pick_sample(self):
        self._store()
        p, _ = QFileDialog.getOpenFileName(self, "Sample document", self.st.output.input_dir,
                                           "PDF / images (*.pdf *.jpg *.jpeg *.png *.bmp *.tif *.tiff *.webp *.gif);;All (*.*)")
        if not p:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.view.set_image(self.runner.pipeline.preview_image(p))
            self.sample_path = p
            self._refresh_specs()
        except Exception as e:
            QMessageBox.critical(self, t("error"), str(e))
        finally:
            QApplication.restoreOverrideCursor()

    def _test_sample(self):
        if not self.sample_path:
            QMessageBox.information(self, "Test", "Pick a sample document first.")
            return
        self._store()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            out = self.runner.pipeline.try_extract(self.sample_path)
        except Exception as e:
            QMessageBox.critical(self, t("error"), str(e))
            return
        finally:
            QApplication.restoreOverrideCursor()
        lines, boxes = [], []
        for n, fv in out.items():
            lines.append(f"{n}: {fv.text!r}  conf {fv.conf:.0%}  {'OK' if fv.valid and fv.found else 'CHECK'}")
            boxes += fv.boxes or ([fv.box] if fv.box else [])
        self.test_out.setPlainText("\n".join(lines))
        self.view.show_overlays(self.st.zones if self.st.mode == "zones" else [], self._sel_row(), boxes)

    def _save_tpl(self):
        self._store()
        name = self.tpl_name.text().strip()
        if not name:
            QMessageBox.information(self, "Template", "Type a template name first.")
            return
        kw, _ = QInputDialog.getText(self, "Auto-match", "Keywords that identify this document type (comma separated, optional):")
        sig = []
        if self.view.pix is not None and not kw.strip() and self.sample_path:
            from ..core.templates import layout_signature
            sig = layout_signature(self.runner.pipeline.preview_image(self.sample_path))
        self.runner.pipeline.templates.save(Template(
            name=name, mode=self.st.mode, zones=self.st.zones, anchors=self.st.anchors, pattern=self.st.naming.pattern,
            fallback=self.st.naming.fallback, rules=self.st.rules.__dict__,
            match_keywords=[k.strip() for k in kw.split(",") if k.strip()], layout_sig=sig))
        self.tpl_pick.clear()
        self.tpl_pick.addItems(self.runner.pipeline.templates.names())

    def _load_tpl(self):
        n = self.tpl_pick.currentText()
        if n:
            self.st = self.runner.pipeline.templates.load(n).apply_to(self.st)
            self.runner.pipeline.st = self.st
            self.go(self.stack.currentIndex())

    # ---- step 6 / 7 ----------------------------------------------------------------------------------------
    def _preview_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        top = QHBoxLayout()
        b = QPushButton("Analyse now (nothing is copied)")
        b.clicked.connect(lambda: self._start("preview"))
        self.pv_info = QLabel("")
        top.addWidget(b)
        top.addWidget(self.pv_info, 1)
        lay.addLayout(top)
        self.pv = QTableWidget(0, 4)
        self.pv.setHorizontalHeaderLabels(["Original", "New name (double-click to edit)", "Status", "Note"])
        self.pv.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.pv.itemChanged.connect(self._name_edited)
        lay.addWidget(self.pv, 1)
        return page

    def _fill_preview(self):
        self._filling = True
        self.pv.setRowCount(0)
        for i, r in enumerate(self.results):
            self.pv.insertRow(i)
            seg = f" [part {r.segment}]" if r.segment > 0 else ""
            cells = [Path(r.path).name + seg, r.new_name, r.status, r.message]
            for c, v in enumerate(cells):
                it = QTableWidgetItem(v)
                if c != 1 or not r.new_name:
                    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                if r.status == "review":
                    it.setBackground(QColor(255, 243, 205))
                    it.setForeground(QColor(0, 0, 0))
                elif r.status in ("error", "encrypted"):
                    it.setBackground(QColor(248, 215, 218))
                    it.setForeground(QColor(0, 0, 0))
                self.pv.setItem(i, c, it)
        self._filling = False
        self.pv_info.setText(f"{len(self.results)} file(s), {sum(1 for r in self.results if r.status == 'review')} need review")

    def _name_edited(self, item):
        if self._filling or item.column() != 1:
            return
        r = self.results[item.row()]
        if item.text().strip():
            r.new_name, r.name_locked = item.text().strip(), True

    def _run_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        row = QHBoxLayout()
        self.start_b = QPushButton(t("start"))
        self.pause_b = QPushButton(t("pause"))
        cancel_b, undo_b = QPushButton(t("cancel")), QPushButton(t("undo"))
        self.start_b.clicked.connect(self._start_run)
        self.pause_b.clicked.connect(self._pause)
        cancel_b.clicked.connect(lambda: self.runner.cancel())
        undo_b.clicked.connect(self._undo)
        for b in (self.start_b, self.pause_b, cancel_b):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(undo_b)
        lay.addLayout(row)
        self.pb = QProgressBar()
        self.pb_l = QLabel("")
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        for w in (self.pb, self.pb_l, self.log):
            lay.addWidget(w)
        return page

    def _start(self, job: str):
        self._store()
        if not self.st.output.input_dir:
            QMessageBox.information(self, "Folder", "Choose the input folder in step 1.")
            return
        self.runner.pipeline.st = self.st
        self.runner.start(job)

    def _start_run(self):
        if self.runner.busy:
            return
        self._after_analysis() if self.results else self._start("analyze")

    def _pause(self):
        if self.pause_b.text() == t("pause"):
            self.runner.pause()
            self.pause_b.setText(t("resume"))
        else:
            self.runner.resume()
            self.pause_b.setText(t("pause"))

    def _after_analysis(self):
        self.runner.results = self.results
        for r, it in self.runner.pending_review():
            d = ReviewDialog.ask(self, it)
            if d:
                self.runner.pipeline.apply_review(r, it, d, self.results)
        self._run_teach_queue()
        self._fill_preview()
        self.runner.start("finish")

    # ---- teaching handwriting ---------------------------------------------------------------------------
    def _teach_zone(self):
        row = self._sel_row()
        if self.view.pix is None or self.st.mode != "zones" or row < 0:
            QMessageBox.information(self, t("teach_title"), t("teach_pick_zone"))
            return
        z = self.st.zones[row]
        img = self.runner.pipeline.preview_image(self.sample_path)
        W, H = img.size
        crop = img.crop((int(z.x * W), int(z.y * H), int((z.x + z.w) * W), int((z.y + z.h) * H)))
        TeachDialog(self, self.runner.pipeline.lib, crop, self.sample_path, 0).exec()

    def _run_teach_queue(self):
        items = self.runner.pending_teach()
        if not items or QMessageBox.question(
                self, t("teach_title"), f"{len(items)} handwritten crop(s) can be added to the library so future documents are "
                                        f"read automatically.\nTeach them now?") != QMessageBox.Yes:
            return
        for r, it in items:
            if it.crop_path and Path(it.crop_path).exists():
                with Image.open(it.crop_path) as im:
                    TeachDialog(self, self.runner.pipeline.lib, im.copy(), r.path, it.page, it.text).exec()
            r.teach = [x for x in r.teach if x.id != it.id]

    def _undo(self):
        out = self.runner.pipeline.out_dir()
        rep = out / "report.csv"
        if rep.exists() and QMessageBox.question(self, t("undo"), f"Delete everything created in\n{out}\n(originals are never touched)?") == QMessageBox.Yes:
            QMessageBox.information(self, t("undo"), str(undo_report(rep, out)))
            self.results = []

    def _poll(self):
        import queue
        while True:
            try:
                ev = self.runner.events.get_nowait()
            except queue.Empty:
                return
            k = ev[0]
            if k == "log":
                self.log.appendPlainText(ev[1])
            elif k == "progress":
                self.pb.setValue(int(100 * ev[1] / max(ev[2], 1)))
                self.pb_l.setText(f"{ev[1]}/{ev[2]}  {ev[3]}")
            elif k == "analyzed":
                self.results = ev[1]
                self._fill_preview()
                if self.stack.currentIndex() == 6:
                    self._after_analysis()
            elif k == "finished":
                self.results = ev[1]
                self.runner.results = self.results
                self._run_teach_queue()
                self._fill_preview()
                QMessageBox.information(self, t("done"), f"{sum(1 for r in self.results if r.status == 'done')} file(s) saved to\n{self.runner.pipeline.out_dir()}")
                self.results = []
            elif k == "review":
                ev[3]["decision"] = ReviewDialog.ask(self, ev[1])
                ev[2].set()
            elif k == "password":
                pw, ok = QInputDialog.getText(self, "Password", f"Password for {Path(ev[1]).name} (cancel = skip)", QLineEdit.Password)
                ev[3]["password"] = pw if ok else None
                ev[2].set()
            elif k == "error":
                QMessageBox.critical(self, t("error"), ev[1])
            elif k == "cancelled":
                self.log.appendPlainText("cancelled")

    # ---- library --------------------------------------------------------------------------------------------------
    def _library(self):
        lib = self.runner.pipeline.lib
        dlg = QDialog(self)
        dlg.setWindowTitle("Handwriting & font library")
        dlg.resize(760, 520)
        lay = QVBoxLayout(dlg)
        tb = QTableWidget(0, 5)
        tb.setHorizontalHeaderLabels(["id", "unit", "kind", "profile", "label"])
        tb.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        tb.setSelectionBehavior(QAbstractItemView.SelectRows)
        lay.addWidget(tb)

        def fill():
            tb.setRowCount(0)
            for s in lib.list_samples(limit=1000):
                r = tb.rowCount()
                tb.insertRow(r)
                for c, v in enumerate((s["id"], s["unit"], s["kind"], s["profile"], s["label"])):
                    tb.setItem(r, c, QTableWidgetItem(str(v)))

        def sel_ids():
            return [int(tb.item(i.row(), 0).text()) for i in tb.selectionModel().selectedRows()]

        def delete():
            for i in sel_ids():
                lib.delete_sample(i)
            fill()

        def edit():
            for i in sel_ids():
                cur = next((s["label"] for s in lib.list_samples(limit=10**6) if s["id"] == i), "")
                n, ok = QInputDialog.getText(dlg, "Label", "Correct label:", text=cur)
                if ok and n:
                    lib.update_label(i, n)
            fill()

        def export():
            p, _ = QFileDialog.getSaveFileName(dlg, "Export library", "library.zip", "zip (*.zip)")
            if p:
                lib.export_zip(p)

        def imp():
            p, _ = QFileDialog.getOpenFileName(dlg, "Import library", "", "zip (*.zip)")
            if p:
                QMessageBox.information(dlg, "Library", f"Imported {lib.import_zip(p)} samples")
                fill()

        def teach_file():
            p, _ = QFileDialog.getOpenFileName(dlg, t("teach_btn"), "", "PDF / images (*.pdf *.jpg *.jpeg *.png *.bmp *.tif *.tiff *.webp)")
            if p:
                try:
                    img = self.runner.pipeline.preview_image(p)
                except Exception as e:
                    QMessageBox.critical(dlg, t("error"), str(e))
                    return
                TeachDialog(dlg, lib, img, p, 0).exec()
                fill()

        def dataset(fmt):
            d = QFileDialog.getExistingDirectory(dlg, "Export training set to")
            if d:
                QMessageBox.information(dlg, "Library", f"{lib.export_dataset(d, fmt)} samples exported ({fmt})")

        row = QHBoxLayout()
        for txt, fn in ((t("teach_btn"), teach_file), ("Edit label", edit), ("Delete", delete), ("Export zip", export), ("Import zip", imp),
                        ("Training set (PaddleOCR)", lambda: dataset("paddle")), ("Training set (TrOCR)", lambda: dataset("trocr"))):
            b = QPushButton(txt)
            b.clicked.connect(fn)
            row.addWidget(b)
        lay.addLayout(row)
        fill()
        dlg.exec()

    def closeEvent(self, e):
        self._store()
        self.runner.cancel()
        super().closeEvent(e)


def main() -> int:
    multiprocessing.freeze_support()
    data = get_data_dir(None)
    setup_logging(data, console=False)
    app = QApplication(sys.argv)
    w = MainWindow(data, app)
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
