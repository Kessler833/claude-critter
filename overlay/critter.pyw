"""Claude-Critter v2: lebt im Claude-Fenster, läuft auf dessen UI-Kanten.

Keine Tokens: liest nur ~/.claude/critter/state.json (vom critter-Plugin) und
den Bildschirminhalt des Claude-Fensters (Kanten = Plattformen). Redet nicht,
zeigt nur Augen, Arme, Körper und kleine Symbole.
"""

import ctypes
import ctypes.wintypes as wt
import json
import math
import os
import random
import socket
import sys
import threading
import time
import tkinter as tk
import re as _re


try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

_lock = socket.socket()
_DIR = os.path.join(os.path.expanduser("~"), ".claude", "critter")
try:
    _lock.bind(("127.0.0.1", 47613))  # nur ein Overlay gleichzeitig
except OSError:
    # läuft schon eins – hängt es (keine Lebenszeichen seit 15 s), wird es ersetzt
    try:
        stale = time.time() - os.path.getmtime(os.path.join(_DIR, "perf.json")) > 15
        old = int(open(os.path.join(_DIR, "overlay.pid")).read().strip())
    except Exception:
        stale, old = False, 0
    if not (stale and old):
        sys.exit(0)
    os.system("taskkill /F /PID %d >NUL 2>&1" % old)
    time.sleep(1.0)
    try:
        _lock.bind(("127.0.0.1", 47613))
    except OSError:
        sys.exit(0)
try:
    os.makedirs(_DIR, exist_ok=True)
    open(os.path.join(_DIR, "overlay.pid"), "w").write(str(os.getpid()))
except Exception:
    pass

DIR = os.path.join(os.path.expanduser("~"), ".claude", "critter")
STATE = os.path.join(DIR, "state.json")
SETTINGS = os.path.join(DIR, "settings.json")
os.makedirs(DIR, exist_ok=True)
user32 = ctypes.windll.user32

KEY = "#ff00fe"
BODY = "#D97757"
BODY_DARK = "#b85f42"
EYE = "#141414"
MODEL_SCALE = {"opus": 1.0, "sonnet": 0.72, "haiku": 0.5}

DEFAULTS = {"scale": 5.0, "shine": "none"}
try:
    settings = {**DEFAULTS, **json.load(open(SETTINGS, encoding="utf-8"))}
except Exception:
    settings = dict(DEFAULTS)


def save_settings():
    try:
        json.dump(settings, open(SETTINGS, "w", encoding="utf-8"))
    except Exception:
        pass


def log_err(msg=None):
    """Fehler protokollieren (das Overlay hat keine Konsole) – Datei bleibt klein."""
    import traceback
    try:
        f = os.path.join(DIR, "overlay.err")
        if os.path.exists(f) and os.path.getsize(f) > 200000:
            os.remove(f)
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + (msg or traceback.format_exc()) + "\n")
    except Exception:
        pass


def restart_self():
    """Hängt das Overlay, startet es sich selbst neu (ohne Konsolenfenster)."""
    import subprocess
    log_err("watchdog: restart")
    try:
        _lock.close()
    except Exception:
        pass
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__)], creationflags=0x00000008 | 0x00000200, close_fds=True)
    finally:
        os._exit(0)


def lerp(a, b, t):
    return a + (b - a) * t


# ------------------------------------------------------------------ Claude-Fenster
class RECT(ctypes.Structure):
    _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long), ("r", ctypes.c_long), ("b", ctypes.c_long)]


EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)


def find_claude():
    found = []

    def cb(h, _):
        if user32.IsWindowVisible(h) and user32.GetWindowTextLengthW(h):
            buf = ctypes.create_unicode_buffer(64)
            user32.GetWindowTextW(h, buf, 64)
            if buf.value == "Claude":
                found.append(h)
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return found[0] if found else None


def client_rect(h):
    r = RECT()
    user32.GetClientRect(h, ctypes.byref(r))
    p = wt.POINT(0, 0)
    user32.ClientToScreen(h, ctypes.byref(p))
    return p.x, p.y, p.x + r.r, p.y + r.b


def cursor():
    p = wt.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


STATUS_KINDS = (("Etwas ist schiefgelaufen", "error"), ("Warte auf Eingabe", "waiting"), ("Ungelesene Antwort", "unread"))
BLOCK_TYPES = (50020, 50008, 50036, 50026)  # Text, List, Table, Group
POPUP_HINTS = ("popover", "dropdown", "menu-content", "dialog", "tooltip", "z-50", "z-popover", "radix")


class UIScanner:
    """Liest das Claude-Fenster über die Windows-Bedienungshilfen (UI Automation) – kein Screenshot.

    box()   -> Eingabefeld-Box
    live()  -> (Text im Eingabefeld, Cursor-Rechteck) – sehr billig, läuft ~20x/s
    world() -> Sidebar-Einträge mit Status, Nachrichten-Blöcke, Seitenpanels, Leiste über dem Eingabefeld
    """

    def __init__(self):
        self.ok = False
        self.edit = None
        self.vp = None
        self.tp = None
        try:
            sys.coinit_flags = 0  # COINIT_MULTITHREADED: sonst können UIA-Aufrufe in andere Apps hängen
            import comtypes
            import comtypes.client
            try:
                comtypes.CoInitializeEx(0)
            except OSError:
                pass
            comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient as U
            self.U = U
            self.uia = comtypes.CoCreateInstance(U.CUIAutomation._reg_clsid_, interface=U.IUIAutomation)
            self.cond = self.uia.CreatePropertyCondition(30003, 50004)  # ControlType = Edit
            self.walker = self.uia.RawViewWalker  # inkl. reiner Layout-Container (die Box selbst)
            self.cache = self.uia.CreateCacheRequest()
            for prop in (30001, 30003, 30005, 30012):  # Rechteck, Typ, Name, Klasse
                self.cache.AddProperty(prop)
            self.cache.TreeScope = 7
            self.cache.AutomationElementMode = 1
            self.ok = True
        except Exception:
            pass

    def _find_edit(self, hwnd):
        root = self.uia.ElementFromHandle(hwnd)
        found = root.FindAll(4, self.cond)
        for i in range(found.Length):
            el = found.GetElement(i)
            if el.CurrentName == "Prompt":
                self.edit = el
                self.vp = self.tp = None
                try:
                    self.vp = el.GetCurrentPattern(10002).QueryInterface(self.U.IUIAutomationValuePattern)
                    self.tp = el.GetCurrentPattern(10014).QueryInterface(self.U.IUIAutomationTextPattern)
                except Exception:
                    pass
                return

    def box(self, hwnd):
        """-> (left, top, right, bottom) der Eingabefeld-Box in Bildschirmkoordinaten oder None."""
        if not self.ok:
            return None
        try:
            if self.edit is None:
                self._find_edit(hwnd)
                if self.edit is None:
                    return None
            r = self.edit.CurrentBoundingRectangle
            if r.right - r.left < 50:
                self.edit = None
                return None
            best = (r.left, r.top, r.right, r.bottom)
            # nach oben laufen bis zur sichtbaren Box (CSS-Klasse "bg-surface-3", umfasst auch Bild-Anhänge)
            el = self.edit
            for _ in range(8):
                el = self.walker.GetParentElement(el)
                if el is None:
                    break
                pr = el.CurrentBoundingRectangle
                if "bg-surface-3" in (el.CurrentClassName or ""):
                    return (pr.left, pr.top, pr.right, pr.bottom)
                if pr.right - pr.left > (r.right - r.left) + 140 or pr.bottom - pr.top > 700:
                    break
                best = (pr.left, pr.top, pr.right, pr.bottom)
            return best
        except Exception:
            self.edit = None  # Element veraltet -> nächstes Mal neu suchen
            return None

    def live(self):
        """-> (text, caret); caret = (x, top, bottom) in Bildschirmkoordinaten oder None. (None, None) wenn nicht lesbar."""
        if self.vp is None:
            return None, None
        try:
            text = self.vp.CurrentValue or ""
        except Exception:
            self.edit = self.vp = self.tp = None
            return None, None
        if text == "\n":
            text = ""
        caret = None
        try:
            sel = self.tp.GetSelection()
            if sel.Length:
                rg = sel.GetElement(0)
                rects = rg.GetBoundingRectangles()
                if len(rects) >= 4 and rects[3] > 2:
                    caret = (rects[0], rects[1], rects[1] + rects[3])
                else:
                    # leere Auswahl = Cursor: ein Zeichen daneben vermessen
                    r2 = rg.Clone()
                    if r2.MoveEndpointByUnit(1, 0, 1):  # Ende 1 Zeichen nach rechts
                        rc = r2.GetBoundingRectangles()
                        if len(rc) >= 4 and rc[3] > 2:
                            caret = (rc[0], rc[1], rc[1] + rc[3])
                    if caret is None:
                        r2 = rg.Clone()
                        if r2.MoveEndpointByUnit(0, 0, -1):  # Anfang 1 Zeichen nach links
                            rc = r2.GetBoundingRectangles()
                            if len(rc) >= 4 and rc[3] > 2:
                                caret = (rc[-4] + rc[-2], rc[-3], rc[-3] + rc[-1])
        except Exception:
            pass
        return text, caret

    def world(self, hwnd):
        """Ein Rundgang durch den (gecachten) Baum: ~50 ms, läuft im Hintergrund."""
        if not self.ok:
            return None
        root = self.uia.ElementFromHandleBuildCache(hwnd, self.cache)
        self.text_els = {}
        out = {"rows": [], "sidebar": None, "panes": [], "blocks": [], "band": None, "view": None,
               "last_msg": None, "streaming": None, "pane_blocks": [], "popups": [], "anch": {}, "texts": [], "send": None}

        def R(el):
            r = el.CachedBoundingRectangle
            return (r.left, r.top, r.right, r.bottom)

        def kids(el):
            k = el.GetCachedChildren()
            return [k.GetElement(i) for i in range(k.Length)] if k else []

        def blocks_in(el, sink, limit, depth=0):
            for k in kids(el):
                if len(sink) >= limit or depth > 6:
                    return
                l, t, r, b = R(k)
                ct = k.CachedControlType
                cls = k.CachedClassName or ""
                if r - l >= 150 and b - t >= 18 and ct in BLOCK_TYPES and (
                        ct != 50026 or any(w in cls for w in ("code", "pre", "rounded", "border"))):
                    sink.append((l, t, r, b))
                    continue
                blocks_in(k, sink, limit, depth + 1)

        def visit(el, depth):
            if depth > 40:
                return
            cls = el.CachedClassName or ""
            name = el.CachedName or ""
            ct = el.CachedControlType
            if depth > 8 and (ct in (50009, 50022, 50032, 50033) or any(w in cls for w in POPUP_HINTS)):
                l, t, r, b = R(el)
                if 90 < r - l < 900 and 30 < b - t < 900:
                    out["popups"].append((l, t, r, b))
                    return
            if "dframe-sidebar" in cls:
                out["sidebar"] = R(el)
                for k in self._all(el):
                    if k.CachedControlType == 50000:
                        n = k.CachedName or ""
                        kind = next((kd for pre, kd in STATUS_KINDS if n.startswith(pre)), "")
                        out["rows"].append((R(k), kind, n))
                        out["anch"]["r:" + n] = (R(k), k)
                return
            if "dframe-pane" in cls and "dframe-pane-primary" not in cls:
                out["panes"].append(R(el))
                blocks_in(el, out["pane_blocks"], 16)
                return
            if ct == 50026 and "overflow-y-auto" in cls and out["view"] is None:
                l, t, r, b = R(el)
                if r - l > 500 and b - t > 300:
                    out["view"] = (l, t, r, b)
            if ct == 50026 and name.startswith("Nachricht"):
                l, t, r, b = R(el)
                if b > t:
                    if not name.startswith("Nachricht wird"):  # gestreamte Nachricht wechselt den Namen: kein Anker
                        out["anch"]["m:" + name] = ((l, t, r, b), el)
                    out["last_msg"] = (l, t, r, b)
                    if name.startswith("Nachricht wird gestreamt"):
                        out["streaming"] = (l, t, r, b)
                    blocks_in(el, out["blocks"], 60)
                    for k in self._all(el):
                        if k.CachedControlType == 50020 and len(k.CachedName or "") > 25 and len(out["texts"]) < 40:
                            out["texts"].append((R(k), k.CachedName))
                            self.text_els[k.CachedName] = k
                return
            if ct == 50000 and out["send"] is None and any(w in name.lower() for w in ("senden", "send", "absenden", "submit")):
                out["send"] = R(el)
            if ct == 50026 and "pointer-events-auto" in cls and "isolate" in cls:
                out["band"] = R(el)
            for k in kids(el):
                visit(k, depth + 1)

        visit(root, 0)
        return out

    def word_rect(self, hwnd, text, word):
        """Bildschirm-Rechteck eines Wortes in einem Chat-Absatz (TextPattern des Dokuments)."""
        el = getattr(self, "text_els", {}).get(text)
        if el is None:
            return None
        if getattr(self, "doc_tp", None) is None:
            root = self.uia.ElementFromHandle(hwnd)
            docs = root.FindAll(4, self.uia.CreatePropertyCondition(30003, 50030))
            for i in range(docs.Length):
                d = docs.GetElement(i)
                if "Claude" in (d.CurrentName or ""):
                    try:
                        self.doc_tp = d.GetCurrentPattern(10014).QueryInterface(self.U.IUIAutomationTextPattern)
                        break
                    except Exception:
                        pass
            if getattr(self, "doc_tp", None) is None:
                return None
        rng = self.doc_tp.RangeFromChild(el)
        hit = rng.FindText(word, 0, 0)
        if not hit:
            return None
        rc = hit.GetBoundingRectangles()
        if len(rc) < 4 or rc[2] <= 0:
            return None
        return (rc[0], rc[1], rc[0] + rc[2], rc[1] + rc[3])

    def _all(self, el):
        st = [el]
        res = []
        while st:
            e = st.pop()
            k = e.GetCachedChildren()
            if k:
                for i in range(k.Length):
                    c = k.GetElement(i)
                    res.append(c)
                    st.append(c)
        return res


def gear(cv, cx, cy, r, teeth, ang, color):
    """Zahnrad für die Denk-Animation."""
    pts = []
    step = math.pi / teeth
    for i in range(teeth * 2):
        a = ang + i * step
        rr = r if i % 2 == 0 else r * 0.72
        pts += [cx + math.cos(a - step * 0.35) * rr, cy + math.sin(a - step * 0.35) * rr,
                cx + math.cos(a + step * 0.35) * rr, cy + math.sin(a + step * 0.35) * rr]
    cv.create_polygon(pts, fill=color, outline="")
    cv.create_oval(cx - r * 0.28, cy - r * 0.28, cx + r * 0.28, cy + r * 0.28, fill=KEY, outline="")


def layout_ledges(W, H):
    """Plattformen rein aus der Fenstergröße: Das Eingabefeld der Claude-App sitzt unten
    mittig in der Inhaltsspalte (Seitenleiste ~446 px, Spalte max. ~940 px breit)."""
    sidebar = 446 if W > 1300 else 0
    center = (sidebar + W) / 2
    half = min(470, (W - sidebar) / 2 - 40)
    comp_y = H - 94
    return [(int(center - half), int(center + half), int(comp_y))]


# ------------------------------------------------------------------ große Aktivitäts-Icons (gut lesbar, auch wenn das Tierchen klein ist)
ICON_KINDS = ("pencil", "book", "hammer", "magnifier", "coffee", "binoculars", "broom", "sign", "push", "ball", "flag", "flower", "mark")


def draw_badge(cv, kind, cx, cy, t, r=19):
    """Runde Sprechblase mit animiertem Icon; (cx, cy) = Mitte der Blase."""
    cv.create_polygon(cx - 6, cy + r - 3, cx + 2, cy + r - 3, cx - 9, cy + r + 8, fill="#fffdf8", outline="")
    cv.create_oval(cx - r - 2, cy - r - 2, cx + r + 2, cy + r + 2, fill="#D97757", outline="")
    cv.create_oval(cx - r, cy - r, cx + r, cy + r, fill="#fffdf8", outline="")
    s = r / 19.0
    ink, acc = "#3f3f46", "#D97757"
    if kind == "pencil":
        # kritzelt: Stift fährt hin und her, Linie wächst
        n = int((t * 6) % 6) + 1
        pts = []
        for i in range(n + 1):
            pts += [cx - 10 * s + i * 3.4 * s, cy + 7 * s + (2.5 * s if i % 2 else -2.5 * s)]
        if len(pts) >= 4:
            cv.create_line(*pts, fill=acc, width=max(2, int(2 * s)))
        px, py = pts[-2], pts[-1]
        cv.create_polygon(px, py, px + 3 * s, py - 4 * s, px + 13 * s, py - 14 * s, px + 10 * s, py - 17 * s,
                          px - 0 * s, py - 7 * s, fill="#facc15", outline=ink)
        cv.create_polygon(px + 10 * s, py - 17 * s, px + 13 * s, py - 14 * s, px + 15 * s, py - 16 * s, px + 12 * s, py - 19 * s,
                          fill="#f472b6", outline=ink)
        cv.create_line(px, py, px + 1.5 * s, py - 2 * s, fill=ink, width=2)
    elif kind == "book":
        flip = (t * 1.5) % 1
        cv.create_polygon(cx, cy - 7 * s, cx - 13 * s, cy - 10 * s, cx - 13 * s, cy + 9 * s, cx, cy + 12 * s, fill="#fef3c7", outline=ink)
        cv.create_polygon(cx, cy - 7 * s, cx + 13 * s, cy - 10 * s, cx + 13 * s, cy + 9 * s, cx, cy + 12 * s, fill="#fef3c7", outline=ink)
        for i in range(3):
            yy = cy - 3 * s + i * 4 * s
            cv.create_line(cx - 10 * s, yy, cx - 3 * s, yy + 1 * s, fill="#a8a29e", width=1)
            cv.create_line(cx + 3 * s, yy + 1 * s, cx + 10 * s, yy, fill="#a8a29e", width=1)
        fx = cx + 13 * s * math.cos(flip * math.pi)  # Seite blättert um
        cv.create_polygon(cx, cy - 7 * s, fx, cy - 10 * s - 3 * s * math.sin(flip * math.pi), fx, cy + 9 * s, cx, cy + 12 * s,
                          fill="#fffbeb", outline=ink)
    elif kind == "hammer":
        k = abs(math.sin(t * 9))
        ang = math.radians(-60 + 80 * k)
        hx, hy = cx - 9 * s, cy + 10 * s
        ex, ey = hx + math.cos(ang) * 17 * s, hy - math.sin(-ang) * 17 * s
        cv.create_line(hx, hy, ex, ey, fill="#92400e", width=max(3, int(3.5 * s)))
        dx, dy = math.cos(ang), math.sin(ang)
        px_, py_ = -dy, dx
        w, h = 7 * s, 4 * s
        cv.create_polygon(ex + px_ * w + dx * h, ey + py_ * w + dy * h, ex - px_ * w + dx * h, ey - py_ * w + dy * h,
                          ex - px_ * w - dx * h, ey - py_ * w - dy * h, ex + px_ * w - dx * h, ey + py_ * w - dy * h,
                          fill="#64748b", outline=ink)
        if k > 0.9:
            for a_ in (-0.6, 0, 0.6):
                cv.create_line(cx + 8 * s, cy + 4 * s, cx + 8 * s + math.cos(a_ - 1.57) * 7 * s, cy + 4 * s + math.sin(a_ - 1.57) * 7 * s,
                               fill="#f59e0b", width=2)
    elif kind == "magnifier":
        ox, oy = math.cos(t * 3) * 3 * s, math.sin(t * 3) * 3 * s
        x0, y0 = cx - 3 * s + ox, cy - 3 * s + oy
        cv.create_line(x0 + 6 * s, y0 + 6 * s, x0 + 13 * s, y0 + 13 * s, fill=ink, width=max(3, int(4 * s)))
        cv.create_oval(x0 - 8 * s, y0 - 8 * s, x0 + 8 * s, y0 + 8 * s, fill="#dbeafe", outline=ink, width=max(2, int(2.5 * s)))
        cv.create_arc(x0 - 5 * s, y0 - 5 * s, x0 + 5 * s, y0 + 5 * s, start=100, extent=70, style="arc", outline="#ffffff", width=2)
    elif kind == "coffee":
        cv.create_rectangle(cx - 9 * s, cy - 3 * s, cx + 6 * s, cy + 11 * s, fill="#fffdf8", outline=ink, width=2)
        cv.create_rectangle(cx - 8 * s, cy - 1 * s, cx + 5 * s, cy + 2 * s, fill="#78350f", outline="")
        cv.create_arc(cx + 2 * s, cy, cx + 12 * s, cy + 8 * s, start=-90, extent=180, style="arc", outline=ink, width=2)
        for i in (-4, 1):
            w_ = math.sin(t * 4 + i) * 2 * s
            cv.create_line(cx + i * s, cy - 6 * s, cx + i * s + w_, cy - 10 * s, cx + i * s - w_, cy - 14 * s, smooth=True,
                           fill="#a8a29e", width=2)
    elif kind == "binoculars":
        sw = math.sin(t * 2) * 3 * s
        for dx in (-6, 6):
            cv.create_oval(cx + dx * s - 6 * s + sw, cy - 4 * s, cx + dx * s + 6 * s + sw, cy + 8 * s, fill="#1e293b", outline=ink)
            cv.create_oval(cx + dx * s - 3 * s + sw, cy - 1 * s, cx + dx * s + 3 * s + sw, cy + 5 * s, fill="#93c5fd", outline="")
        cv.create_rectangle(cx - 2 * s + sw, cy - 7 * s, cx + 2 * s + sw, cy + 2 * s, fill="#334155", outline="")
    elif kind == "broom":
        a_ = math.sin(t * 7) * 0.4
        bx, by = cx + math.sin(a_) * 10 * s, cy + 9 * s
        cv.create_line(cx + 6 * s, cy - 13 * s, bx, by - 4 * s, fill="#92400e", width=max(2, int(3 * s)))
        cv.create_polygon(bx - 7 * s, by - 4 * s, bx + 7 * s, by - 4 * s, bx + 9 * s, by + 4 * s, bx - 9 * s, by + 4 * s,
                          fill="#fbbf24", outline="#b45309")
        for i in range(3):
            cv.create_oval(bx - 12 * s - i * 3 * s, by + 1 * s - i * 2 * s, bx - 9 * s - i * 3 * s, by + 4 * s - i * 2 * s, fill="#d6d3d1", outline="")
    elif kind in ("sign", "push"):
        txt = "?" if kind == "sign" else "↑"
        bob = math.sin(t * 6) * 1.5 * s
        cv.create_text(cx, cy + bob, text=txt, fill=acc, font=("Segoe UI", int(19 * s), "bold"))
    elif kind == "ball":
        for i in range(6):
            cv.create_arc(cx - 11 * s, cy - 11 * s, cx + 11 * s, cy + 11 * s, start=-t * 200 + i * 60, extent=60,
                          fill=acc if i % 2 == 0 else "#fffdf8", outline="")
        cv.create_oval(cx - 11 * s, cy - 11 * s, cx + 11 * s, cy + 11 * s, outline=ink, width=2)
    elif kind == "flag":
        cv.create_line(cx - 6 * s, cy + 12 * s, cx - 6 * s, cy - 12 * s, fill=ink, width=2)
        w_ = math.sin(t * 5) * 2 * s
        cv.create_polygon(cx - 6 * s, cy - 12 * s, cx + 10 * s, cy - 6 * s + w_, cx - 6 * s, cy, fill=acc, outline="")
    elif kind == "flower":
        for i in range(5):
            a_ = i * 1.2566 + t
            cv.create_oval(cx + math.cos(a_) * 6 * s - 4.5 * s, cy - 3 * s + math.sin(a_) * 6 * s - 4.5 * s,
                           cx + math.cos(a_) * 6 * s + 4.5 * s, cy - 3 * s + math.sin(a_) * 6 * s + 4.5 * s, fill="#f472b6", outline="")
        cv.create_oval(cx - 3.5 * s, cy - 6.5 * s, cx + 3.5 * s, cy + 0.5 * s, fill="#facc15", outline="")
        cv.create_line(cx, cy + 4 * s, cx, cy + 13 * s, fill="#16a34a", width=2)
    elif kind == "mark":
        cv.create_text(cx, cy - 4 * s, text="Aa", fill=ink, font=("Segoe UI", int(10 * s), "bold"))
        prog = (t * 1.5) % 1
        cv.create_line(cx - 11 * s, cy + 7 * s, cx - 11 * s + 22 * s * prog, cy + 7 * s, fill="#f59e0b", width=max(3, int(4 * s)))


# ------------------------------------------------------------------ Pixel-Sprites für Requisiten
PAL = {"K": "#1e293b", "W": "#fffdf8", "s": "#a8a29e", "O": "#D97757", "o": "#b85f42", "G": "#64748b", "g": "#94a3b8",
       "L": "#cbd5e1", "C": "#78350f", "Y": "#facc15", "y": "#fbbf24", "P": "#f472b6", "n": "#92400e", "b": "#93c5fd"}
BOOK = [".KKKKKKK.KKKKKKK.",
        "KWWWWWWWKWWWWWWWK",
        "KWsssWWWKWsssssWK",
        "KWWWWWWWKWWWWWWWK",
        "KWssssWWKWssssWWK",
        "KWWWWWWWKWWWWWWWK",
        "KWsssssWKWsssWWWK",
        "KWWWWWWWKWWWWWWWK",
        "KOOOOOOOKOOOOOOOK",
        ".KKKKKKKKKKKKKKK."]
ANVIL = ["...LLLLLLLLLLL....",
         "..KGGGGGGGGGGGGGgg",
         "..KGGGGGGGGGGGGgg.",
         "...KKGGGGGGGKK....",
         ".....KGGGGGK......",
         ".....KGGGGGK......",
         "....KKGGGGGKK.....",
         "...KKKKKKKKKKK...."]
NOTE = [".KsKsKsKsKK.",
        "KWWWWWWWWWWK",
        "KWWWWWWWWWWK",
        "KWWWWWWWWWWK",
        "KWWWWWWWWWWK",
        "KWWWWWWWWWWK",
        "KWWWWWWWWWWK",
        "KWWWWWWWWWWK",
        ".KKKKKKKKKK."]
CUP = ["KKKKKK..",
       "KCCCCKKK",
       "KWWWWK.K",
       "KWWWWK.K",
       "KWWWWKKK",
       ".KWWK...",
       "..KK...."]
QMARK = ["OOO", "..O", ".OO", "...", ".O."]


# ------------------------------------------------------------------ ein Tierchen
class Critter:
    def __init__(self, app, x, y, size=1.0, mini=None):
        self.app = app
        self.x, self.y = x, y  # Fußmitte, relativ zum Claude-Fenster
        self.vx = self.vy = 0.0
        self.size = size
        self.mini = mini  # Agent-ID bei Mini-Tierchen
        self.facing = 1
        self.state = "fall"
        self.until = 0.0
        self.target = None
        self.ledge = None
        # weich animierte Pose
        self.eye = [0.0, 0.0]
        self.eye_to = [0.0, 0.0]
        self.eye_h = 1.0  # 1 offen, 0 zu
        self.eye_h_to = 1.0
        self.eye_kind = "normal"
        self.arm = [0.0, 0.0]  # -1 oben, 0 seitlich, 1 unten (links, rechts)
        self.arm_to = [0.0, 0.0]
        self.squash = 0.0
        self.bob = 0.0
        self.t = random.random() * 10
        self.next_blink = time.time() + random.uniform(1.5, 4)
        self.emotes = []
        self.leaving = None
        self.walk_phase = 0.0
        self.spawned = time.time()
        self.fly = None
        self.hover_y = 0.0
        self.point = None  # (tx, ty, bis)

    @property
    def u(self):
        return settings["scale"] * self.size * getattr(self, "vis", 1.0)

    @property
    def w(self):
        return 12 * self.u

    @property
    def h(self):
        return 9 * self.u

    def set(self, st, secs=None):
        if st != "portal" and getattr(self, "portal", None):
            self.abort_portal()
        if st == "sleep":
            self.target = None
            self.fly = None
        self.state = st
        self.until = time.time() + secs if secs else 0

    def emote(self, sym, color="#ec4899", secs=1.4):
        if self.state == "sleep":
            return  # schläft: keine Symbole über dem Kopf
        now = time.time()
        live = [e for e in self.emotes if now - e["t0"] < e["dur"]]
        if any(e["sym"] == sym for e in live):
            return  # dasselbe Symbol läuft schon
        self.emotes = [{"sym": sym, "c": color, "t0": now, "dur": secs, "dx": random.uniform(-6, 6)}]  # immer nur eins

    def hop(self, power=9):
        self.vy = -power * (0.7 + 0.3 * self.size)
        self.set("air")
        self.ledge = None

    @property
    def grounded(self):
        return self.state not in ("fly", "air", "fall", "drag", "hover", "portal") and self.vy == 0

    def busy(self):
        return self.state in ("air", "fall", "drag", "fly", "portal")

    def fly_to(self, x, y, mode=None):
        """Reisen: meist fliegen, bei weiten Wegen manchmal Portal-Gun, manchmal durch eine Wand brechen."""
        a = self.app
        dist = math.hypot(x - self.x, y - self.y)
        if mode is None and not self.mini and self.state not in ("air", "fall", "drag"):
            r = random.random()
            if (dist > 380 and r < 0.4) or (dist > 180 and r < 0.2):
                mode = "portal"
            elif any(min(self.x, x) < wx < max(self.x, x) for wx in a.walls()) and r < 0.75:
                mode = "smash"
        if mode == "portal":
            now = time.time()
            self.portal = {"t0": now, "from": (self.x, self.y), "to": (x, y), "shot": False}
            self.facing = 1 if x > self.x else -1
            self.state, self.until = "portal", 0
            self.fly = None
            self.ledge = None
            self.target = None
            return
        self.smash = mode == "smash"
        self._fly_to(x, y)

    def _fly_to(self, x, y):
        """Propeller raus und zu einer anderen Box fliegen."""
        self.fly = (x, y)
        self.set("fly")
        self.ledge = None
        self.target = None
        self.vx = self.vy = 0.0

    # -------------------------------------------------- Physik
    def physics(self, W, H):
        if self.state == "drag":
            return
        if self.state == "portal":
            self.portal_step()
            return
        floor = H - 2
        if self.state == "hover" or (self.state in ("pull", "pound", "sulk") and self.ledge is None and self.app.scene):
            self.y = self.hover_y + math.sin(self.t * 5) * 1.5  # schwebt (auch beim Ziehen in der Luft)
            return
        if self.state == "fly" and self.fly:
            tx, ty = self.fly
            dx, dy = tx - self.x, ty - self.y
            dist = math.hypot(dx, dy)
            if dx:
                self.facing = 1 if dx > 0 else -1
            if dist < 4:
                self.x, self.y = tx, ty
                self.fly = None
                self.ledge = next((lg for lg in self.app.ledges if abs(lg[2] - ty) <= 6 and lg[0] <= tx <= lg[1]), None)
                self.squash = 0.15
                self.set("idle")
            else:
                sp = min(dist, (9.0 if getattr(self, "smash", False) else 5.5) + 2 * self.size)
                ox_ = self.x
                self.x += dx / dist * sp
                if getattr(self, "smash", False):
                    for wx in self.app.walls():
                        if min(ox_, self.x) < wx <= max(ox_, self.x):
                            self.smash = False
                            self.app.add_decor("crack", wx, self.y - 10, 7)
                            self.squash = 0.45
                            self.emote("✦", "#f59e0b", 1.0)
                            self.app.flash("determined", 1.0)
                self.y += dy / dist * sp + math.sin(self.t * 6) * 0.6  # leichtes Schweben
            return
        if self.state in ("air", "fall") or self.vy != 0:
            prev = self.y
            self.vy += 0.9
            self.x += self.vx
            self.y += self.vy
            self.vx *= 0.985
            if self.x < self.w / 2:
                self.x, self.vx = self.w / 2, abs(self.vx) * 0.6
            if self.x > W - self.w / 2:
                self.x, self.vx = W - self.w / 2, -abs(self.vx) * 0.6
            if self.y - self.h < 0:
                self.y, self.vy = self.h, abs(self.vy) * 0.3
            if self.vy >= 0:
                land = floor
                led = None
                for lg in self.app.ledges:
                    if lg[0] <= self.x <= lg[1] and prev - 1 <= lg[2] <= self.y and lg[2] < land:
                        land, led = lg[2], lg
                if self.y >= land:
                    hard = self.vy > 20
                    self.y, self.vy, self.vx = land, 0.0, 0.0
                    self.ledge = led
                    self.squash = 0.35 if hard else 0.2
                    if hard and not self.mini:
                        self.set("dizzy", 1.5)
                        self.emote("✦", "#f59e0b", 1.4)
                    else:
                        self.set("idle")
            return
        # steht: Plattform noch genau so da? (Plattformer: verändert = neue Plattform, nichts fährt mit)
        if self.ledge:
            old = self.ledge
            near = sorted(
                [lg for lg in self.app.ledges if abs(lg[2] - old[2]) <= 90 and abs(lg[0] - old[0]) <= 12 and abs(lg[1] - old[1]) <= 12],
                key=lambda lg: abs(lg[2] - old[2]))
            if near:
                lg = near[0]
                dy = lg[2] - old[2]
                self.ledge = lg
                comp = self.app.comp_exact
                if abs(dy) > 5 and comp and abs(old[2] - comp[2]) < 120 and abs(lg[0] - comp[0]) < 14 and self.state != "portal":
                    r = random.random()
                    if dy > 0:  # Eingabefeld wird kleiner: Boden sackt weg
                        if r < 0.5:
                            self.ledge = None
                            self.set("fall")  # plumpst runter
                            return
                        elif r < 0.75 and not self.mini:
                            self.fly_to(self.x, lg[2], "fly")  # Propeller, schwebt runter
                            return
                        self.squash = 0.3
                    else:  # wird höher: wird hochgeschoben
                        if r < 0.3:
                            self.y = lg[2]
                            self.hop(5)  # hochgeschubst
                            self.emote("!", "#f59e0b", 0.8)
                            return
                        self.squash = 0.35
                elif abs(dy) > 3 and self.state != "portal":
                    # Plattform hat sich bewegt (Scrollen o. ä.): nicht mitfahren – runterfallen oder aufschrecken
                    self.ledge = None
                    if random.random() < 0.5 and not self.mini:
                        self.emote("!", "#f59e0b", 0.9)
                        self.app.flash("startle", 0.9)
                        self.hop(4)
                    else:
                        self.set("fall")
                    return
                self.y = lg[2]
                if not (lg[0] <= self.x <= lg[1]):
                    self.set("fall")
                    self.ledge = None
            else:
                self.set("fall")
                self.ledge = None
        elif self.y < floor - 1:
            self.set("fall")

    def abort_portal(self):
        now = time.time()
        for key in ("blue", "orange"):
            d = self.portal.get(key)
            if d:
                d.life = min(d.life, now - d.t0 + 0.4)
        self.portal = None
        self.vis = 1.0
        self.state_anim = None

    def portal_step(self):
        """Portal-Gun: Portal ein Stück vor sich schießen, reinlaufen, am Ziel rauslaufen, Portale gehen zu."""
        a, pt = self.app, self.portal
        now = time.time()
        ph = pt.get("ph", "aim")
        el = now - pt.get("pt0", pt["t0"])
        (fx, fy), (tx, ty) = pt["from"], pt["to"]
        W = a.W

        def nxt(name):
            pt["ph"], pt["pt0"] = name, now

        def walk_to(x):
            d = x - self.x
            self.facing = 1 if d > 0 else -1
            self.x += max(-3.4, min(3.4, d))
            self.walk_phase += 0.6
            return abs(d) <= 3.4

        if ph == "aim":
            self.vis = 1.0
            if "in_x" not in pt:
                lg = self.ledge or a.ledge_at(fx, fy, 6)
                lo, hi = (lg[0] + 25, lg[1] - 25) if lg else (25, W - 25)
                d = 1 if tx > fx else -1
                ix = fx + d * 85
                if not lo <= ix <= hi:
                    d = -d
                    ix = fx + d * 85
                pt["in_x"] = max(lo, min(hi, ix))
                tl = a.ledge_at(tx, ty, 8)
                tlo, thi = (tl[0] + 25, tl[1] - 25) if tl else (25, W - 25)
                od = 1 if tx < (tlo + thi) / 2 else -1  # rauslaufen Richtung Kantenmitte
                pt["out_x"] = max(tlo, min(thi, tx - od * 40))
                pt["end_x"] = max(tlo, min(thi, pt["out_x"] + od * 75))
                self.facing = 1 if pt["in_x"] > self.x else -1
            if el > 0.12 and not pt["shot"]:
                pt["shot"] = True
                pt["blue"] = a.add_decor("portal", pt["in_x"], fy, 30, color="blue")
                pt["orange"] = a.add_decor("portal", pt["out_x"], ty, 30, color="orange")
                self.emote("◉", "#3b82f6", 0.8)
            if el > 0.45:
                nxt("walk_in")
        elif ph == "walk_in":
            self.state_anim = "walk"
            if walk_to(pt["in_x"]) or el > 3:
                nxt("enter")
        elif ph == "enter":
            self.x += self.facing * 0.8
            self.vis = max(0.05, 1 - el / 0.3)  # verschwindet im blauen Portal
            if el > 0.3:
                self.x, self.y = pt["out_x"], ty
                self.vis = 0.05
                b = pt.get("blue")
                if b:
                    b.life = (now - b.t0) + 1.0  # blaues Portal schließt sich
                nxt("exit")
        elif ph == "exit":
            self.facing = 1 if pt["end_x"] > pt["out_x"] else -1
            self.x += self.facing * 0.8
            self.vis = min(1.0, 0.05 + el / 0.3)  # kommt aus dem orangen raus
            if el > 0.3:
                self.vis = 1.0
                nxt("walk_out")
        elif ph == "walk_out":
            self.state_anim = "walk"
            if walk_to(pt["end_x"]) or el > 3:
                o = pt.get("orange")
                if o:
                    o.life = (now - o.t0) + 1.0  # oranges Portal schließt sich hinter ihm
                self.vis = 1.0
                self.portal = None
                self.state_anim = None
                self.ledge = a.ledge_at(self.x, ty, 8)
                self.squash = 0.15
                self.set("idle")

    # -------------------------------------------------- Verhalten
    def think(self, now):
        a = self.app
        if self.state == "sleep":
            return  # schläft: kein Laufen, kein Fliegen
        if self.until and now > self.until and not self.busy():
            self.set("idle")
        if self.busy():
            return
        # Ziel ansteuern
        if self.target is not None:
            dx = self.target - self.x
            if abs(dx) < 3:
                self.target = None
                if self.state == "walk":
                    self.set("idle")
            else:
                self.facing = 1 if dx > 0 else -1
                speed = (3.2 if a.working or self.mini else 2.0) * (0.8 + 0.4 * self.size)
                if a.typing_fast:
                    speed *= 1.8
                nx = self.x + self.facing * min(abs(dx), speed)
                if self.ledge and not (self.ledge[0] + 4 <= nx <= self.ledge[1] - 4):
                    # Kante zu Ende: runterspringen oder umdrehen (in einer Szene nie springen)
                    if not (a.scene or a.step) and (random.random() < 0.5 or self.target_outside_ledge()):
                        self.vx = self.facing * 3
                        self.hop(5)
                    else:
                        self.target = None
                        self.set("idle")
                    return
                self.x = nx
                self.state = "walk" if self.state in ("idle", "walk") else self.state
                self.walk_phase += speed * 0.18
            return
        if not self.mini and (a.scene or a.step):
            return  # eine Szene läuft: nichts Zufälliges dazwischen
        if a.working and not self.mini:
            self.target = None
            if self.state == "walk":
                self.set("idle")
            return
        if self.mini:
            r = random.random()
            if r < 0.006:
                self.fly_random()
            elif r < 0.03:
                self.wander(220)
            return
        if self.state == "idle" and random.random() < 0.0015:
            r = random.random()
            if r < 0.5:
                self.wander(260)
            elif r < 0.62:
                self.fly_random()
            elif r < 0.7:
                self.hop(8)
            elif r < 0.8:
                self.set("look", 2.0)
            else:
                self.set("stretch", 1.4)

    def fly_random(self):
        opts = [lg for lg in self.app.ledges if lg[1] - lg[0] > self.w * 2 and abs(lg[2] - self.y) > 20]
        if opts:
            lg = random.choice(opts)
            self.fly_to(random.uniform(lg[0] + self.w, lg[1] - self.w), lg[2])

    def target_outside_ledge(self):
        return self.ledge is not None and self.target is not None and not (self.ledge[0] <= self.target <= self.ledge[1])

    def wander(self, dist):
        lo, hi = self.w, self.app.W - self.w
        if self.ledge and random.random() < 0.85:  # meist auf der eigenen Plattform bleiben
            lo, hi = max(lo, self.ledge[0] + self.w * 0.6), min(hi, self.ledge[1] - self.w * 0.6)
        self.target = max(lo, min(hi, self.x + random.choice([-1, 1]) * random.uniform(40, dist)))
        self.set("walk")

    def pose(self, now):
        a = self.app
        st = self.state
        if st == "portal" and getattr(self, "state_anim", None) == "walk":
            st = "walk"
        # Augen: Blick
        mx, my = cursor()
        sx, sy = a.to_screen(self.x, self.y - self.h * 0.6)
        if a.caret_x is not None and not self.mini and a.typing_recent:
            look = ((a.caret_x - self.x) / 120, 0.2)
        elif self.mini:
            look = (math.sin(self.t * 0.7) * 0.8, 0)
        elif st == "look":
            look = (math.sin(self.t * 2), -0.2)
        elif a.glance and now < a.glance[2] and not self.mini:
            look = ((a.glance[0] - self.x) / 160, (a.glance[1] - (self.y - self.h * 0.6)) / 160)
        elif a.ball and not self.mini and (not a.working or now < a.ball_hype or now < a.ball_chase):
            look = ((a.ball.x - self.x) / 140, (a.ball.y - a.ball.r - (self.y - self.h * 0.6)) / 140)  # behält den Ball im Blick
        elif a.mode == "thinking":
            look = (0.6 * math.sin(self.t * 0.6), -0.9)  # nach oben, grübelnd
        elif a.mode == "responding" and a.world.get("streaming") and not self.mini:
            sl, stp, sr, sb = a.world["streaming"]
            look = (((sl + sr) / 2 - self.x) / 300, (sb - (self.y - self.h)) / 200)  # schaut der Antwort beim Wachsen zu
        elif a.tool in ("Read", "Grep", "Glob", "WebFetch", "WebSearch"):
            look = (math.sin(self.t * 4), 0.4)  # liest: hin und her
        else:
            look = ((mx - sx) / 250, (my - sy) / 250)
        if not self.mini and a.cur_prop in ("book", "pencil"):
            look = (math.sin(self.t * 2.4) * 0.9, 0.9)  # liest/schreibt: Augen wandern über die Zeilen
        self.eye_to = [max(-1, min(1, look[0])), max(-1, min(1, look[1]))]

        # Augenform (Gefühle laufen fast nur über die Augen)
        ev = a.event_recent
        age = time.time() - (a.event_until - 1.6) if ev else 99
        idle_for = time.time() - a.last_act
        kind = "normal"
        if st == "sleep":
            kind = "closed"
        elif st == "hide":
            kind = "closed"
        elif st == "dizzy":
            kind = "x"
        elif ev in ("love", "pet"):
            kind = "heart"
        elif ev in ("laugh", "purr"):
            kind = "happy"
        elif ev == "sad":
            kind = "sad"
        elif ev == "sleepy":
            kind = "sleepy"
        elif ev in ("done", "excited", "wow"):
            kind = "star" if age < 0.7 else "happy"
        elif st in ("dance", "happy"):
            kind = "happy"
        elif ev in ("error", "startle", "scared"):
            kind = "wide"
        elif ev == "denied":
            kind = "sad"
        elif ev == "determined":
            kind = "angry"
        elif ev == "question" or (a.text.rstrip().endswith("?") and a.typing_recent):
            kind = "curious"
        elif a.mode == "thinking":
            kind = "ponder"
        elif a.tool in ("Bash", "PowerShell"):
            kind = "focus"
        elif a.typing_recent:
            kind = "focus" if a.typing_fast else "normal"
        elif idle_for > 100 and not a.working:
            kind = "sleepy"
        elif st == "idle" and int(self.t / 7) % 9 == 4 and (self.t % 7) < 1.2:
            kind = "roll"
        if self.mini and kind not in ("closed", "x"):
            kind = "focus" if a.working else "normal"
        self.eye_kind = kind
        if kind == "roll":
            r = (self.t % 7) / 1.2 * math.pi * 2
            self.eye_to = [math.cos(r), math.sin(r)]

        # Blinzeln (nur bei normal offenen Augen, sonst überlagert es Gefühle)
        if now > self.next_blink and kind not in ("normal", "focus", "curious", "wide", "ponder", "sad", "angry", "sleepy"):
            self.next_blink = now + random.uniform(1.5, 3)
        if now > self.next_blink:
            self.eye_h_to = 0.0
            if now > self.next_blink + 0.12:
                self.eye_h_to = 1.0
                self.next_blink = now + random.uniform(2, 5) * (0.5 if a.typing_recent else 1)
                if random.random() < 0.2:
                    self.next_blink = now + 0.25  # Doppel-Blinzeln
        if kind == "closed":
            self.eye_h_to = 0.0

        # Arme
        t = self.t
        if st == "drag":
            arms = (-1 + 0.4 * math.sin(t * 14), -1 + 0.4 * math.sin(t * 14 + 1.5))  # zappelt
        elif st == "fly" and getattr(self, "smash", False):
            arms = (0.9, 0.9)  # Kopf voraus, Arme angelegt
        elif st == "fly":
            arms = (-0.4 + 0.3 * math.sin(t * 8), -0.4 + 0.3 * math.sin(t * 8 + 1))
        elif st == "portal":
            arms = (0.0, -0.2) if self.facing > 0 else (-0.2, 0.0)
        elif st in ("air",) and self.vy < 0:
            arms = (-0.9, -0.9)
        elif st == "fall":
            arms = (-1, -1)
        elif st == "dance":
            arms = (math.sin(t * 6), -math.sin(t * 6))
        elif st == "giggle":
            arms = (0.6 + 0.3 * math.sin(t * 22), 0.6 + 0.3 * math.sin(t * 22 + 1))  # hält sich den Bauch
            self.squash = max(self.squash, 0.06 * abs(math.sin(t * 22)))
        elif st == "surf":
            arms = (-0.2 + 0.25 * math.sin(t * 7), -0.2 - 0.25 * math.sin(t * 7))  # balanciert
        elif st == "purr":
            arms = (0.5, 0.5)
            self.squash = max(self.squash, 0.05 * abs(math.sin(t * 9)))
        elif st == "kick":
            arms = (-0.8, -0.8)
        elif st == "sit":  # sitzt: tiefer, Arme ruhen
            arms = (0.7, 0.7)
            self.squash = max(self.squash, 0.22)
        elif st == "hide":  # hält sich die Augen zu
            arms = (-0.95, -0.95)
        elif st == "scratch":  # kratzt sich am Kopf
            wig = -0.9 + 0.25 * math.sin(t * 14)
            arms = (0.2, wig) if self.facing > 0 else (wig, 0.2)
        elif st == "pound":  # haut mit beiden Fäusten drauf
            hit = abs(math.sin(t * 15))
            arms = (-1 + 1.9 * hit, -1 + 1.9 * abs(math.sin(t * 15 + 1.6)))
            self.squash = max(self.squash, 0.12 * hit)
        elif st == "pull":  # zieht an etwas
            arms = (-0.2, -0.2) if True else (0, 0)
            self.squash = max(self.squash, 0.05 * abs(math.sin(t * 6)))
        elif st == "sulk":  # beleidigt: Arme runter, abgewandt
            arms = (0.75, 0.75)
        elif st == "tap":
            arms = (0.3, 0.3)
            self.squash = max(self.squash, 0.05 if math.sin(t * 9) > 0.6 else 0.0)  # tippt mit dem Fuß
        elif st == "stretch":
            arms = (-1, -1)
        elif st == "wave":
            arms = (0, -0.6 + 0.5 * math.sin(t * 12)) if self.facing > 0 else (-0.6 + 0.5 * math.sin(t * 12), 0)
        elif a.event_recent in ("done", "excited", "love"):
            arms = (-1 + 0.3 * math.sin(t * 10), -1 + 0.3 * math.sin(t * 10 + 3))
        elif a.event_recent in ("error", "scared"):
            arms = (-0.8, -0.8)
        elif a.cur_prop == "hammer" and not self.mini:
            hit = abs(math.sin(t * 9))
            arms = (0.2, -1 + 1.6 * hit) if self.facing > 0 else (-1 + 1.6 * hit, 0.2)  # hämmern
        elif a.tool in ("Edit", "Write") or (a.typing_recent and not self.mini):
            arms = (0.4 + 0.5 * math.sin(t * 16), 0.4 + 0.5 * math.sin(t * 16 + 2))  # tippt mit
        elif st == "walk":
            arms = (0.3 * math.sin(self.walk_phase), -0.3 * math.sin(self.walk_phase))
        elif a.mode == "thinking" and not self.mini:
            arms = (0.3, -0.7 + 0.15 * math.sin(t * 2))  # "Hand am Kinn"
        elif st == "sleep":
            arms = (0.8, 0.8)
        else:
            arms = (0.15 * math.sin(t * 1.3), 0.15 * math.sin(t * 1.3 + 1))
        self.arm_to = list(arms)
        if not self.mini:
            prop = a.cur_prop
            if prop == "hammer":
                hit = abs(math.sin(t * 9))
                self.arm_to = [0.3, -1 + 1.6 * hit] if self.facing > 0 else [-1 + 1.6 * hit, 0.3]
            elif prop == "book":
                self.arm_to = [0.15, 0.15]  # hält das Buch mit beiden Händen vor sich
            elif prop in ("binoculars",):
                self.arm_to = [-0.5, -0.5]
            elif prop == "pencil":
                w_ = 0.45 + 0.15 * math.sin(t * 14)
                self.arm_to = [0.55, w_] if self.facing > 0 else [w_, 0.55]
            elif prop == "binoculars":
                self.arm_to = [-0.35, -0.35]
            elif prop in ("cpu", "gpu", "machine", "laptop"):
                self.squash = max(self.squash, 0.12)  # sitzt seitlich davor
                w_ = 0.25 + 0.25 * math.sin(t * (16 if prop == "laptop" else 8))
                self.arm_to = [0.5, w_] if self.facing > 0 else [w_, 0.5]
            elif prop == "cloud":
                self.arm_to = [-0.6, -0.6]  # fängt den Download auf
            elif prop == "scanner":
                self.arm_to = [0.3, -0.5] if self.facing > 0 else [-0.5, 0.3]  # hält das Gerät nach vorn
            elif prop == "broom":
                sw = math.sin(t * 7)
                self.arm_to = [0.4 + 0.4 * sw, 0.4 - 0.4 * sw]
            elif prop in ("coffee", "magnifier"):
                self.arm_to = [0.4, -0.3] if self.facing > 0 else [-0.3, 0.4]
            elif prop in ("sign", "push"):
                self.arm_to = [-1, -1] if prop == "push" else ([0.3, -1] if self.facing > 0 else [-1, 0.3])
        if False and self.point and now < self.point[2]:
            tx = self.point[0]
            self.facing = 1 if tx > self.x else -1
            # Zeige-Arm macht draw_extras; der andere winkt
            wave = -0.7 + 0.5 * math.sin(t * 12)
            self.arm_to = [wave, 0] if self.facing > 0 else [0, wave]
        elif self.point:
            self.point = None

        # weich überblenden
        k = 0.25
        self.eye = [lerp(self.eye[0], self.eye_to[0], k), lerp(self.eye[1], self.eye_to[1], k)]
        self.eye_h = lerp(self.eye_h, self.eye_h_to, 0.5)
        self.arm = [lerp(self.arm[0], self.arm_to[0], 0.3), lerp(self.arm[1], self.arm_to[1], 0.3)]
        self.squash = max(0.0, self.squash - 0.025)

        if st == "walk":
            self.bob = 0.05 * abs(math.sin(self.walk_phase))
        elif st == "sleep":
            self.bob = 0.012 * math.sin(t * 1.2)  # nur ganz leises Atmen
        else:
            self.bob = 0.0  # still stehen heißt still stehen

    # -------------------------------------------------- Zeichnen
    def draw(self, cv, now):
        u = self.u
        sq = self.squash
        sx, sy = 1 + sq * 0.6, 1 - sq
        if self.leaving:
            k = max(0.0, 1 - (now - self.leaving) / 0.5)
            sx, sy = sx * k, sy * k
        cx, base = self.x, self.y - self.bob * 9 * u
        st = self.state
        if st == "portal" and getattr(self, "state_anim", None) == "walk":
            st = "walk"
        color = BODY
        if self.app.event_recent in ("error", "denied") and not self.mini and int(self.t * 8) % 2:
            color = "#e5484d"

        def box(x0, y0, x1, y1, fill):
            # Einheiten (0..12, 0..9 von oben) -> Pixel, mit Squash um die Fußmitte
            X0 = cx + (x0 - 6) * u * sx
            X1 = cx + (x1 - 6) * u * sx
            Y0 = base - (9 - y0) * u * sy
            Y1 = base - (9 - y1) * u * sy
            cv.create_rectangle(round(X0), round(Y0), round(X1), round(Y1), fill=fill, outline=fill)

        # Beine (vier, beim Laufen abwechselnd)
        legs = [2, 4, 7, 9]
        for i, lx in enumerate(legs):
            lift = 0.0
            if st == "walk" or (st == "drag"):
                lift = max(0.0, math.sin(self.walk_phase + (0 if i % 2 == 0 else math.pi))) * 0.9
                if st == "drag":
                    lift = abs(math.sin(self.t * 12 + i)) * 0.8
            if st in ("sleep", "sit"):
                box(lx, 7.3, lx + 1, 8.3, BODY_DARK)
            else:
                box(lx, 7, lx + 1, 9 - lift, color)
        # Körper
        box(1, 0, 11, 7.2, color)
        if color == BODY:
            box(1, 0, 11, 0.45, "#e8967a")   # Licht von oben
            box(1, 6.7, 11, 7.2, "#c2633f")  # Schatten unten
            box(1, 0, 1.45, 7.2, "#cf6c4c")  # Kante links
        # Arme (Stummel links/rechts), drehen um die Schulter
        for side, a in ((-1, self.arm[0]), (1, self.arm[1])):
            ay = 2.6 + a * 1.4  # -1 oben, 1 unten
            if side < 0:
                box(0, ay, 1.05, ay + 1.6, color)
                if a < -0.6:
                    box(0.2, ay - 0.6, 0.85, ay, color)  # erhobene Schere
            else:
                box(10.95, ay, 12, ay + 1.6, color)
                if a < -0.6:
                    box(11.15, ay - 0.6, 11.8, ay, color)
        # Propeller-Mütze beim Fliegen
        if st == "fly" or (self.fly is None and st == "idle" and now - getattr(self, "landed", 0) < 0):
            box(5.5, -0.9, 6.5, 0, "#3b82f6")
            box(5.8, -1.4, 6.2, -0.9, "#1e293b")
            spin = abs(math.cos(self.t * 28))
            box(6 - 3 * spin, -1.7, 6 + 3 * spin, -1.35, "#1e293b")
        # Augen (bei Rückenansicht kein Gesicht)
        back = getattr(self, "back_until", 0) > now
        if back:
            box(5.6, 1.2, 6.4, 6.2, BODY_DARK)  # Rücken
        ex, ey = self.eye[0] * 0.6, self.eye[1] * 0.55
        k = self.eye_kind
        side_view = not self.mini and self.app.cur_prop in ("laptop", "cpu", "gpu", "machine") and not back
        eyes_x = (3, 8)
        if side_view:  # dreht sich zur Seite: Profil, beide Augen auf der Blickseite
            eyes_x = (6.6, 9.4) if self.facing > 0 else (1.6, 4.4)
            ex = self.facing * 0.35
        for i, bx in enumerate(() if back else eyes_x):
            x0, y0 = bx + ex, 2.2 + ey
            if k == "happy":
                box(x0 - 0.25, y0 + 0.5, x0 + 0.25, y0 + 0.85, EYE)
                box(x0 + 0.25, y0 + 0.15, x0 + 0.75, y0 + 0.5, EYE)
                box(x0 + 0.75, y0 + 0.5, x0 + 1.25, y0 + 0.85, EYE)
            elif k == "x":
                for (dx, dy) in ((0, 0), (0.6, 0), (0.3, 0.4), (0, 0.8), (0.6, 0.8)):
                    box(x0 + dx, y0 + dy, x0 + dx + 0.4, y0 + dy + 0.4, EYE)
            elif k == "heart":
                pulse = 1 + 0.12 * math.sin(self.t * 10)
                for (dx, dy, w_) in ((-0.2, 0.1, 0.6), (0.6, 0.1, 0.6), (-0.3, 0.45, 1.6), (0, 0.8, 1.0), (0.3, 1.15, 0.4)):
                    box(x0 + dx * pulse, y0 + dy * pulse, x0 + (dx + w_) * pulse, y0 + (dy + 0.4) * pulse, "#e11d48")
            elif k == "star":
                c = "#f59e0b"
                box(x0 + 0.35, y0 - 0.1, x0 + 0.65, y0 + 1.5, c)
                box(x0 - 0.2, y0 + 0.55, x0 + 1.2, y0 + 0.85, c)
                box(x0 + 0.15, y0 + 0.35, x0 + 0.85, y0 + 1.05, c)
            elif k == "closed":
                box(x0 - 0.1, y0 + 0.9, x0 + 1.1, y0 + 1.15, EYE)
            else:
                full = {"wide": 1.8, "focus": 0.75, "sleepy": 0.6, "sad": 1.1, "angry": 1.0, "ponder": 1.3,
                        "curious": 1.5 if i == 1 else 1.0}.get(k, 1.5)
                wdt = 1.3 if k == "wide" else 1.2 if (k == "curious" and i == 1) else 1.0
                hgt = max(0.16, full * self.eye_h)
                mid = y0 + (1.05 if k == "sleepy" else 0.75)
                if k == "wide":
                    mid += 0.15 * math.sin(self.t * 40)
                box(x0 - (wdt - 1) / 2, mid - hgt / 2, x0 + 1 + (wdt - 1) / 2, mid + hgt / 2, EYE)
                shine = settings.get("shine", "none")
                if hgt > 0.7 and shine != "none":  # Glanzpunkt: aus oder dunkelorange
                    box(x0 + 0.1, mid - hgt / 2 + 0.12, x0 + 0.45, mid - hgt / 2 + 0.47, "#9a3412")
            br = "#7c2d12"

            def brow(xa, ya, xb, yb, _c=None):
                box(xa - 0.1, ya - 0.2, xb + 0.1, yb + 0.05, br)  # dicker und etwas breiter
            if k == "angry":
                inner = 0.35 if i == 0 else 0.0
                brow(x0 - 0.2, y0 - 0.2 - inner, x0 + 0.5, y0 + 0.1 - inner, br)
                brow(x0 + 0.5, y0 - 0.55 + inner, x0 + 1.2, y0 - 0.25 + inner, br)
            elif k == "sad":
                o = 0.35
                brow(x0 - 0.2, y0 - 0.5 + (o if i == 0 else 0), x0 + 0.5, y0 - 0.2 + (o if i == 0 else 0), br)
                brow(x0 + 0.5, y0 - 0.5 + (0 if i == 0 else o), x0 + 1.2, y0 - 0.2 + (0 if i == 0 else o), br)
            elif k == "curious" and i == 1:
                brow(x0 - 0.1, y0 - 0.9, x0 + 1.2, y0 - 0.6, br)
            elif k == "wide":
                brow(x0 - 0.1, y0 - 0.95, x0 + 1.1, y0 - 0.65, br)
            elif k == "ponder":
                lift = 0.15 if i == 1 else 0
                brow(x0 - 0.1, y0 - 0.45 - lift, x0 + 1.1, y0 - 0.2 - lift, br)
            elif k == "focus":
                brow(x0 - 0.1, y0 - 0.25, x0 + 1.1, y0 - 0.02)
        self.draw_extras(cv, now, cx, base, sx, sy, box)
        if st == "portal" and self.portal and time.time() - self.portal["t0"] < 0.4:
            gx, gy = cx + self.facing * 6.5 * u, base - 4 * u
            cv.create_rectangle(min(gx, gx + self.facing * 3 * u), gy - 0.8 * u, max(gx, gx + self.facing * 3 * u), gy + 0.8 * u,
                                fill="#e5e7eb", outline="#475569")
            cv.create_oval(gx + self.facing * 3 * u - 0.6 * u, gy - 0.6 * u, gx + self.facing * 3 * u + 0.6 * u, gy + 0.6 * u,
                           fill="#3b82f6", outline="")
        # Symbole über dem Kopf (keine Worte)
        alive = []
        for e in self.emotes:
            age = (now - e["t0"]) / e["dur"]
            if age < 1:
                alive.append(e)
                ex_, ey_ = cx + e["dx"] - self.w * 0.3, base - self.h - 14 - age * 26
                pop = min(1.0, age / 0.12)
                fs = int((9 + 6 * self.size) * (0.6 + 0.4 * pop))
                for ddx, ddy in ((-2, 0), (2, 0), (0, -2), (0, 2)):  # weiße Kontur, gut lesbar
                    cv.create_text(ex_ + ddx, ey_ + ddy, text=e["sym"], fill="#fffdf8", font=("Segoe UI Symbol", fs, "bold"))
                cv.create_text(ex_, ey_, text=e["sym"], fill=e["c"], font=("Segoe UI Symbol", fs, "bold"))
        self.emotes = alive
        if st == "sleep":
            for i in range(3):
                z = (self.t / 1.2 + i / 3) % 1
                cv.create_text(cx + self.w * 0.35 + z * 14, base - self.h - z * 26, text="z", fill="#94a3b8",
                               font=("Segoe UI", int(7 + 3 * z + 2 * self.size), "bold"))
        head_x, head_y = cx, base - self.h * sy - 6
        badge = getattr(self.app, "badge", None) if not self.mini else None
        if False:  # (Icon-Blasen entfernt)
            pop = min(1.0, (now - badge[2]) / 0.15)  # ploppt auf
            r = 19 * (0.6 + 0.4 * pop)
            draw_badge(cv, badge[0], head_x + self.facing * (self.w * 0.55), head_y - 30 - (1 - pop) * 6, self.t, r)
        if self.app.mode == "thinking" and not self.mini and st not in ("sleep",):
            gear(cv, head_x - 7 * self.size, head_y - 12 * self.size, 9 * self.size, 8, self.t * 2.2, "#78716c")
            gear(cv, head_x + 8 * self.size, head_y - 22 * self.size, 6 * self.size, 6, -self.t * 3.3 + 0.3, "#a8a29e")
        bulb_age = now - self.app.bulb_at
        if bulb_age < 1.8 and not self.mini:
            by = head_y - 18 * self.size - min(bulb_age, 0.3) * 20
            glow = 0.5 + 0.5 * math.sin(bulb_age * 14)
            r = 7 * self.size
            for ang in range(0, 360, 45):
                ra = math.radians(ang)
                l1, l2 = r + 3, r + 6 + 3 * glow
                cv.create_line(head_x + math.cos(ra) * l1, by + math.sin(ra) * l1,
                               head_x + math.cos(ra) * l2, by + math.sin(ra) * l2, fill="#facc15", width=2)
            cv.create_oval(head_x - r, by - r, head_x + r, by + r, fill="#fde047", outline="#eab308", width=1)
            cv.create_rectangle(head_x - r * 0.45, by + r * 0.8, head_x + r * 0.45, by + r * 1.35, fill="#94a3b8", outline="")
            cv.create_line(head_x - r * 0.3, by + r * 0.2, head_x, by - r * 0.3, head_x + r * 0.3, by + r * 0.2, fill="#ca8a04", width=1)

    def draw_extras(self, cv, now, cx, base, sx, sy, box):
        u, a, t = self.u, self.app, self.t
        side = self.facing
        hx = 12.6 if side > 0 else -0.6  # Hand-Position (Einheiten)
        # Zeigen: langer Pixel-Arm Richtung Ziel + pulsierender Ring am Ziel
        if False and self.point:
            tx, ty, _ = self.point
            shx = cx + (5.6 * side) * u
            shy = base - 6 * u
            ang = math.atan2(ty - shy, tx - shx)
            for i in range(5):
                px, py = shx + math.cos(ang) * i * u * 0.9, shy + math.sin(ang) * i * u * 0.9
                cv.create_rectangle(px - u / 2, py - u / 2, px + u / 2, py + u / 2, fill=BODY, outline=BODY)
            r = 10 + 4 * math.sin(t * 8)
            cv.create_oval(tx - r, ty - r, tx + r, ty + r, outline="#D97757", width=3)
        if self.mini:
            return
        prop = a.cur_prop
        def hand(sd):
            """Bildschirmpunkt der Hand (Ende des Armstummels) der Seite sd und deren Armwert."""
            av = self.arm[0] if sd < 0 else self.arm[1]
            ay = 2.6 + av * 1.4 + 0.8
            ux = 11.6 if sd > 0 else 0.4
            return cx + (ux - 6) * u * sx, base - (9 - ay) * u * sy, av

        # Requisiten im Stil des Tierchens: flache Formen, je eine Licht- und Schattenkante, weiche Ecken
        SL, SLL, SLD = "#64748b", "#94a3b8", "#475569"   # Metall
        WD, WDD = "#a16207", "#854d0e"                    # Holz
        PAPER, PAPER2, INK = "#f1e4cc", "#dccaa8", "#a8977a"  # Creme statt Weiß: hebt sich vom hellen Hintergrund ab
        ground = self.y
        lw = max(2, int(u * 0.5))

        def rr(x0, y0, x1, y1, r, fill, rim=None):
            """abgerundetes Rechteck (optional mit dunklerem Rand)"""
            if rim:
                rr(x0 - 0.18 * u, y0 - 0.18 * u, x1 + 0.18 * u, y1 + 0.18 * u, r + 0.1 * u, rim)
            r = min(r, abs(x1 - x0) / 2, abs(y1 - y0) / 2)
            x0, x1 = min(x0, x1), max(x0, x1)
            y0, y1 = min(y0, y1), max(y0, y1)
            cv.create_polygon(x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
                              x0, y1, x0, y1 - r, x0, y0 + r, x0, y0, smooth=True, fill=fill, outline="")

        def stick(x0, y0, x1, y1, w, fill):
            cv.create_line(x0, y0, x1, y1, fill=fill, width=w, capstyle="round")

        def quad(pts, fill):
            cv.create_polygon(*pts, fill=fill, outline="", joinstyle="round")

        if prop == "hammer":
            hx_, hy_, av = hand(side)
            # Amboss: fest auf der Kante, Horn zur Außenseite
            aw = 7.6 * u
            ix = cx + side * (6 * u + aw / 2 + 0.6 * u)
            top = ground - 3.6 * u
            if a.tool in ("Bash", "PowerShell") and self.grounded:
                f = side

                def poly(pts, fill):
                    cv.create_polygon(*[v for (px_, py_) in pts for v in (ix + px_ * u * f, top + py_ * u)], fill=fill, outline="")
                poly([(-2.3, 3.6), (2.3, 3.6), (1.8, 2.7), (-1.8, 2.7)], SLD)                       # Fuß
                poly([(-1.0, 1.3), (1.0, 1.3), (0.75, 2.75), (-0.75, 2.75)], SL)                    # Taille
                poly([(0.2, 1.3), (1.0, 1.3), (0.75, 2.75), (0.2, 2.75)], SLD)                      # Schattenseite
                poly([(-2.7, 0), (1.5, 0), (3.7, 0.35), (3.7, 0.55), (1.5, 1.3), (-2.4, 1.3), (-2.7, 1.0)], SL)  # Bahn + Horn
                poly([(-2.7, 0), (1.5, 0), (3.7, 0.35), (3.5, 0.45), (1.5, 0.32), (-2.7, 0.32)], SLL)  # Lichtkante
                poly([(-2.4, 1.05), (1.5, 1.05), (1.5, 1.3), (-2.4, 1.3)], SLD)                     # Schatten unter der Bahn
            # Hammer: schwingt vom Ausholen auf den Amboss
            k = max(0.0, min(1.0, (av + 1) / 1.6))
            tx_, ty_ = ix - side * 0.8 * u, top - 0.4 * u
            hit_ang = math.atan2(ty_ - hy_, tx_ - hx_)
            up_ang = -math.pi / 2 - side * 0.55
            diff = (hit_ang - up_ang + math.pi) % (2 * math.pi) - math.pi  # kürzester Bogen: immer über oben
            ang = up_ang + diff * k
            L = max(3.5 * u, min(7 * u, math.hypot(tx_ - hx_, ty_ - hy_)))
            ex, ey = hx_ + math.cos(ang) * L, hy_ + math.sin(ang) * L
            stick(hx_, hy_, ex, ey, lw, WD)
            dx, dy = math.cos(ang), math.sin(ang)
            nx, ny = -dy, dx
            hw, hh = 1.6 * u, 0.9 * u
            quad([ex + nx * hw + dx * hh, ey + ny * hw + dy * hh, ex - nx * hw + dx * hh, ey - ny * hw + dy * hh,
                  ex - nx * hw - dx * hh, ey - ny * hw - dy * hh, ex + nx * hw - dx * hh, ey + ny * hw - dy * hh], SL)
            quad([ex + nx * hw + dx * hh, ey + ny * hw + dy * hh, ex + nx * hw * 0.55 + dx * hh, ey + ny * hw * 0.55 + dy * hh,
                  ex + nx * hw * 0.55 - dx * hh, ey + ny * hw * 0.55 - dy * hh, ex + nx * hw - dx * hh, ey + ny * hw - dy * hh], SLL)
            sparks = getattr(self, "sparks", [])
            if k > 0.93 and getattr(self, "last_hit", 0) < now - 0.2:
                self.last_hit = now
                for _ in range(7):
                    sparks.append([tx_, ty_, random.uniform(-3.5, 3.5), random.uniform(-5, -1.5), 12])
            alive = []
            for sp in sparks:
                sp[0] += sp[2]
                sp[1] += sp[3]
                sp[3] += 0.5
                sp[4] -= 1
                if sp[4] > 0:
                    alive.append(sp)
                    stick(sp[0], sp[1], sp[0] - sp[2] * 0.8, sp[1] - sp[3] * 0.8, max(2, int(u * 0.35)),
                          "#facc15" if sp[4] > 5 else "#f59e0b")
            self.sparks = alive
        elif prop == "book":
            # offenes Buch von hinten, leicht hochgekippt: große braune Deckel, Seiten gucken unten raus
            bx, by = cx, base - 1.4 * u
            W, H, rise = 5.0 * u, 3.6 * u, 0.9 * u
            for sd, col in ((-1, "#8b5a2b"), (1, "#a0522d")):
                ox = bx + sd * W
                quad([bx, by + 0.5 * u, ox - sd * 0.3 * u, by - rise + 0.5 * u, ox - sd * 0.3 * u, by - rise, bx, by], "#f1e4cc")  # Seiten unten
                quad([bx, by, ox, by - rise, ox, by - rise - H, bx, by - H], col)                                           # Deckel
            rr(bx - 0.5 * u, by - H - 0.1 * u, bx + 0.5 * u, by + 0.5 * u, 0.25 * u, "#5c3a1e")                           # Rücken
            for i in range(3):
                yy = by - H + 0.6 * u + i * 1.1 * u
                stick(bx - 0.4 * u, yy, bx + 0.4 * u, yy, max(2, int(u * 0.3)), "#d4a373")
            ph = (t * 0.6) % 1.8
            if ph < 1:  # Umblättern: Seitenspitze wandert oben von rechts nach links
                k = ph * ph * (3 - 2 * ph)
                tx_ = bx + W * (1 - 2 * k) * 0.85
                ty_ = by - H - rise * abs(1 - 2 * k) - math.sin(k * math.pi) * 1.6 * u
                quad([bx - 0.4 * u, by - H, tx_, ty_, tx_ + 0.5 * u, ty_ + 0.4 * u, bx + 0.4 * u, by - H], "#fffaf0")
        elif prop == "pencil":
            # Notizblock, darauf wachsende Schrift, Stift folgt der Spitze
            nx0, ny0 = cx - 3.4 * u + side * 0.5 * u, base - 6.0 * u
            nx1, ny1 = nx0 + 6.8 * u, ny0 + 5.2 * u
            rr(nx0, ny0, nx1, ny1, 0.45 * u, PAPER, rim="#b9a582")
            rr(nx0, ny0, nx1, ny0 + 0.8 * u, 0.3 * u, "#D97757")
            for i in range(4):
                cv.create_oval(nx0 + (1 + i * 1.5) * u - 0.2 * u, ny0 - 0.2 * u, nx0 + (1 + i * 1.5) * u + 0.2 * u, ny0 + 0.3 * u,
                               fill=SLD, outline="")
            prog = (t * 0.45) % 1.0
            tip = (nx0 + 0.8 * u, ny0 + 1.9 * u)
            for row in range(3):
                r0 = row / 3
                if prog <= r0:
                    break
                fr = min(1.0, (prog - r0) * 3)
                yy = ny0 + (1.9 + row * 1.15) * u
                pts = []
                n = max(2, int(14 * fr))
                for j in range(n + 1):
                    xx = nx0 + 0.8 * u + j / 14 * 5.2 * u
                    pts += [xx, yy + math.sin(j * 1.7 + row) * 0.25 * u]
                cv.create_line(*pts, fill="#D97757", width=max(1, int(u * 0.3)), smooth=True, capstyle="round")
                tip = (pts[-2], pts[-1])
            ang = -math.radians(55) if side > 0 else -math.radians(125)
            dx, dy = math.cos(ang), math.sin(ang)
            ex, ey = tip[0] + dx * 4.2 * u, tip[1] + dy * 4.2 * u
            quad([tip[0], tip[1], tip[0] + dx * 0.9 * u - dy * 0.35 * u, tip[1] + dy * 0.9 * u + dx * 0.35 * u,
                  tip[0] + dx * 0.9 * u + dy * 0.35 * u, tip[1] + dy * 0.9 * u - dx * 0.35 * u], "#e7c9a0")
            stick(tip[0] + dx * 1.1 * u, tip[1] + dy * 1.1 * u, ex, ey, max(3, int(u * 0.75)), "#facc15")
            stick(ex - dx * 0.2 * u, ey - dy * 0.2 * u, ex + dx * 0.4 * u, ey + dy * 0.4 * u, max(3, int(u * 0.75)), "#f472b6")
            cv.create_oval(tip[0] - 0.18 * u, tip[1] - 0.18 * u, tip[0] + 0.18 * u, tip[1] + 0.18 * u, fill="#3f3f46", outline="")
        elif prop == "cpu":
            ix, iy = cx + side * (6 * u + 2.6 * u), ground - 2.2 * u
            for i in (-1, 0, 1):  # kurze Beinchen
                stick(ix + i * 1.0 * u, iy - 2.4 * u, ix + i * 1.0 * u, iy + 2.4 * u, max(2, int(u * 0.35)), "#94a3b8")
                stick(ix - 2.4 * u, iy + i * 1.0 * u, ix + 2.4 * u, iy + i * 1.0 * u, max(2, int(u * 0.35)), "#94a3b8")
            rr(ix - 1.9 * u, iy - 1.9 * u, ix + 1.9 * u, iy + 1.9 * u, 0.4 * u, "#334155")
            rr(ix - 0.9 * u, iy - 0.9 * u, ix + 0.9 * u, iy + 0.9 * u, 0.2 * u, "#D97757")
        elif prop == "gpu":
            ix, iy = cx + side * (6 * u + 3.0 * u), ground - 2.0 * u
            rr(ix - 2.8 * u, iy - 2.0 * u, ix + 2.8 * u, iy + 2.0 * u, 0.5 * u, "#334155")
            cv.create_oval(ix - 1.6 * u, iy - 1.6 * u, ix + 1.6 * u, iy + 1.6 * u, fill="#475569", outline="")
            for k_ in range(2):
                a_ = t * 10 + k_ * math.pi
                stick(ix - math.cos(a_) * 1.3 * u, iy - math.sin(a_) * 1.3 * u, ix + math.cos(a_) * 1.3 * u, iy + math.sin(a_) * 1.3 * u,
                      max(3, int(u * 0.6)), "#cbd5e1")
            cv.create_oval(ix - 0.4 * u, iy - 0.4 * u, ix + 0.4 * u, iy + 0.4 * u, fill="#D97757", outline="")
        elif prop == "cloud":
            # Download-Wolke über ihm, Datenpäckchen fallen in seine Arme
            ccx, ccy = cx, base - 19.0 * u
            for (ox_, oy_, r_) in ((-2.5, 0.5, 2.1), (0, -0.7, 2.9), (2.6, 0.5, 2.1)):
                cv.create_oval(ccx + ox_ * u - r_ * u, ccy + oy_ * u - r_ * u, ccx + ox_ * u + r_ * u, ccy + oy_ * u + r_ * u,
                               fill="#cbd5e1", outline="")
            rr(ccx - 4.6 * u, ccy + 0.3 * u, ccx + 4.6 * u, ccy + 2.4 * u, 0.9 * u, "#cbd5e1")
            for k_ in range(3):
                ph = (t * 1.2 + k_ / 3) % 1
                yy = ccy + 2.6 * u + ph * 8.6 * u
                xx = ccx + (k_ - 1) * 1.1 * u
                a_ = 0.5 * u  # kleiner grüner Download-Pfeil nach unten
                cv.create_rectangle(xx - a_ * 0.35, yy - a_ * 1.2, xx + a_ * 0.35, yy, fill="#22c55e", outline="")
                cv.create_polygon(xx - a_, yy - 0.1 * u, xx + a_, yy - 0.1 * u, xx, yy + a_ * 1.1, fill="#22c55e", outline="")
        elif prop == "machine":
            ix, iy = cx + side * (6 * u + 2.8 * u), ground - 2.6 * u
            gear(cv, ix, iy, 2.4 * u, 8, t * 2.5, "#D97757")
        elif prop == "laptop":
            ix = cx + side * (6 * u + 2.8 * u)
            rr(ix - 2.6 * u, ground - 4.6 * u, ix + 2.6 * u, ground - 1.0 * u, 0.35 * u, "#334155")      # Bildschirm
            glow = 0.6 + 0.4 * math.sin(t * 3)
            rr(ix - 2.0 * u, ground - 4.0 * u, ix + 2.0 * u, ground - 1.6 * u, 0.2 * u, "#D97757" if glow > 0.8 else "#e8967a")
            rr(ix - 3.2 * u, ground - 1.0 * u, ix + 3.2 * u, ground - 0.2 * u, 0.3 * u, "#475569")       # Tastatur
        elif prop == "scanner":
            hx_, hy_, _ = hand(side)
            gx0, gy0 = hx_ - (0 if side > 0 else 2.6 * u), hy_ - 3.4 * u
            rr(gx0, gy0, gx0 + 2.6 * u, gy0 + 3.6 * u, 0.4 * u, "#475569")
            on = int(t * 8) % 2
            rr(gx0 + 0.4 * u, gy0 + 0.4 * u, gx0 + 2.2 * u, gy0 + 2.0 * u, 0.2 * u, "#D97757" if on else "#e8967a")
            cv.create_oval(gx0 + 1.0 * u, gy0 + 2.5 * u, gx0 + 1.6 * u, gy0 + 3.1 * u, fill="#94a3b8", outline="")
        elif prop == "magnifier":
            mx_ = cx + side * 5.4 * u + math.cos(t * 2) * 0.4 * u
            my_ = base - 6.8 * u + math.sin(t * 3) * 0.4 * u
            R = 1.9 * u
            stick(mx_ + side * R * 0.7, my_ + R * 0.7, mx_ + side * R * 1.7, my_ + R * 1.7, max(3, int(u * 0.7)), WD)
            cv.create_oval(mx_ - R, my_ - R, mx_ + R, my_ + R, fill="#dbeafe", outline=SL, width=max(2, int(u * 0.5)))
            cv.create_arc(mx_ - R * 0.6, my_ - R * 0.6, mx_ + R * 0.6, my_ + R * 0.6, start=100, extent=60, style="arc",
                          outline="#ffffff", width=max(2, int(u * 0.35)))
        elif prop == "coffee":
            sip = (t % 4.5) > 3.6
            hx_, hy_, _ = hand(side)
            cw, ch = 2.6 * u, 2.8 * u
            x0 = hx_ - (0.4 * u if side > 0 else cw - 0.4 * u)
            y0 = (base - 8.0 * u) if sip else hy_ - ch * 0.75
            hxx = x0 + (cw if side > 0 else 0)
            cv.create_arc(hxx - 0.9 * u, y0 + 0.6 * u, hxx + 0.9 * u, y0 + 2.0 * u, start=-90 if side > 0 else 90, extent=180,
                          style="arc", outline=PAPER2, width=max(2, int(u * 0.45)))
            rr(x0, y0, x0 + cw, y0 + ch, 0.5 * u, PAPER, rim="#b9a582")
            rr(x0, y0 + 1.1 * u, x0 + cw, y0 + 1.7 * u, 0.1 * u, "#D97757")  # Claude-Tasse
            cv.create_oval(x0 + 0.15 * u, y0 - 0.2 * u, x0 + cw - 0.15 * u, y0 + 0.45 * u, fill="#78350f", outline="")
            for i in range(2):
                ph = (t * 0.9 + i * 0.5) % 1
                sx0 = x0 + cw * (0.35 + i * 0.3)
                pts = []
                for j in range(5):
                    yy = y0 - 0.6 * u - ph * 2.2 * u - j * 0.6 * u
                    pts += [sx0 + math.sin(t * 3 + j + i) * 0.35 * u, yy]
                cv.create_line(*pts, fill="#d6d3d1", width=max(1, int(u * 0.3)), smooth=True, capstyle="round")
        elif prop == "broom":
            hx_, hy_, _ = hand(side)
            sw = math.sin(t * 7)
            bx_, by_ = hx_ + side * 1.4 * u + sw * 1.4 * u, ground - 1.4 * u
            stick(hx_ - side * 0.6 * u, hy_ - 2.2 * u, bx_, by_, max(2, int(u * 0.5)), WD)
            quad([bx_ - 1.1 * u, by_, bx_ + 1.1 * u, by_, bx_ + 1.7 * u + sw * 0.3 * u, ground, bx_ - 1.7 * u + sw * 0.3 * u, ground], "#fbbf24")
            rr(bx_ - 1.2 * u, by_ - 0.35 * u, bx_ + 1.2 * u, by_ + 0.35 * u, 0.2 * u, WDD)
            if abs(sw) > 0.8:
                for i in range(3):
                    r_ = (0.35 + i * 0.12) * u
                    ddx = bx_ - side * (2.4 + i * 0.9) * u
                    cv.create_oval(ddx - r_, ground - (0.6 + i * 0.35) * u - r_, ddx + r_, ground - (0.6 + i * 0.35) * u + r_,
                                   fill="#e7e5e4", outline="")
        elif prop == "binoculars":
            sweep = math.sin(t * 1.6) * 0.5 * u
            ey0 = base - 6.4 * u
            for ex_u in (3.5, 8.5):
                exx = cx + (ex_u - 6) * u * sx + sweep
                rr(exx - 1.1 * u, ey0 - 1.0 * u, exx + 1.1 * u, ey0 + 1.4 * u, 0.4 * u, "#334155")
                cv.create_oval(exx - 0.7 * u, ey0 - 0.6 * u, exx + 0.7 * u, ey0 + 0.8 * u, fill="#93c5fd", outline="")
                cv.create_oval(exx - 0.45 * u, ey0 - 0.4 * u, exx - 0.05 * u, ey0, fill="#ffffff", outline="")
            rr(cx - 0.9 * u + sweep, ey0 - 0.4 * u, cx + 0.9 * u + sweep, ey0 + 0.4 * u, 0.2 * u, "#475569")
        elif prop == "sign":
            hx_, hy_, _ = hand(side)
            stick(hx_, hy_, hx_, hy_ - 5.2 * u, max(2, int(u * 0.45)), WD)
            bob = math.sin(t * 6) * 0.25 * u
            rr(hx_ - 2.3 * u, hy_ - 8.6 * u + bob, hx_ + 2.3 * u, hy_ - 5.0 * u + bob, 0.5 * u, PAPER, rim="#b9a582")
            rr(hx_ - 2.3 * u, hy_ - 5.5 * u + bob, hx_ + 2.3 * u, hy_ - 5.0 * u + bob, 0.3 * u, PAPER2)
            cv.create_text(hx_, hy_ - 6.9 * u + bob, text="?", fill="#D97757", font=("Segoe UI", int(u * 2.4), "bold"))
        # Nachtmütze (22-6 Uhr)
        hour = time.localtime().tm_hour
        if hour >= 22 or hour < 6:
            box(4, -1.4, 9, 0, "#1e3a8a")
            box(8, -2.2, 10, -1.2, "#1e3a8a")
            box(9.6, -2.6, 10.6, -1.8, "#fde68a")
        # Decke beim Schlafen
        if self.state == "sleep":
            box(0.5, 5.6, 11.5, 8.6, "#6366f1")
            box(0.5, 5.6, 11.5, 6.1, "#a5b4fc")
        # Rot werden (Maus streichelt / Liebe)
        if a.blush_until > now and getattr(self, "back_until", 0) <= now:
            box(1.6, 4.1, 2.8, 4.7, "#f472b6")
            box(9.2, 4.1, 10.4, 4.7, "#f472b6")

    def hit(self, x, y):
        return abs(x - self.x) <= self.w / 2 + 2 and self.y - self.h - 2 <= y <= self.y + 2


# ------------------------------------------------------------------ Spielzeug: Ball
class Ball:
    """Wasserball: fällt, hüpft, rollt über alle UI-Kanten; man kann ihn werfen, das Tierchen kickt ihn."""

    def __init__(self, app, x, y):
        self.app = app
        self.x, self.y = x, y
        self.vx, self.vy = random.uniform(-3, 3), 0.0
        self.r = 2.6 * settings["scale"]
        self.win = (int(self.r * 2 + 40), int(self.r * 2 + 90))  # Fenster hängt 60 px unter den Füßen
        self.state = "air"
        self.ledge = None
        self.target = None
        self.mini = "ball"
        self.leaving = None
        self.ang = 0.0
        self.t = 0.0
        self.touched = time.time()
        self.emotes = []
        self.size = 0.5

    @property
    def w(self):
        return self.r * 2

    @property
    def h(self):
        return self.r * 2

    @property
    def grounded(self):
        return self.ledge is not None and abs(self.vy) < 0.01

    def set(self, st, secs=None):
        self.state = st
        if st == "drag":
            self.touched = time.time()

    def hop(self, power=9):
        self.vy = -power
        self.ledge = None
        self.state = "air"
        self.touched = time.time()

    def emote(self, *a, **k):
        pass

    def busy(self):
        return False

    def think(self, now):
        pass

    def pose(self, now):
        pass

    def hit(self, x, y):
        return math.hypot(x - self.x, y - (self.y - self.r)) <= self.r + 4

    def physics(self, W, H):
        if self.state == "drag" or getattr(self, "carried", False):
            return
        floor = H - 2
        prev = self.y
        if self.ledge is None or self.vy != 0:
            self.vy += 0.97  # mittelschwer
            self.vx *= 0.993  # Luftwiderstand
        self.x += self.vx
        self.y += self.vy
        self.ang -= self.vx / max(1, self.r)  # Tk-Winkel laufen gegen den Uhrzeigersinn
        if self.x < self.r:
            self.x, self.vx = self.r, abs(self.vx) * 0.55
        if self.x > W - self.r:
            self.x, self.vx = W - self.r, -abs(self.vx) * 0.55
        if self.y - 2 * self.r < 0:
            self.y, self.vy = 2 * self.r, abs(self.vy) * 0.5
        if self.vy >= 0:
            land, led = floor, None
            for lg in self.app.ledges:
                if lg[0] <= self.x <= lg[1] and prev - 1 <= lg[2] <= self.y + 0.5 and lg[2] < land:
                    land, led = lg[2], lg
            if self.y >= land:
                self.y = land
                self.ledge = led
                if self.vy > 4.5:
                    self.vy = -self.vy * 0.44  # hüpft mittel
                    self.vx *= 0.9
                    self.ledge = None
                else:
                    self.vy = 0.0
                    self.vx *= 0.935  # rollt aus
                    if abs(self.vx) < 0.05:
                        self.vx = 0.0
                    self.state = "rest"
        if self.ledge is not None and self.vy == 0:
            old = self.ledge
            near = [lg for lg in self.app.ledges if abs(lg[2] - old[2]) <= 90 and abs(lg[0] - old[0]) <= 12 and abs(lg[1] - old[1]) <= 12]
            if near:
                lg = min(near, key=lambda lg: abs(lg[2] - old[2]))
                if abs(lg[2] - old[2]) > 3:  # Kante bewegt sich: Ball fällt
                    self.ledge = None
                    self.state = "air"
                    return
                self.ledge = lg
                if not (self.ledge[0] <= self.x <= self.ledge[1]):
                    self.ledge = None  # rollt über die Kante
                    self.state = "air"
            else:
                self.ledge = None
                self.state = "air"

    def draw(self, cv, now):
        cx, cy, r = self.x, self.y - self.r, self.r
        cols = ("#D97757", "#f1e4cc", "#D97757", "#f1e4cc", "#D97757", "#f1e4cc")
        base = math.degrees(self.ang)
        for i, c in enumerate(cols):
            cv.create_arc(cx - r, cy - r, cx + r, cy + r, start=base + i * 60, extent=60, fill=c, outline="")
        cv.create_oval(cx - r * 0.22, cy - r * 0.22, cx + r * 0.22, cy + r * 0.22, fill="#f1e4cc", outline="")
        cv.create_oval(cx - r, cy - r, cx + r, cy + r, outline="#7c2d12", width=max(1, int(r * 0.12)))
        cv.create_oval(cx - r * 0.6, cy - r * 0.65, cx - r * 0.25, cy - r * 0.35, fill="#ffffff", outline="")


# ------------------------------------------------------------------ Hinterlassenes: Portale, Risse, Fähnchen, Blumen, Marker, Türme
class Decor:
    """Ein Ding in der Welt mit eigenem Fenster. Fährt mit seiner Kante mit (Scrollen), verschwindet nach `life`."""

    SIZES = {"portal": (90, 160), "crack": (140, 420), "flag": (70, 130), "flower": (60, 110), "tower": (90, 160)}

    def __init__(self, app, kind, x, y, life, ledge=None, **kw):
        self.app = app
        self.kind = kind
        self.x, self.y = x, y
        self.t0 = time.time()
        self.life = life
        self.ledge = ledge
        self.kw = kw
        if kind == "mark":
            self.win = (int(kw.get("w", 200)) + 20, 90)
        else:
            self.win = kw.get("win") or self.SIZES.get(kind, (80, 120))
        self.mini = "decor"
        self.leaving = None
        self.state = "decor"
        self.target = None
        self.emotes = []
        self.size = 0.5
        self.t = 0.0
        self.vx = self.vy = 0.0
        self.dead = False
        self.prev_y = None
        self.role = None
        self.base = None
        self.broken = 0.0
        self.pieces = []
        self.falling = False
        self.lying = False
        self.anchor = None
        self.tried = True  # keine Verankerung (Plattformer-Regel)
        self.hidden = False
        self.seen = time.time()
        self.debris = [[x + random.uniform(-6, 6), y - random.uniform(10, 60), random.uniform(-2.5, 2.5), random.uniform(-3, 0)]
                       for _ in range(10)] if kind == "crack" else []

    grounded = True
    w = h = 0

    def set(self, *a, **k):
        pass

    def hop(self, *a, **k):
        pass

    def emote(self, *a, **k):
        pass

    def busy(self):
        return False

    def think(self, now):
        pass

    def pose(self, now):
        pass

    def hit(self, x, y):
        if self.hidden or self.pieces or self.dead:
            return False
        X, Y, k = self.x, self.y, self.kind
        box = {"flag": (-6, -52, 26, 4), "flower": (-12, -38, 12, 4), "tower": (-16, -80, 16, 4)}.get(k)
        if k == "flower" and self.lying:
            box = (-26, -14, 14, 4)
        if k == "mark":
            w = self.kw.get("w", 200)
            dy = self.kw.get("dy", 24)
            box = (-w / 2, dy - 8, w / 2, dy + 8)
        return bool(box) and X + box[0] <= x <= X + box[2] and Y + box[1] <= y <= Y + box[3]

    def smash(self):
        """vom Nutzer angeklickt: zerspringt"""
        now = time.time()
        if self.kind == "mark":
            self.life = (now - self.t0) + 0.35
        else:
            cols = {"flag": ["#57534e", self.kw.get("color", "#D97757")], "flower": [self.kw.get("color", "#ec4899"), "#facc15", "#16a34a"],
                    "tower": ["#D97757", "#f59e0b", "#3b82f6", "#22c55e", "#ec4899", "#8b5cf6", "#facc15"]}.get(self.kind, ["#a8a29e"])
            for i in range(14):
                self.pieces.append([self.x + random.uniform(-8, 8), self.y - random.uniform(4, 50),
                                    random.uniform(-4, 4), random.uniform(-7, -2), random.choice(cols)])
            self.life = (now - self.t0) + 1.3
            self.anchor, self.falling, self.lying = None, False, False
        self.app.decor_broken(self, by_user=True)

    def physics(self, W, H):
        if time.time() - self.t0 > self.life:
            self.dead = True
            return
        a = self.app
        if self.pieces:
            for pc in self.pieces:
                pc[0] += pc[2]
                pc[1] += pc[3]
                pc[3] += 0.7
            return
        if self.falling:  # entwurzelte Blume fällt
            prev = self.y
            self.vy += 0.7
            self.x += self.vx
            self.y += self.vy
            self.vx *= 0.97
            comp = a.composer()
            if not comp:
                self.dead = True
                return
            mid = (comp[0] + comp[1]) / 2
            if not (comp[0] + 20 <= self.x <= comp[1] - 20):
                self.vx += 0.25 if self.x < mid else -0.25  # trudelt zum Eingabefeld
            land = comp[2]
            if self.y >= land and comp[0] + 10 <= self.x <= comp[1] - 10:
                fx_ = a.free_x(comp, self.x, gap=26)
                if fx_ is not None:
                    self.x = fx_
                self.y = land
                self.falling = False
                self.lying = True
                self.ledge = comp
                self.role, self.base = "comp", None
                self.anchor, self.tried, self.prev_y = None, True, None
                self.life = (time.time() - self.t0) + 180
            return
        if self.ledge is not None and self.anchor is None and not self.tried:
            self.tried = True
            hit = a.anchor_for(self.x, self.ledge[2])
            if hit:
                k, r = hit
                self.anchor = (k, self.x - r[0], self.y - r[1])
        if self.anchor:
            k, offx, offy = self.anchor
            r = a.anchor_rect(k)
            if r is None:
                self.hidden = True
                if time.time() - self.seen > 120:
                    self.dead = True  # Nachricht ist weg
                return
            self.seen = time.time()
            nx, ny = r[0] + offx, r[1] + offy
            if self.prev_y is not None and abs(ny - self.prev_y) > 1.5 and not self.lying:
                self.on_scroll(ny - self.prev_y)
                return
            self.prev_y = ny
            self.x, self.y = nx, ny
            self.hidden = not a.in_view(k, self.y)
        elif self.ledge is not None and self.role is None:
            comp, band = a.comp_exact, a.world.get("band")
            if comp and abs(self.ledge[2] - comp[2]) <= 6:
                self.role = "comp"
            elif band and abs(self.ledge[2] - band[1]) <= 6:
                self.role = "band"
            else:
                self.role = "other"
        if self.anchor:
            pass
        elif self.role in ("comp", "band") and not self.lying_fix():
            return
        elif self.ledge is not None and self.role == "other":
            old = self.ledge
            near = [lg for lg in a.ledges if abs(lg[2] - old[2]) <= 40 and abs(lg[0] - old[0]) <= 12 and abs(lg[1] - old[1]) <= 12]
            if near:
                lg = min(near, key=lambda g: abs(g[2] - old[2]))
                if abs(lg[2] - old[2]) > 3 and not self.lying:
                    self.on_scroll(lg[2] - old[2])  # Untergrund bewegt sich: kein Mitfahren
                    return
                self.ledge = lg
            elif not self.lying:
                self.on_scroll(0)  # Untergrund weg: Fahne bricht, Turm stürzt ein, Blume fällt …

    def lying_fix(self):
        """Deko auf Eingabefeld/Leiste: exakt mit der Box mitfahren. False = Box weg -> zerbrochen."""
        a = self.app
        if self.role == "comp":
            c = a.comp_exact
            box = (c[0], c[1], c[2]) if c else None
        else:
            b = a.world.get("band")
            box = (b[0], b[2], b[1]) if b else None
        if box is None:
            if not self.lying:
                self.on_scroll(0)
            else:
                self.dead = True
            return False
        if self.base is None:
            self.base = (box[0], box[2], self.x - box[0])
        dy = box[2] - self.base[1]
        if abs(dy) > 3 or abs(box[0] - self.base[0]) > 6:
            if self.lying:
                self.life = min(self.life, time.time() - self.t0 + 0.6)  # liegende Blume verschwindet
            else:
                self.on_scroll(dy)  # Box verändert sich: kippt um / fällt
            return False
        self.x = box[0] + self.base[2]
        self.y = box[2]
        self.base = (box[0], box[2], self.base[2])
        self.ledge = (box[0], box[1], box[2])
        if not (box[0] <= self.x <= box[1]):
            self.on_scroll(0) if not self.lying else None
        return True

    def on_scroll(self, dy):
        """Untergrund bewegt sich: Fahne bricht, Turm stürzt ein, Strich verblasst, Blume wird entwurzelt."""
        now = time.time()
        k = self.kind
        self.anchor = None
        if k == "mark":
            self.life = (now - self.t0) + 0.35
        elif k == "flower":
            self.falling = True
            self.vx, self.vy = random.uniform(-2.5, 2.5), -4.0
            self.ledge = None
        elif k in ("flag", "tower"):
            cols = (["#57534e"] * 4 + [self.kw.get("color", "#D97757")] * 4) if k == "flag" else                 ["#D97757", "#f59e0b", "#3b82f6", "#22c55e", "#ec4899", "#8b5cf6", "#facc15"]
            for i in range(10 if k == "flag" else 14):
                self.pieces.append([self.x + random.uniform(-8, 8), self.y - random.uniform(4, 50),
                                    random.uniform(-3, 3), random.uniform(-6, -1), random.choice(cols)])
            self.life = (now - self.t0) + 1.3
            self.app.decor_broken(self)

    def draw(self, cv, now):
        if self.hidden:
            return
        if self.pieces:
            for pc in self.pieces:
                cv.create_rectangle(pc[0] - 3, pc[1] - 3, pc[0] + 3, pc[1] + 3, fill=pc[4], outline="")
            return
        if self.kind == "flower" and (self.lying or self.falling):
            x, y = self.x, self.y
            rot = self.falling and (now * 8) or 0
            if self.lying:
                cv.create_line(x - 14, y - 3, x + 10, y - 3, fill="#16a34a", width=2)
                for i in range(5):
                    a_ = i * 1.2566
                    cv.create_oval(x - 18 + math.cos(a_) * 4 - 3, y - 6 + math.sin(a_) * 4 - 3,
                                   x - 18 + math.cos(a_) * 4 + 3, y - 6 + math.sin(a_) * 4 + 3, fill=self.kw.get("color", "#ec4899"), outline="")
                cv.create_oval(x - 21, y - 9, x - 15, y - 3, fill="#facc15", outline="")
            else:
                for i in range(5):
                    a_ = i * 1.2566 + rot
                    cv.create_oval(x + math.cos(a_) * 4 - 3, y - 20 + math.sin(a_) * 4 - 3,
                                   x + math.cos(a_) * 4 + 3, y - 20 + math.sin(a_) * 4 + 3, fill=self.kw.get("color", "#ec4899"), outline="")
                cv.create_line(x, y - 16, x + math.sin(rot) * 10, y, fill="#16a34a", width=2)
            return
        age = now - self.t0
        k = self.kind
        x, y = self.x, self.y
        fade = max(0.0, min(1.0, (self.life - age) / 1.0))  # letzte Sekunde: schrumpft weg
        if k == "fx":
            self.kw["fn"](cv, now, self)
            return
        if k == "portal":
            col, dark = (("#3b82f6", "#1e3a8a") if self.kw.get("color") == "blue" else ("#f97316", "#7c2d12"))
            grow = min(1.0, age / 0.18) * fade
            rw, rh = 17 * grow, 40 * grow
            cy = y - 40
            if rw > 1:
                cv.create_oval(x - rw - 4, cy - rh - 4, x + rw + 4, cy + rh + 4, fill=col, outline="")
                cv.create_oval(x - rw, cy - rh, x + rw, cy + rh, fill=dark, outline="")
                for i in range(3):  # Wirbel
                    a = now * 6 + i * 2.1
                    cv.create_oval(x + math.cos(a) * rw * 0.5 - 2, cy + math.sin(a) * rh * 0.5 - 2,
                                   x + math.cos(a) * rw * 0.5 + 2, cy + math.sin(a) * rh * 0.5 + 2, fill=col, outline="")
        elif k == "crack":
            c = "#1c1917"
            pts = [(0, -95), (-6, -78), (4, -66), (-8, -50), (3, -38), (-5, -22), (2, -8)]
            for (ax, ay), (bx, by) in zip(pts, pts[1:]):
                cv.create_line(x + ax, y + ay, x + bx, y + by, fill=c, width=3)
            for (ax, ay, bx, by) in ((-8, -50, -20, -58), (4, -66, 16, -72), (3, -38, 14, -30)):
                cv.create_line(x + ax, y + ay, x + bx, y + by, fill=c, width=2)
            for d in self.debris:
                d[0] += d[2]
                d[3] += 0.5
                d[1] += d[3]  # fällt einfach aus dem Bild
                cv.create_rectangle(d[0] - 2, d[1] - 2, d[0] + 2, d[1] + 2, fill="#a8a29e", outline="")
        elif k == "flag":
            col = self.kw.get("color", "#D97757")
            grow = min(1.0, age / 0.4) * fade
            top = y - 46 * grow
            cv.create_line(x, y, x, top, fill="#57534e", width=2)
            wave = math.sin(now * 5) * 3
            cv.create_polygon(x, top, x + 22 * grow, top + 6 + wave, x, top + 13 * grow, fill=col, outline="")
            cv.create_oval(x - 2, top - 3, x + 2, top + 1, fill="#facc15", outline="")
        elif k == "flower":
            col = self.kw.get("color", "#ec4899")
            grow = min(1.0, age / 0.8) * fade
            top = y - 30 * grow
            cv.create_line(x, y, x, top, fill="#16a34a", width=2)
            cv.create_polygon(x, y - 10 * grow, x + 8 * grow, y - 16 * grow, x + 2, y - 8 * grow, fill="#22c55e", outline="")
            r = 5 * grow
            sway = math.sin(now * 2 + self.t0) * 2
            for i in range(5):
                a = i * 1.2566 + now * 0.3
                cv.create_oval(x + sway + math.cos(a) * r - r * 0.7, top + math.sin(a) * r - r * 0.7,
                               x + sway + math.cos(a) * r + r * 0.7, top + math.sin(a) * r + r * 0.7, fill=col, outline="")
            cv.create_oval(x + sway - r * 0.55, top - r * 0.55, x + sway + r * 0.55, top + r * 0.55, fill="#facc15", outline="")
        elif k == "mark":
            wdt = self.kw.get("w", 200)
            prog = min(1.0, age / 0.6)  # wird von links nach rechts gezogen
            left = x - wdt / 2
            n = int(wdt / 6 * prog * fade)
            yy = y + self.kw.get("dy", 24)
            for i in range(n):
                xx = left + i * 6
                cv.create_line(xx, yy + math.sin(i * 0.9) * 1.2, xx + 6, yy + math.sin((i + 1) * 0.9) * 1.2,
                               fill="#f59e0b", width=3, capstyle="round")
        elif k == "tower":
            grow = min(1.0, age / self.kw.get("build", 4.0)) * fade
            n = max(1, int(7 * grow))
            cols = ("#D97757", "#f59e0b", "#3b82f6", "#22c55e", "#ec4899", "#8b5cf6", "#facc15")
            for i in range(n):
                off = 3 if i % 2 else -3
                cv.create_rectangle(x - 9 + off, y - (i + 1) * 9, x + 9 + off, y - i * 9 - 1, fill=cols[i % 7], outline="#44403c")
            if grow >= 1:
                cv.create_line(x, y - n * 9, x, y - n * 9 - 14, fill="#57534e", width=2)
                cv.create_polygon(x, y - n * 9 - 14, x + 11, y - n * 9 - 10, x, y - n * 9 - 6, fill="#D97757", outline="")


# ------------------------------------------------------------------ die App
class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.wins = {}  # Critter -> (Toplevel, Canvas, ww, wh)
        self.win_origin = {}
        self.win_geo = {}
        self.owns = set()

        self.claude = None
        self.rect = (0, 0, 800, 600)
        self.W, self.H = 800, 600
        self.visible = True
        self.ledges = []
        self.last_scan = 0.0
        self.mode = "idle"
        self.tool = ""
        self.event = ""
        self.event_at = 0
        self.event_until = 0.0
        self.typing = 0
        self.typing_times = []
        self.text = ""
        self.caret_x = None
        self.state = {}
        self.agents = {}
        self.drag = None
        self.main = None
        self.last_act = time.time()
        self.bulb_at = 0.0
        self.live_at = 0.0
        self.demo_until = 0.0
        self.ball = None
        self.decor = []
        self.brain_at = time.time() + 8
        self.recent = []
        self.joy = 0.0
        self.fetch = None
        self.fetch_t = 0.0
        self.fetch_fly = 0.0
        self.crumb_at = 0.0
        self.del_times = []
        self.caps_seen = False
        self.long_word = ''
        self.active_since = None
        self.break_cool = time.time() + 600
        self.think_since = None
        self.think_emote = 0.0
        self.evt_cool = {}
        self.crumbs = []
        self.stuck_since = None
        self.act = ''
        self.limit = -1
        self.clip_seq = None
        self.papers = []
        self.hide_at = None
        self.last_mode = 'idle'
        self.dismissed = set()
        self.agent_seen = {}
        self.keep_until = 0.0
        self.word_req = []
        self.word_res = {}
        self.enter_cool = 0.0
        self.type_start = 0.0
        self.carry = None
        self.pets = []
        self.anchors = {}
        self.anchor_want = set()
        self.ball_hype = 0.0
        self.ball_chase = 0.0
        self.popup_seen = 0
        self.input_at = time.time()
        self.solo_at = 0.0
        self.click_hunt_cool = 0.0
        self.para_level = 0
        self.kick_cool = 0.0
        self.mouse_hist = []
        self.pet_flips = []
        self.pet_cool = 0.0
        self.flinch_cool = 0.0
        self.curious_cool = 0.0
        self.mouse_still = 0.0
        self.lmb = False
        self.clicks = []
        self.glance = None
        self.pause_seen = ""
        self.away_since = None
        self.last_size = None
        self.last_msg_top = None
        self.caret_y = None
        self.greeted = False
        self.checkin_at = 0.0
        self.work_sig = None
        self.seen_floor = 0.0
        self.caret = None
        self.comp_exact = None
        self.world = {"ledges": [], "rows": [], "panes": [], "last_msg": None, "streaming": None, "sidebar": None}
        self.seen_rows = None
        self.seen_panes = None
        self.explore_at = time.time() + 20
        self.base_ledges = []
        self.scene = []
        self.step = None
        self.built = []  # selbstgebaute Plattformen
        self.prop = None  # (name, bis) – kurzzeitige Requisite
        self.cur_prop = None
        self.work_since = None
        self.hover_since = None
        self.blush_until = 0.0
        self.spell = None  # letzter Rotstrich (x, y)
        self.spell_cool = 0.0
        self.error_cool = 0.0

        self.pending = None
        self.scan_now = True
        self.scans = 0
        self.perf = {'n': 0, 'sum': 0.0, 'max': 0.0, 'last': time.time(), 't': time.time()}
        self.scan_lock = threading.Lock()
        self.beat = self.scan_beat = time.time()
        threading.Thread(target=self.scanner, daemon=True).start()
        threading.Thread(target=self.watchdog, daemon=True).start()
        self.poll()

    # ---------------------------------------------- Helfer
    @property
    def working(self):
        return self.mode in ("thinking", "tool-use", "responding")

    @property
    def typing_recent(self):
        return bool(self.typing_times) and time.time() - self.typing_times[-1] < 1.5

    @property
    def typing_fast(self):
        now = time.time()
        return len([t for t in self.typing_times if now - t < 1.0]) >= 6

    @property
    def event_recent(self):
        return self.event if time.time() < self.event_until else ""

    def to_screen(self, x, y):
        return self.rect[0] + x, self.rect[1] + y

    def composer(self):
        """Eingabefeld = unterste lange Kante im unteren Viertel des Inhalts."""
        ce = getattr(self, "comp_exact", None)
        if ce:
            near = [lg for lg in self.ledges if abs(lg[2] - ce[2]) <= 8 and abs(lg[0] - ce[0]) <= 12]
            return near[0] if near else ce
        cands = [lg for lg in self.ledges if lg[2] > self.H * 0.7 and lg[1] - lg[0] > self.W * 0.25]
        return min(cands, key=lambda lg: abs(lg[2] - (self.H - 110))) if cands else None

    def all(self):
        return [c for c in [self.main, *self.agents.values(), self.ball, *self.decor] if c]

    NATURE = ("flag", "flower", "tower")

    def free_x(self, ledge, px, gap=30):
        """Freier Platz auf der Kante nahe px (kein Ding auf einem anderen). None = kein Platz."""
        lo, hi = ledge[0] + 16, ledge[1] - 16
        if hi < lo:
            return None  # Kante zu schmal
        taken = [d.x for d in self.decor if not d.dead and d.kind in self.NATURE and not d.pieces
                 and not d.falling and not d.hidden and abs(d.y - ledge[2]) < 10]
        for k in (0, 1, -1, 2, -2, 3, -3, 4, -4):
            x = max(lo, min(hi, px + k * gap))
            if all(abs(x - o) >= gap for o in taken):
                return x
        return None

    def add_decor(self, kind, x, y, life, ledge=None, **kw):
        d = Decor(self, kind, x, y, life, ledge, **kw)
        self.decor = [e for e in self.decor if not e.dead]
        if kind in self.NATURE:
            nat = [e for e in self.decor if e.kind in self.NATURE and not e.pieces]
            if len(nat) >= 8:  # zu viele: das älteste schrumpft weich weg statt hart zu verschwinden
                old = nat[0]
                old.life = min(old.life, time.time() - old.t0 + 1.0)
        self.decor = (self.decor + [d])[-24:]
        return d

    # ---------------------------------------------- Anker: Dinge kleben an Nachrichten / Sidebar-Zeilen
    def anchor_rect(self, key):
        return self.anchors.get(key) or self.world.get("anchor_rects", {}).get(key)

    def anchor_for(self, x, y):
        best = None
        for k, r in self.world.get("anchor_rects", {}).items():
            if r[0] - 2 <= x <= r[2] + 2 and r[1] - 2 <= y + 3 <= r[3] + 2 and self.in_view(k, y):
                area = (r[2] - r[0]) * (r[3] - r[1])
                if best is None or area < best[0]:
                    best = (area, k, r)
        return (best[1], best[2]) if best else None

    def in_view(self, key, y):
        w = self.world
        if key.startswith("m:"):
            vt, vb = w.get("view") or (0, self.H)
            return vt + 2 <= y <= vb
        if key.startswith("r:") and w.get("sidebar"):
            sb = w["sidebar"]
            return sb[1] + 70 <= y <= sb[3] - 4
        return True

    def ride(self, o):
        """o steht auf o.ledge: exakt mit seinem UI-Element mitfahren.
        True = erledigt, False = kein Anker (alte Logik), None = Kante weg/aus dem Sichtbereich."""
        if o.ledge is None:
            o.anchor = None
            return False
        if getattr(o, "anchor_src", None) is not o.ledge:
            o.anchor_src = o.ledge
            hit = self.anchor_for(o.x, o.ledge[2])
            if not hit:
                o.anchor = None
                return False
            k, r = hit
            o.anchor = (k, o.ledge[0] - r[0], o.ledge[1] - r[0], o.ledge[2] - r[1])
            o.anchor_x = r[0]
        if getattr(o, "anchor", None) is None:
            return False
        k, l0, l1, dy = o.anchor
        r = self.anchor_rect(k)
        if r is None or not self.in_view(k, r[1] + dy):
            o.anchor = None
            return None
        o.x += r[0] - o.anchor_x
        o.anchor_x = r[0]
        o.ledge = (r[0] + l0, r[0] + l1, r[1] + dy)
        o.anchor_src = o.ledge
        o.y = o.ledge[2]
        return True

    def ledge_at(self, x, y, tol=8):
        return next((lg for lg in self.ledges if abs(lg[2] - y) <= tol and lg[0] - 2 <= x <= lg[1] + 2), None)

    def walls(self):
        """Senkrechte UI-Grenzen, durch die das Tierchen brechen kann."""
        w = self.world
        xs = []
        if w.get("sidebar"):
            xs.append(w["sidebar"][2])
        for pl, *_r in w.get("panes", []):
            xs.append(pl)
        return xs

    # ---------------------------------------------- Maus
    def press(self, ev):
        ev.x, ev.y = ev.x_root - self.rect[0], ev.y_root - self.rect[1]
        self.last_act = time.time()
        for d in reversed(self.decor):
            if d.hit(ev.x, ev.y):
                self.smash_click_at = time.time()  # dieser Klick gilt dem Bauwerk, nicht ihm (kein Erschrecken)
                d.smash()
                return
        for c in self.all():
            if c.hit(ev.x, ev.y):
                self.drag = {"c": c, "dx": ev.x - c.x, "dy": ev.y - c.y, "hist": [(time.time(), ev.x, ev.y)], "moved": False, "t0": time.time()}
                return

    def move(self, ev):
        ev.x, ev.y = ev.x_root - self.rect[0], ev.y_root - self.rect[1]
        d = self.drag
        if not d:
            return
        c = d["c"]
        d["moved"] = True
        c.set("drag")
        c.ledge = None
        c.target = None
        c.x, c.y = ev.x - d["dx"], ev.y - d["dy"]
        d["hist"] = (d["hist"] + [(time.time(), ev.x, ev.y)])[-6:]
        if c is self.ball and len(d["hist"]) >= 2:
            now = time.time()
            dx = d["hist"][-1][1] - d["hist"][-2][1]
            if abs(dx) > 3:
                sg = 1 if dx > 0 else -1
                fl = d.setdefault("flips", [])
                if not fl or fl[-1][1] != sg:
                    fl.append((now, sg))
                d["flips"] = [f for f in fl if now - f[0] < 1.5]
                if len(d["flips"]) >= 4 and self.main and now > self.ball_hype:
                    self.ball_hype = now + 6  # schaut gebannt, will ihn haben
                    m = self.main
                    self.flash("excited", 1.5)
                    m.emote("!", "#D97757")
                    if m.state == "sleep":
                        m.set("idle")
                    m.hop(4)

    def release(self, ev):
        ev.x, ev.y = ev.x_root - self.rect[0], ev.y_root - self.rect[1]
        d = self.drag
        self.drag = None
        if not d:
            return
        c = d["c"]
        if c is self.ball and time.time() < self.ball_hype:
            self.ball_chase = time.time() + 10  # losgelassen: hinterher!
        if c is self.ball and d["moved"]:
            self.fetch, self.fetch_t, self.fetch_fly = "get", time.time(), 0.0  # geworfen: er holt ihn
        if d["moved"]:
            (t0, x0, y0), (t1, x1, y1) = d["hist"][0], d["hist"][-1]
            dt = max(0.016, t1 - t0)
            c.vx, c.vy = (x1 - x0) / dt / 33, (y1 - y0) / dt / 33
            if c is self.ball:  # schwerer Ball: Wurf gedeckelt
                c.vx = max(-15, min(15, c.vx * 0.85))
                c.vy = max(-18, min(18, c.vy * 0.85))
            c.set("air")
        else:
            c.hop(8)
            c.emote("♥")
            self.flash("pet", 1.0)

    def double(self, ev):
        ev.x, ev.y = ev.x_root - self.rect[0], ev.y_root - self.rect[1]
        for c in self.all():
            if c.hit(ev.x, ev.y):
                c.hop(13)
                c.set("air")
                c.emote("✦", "#f59e0b")

    def menu(self, ev):
        ev.x, ev.y = ev.x_root - self.rect[0], ev.y_root - self.rect[1]
        if not any(c.hit(ev.x, ev.y) for c in self.all()):
            return
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="Sleep / Wake up", command=self.toggle_sleep)
        m.add_command(label="Dance", command=lambda: self.main and self.main.set("dance", 4))
        m.add_command(label="Put the ball away" if self.ball else "Play ball", command=self.toggle_ball)
        if self.agents:
            m.add_command(label="Send mini agents away", command=self.dismiss_agents)
        sz = tk.Menu(m, tearoff=0)
        for name, sc in (("Small", 3.5), ("Medium", 5.0), ("Large", 7.0)):
            sz.add_command(label=name, command=lambda sc=sc: (settings.update(scale=sc), save_settings()))
        m.add_cascade(label="Size", menu=sz)
        m.add_command(label="Eyes: black / orange shine",
                      command=lambda: (settings.update(shine="none" if settings.get("shine") == "orange" else "orange"), save_settings()))
        m.add_separator()
        m.add_command(label="Quit (bring back with /critter)", command=self.root.destroy)
        m.tk_popup(ev.x_root, ev.y_root)

    def dismiss_agents(self):
        now = time.time()
        for aid, c in self.agents.items():
            self.dismissed.add(aid)
            if not c.leaving:
                c.set("wave", 0.4)
                c.leaving = now
        if self.main:
            self.main.emote("♥", "#22c55e")

    def toggle_ball(self):
        if self.ball:
            self.ball = None
            return
        m = self.main
        if m:
            self.ball = Ball(self, max(40, min(self.W - 40, m.x + m.facing * 60)), max(40, m.y - 160))
            self.event_until = 0  # Ball raus: schlechte Laune ist vergessen
            m.emote("✦", "#f59e0b")
            m.hop(6)

    def toggle_sleep(self):
        if self.main:
            self.main.set("idle" if self.main.state == "sleep" else "sleep")
            self.last_act = time.time()

    def flash(self, ev, secs=1.6):
        self.event, self.event_until = ev, time.time() + secs

    # ---------------------------------------------- Status vom Plugin
    def poll(self):
        try:
            try:
                st = json.load(open(STATE, encoding="utf-8"))
            except Exception:
                st = None
            if st:
                self.react(st)
            self.demo()
        except Exception:
            log_err()
        self.root.after(100, self.poll)

    def unstick(self, now):
        """Szene abgebrochen (z. B. durch Tippen)? Dann nicht in der Luft / beim Ziehen hängen bleiben."""
        m = self.main
        if not m or self.scene or self.step is not None:
            self.stuck_since = None
            return
        if m.state in ("hover", "pull", "pound") or (m.state == "tap" and self.mode != "waiting" and not m.until):
            self.stuck_since = self.stuck_since or now
            if now - self.stuck_since > 0.6:
                self.stuck_since = None
                if m.state in ("hover", "pull") and m.ledge is None:
                    m.set("fall")  # fällt sanft auf die nächste Kante
                else:
                    m.set("idle")
        else:
            self.stuck_since = None

    def watchdog(self):
        """Neustart, wenn die Haupt-Schleife (8 s) oder der UI-Leser (20 s) hängt."""
        while True:
            time.sleep(2)
            now = time.time()
            if now - self.beat > 8 or now - self.scan_beat > 20:
                restart_self()

    def demo(self):
        """Test-Auslöser: ~/.claude/critter/demo.json = {"do": "row"|"pane"|"read"|"side"|"home"|"error"}."""
        f = os.path.join(DIR, "demo.json")
        if not os.path.exists(f) or not self.main:
            return
        try:
            do = json.load(open(f, encoding="utf-8-sig")).get("do")
        except Exception:
            do = None
        try:
            os.remove(f)
        except OSError:
            pass
        m, w = self.main, self.world
        self.scene, self.step = [], None
        self.demo_until = time.time() + 16
        if do == "row" and w.get("rows"):
            lg, kind, _ = w["rows"][0]
            self.visit_row(lg, kind)
        elif do == "pane":
            self.seen_panes = max(0, len(w.get("panes", [])) - 1)
            self.explore(time.time())
        elif do in ("read", "side"):
            self.explore_at, self.last_act = 0, 0
            self.mode_backup = self.mode
            comp = self.composer()
            pool = [lg for lg in self.ledges if comp and lg[2] < comp[2] - 40 and lg[1] - lg[0] > m.w * 2 and
                    ((lg[0] > (w["sidebar"][2] if w.get("sidebar") else 0)) == (do == "read"))]
            if pool:
                lg = random.choice(pool)
                self.play([{"do": "fly", "x": random.uniform(lg[0] + m.w, lg[1] - m.w), "y": lg[2]},
                           {"do": "prop", "name": "book" if do == "read" else None, "secs": 4},
                           {"do": "wait", "secs": 4}, {"do": "home"}])
        elif do == "home":
            self.play([{"do": "home"}])
        elif do in ():
            self.keep_until = time.time() + 60
            self.demo_until = time.time() + 60
            ok_ = getattr(self, "sc_" + do)()
            w_ = self.world
            open(os.path.join(DIR, "overlay.log"), "a", encoding="utf-8").write(
                "demo %s -> %s | texts=%d sidebar=%s W=%s" % (do, ok_, len(w_.get("texts", [])), w_.get("sidebar"), self.W) + chr(10))
        elif do and do.startswith("smash:"):  # Nutzer-Klick simulieren: Deko einer Art zerstören
            for d in [d for d in self.decor if d.kind == do[6:] and not d.pieces and not d.dead][:1]:
                d.smash()
        elif do == "uproot":  # Scrollen simulieren: Blumen entwurzeln -> fallen aufs Eingabefeld
            for d in [d for d in self.decor if d.kind == "flower" and not d.lying and not d.falling]:
                d.on_scroll(10)
        elif do == "shatter":  # Scrollen simulieren: Fahnen/Türme brechen
            for d in [d for d in self.decor if d.kind in ("flag", "tower") and not d.pieces]:
                d.on_scroll(10)
        elif do == "annoyed":
            self.flash("determined", 3.0)
            m.emote("✕", "#e5484d", 1.8)
            m.point = (m.x + 120, m.y - 60, time.time() + 0.6)
            if m.ledge:
                m.target = max(m.ledge[0] + m.w * 0.6, min(m.ledge[1] - m.w * 0.6, m.x - 170))
                m.set("walk")
        elif do == "breakfree":
            m.vx, m.vy = random.choice([-1, 1]) * 6, -9
            m.set("air")
            self.flash("determined", 3.0)
            m.emote("!", "#e5484d", 1.6)
        elif do == "agent":
            aid = "demo%d" % len(self.agents)
            c = Critter(self, max(60, min(self.W - 60, m.x + 80)), m.y - m.h - 10, size=random.choice([0.5, 0.72]), mini=aid)
            c.hop(9)
            self.agents[aid] = c
            m.set("wave", 1.2)
        elif do == "agentbye":
            for c in self.agents.values():
                c.leaving = time.time()
            m.emote("♥", "#22c55e")
        elif do == "sleep":
            m.set("sleep")
        elif do == "wake":
            m.set("idle")
            self.flash("startle", 0.8)
        elif do == "dance":
            m.set("dance", 3)
        elif do == "checkin":
            self.check_in(time.time())
        elif do == "ask":
            m.set("tap", 4)
            self.prop = ("sign", time.time() + 4)
            m.emote("?", "#D97757", 2.0)
        elif do == "granted":
            m.set("idle")
            m.hop(7)
            m.emote("✓", "#22c55e")
            self.flash("love", 1.2)
        elif do == "refused":
            m.set("sulk", 2.5)
            m.emote("!", "#e5484d")
        elif do == "clipseq":
            self.clip_seq = (self.clip_seq or 0) - 12345  # Zwischenablage-Wechsel vortäuschen (Test)
        elif do == "clip":
            self.papers.append({"t0": time.time(), "dx": random.uniform(-40, 40)})
            m.set("kick", 0.5)
            m.emote("⧉", "#D97757", 1.2)
        elif do == "tired":
            m.emote("~", "#94a3b8", 2.0)
            self.flash("sleepy", 3.0)
            m.set("stretch", 1.5)
        elif do == "energy":
            m.hop(12)
            m.emote("⚡", "#f59e0b", 2.0)
            self.flash("wow", 2.0)
        elif do and do.startswith("back:"):
            self.away_since = time.time() - float(do[5:])
            self.on_show(time.time())
        elif do == "breakbuddy":
            self.active_since, self.break_cool, self.input_at = time.time() - 3100, 0, time.time()
        elif do and do.startswith("prop:"):
            self.prop = (do[5:], time.time() + 6)
        elif do and do.startswith("act:"):
            getattr(self, "act_" + do[4:])()
        elif do == "fetch":
            if not self.ball:
                self.toggle_ball()
            self.fetch, self.fetch_t, self.fetch_fly = "get", time.time(), 0.0
        elif do == "ball":
            if not self.ball:
                self.toggle_ball()
            self.ball_chase = time.time() + 40
        elif do and do.startswith("event:"):
            st = dict(self.state)
            st["event"], st["eventAt"] = do[6:], time.time()
            self.react(st)
        elif do == "portal":
            comp = self.composer()
            pool = [lg for lg in self.ledges if comp and lg[2] < comp[2] - 40 and lg[1] - lg[0] > m.w * 2 and abs(lg[2] - m.y) > 150]
            if pool:
                lg = random.choice(pool)
                self.play([{"do": "wait", "secs": 0.1}])
                m.fly_to((lg[0] + lg[1]) / 2, lg[2], "portal")
        elif do == "smash":
            sb = w.get("sidebar")
            if sb:
                side = [lg for lg in self.ledges if lg[1] <= sb[2] + 4 and lg[1] - lg[0] > m.w * 2]
                if side:
                    lg = random.choice(side)
                    m.fly_to((lg[0] + lg[1]) / 2, lg[2], "smash")
        elif do == "tower":
            self.play([{"do": "tower", "secs": 4}])
        elif do == "plant":
            self.play([{"do": "plant"}, {"do": "plant", "kind": "flag"}])

        elif do and do.startswith("say:"):
            self.typed(do[4:], "")
        elif do == "pet":
            m.set("purr", 1.4)
            self.flash("purr", 1.6)
            self.blush_until = time.time() + 1.6
            m.emote("♥", "#ec4899")
        elif do == "error":
            self.scene_error()

    def react(self, st):
        old, self.state = self.state, st
        self.mode = st.get("mode", "idle")
        self.tool = st.get("tool", "") if self.mode == "tool-use" else ""
        new_act = st.get("act", "") if self.mode == "tool-use" else ""
        if new_act == "delete" and self.act != "delete" and self.main:
            self.main.emote("!", "#e5484d", 1.4)  # es wird gelöscht: besorgt
            self.flash("scared", 1.6)
        self.act = new_act
        if self.main and self.mode != self.last_mode:
            mm = self.main
            if self.mode == "waiting":  # Claude wartet auf deine Freigabe
                mm.back_until = 0
                mm.set("tap", 30)
                mm.emote("?", "#D97757", 2.0)
            elif self.last_mode == "waiting" and self.mode == "tool-use":  # du hast erlaubt
                mm.set("idle")
                mm.hop(7)
                mm.emote("✓", "#22c55e")
                self.flash("love", 1.2)
                self.joy = min(1, self.joy + 0.1)
            elif self.last_mode == "waiting" and mm.state == "tap":
                mm.set("idle")
        self.last_mode = self.mode
        m = self.main
        if not old or not m:
            return
        now = time.time()
        # Live-Tippen (nur aus dem Plugin, wenn UI Automation den Text nicht selbst liest)
        if st.get("typing", 0) > old.get("typing", 0) and time.time() - self.live_at > 2:
            self.typed(st.get("text", ""), old.get("text", ""))
        if st.get("tool") == "step" and st.get("eventAt") == old.get("eventAt") and old.get("tool") != "step":
            m.hop(5)
            m.emote("✓", "#22c55e")
        # Arbeitsdauer (für Kaffee)
        if self.working and self.work_since is None:
            self.work_since = now
            self.checkin_at = now + 30
            self.work_sig = self.world_sig()
        elif not self.working:
            self.work_since = None
        # Ereignisse
        if st.get("eventAt") != old.get("eventAt"):
            self.last_act = now
            self.scan_now = True
            ev = st.get("event", "")
            self.flash(ev)
            if m.state == "sleep":
                m.set("idle")
            if ev in ("error", "denied"):
                self.joy = max(-1, self.joy - 0.15)
            if ev in ("love",):
                self.joy = min(1, self.joy + 0.2)
            if ev == "denied" and self.last_mode in ("waiting", "idle"):
                m.set("sulk", 2.5)  # Freigabe abgelehnt: geknickt
            if ev == "error" and now > self.error_cool:
                self.error_cool = now + 12
                self.scene_error()
            elif ev in ("error", "denied", "scared"):
                m.hop(7)
                m.emote("!", "#e5484d")
            elif ev in ("commit", "push", "tests_pass", "tests_fail", "install", "saved", "found"):
                if now > self.evt_cool.get(ev, 0):
                    self.evt_cool[ev] = now + (6 if ev in ("saved", "found") else 2)
                    if ev == "commit":
                        self.confetti()
                        m.hop(9)
                        m.emote("✓", "#22c55e")
                        self.flash("excited", 2.0)
                        self.joy = min(1, self.joy + 0.25)
                    elif ev == "push":
                        m.hop(13)
                        m.emote("↑", "#3b82f6")
                        m.emote("✦", "#f59e0b")
                        self.flash("wow", 1.5)
                    elif ev == "tests_pass":
                        self.confetti()
                        m.set("dance", 2.5)
                        m.emote("✓", "#22c55e")
                        self.joy = min(1, self.joy + 0.2)
                    elif ev == "tests_fail":
                        m.set("hide", 2.2)
                        self.flash("sad", 3)
                        m.emote("✕", "#e5484d")
                        self.joy = max(-1, self.joy - 0.2)
                    elif ev == "install":
                        m.emote("↓", "#64748b")
                        m.set("tap", 2)
                    elif ev == "saved" and m.grounded:
                        m.squash = 0.15
                        if random.random() < 0.4:
                            m.emote("✎", "#a16207", 1.0)
                    elif ev == "found":
                        self.prop = ("binoculars", now + 2)
                        m.emote("!", "#3b82f6", 1.0)
            elif ev == "done":
                self.joy = min(1, self.joy + 0.1)
                lim = st.get("limit", -1)
                if lim is not None and lim >= 0:
                    if self.limit >= 70 and lim < 20:  # Limit zurückgesetzt: wieder voller Energie
                        m.hop(12)
                        m.emote("⚡", "#f59e0b", 2.0)
                        self.flash("wow", 2.0)
                    elif lim >= 85 and self.limit < 85:
                        m.emote("~", "#94a3b8", 2.0)
                        self.flash("sleepy", 3.0)
                    self.limit = lim
                self.bulb_at = now
                m.set("happy", 1.4)
                m.hop(11)
                m.set("air")
                for s in ("✦", "✦", "♪"):
                    m.emote(s, random.choice(["#22c55e", "#f59e0b", "#3b82f6"]))
            elif ev in ("love", "excited"):
                m.hop(9)
                m.emote("♥" if ev == "love" else "!", "#ec4899" if ev == "love" else "#f59e0b")
            elif ev == "wave":
                m.set("wave", 1.6)
            elif ev == "question":
                m.emote("?", "#64748b")
            elif ev == "yawn":
                m.set("stretch", 1.5)
            elif ev in ("submit", "curious", "determined"):
                self.prop = ("push", now + 0.9)  # schiebt die Nachricht nach oben
                m.hop(8)
                m.emote("↑", "#D97757")
        # Mini-Tierchen für Agents
        for a_ in st.get("agents", []):
            self.agent_seen.setdefault(a_["id"], now)
        agents = {a["id"]: a for a in st.get("agents", [])
                  if a["id"] not in self.dismissed and now - self.agent_seen.get(a["id"], now) < 600}  # max. 10 min
        for aid, a in agents.items():
            if aid not in self.agents:
                size = MODEL_SCALE.get(a.get("model", "").lower(), 0.72)
                n = len(self.agents)
                lg = m.ledge
                if lg and m.grounded:
                    # kommt vom Rand seiner Kante angelaufen und stellt sich neben ihn (nicht in ihn hinein)
                    side = 1 if (lg[1] - m.x) > (m.x - lg[0]) else -1
                    start = lg[1] - 14 if side > 0 else lg[0] + 14
                    c = Critter(self, start, lg[2], size=size, mini=aid)
                    c.ledge = lg
                    c.target = max(lg[0] + 20, min(lg[1] - 20, m.x + side * (m.w * 0.55 + 26 + n * 44 * size)))
                    c.set("walk")
                    c.facing = -side
                else:
                    pad_x = max(60, min(self.W - 60, m.x + (1 if n % 2 == 0 else -1) * (m.w * 0.6 + 30 + n * 30)))
                    c = Critter(self, pad_x, m.y - m.h - 10, size=size, mini=aid)
                    c.hop(9)
                self.agents[aid] = c
                m.set("wave", 1.2)
        for aid, c in list(self.agents.items()):
            if aid not in agents and not c.leaving:
                c.leaving = now
                m.emote("♥", "#22c55e")

    KEYWORDS = (
        (("kaffee", "coffee", "espresso"), "coffee"),
        (("musik", "tanz", "party", "song", "feier"), "dance"),
        (("schlaf", "müde", "gute nacht", "gn8", "bett"), "yawn"),
        (("hilfe", "bug", "fehler", "kaputt", "error", "debug"), "magnifier"),
        (("wow", "krass", "geil", "cool", "nice", "mega", "omg"), "wow"),
        (("haha", "lol", "xd", "hihi", "lustig", "😂"), "laugh"),
        (("nein", "falsch", "schlecht", "mist", "doof", "nervt"), "sad"),
        (("traurig", ":(", "schade", "leider"), "cry"),
        (("hallo", "hi ", "hey", "moin", "servus", "tschüss", "ciao"), "wave"),
        (("tierchen", "critter", "süß", "suess"), "shy"),
        (("ball",), "ball"),
        (("lies", "lesen", "doku", "buch"), "book"),
        (("schreib", "notiz", "stift"), "pencil"),
        (("bau", "hammer", "build"), "hammer"),
        (("schnell", "sofort", "beeil", "asap"), "zoom"),
        (("pause", "warte", "moment"), "wait"),
        (("liebe", "love", "<3", "danke"), "love"),
    )

    def text_reactions(self, text, old_text, now, m):
        """Schlüsselwörter, Einfügen/Ausschneiden – alles lokal, keine Tokens."""
        grew = len(text) - len(old_text)
        if grew >= 25:
            self.flash("wow", 1.2)
            m.emote("⧉", "#3b82f6")  # eingefügt: staunt
            m.hop(6)
            return
        if grew <= -25 and text:
            m.emote("✂", "#64748b")
            self.flash("startle", 0.8)
            return
        if grew <= 0:
            return
        low, olow = text.lower(), old_text.lower()
        for words, act in self.KEYWORDS:
            if any(w in low and w not in olow for w in words):
                self.keyword(act, now, m)
                return

    def keyword(self, act, now, m):
        if act in ("coffee", "magnifier", "book", "hammer", "pencil"):
            self.prop = (act, now + 3.2)
            m.emote({"coffee": "~", "magnifier": "?", "book": "…", "hammer": "✦", "pencil": "✎"}[act], "#a16207")
        elif act == "dance":
            m.set("dance", 2.5)
            m.emote("♪", "#8b5cf6")
            m.emote("♫", "#ec4899")
        elif act == "yawn":
            m.set("stretch", 1.4)
            self.flash("sleepy", 2.0)
            m.emote("z", "#94a3b8")
        elif act == "wow":
            self.flash("wow", 1.2)
            m.emote("✦", "#f59e0b")
            m.hop(7)
        elif act == "laugh":
            m.set("giggle", 1.4)
            self.flash("laugh", 1.4)
            m.emote("♪", "#f59e0b")
        elif act == "sad":
            self.flash("sad", 1.8)
            m.emote("…", "#64748b")
        elif act == "cry":
            self.flash("sad", 2.2)
            m.emote("💧", "#38bdf8")
        elif act == "wave":
            m.set("wave", 1.6)
            self.flash("pet", 1.0)
        elif act == "shy":
            self.blush_until = now + 2.0
            self.flash("love", 1.5)
            m.emote("♥", "#ec4899")
        elif act == "ball":
            if not self.ball:
                self.toggle_ball()
        elif act == "zoom":
            m.emote("»", "#ef4444")
            self.flash("determined", 1.2)
        elif act == "wait":
            m.emote("⏸", "#64748b")
        elif act == "love":
            m.emote("♥", "#ec4899")
            self.flash("love", 1.4)
            m.hop(6)

    # ---------------------------------------------- Maus: streicheln, erschrecken, neugierig, Klicks
    def update_mouse(self, now):
        m = self.main
        d = self.drag
        if d and d["c"] is m and d.get("moved") and now - d["t0"] > 3.2:
            # zu lange festgehalten: windet sich frei
            self.drag = None
            m.vx, m.vy = random.choice([-1, 1]) * random.uniform(5, 8), -9
            m.set("air")
            self.flash("determined", 3.0)
            m.emote("!", "#e5484d", 1.6)
            return
        mx, my = cursor()
        lx, ly = mx - self.rect[0], my - self.rect[1]
        inside = 0 <= lx <= self.W and 0 <= ly <= self.H
        h = self.mouse_hist = [(t, x, y) for (t, x, y) in self.mouse_hist if now - t < 0.25] + [(now, lx, ly)]
        speed = math.hypot(h[-1][1] - h[0][1], h[-1][2] - h[0][2]) / max(0.03, h[-1][0] - h[0][0])
        moving = speed > 40
        if moving:
            self.mouse_still = 0.0
            self.input_at = now
        elif not self.mouse_still:
            self.mouse_still = now
        down = bool(user32.GetAsyncKeyState(0x01) & 0x8000)
        clicked = down and not self.lmb
        self.lmb = down
        if not inside or self.drag or m.state in ("drag", "fly"):
            return
        busy = self.scene or self.step is not None
        # Streicheln: Maus wackelt hin und her über dem Tierchen
        if m.hit(lx, ly) and len(h) >= 2:
            dx = h[-1][1] - h[-2][1]
            if abs(dx) > 2:
                sgn = 1 if dx > 0 else -1
                if not self.pet_flips or self.pet_flips[-1][1] != sgn:
                    self.pet_flips.append((now, sgn))
            self.pet_flips = [f for f in self.pet_flips if now - f[0] < 1.5]
            if len(self.pet_flips) >= 5 and now > self.pet_cool:
                self.pet_cool = now + 1.5
                self.pet_flips = []
                self.last_act = now
                self.pets = [t_ for t_ in self.pets if now - t_ < 40] + [now]
                if random.random() < 0.2 + 0.15 * (len(self.pets) - 1) and m.grounded:
                    # genervt: stupst Richtung Zeiger und geht weg
                    self.flash("determined", 3.0)
                    m.emote("✕", "#e5484d", 1.8)
                    m.point = (lx, ly, now + 0.5)
                    if m.ledge:
                        away = -1 if lx > m.x else 1
                        m.target = max(m.ledge[0] + m.w * 0.6, min(m.ledge[1] - m.w * 0.6, m.x + away * 170))
                        m.set("walk")
                    return
                if m.state == "sleep":
                    m.set("idle")
                m.set("purr", 1.4)
                self.joy = min(1, self.joy + 0.1)
                self.flash("purr", 1.6)
                self.blush_until = now + 1.6
                m.emote("♥", "#ec4899")
                return
        # Erschrecken: Maus rast knapp vorbei
        dist = math.hypot(lx - m.x, ly - (m.y - m.h / 2))
        if speed > 2600 and dist < 110 and not m.hit(lx, ly) and now > self.flinch_cool and m.grounded and not busy:
            self.flinch_cool = now + 4
            self.flash("startle", 0.9)
            m.vx = -3 if lx > m.x else 3
            m.hop(5)
            m.emote("!", "#f59e0b")
            return
        # Klicks im Fenster: hinschauen; Klick-Gewitter = Party
        if clicked:
            self.input_at = now
        if clicked and now - getattr(self, "smash_click_at", 0) < 0.4:
            return  # Klick hat gerade etwas zerschlagen: er schmollt, statt zu erschrecken
        if clicked and not m.hit(lx, ly):
            self.last_act = now
            self.clicks = [t for t in self.clicks if now - t < 1.5] + [now]
            self.glance = (lx, ly, now + 1.1)
            if len(self.clicks) >= 5 and m.grounded and not self.working:
                self.clicks = []
                m.set("dance", 2.0)
                m.emote("♪", "#8b5cf6")
            elif dist < 130 and m.grounded:
                self.flash("startle", 0.6)
                m.hop(4)
            elif m.state == "sleep" and dist < 300:
                m.set("idle")
                self.flash("startle", 0.8)
            elif self.free() and random.random() < 0.3 and m.ledge and m.ledge[0] < lx < m.ledge[1]:
                m.target = lx  # läuft neugierig zur Klickstelle
                m.set("walk")
            return
        # Neugier: Maus ruht über seiner Kante -> hinlaufen und hochschauen
        if (self.mouse_still and now - self.mouse_still > 1.4 and now > self.curious_cool and self.free()
                and m.ledge and m.ledge[0] < lx < m.ledge[1] and m.y - 320 < ly < m.y - m.h and abs(lx - m.x) > 50):
            self.curious_cool = now + random.uniform(8, 16)
            m.target = max(m.ledge[0] + m.w * 0.6, min(m.ledge[1] - m.w * 0.6, lx))
            m.set("walk")
            m.emote("?", "#64748b")

    # ---------------------------------------------- Ball spielen
    def play_ball(self, now):
        b = self.ball
        if not b:
            return
        if self.fetch and self.do_fetch(now, b):
            return
        if now - b.touched > 180:
            self.ball = None  # nach 3 min ohne Spiel wegräumen
            return
        for c in [self.main, *self.agents.values()]:
            if not c or c.state in ("drag", "fly", "sleep") or c.leaving:
                continue
            near = abs(b.x - c.x) < c.w * 0.55 + b.r and c.y - c.h - b.r * 2 - 4 <= b.y <= c.y + 4
            # Ball fällt auf den Kopf
            if near and b.vy > 2 and b.y < c.y - c.h * 0.6:
                b.vy = -abs(b.vy) * 0.62 - 1.5
                b.vx += random.uniform(-2, 2)
                b.touched = now
                if c is self.main:
                    self.flash("startle", 0.7)
                    c.emote("!", "#f59e0b")
                continue
            if near and c.grounded and now > self.kick_cool and abs(b.vy) < 3 and b.state != "drag" and not self.fetch:
                self.kick_cool = now + 0.7
                c.facing = 1 if b.x >= c.x else -1
                b.vx = c.facing * random.uniform(4, 8)
                b.vy = -random.uniform(3.5, 8)
                b.ledge = None
                b.state = "air"
                b.touched = now
                c.set("kick", 0.4)
                if c is self.main:
                    self.flash("excited", 0.9)
                    if random.random() < 0.4:
                        c.emote(random.choice(["✦", "♪", "!"]), random.choice(["#f59e0b", "#22c55e", "#3b82f6"]))
        m = self.main
        chase = now < self.ball_chase
        if not m or (self.working and not chase) or self.scene or self.step is not None or m.state not in ("idle", "walk", "look", "kick"):
            return
        if chase and b.ledge is not None and m.grounded and (m.ledge is None or abs(m.ledge[2] - b.ledge[2]) >= 6) and abs(b.vx) < 3:
            self.ball_chase = now + 6
            x = max(b.ledge[0] + m.w * 0.6, min(b.ledge[1] - m.w * 0.6, b.x - 40 * (1 if b.x > m.x else -1)))
            m.fly_to(x, b.ledge[2])  # fliegt oder portet hin
            return
        # hinterherlaufen / -fliegen
        if b.ledge is not None and abs(b.vx) < 1.5:
            if m.ledge is not None and abs(m.ledge[2] - b.ledge[2]) < 6:
                if abs(b.x - m.x) > m.w * 0.4:
                    m.target = b.x - (m.w * 0.35 if b.x > m.x else -m.w * 0.35)
                    if m.state == "idle":
                        m.set("walk")
            elif m.grounded and random.random() < (0.3 if chase else 0.03):
                m.fly_to(max(b.ledge[0] + m.w * 0.6, min(b.ledge[1] - m.w * 0.6, b.x - 40 * (1 if b.x > m.x else -1))), b.ledge[2])

    def do_fetch(self, now, b):
        """Apportieren: Ball holen und zu deinem Mauszeiger bringen. -> True solange es läuft."""
        m = self.main
        if not m or now - self.fetch_t > 30 or m.state in ("drag", "sleep") or b.state == "drag":
            b.carried = False
            self.fetch = None
            return False
        if self.fetch == "get":
            if abs(b.vx) > 1 or abs(b.vy) > 1:
                return True  # wartet, bis der Ball liegt
            if abs(b.x - m.x) < m.w * 0.7 and abs(b.y - m.y) < 40 and m.grounded:
                b.carried, self.fetch, self.fetch_fly = True, "carry", 0.0
                m.emote("!", "#f59e0b")
                self.flash("excited", 1.2)
                return True
            if m.grounded and m.ledge and abs(m.ledge[2] - b.y) < 6 and m.ledge[0] <= b.x <= m.ledge[1]:
                m.target = b.x - (m.w * 0.3 if b.x > m.x else -m.w * 0.3)
                if m.state == "idle":
                    m.set("walk")
            elif m.grounded and now > self.fetch_fly:
                self.fetch_fly = now + 3
                m.fly_to(b.x - 30 * (1 if b.x > m.x else -1), b.y, "fly")
            return True
        # trägt den Ball über dem Kopf zu dir
        b.x, b.y, b.vx, b.vy, b.ledge = m.x + m.facing * m.w * 0.15, m.y - m.h - 2, 0.0, 0.0, None
        mx, my = cursor()
        cx_, cy_ = mx - self.rect[0], my - self.rect[1]
        below = [lg for lg in self.ledges if lg[0] + 20 < cx_ < lg[1] - 20 and lg[2] >= cy_ - 10]
        dest = min(below, key=lambda g: g[2]) if below else self.composer()
        if not dest:
            return True
        tx = max(dest[0] + m.w * 0.6, min(dest[1] - m.w * 0.6, cx_))
        if m.ledge is None or abs(m.ledge[2] - dest[2]) > 6:
            if m.grounded and now > self.fetch_fly:
                self.fetch_fly = now + 3
                m.fly_to(tx, dest[2], "fly")
            return True
        if abs(tx - m.x) > 18:
            m.target = tx
            if m.state == "idle":
                m.set("walk")
            return True
        b.carried = False  # abgelegt – nochmal werfen!
        b.vy, b.vx, b.state = -3, m.facing * 1.0, "air"
        b.touched = now
        self.fetch = None
        m.set("wave", 1.2)
        m.emote("♥", "#ec4899")
        self.flash("excited", 1.5)
        self.joy = min(1, self.joy + 0.1)
        return True

    # ---------------------------------------------- Fenster, Scrollen, Pausen, Tageszeit
    def ambient(self, now):
        m, w = self.main, self.world
        # Tipp-Pause mitten im Satz: schaut fragend hoch
        if (self.text and self.typing_times and 4 < now - self.typing_times[-1] < 9 and self.pause_seen != self.text
                and not self.working and m.grounded and not self.scene and self.step is None):
            self.pause_seen = self.text
            self.flash("question", 1.6)
            m.emote("?", "#64748b")
            self.glance = (m.x, m.y - 400, now + 1.6)
        # Chat wurde gescrollt
        lm = w.get("last_msg")
        if lm:
            if self.last_msg_top is not None and abs(lm[1] - self.last_msg_top) > 140 and now - self.live_at < 1:
                comp = self.composer()
                dy = lm[1] - self.last_msg_top
                if m.ledge and comp and m.ledge[2] < comp[2] - 10 and m.grounded:
                    m.set("surf", 1.2)  # reitet auf der Nachricht mit
                    m.emote("~", "#3b82f6")
                    self.flash("excited", 0.8)
                elif m.grounded and not self.working:
                    self.glance = (m.x, m.y + (300 if dy > 0 else -300), now + 0.8)
            self.last_msg_top = lm[1]
        # Nachts schneller müde
        hour = time.localtime().tm_hour
        if (hour >= 23 or hour < 5) and m.state == "idle" and not self.working and not self.typing_recent and now - self.last_act > 60:
            m.set("sleep")

    def decor_broken(self, d, by_user=False):
        m = self.main
        now = time.time()
        self.joy = max(-1, self.joy - (0.25 if by_user else 0.1))
        if not m or now < getattr(self, "broken_cool", 0) or m.state in ("drag", "portal", "sleep"):
            return
        self.broken_cool = now + (1.5 if by_user else 3)
        self.glance = (d.x, d.y - 20, now + 1.2)
        if by_user:
            mx, my = cursor()
            self.glance = (mx - self.rect[0], my - self.rect[1], now + 1.8)  # schaut dich an
        if by_user:
            mood = "sad" if random.random() < 0.6 else "determined"
            self.flash(mood, 5.0)  # bleibt eine Weile beleidigt
            m.emote("…" if mood == "sad" else "✕", "#64748b" if mood == "sad" else "#e5484d", 2.5)
            if m.grounded and not self.scene:
                m.facing = -1 if self.glance[0] > m.x else 1  # dreht sich weg
                m.set("sulk", 4.5)
            return
        if random.random() < 0.5:
            self.flash("sad", 1.8)
            m.emote("…", "#64748b")
        else:
            self.flash("determined", 1.6)  # sauer: böse Brauen
            m.emote("✕", "#e5484d")
            if m.grounded:
                m.hop(4)


    # ---------------------------------------------- eigener Kopf (Leerlauf ohne Wiederholungen)
    def brain(self, now):
        m = self.main
        if now < self.brain_at or not self.free() or m.state == "sleep" or not m.grounded or now - self.last_act < 4:
            return False
        idle = now - self.last_act
        self.brain_at = now + random.uniform(6, 16)
        acts = self.brain_acts(idle)
        weights = [w * (0.12 ** self.recent.count(n)) for n, w in acts]  # kürzlich Gemachtes stark abwerten
        name = random.choices([n for n, _ in acts], weights=weights)[0]
        self.recent = (self.recent + [name])[-9:]
        getattr(self, "act_" + name)()
        return True

    def brain_acts(self, idle):
        hour = time.localtime().tm_hour
        night = hour >= 22 or hour < 6
        j = self.joy
        A = [("look", 3), ("stretch", 2), ("tap", 1.5), ("turn", 2.2), ("hop", 1.5), ("whistle", 2 + 2 * j), ("scratch", 2),
             ("sneeze", 0.7), ("peer", 2), ("wave", 1.2 + j), ("spin", 1 + j), ("hover", 1.4), ("sit", 2.5 if idle > 40 else 0.6),
             ("nap", 2.5 if (night or idle > 90) else 0.4), ("dance", 1 + 3 * max(0, j)), ("sulk", 3 * max(0, -j)),
             ("plant", 1.2), ("tower", 0.8), ("wander", 2.5), ("travel", 1.5), ("read", 1.2), ("yawn", 2 if night else 0.6)]
        if self.ball:
            A.append(("kickball", 3))
        if self.limit >= 80:  # Nutzungslimit fast erreicht: müde
            A = [(n, w * (3 if n in ("nap", "yawn", "sit") else 0.4 if n in ("dance", "spin", "hop") else 1)) for n, w in A]
        return [(n, max(0.05, w)) for n, w in A]

    def act_look(self):
        self.main.set("look", random.uniform(1.5, 3))

    def act_stretch(self):
        self.main.set("stretch", 1.4)

    def act_tap(self):
        self.main.set("tap", random.uniform(2, 3.5))

    def act_turn(self):
        m = self.main
        m.facing = random.choice([-1, 1])
        m.back_until = time.time() + random.uniform(2.5, 4.5)  # dreht dir den Rücken zu

    def act_hop(self):
        m = self.main
        if random.random() < 0.5:
            m.hop(random.uniform(5, 9))
        else:
            self.play([{"do": "call", "f": lambda: m.hop(5)}, {"do": "wait", "secs": 0.7}, {"do": "call", "f": lambda: m.hop(7)}])

    def act_whistle(self):
        m = self.main
        cols = ["#8b5cf6", "#ec4899", "#3b82f6", "#22c55e"]
        steps = []
        for i in range(random.randint(2, 4)):
            steps += [{"do": "call", "f": lambda c=cols[i % 4]: (m.emote(random.choice("♪♫"), c, 1.6), self.flash("purr", 1.2))},
                      {"do": "wait", "secs": random.uniform(0.5, 0.9)}]
        self.play(steps)

    def act_scratch(self):
        m = self.main
        m.set("scratch", 2.0)
        self.flash("question", 2.0)
        if random.random() < 0.5:
            m.emote("?", "#64748b")

    def act_sneeze(self):
        m = self.main
        self.play([{"do": "call", "f": lambda: (setattr(m, "squash", 0.3), self.flash("sleepy", 0.6))}, {"do": "wait", "secs": 0.5},
                   {"do": "call", "f": lambda: (m.emote("*", "#64748b"), setattr(m, "vx", -m.facing * 2), m.hop(3), self.flash("startle", 0.6))}])

    def act_peer(self):
        m = self.main
        if not m.ledge:
            return self.act_look()
        side = random.choice([-1, 1])
        edge = m.ledge[0] + m.w * 0.5 if side < 0 else m.ledge[1] - m.w * 0.5
        self.play([{"do": "walk", "x": edge},
                   {"do": "call", "f": lambda: setattr(self, "glance", (m.x + side * 40, m.y + 300, time.time() + 2.2))},
                   {"do": "wait", "secs": 2.2},
                   {"do": "call", "f": lambda: m.emote(random.choice(["!", "?", "…"]), "#64748b")}])

    def act_wave(self):
        m = self.main
        mx, _ = cursor()
        m.facing = 1 if mx - self.rect[0] > m.x else -1
        m.set("wave", 1.6)

    def act_spin(self):
        m = self.main
        self.play([{"do": "tween", "secs": 1.0, "f": lambda p: setattr(m, "facing", 1 if int(p * 8) % 2 else -1)},
                   {"do": "call", "f": lambda: (self.flash("wow", 1.0), m.emote("✦", "#f59e0b"))}])

    def act_hover(self):
        m = self.main
        if not m.ledge:
            return self.act_look()
        x, y = m.x, m.ledge[2]
        self.play([{"do": "fly", "x": x, "y": y - random.uniform(50, 90), "hold": True}, {"do": "wait", "secs": random.uniform(0.8, 1.6)},
                   {"do": "fly", "x": x + random.uniform(-40, 40), "y": y, "mode": "fly"}])

    def act_sit(self):
        self.main.set("sit", random.uniform(6, 14))

    def act_nap(self):
        m = self.main
        self.play([{"do": "call", "f": lambda: m.set("sleep")}, {"do": "wait", "secs": random.uniform(6, 12)},
                   {"do": "call", "f": lambda: (m.set("idle"), self.flash("startle", 1.0), m.emote("!", "#f59e0b"), m.hop(4))}])

    def act_dance(self):
        self.main.set("dance", random.uniform(2, 3.5))

    def act_sulk(self):
        m = self.main
        m.set("sulk", 3)
        self.flash("sad", 3)

    def act_yawn(self):
        self.main.set("stretch", 1.6)
        self.flash("sleepy", 2.0)
        self.main.emote("~", "#94a3b8")

    def act_plant(self):
        self.play([{"do": "plant"}])

    def act_tower(self):
        self.play([{"do": "tower", "secs": 4}])

    def act_wander(self):
        self.main.wander(300)

    def act_travel(self):
        self.main.fly_random()

    def act_read(self):
        self.prop = ("book", time.time() + random.uniform(3, 6))

    def act_kickball(self):
        self.ball_chase = time.time() + 8

    # ---------------------------------------------- Tokens essen + Wort ausspucken
    def crumb(self, delay=0.0):
        m = self.main
        self.crumbs.append({"sx": m.x + random.uniform(-90, 90), "sy": m.y - m.h - random.uniform(90, 150),
                            "t0": time.time() + delay, "c": random.choice(["#D97757", "#e8967a", "#f59e0b"])})

    def draw_crumbs(self, cv, now):
        m = self.main
        live = []
        for pp in self.papers:
            k = (now - pp["t0"]) / 0.7
            if k >= 1:
                continue
            live.append(pp)
            x = m.x + m.facing * 6 + pp["dx"] * (1 - k)
            y = m.y - m.h * 1.0 - 150 * (1 - k) ** 2
            sway = math.sin(now * 9) * 4 * (1 - k)
            cv.create_rectangle(x - 7 + sway, y - 9, x + 7 + sway, y + 9, fill="#b9a582", outline="")
            cv.create_rectangle(x - 6 + sway, y - 8, x + 6 + sway, y + 8, fill="#f1e4cc", outline="")
            for i in range(3):
                cv.create_line(x - 4 + sway, y - 4 + i * 4, x + 4 + sway, y - 4 + i * 4, fill="#D97757")
        self.papers = live
        alive = []
        for c in self.crumbs:
            k = (now - c["t0"]) / 0.55
            if k >= 1:
                m.squash = max(m.squash, 0.14)  # mampf
                continue
            alive.append(c)
            if k < 0:
                continue
            tx, ty = m.x + m.facing * 4, m.y - m.h * 0.35
            x = c["sx"] + (tx - c["sx"]) * k
            y = c["sy"] + (ty - c["sy"]) * k - math.sin(k * math.pi) * 30
            r = 4 * (1 - 0.5 * k)
            cv.create_rectangle(x - r, y - r, x + r, y + r, fill=c["c"], outline="")
        self.crumbs = alive

    def feast(self, tokens, word):
        m = self.main
        if not m:
            return
        k = min(14, 2 + int(tokens or 0) // 250)
        for i in range(k):
            self.crumb(i * 0.09)
        steps = [{"do": "wait", "secs": 0.09 * k + 0.6},
                 {"do": "call", "f": lambda: (setattr(m, "squash", 0.35), m.emote("*", "#D97757"), self.flash("purr", 1.0))}]
        if word:
            steps += [{"do": "wait", "secs": 0.5}, {"do": "call", "f": lambda: self.spit(word)}]
        self.play(steps)

    def spit(self, word):
        m = self.main
        x0, y0 = m.x + m.facing * m.w * 0.4, m.y - m.h * 0.4
        vx, vy, t0 = m.facing * random.uniform(2.5, 4), -random.uniform(5, 7), time.time()
        tw = len(word) * 8 + 18

        def fn(cv, now, d):
            t = (now - t0) * 30
            x, y = x0 + vx * t, y0 + vy * t + 0.35 * t * t  # fliegt im Bogen und fällt aus dem Bild
            cv.create_rectangle(x - tw / 2 - 2, y - 13, x + tw / 2 + 2, y + 13, fill="#b85f42", outline="")
            cv.create_rectangle(x - tw / 2, y - 11, x + tw / 2, y + 11, fill="#f1e4cc", outline="")
            cv.create_text(x, y, text=word, fill="#3f3f46", font=("Segoe UI", 10, "bold"))
        m.squash = 0.3
        self.fx((x0 - 200, y0 - 120, x0 + 200, y0 + 500), 2.2, fn, pad=60, padt=60)

    # ---------------------------------------------- Konfetti (Commit, Tests grün)
    def confetti(self):
        m = self.main
        cx, cy, t0 = m.x, m.y - m.h, time.time()
        parts = [(random.uniform(-6, 6), random.uniform(-11, -5), random.choice(["#D97757", "#f59e0b", "#22c55e", "#3b82f6", "#ec4899", "#8b5cf6"]))
                 for _ in range(26)]

        def fn(cv, now, d):
            t = (now - t0) * 30
            for vx, vy, c in parts:
                x, y = cx + vx * t, cy + vy * t + 0.3 * t * t
                cv.create_rectangle(x - 3, y - 2, x + 3, y + 2, fill=c, outline="")
        self.fx((cx - 260, cy - 260, cx + 260, cy + 400), 1.8, fn, pad=40, padt=40)

    def contexts(self, now):
        m, w = self.main, self.world
        self.joy *= 0.9995  # Laune pendelt langsam zurück
        # Tokens knabbern, während Claude arbeitet
        if False and self.working and now > self.crumb_at:  # (Tokens essen entfernt)
            self.crumb_at = now + random.uniform(1.2, 3.0)
            self.crumb()
        # Denk-Stufen
        if self.mode == "thinking":
            self.think_since = self.think_since or now
            dur = now - self.think_since
            if dur > 25 and now > self.think_emote:
                if dur > 60:
                    self.think_emote = now + 15
                    m.emote("💧", "#38bdf8", 1.8)
                    if m.grounded:
                        m.set("scratch", 1.6)
                else:
                    self.think_emote = now + 10
                    m.emote("…", "#94a3b8", 1.6)
        else:
            self.think_since = None
        # Pausen-Buddy: nach 50 min Aktivität am Stück
        if now - self.input_at > 300:
            self.active_since = None
        elif now - self.input_at < 180 and self.active_since is None:
            self.active_since = now
        if (self.active_since and now - self.active_since > 50 * 60 and now > self.break_cool and not self.scene
                and self.step is None and m.grounded and m.state != "sleep"):
            self.break_cool = now + 15 * 60
            self.play([{"do": "call", "f": lambda: m.set("stretch", 1.6)}, {"do": "wait", "secs": 1.7},
                       {"do": "call", "f": lambda: (self.flash("sleepy", 2.5), m.emote("~", "#94a3b8"))}, {"do": "wait", "secs": 1.0},
                       {"do": "call", "f": lambda: (setattr(self, "prop", ("coffee", time.time() + 6)), m.emote("⏸", "#a16207", 2.5))},
                       {"do": "wait", "secs": 6}, {"do": "call", "f": lambda: m.set("wave", 1.5)}])
        if (False and self.typing_recent and now > self.enter_cool and not self.scene and self.step is None and m.grounded
                and (len(self.text) > 350 or now - self.type_start > 35) and getattr(self, "comp_box", None)):
            self.enter_cool = now + 75
        # entwurzelte Blume liegt herum: hingehen und wieder einpflanzen
        lying = [d for d in self.decor if d.kind == "flower" and d.lying and not d.dead]
        if lying and self.free() and m.grounded and now > getattr(self, "replant_cool", 0):
            d = lying[0]
            self.replant_cool = now + 6
            if m.ledge and abs(m.ledge[2] - d.y) < 8 and m.ledge[0] <= d.x <= m.ledge[1]:
                self.play([{"do": "walk", "x": d.x + (m.w * 0.6 if m.x > d.x else -m.w * 0.6)},
                           {"do": "replant", "d": d}])
            elif random.random() < 0.5:
                lg = self.ledge_at(d.x, d.y, 8)
                if lg:
                    self.play([{"do": "fly", "x": max(lg[0] + m.w, min(lg[1] - m.w, d.x + 40)), "y": lg[2]},
                               {"do": "walk", "x": d.x + m.w * 0.6}, {"do": "replant", "d": d}])
        # Popup / Menü ging auf
        pops = w.get("popups", [])
        if len(pops) > self.popup_seen and m.state not in ("drag", "portal"):
            pl, pt, pr, pb = pops[-1]
            inside = pl - 10 < m.x < pr + 10 and pt - 10 < m.y - m.h / 2 < pb + 10
            self.glance = ((pl + pr) / 2, (pt + pb) / 2, now + 1.5)
            if inside or (not self.working and random.random() < 0.45 and pt > 90):
                x = max(pl + m.w, min(pr - m.w, m.x))
                self.scene, self.step = [], None
                if inside and random.random() < 0.5:
                    m.y = pt
                    m.hop(7)  # wird hochgeschoben und hüpft drauf
                else:
                    m.fly_to(x, pt)
            else:
                self.flash("startle", 0.7)
                m.emote("!", "#64748b")
        self.popup_seen = len(pops)
        idle_user = now - self.input_at
        comp = self.composer()
        # Nutzer ist weg (1-2,5 min): spielt/baut allein, danach schläft er
        if 60 < idle_user < 150 and now > self.solo_at and self.free() and m.grounded and m.state != "sleep":
            self.solo_at = now + 70
            r = random.random()
            if r < 0.45:
                self.play([{"do": "tower", "secs": 5}, {"do": "wait", "secs": 1.5},
                           {"do": "prop", "name": None, "secs": 0.1, "sym": "✦", "color": "#f59e0b"}])
            elif r < 0.75:
                self.play([{"do": "plant"}, {"do": "wait", "secs": 1.0}, {"do": "plant", "kind": "flower"}])
            elif not self.ball:
                self.toggle_ball()  # spielt allein Ball
        # langer Absatz: liest mit, staunt, schwitzt
        n = len(self.text)
        lvl = 2 if n > 800 else 1 if n > 300 else 0
        if lvl > self.para_level and self.typing_recent:
            m.emote("✎" if lvl == 1 else "💧", "#64748b" if lvl == 1 else "#38bdf8")
            self.flash("wow" if lvl == 1 else "startle", 1.0)
        self.para_level = lvl
        if lvl and self.typing_recent and not self.working and m.grounded:
            if not self.prop or now > self.prop[1]:
                self.prop = ("book", now + 0.8)  # liest mit
        # nur Klicks, kein Tippen: hinporten und zuschauen
        if (len([t for t in self.clicks if now - t < 8]) >= 3 and now > self.click_hunt_cool and self.free()
                and m.grounded and self.glance):
            cx_, cy_ = self.glance[0], self.glance[1]
            below = [lg for lg in self.ledges if lg[0] + 20 < cx_ < lg[1] - 20 and cy_ < lg[2] < cy_ + 260]
            if below:
                self.click_hunt_cool = now + 15
                lg = min(below, key=lambda g: g[2])
                x = max(lg[0] + m.w, min(lg[1] - m.w, cx_ + 50))
                if abs(x - m.x) + abs(lg[2] - m.y) > 120:
                    m.fly_to(x, lg[2], "portal")

    def on_show(self, now):
        """Claude-Fenster ist wieder vorne."""
        m = self.main
        if not m:
            return
        away = now - self.away_since if self.away_since else 0
        self.away_since = None
        self.last_act = now
        if away > 600:  # lange weg: aufwachen, strecken, dann freuen
            m.set("idle")
            self.play([{"do": "call", "f": lambda: (m.set("stretch", 1.5), self.flash("sleepy", 1.5))}, {"do": "wait", "secs": 1.6},
                       {"do": "call", "f": lambda: (m.hop(9), m.emote("♥", "#D97757"), self.flash("love", 1.6))}])
        elif away > 60:
            if m.state == "sleep":
                m.set("idle")
            m.hop(8)
            m.emote("♥", "#D97757")
            self.flash("love", 1.4)
        elif away > 10:
            m.set("wave", 1.4)
            self.flash("pet", 1.0)
        elif away > 1.5:
            m.emote("!", "#64748b", 0.9)
            self.flash("startle", 0.5)

    def typed(self, text, old_text):
        """Der Text im Eingabefeld hat sich geändert (live über UI Automation oder vom Plugin)."""
        m = self.main
        if not m:
            self.text = text
            return
        now = time.time()
        if not self.typing_recent:
            self.scan_now = True
        self.last_act = now
        if not self.typing_times or now - self.typing_times[-1] > 6:
            self.type_start = now
        self.typing_times = [t for t in self.typing_times if now - t < 3] + [now]
        self.input_at = now
        grew = len(text) >= len(old_text)
        self.text = text
        comp = self.composer()
        if comp:
            line = text.split("\n")[-1]
            est = min(comp[1] - 20, comp[0] + 22 + len(line) * 8.6)
            self.caret_x = self.caret[0] if self.caret else est
            if m.state in ("sleep", "sit"):
                m.set("idle")
            if m.ledge is None or abs(m.ledge[2] - comp[2]) > 6:
                if (self.scene or self.step is not None) and now > self.demo_until and now > self.keep_until:
                    self.scene, self.step = [], None  # Erkundung abbrechen: Tippen geht vor
                    m.point = None
                if m.state != "drag" and now > self.demo_until and now > self.keep_until:
                    # zum Eingabefeld fliegen
                    m.fly_to(max(comp[0] + m.w, min(comp[1] - m.w, self.caret_x)), comp[2], "fly")
            elif not m.busy() and not self.working and now > self.keep_until:
                m.target = self.caret_x + (m.w * 0.8 if grew else -m.w * 0.2)
        self.text_reactions(text, old_text, now, m)
        if m.grounded and m.state not in ("sleep", "drag"):
            m.squash = max(m.squash, 0.05)  # nickt bei jedem Tastendruck mit
        if len(text) < len(old_text):
            self.del_times = [t for t in self.del_times if now - t < 2] + [now]
            if len(self.del_times) >= 6 and m.state != "hide" and m.grounded:
                m.set("hide", 1.4)  # so viel Löschen: hält sich die Augen zu
                m.emote("…", "#94a3b8")
        tail = text[-5:]
        caps = len(tail) == 5 and tail.isalpha() and tail.isupper()
        if caps and not self.caps_seen and m.grounded:
            m.set("hide", 0.8)
            self.flash("startle", 1.0)
            m.emote("!", "#e5484d")
        self.caps_seen = caps
        word = (text.split() or [""])[-1]
        if len(word) >= 16 and word != self.long_word:
            self.long_word = word
            self.flash("question", 1.2)
        if text.endswith(":)") and not old_text.endswith(":)"):
            self.flash("laugh", 1.4)
            m.emote("♥", "#ec4899")
        if self.caret and self.caret_y is not None and abs(self.caret[1] - self.caret_y) > 10 and m.grounded and not self.working:
            m.hop(4)  # neue Zeile: klettert mit hoch
        if self.caret:
            self.caret_y = self.caret[1]
        if text.rstrip().endswith("?"):
            m.eye_to = [0, -1]
            if not old_text.rstrip().endswith("?"):
                self.prop = ("sign", now + 2.2)  # hält ein ?-Schild hoch
        if text.endswith("!") and not old_text.endswith("!"):
            m.hop(6)
            m.emote("!", "#f59e0b")
        low = text.lower()
        kw = ("danke", "perfekt", "super", "<3", "lieb")
        if any(w in low for w in kw) and not any(w in old_text.lower() for w in kw):
            m.emote("♥")
            self.flash("love", 1.2)
        self.del_run = 0 if grew else getattr(self, "del_run", 0) + len(old_text) - len(text)
        if not grew and len(text) == 0 and len(old_text) <= 3:
            if self.del_run > 15:
                self.prop = ("broom", now + 1.8)  # alles weggelöscht: kehrt auf
            else:
                m.emote("…", "#94a3b8")

    # ---------------------------------------------- Umgebung (Sidebar, Panels, Nachrichten)
    def free(self):
        return (self.main and not self.working and self.mode != "waiting" and not self.typing_recent and not self.scene and self.step is None
                and not (self.ball and time.time() - self.ball.touched < 20)
                and not self.drag and self.main.state in ("idle", "walk", "look", "sit"))

    def explore(self, now):
        m, w = self.main, self.world
        # lange Arbeit: alle 30-45 s kurz umschauen (bleibt dabei an seinem Platz)
        if (self.working and self.work_since and now > self.checkin_at and not self.scene and self.step is None
                and not self.drag and m.state in ("idle", "walk") and now > self.demo_until):
            self.checkin_at = now + random.uniform(30, 45)
            self.check_in(now)
            return
        # auf dem Boden gelandet (unter allen Kanten): nach kurzem Moment heim zum Eingabefeld
        comp = self.composer()
        if m.ledge is None and m.state == "idle" and comp and m.y > comp[2] + 30:
            if not self.seen_floor:
                self.seen_floor = now
            elif now - self.seen_floor > 2.5 and not self.scene and self.step is None:
                self.seen_floor = 0.0
                self.play([{"do": "home"}])
        else:
            self.seen_floor = 0.0
        # Sidebar: neue Status-Einträge (Fehler / wartet / ungelesen) in anderen Sitzungen
        rows = {(kind, name): lg for lg, kind, name in w.get("rows", [])}
        if self.seen_rows is None:
            if w.get("sidebar") is not None:
                self.seen_rows = set(rows)
        else:
            new = [k for k in rows if k not in self.seen_rows]
            if new and self.free():
                self.seen_rows |= set(new)
                kind, name = sorted(new, key=lambda k: ("error", "waiting", "unread").index(k[0]))[0]
                self.visit_row(rows[(kind, name)], kind)
                return
            self.seen_rows &= set(rows) | set(new)
        # Seitenpanel (Artefakt, Aufgaben, Diff …) ging auf
        panes = w.get("panes", [])
        n = len(panes)
        if self.seen_panes is None:
            self.seen_panes = n
        elif n != self.seen_panes:
            opened = n > self.seen_panes
            self.seen_panes = n
            if opened and self.free():
                pl, pt, pr, pb = panes[-1]
                spots = [lg for lg in self.ledges if lg[0] >= pl - 4 and lg[1] <= pr + 4 and lg[1] - lg[0] > m.w * 2]
                m.hop(7)
                m.emote("✦", "#f59e0b")
                if spots:
                    lg = spots[0]
                    self.play([{"do": "wait", "secs": 0.5},
                               {"do": "fly", "x": lg[0] + m.w * 1.5, "y": lg[2]},
                               {"do": "point", "tx": (pl + pr) / 2, "ty": lg[2] + 40, "secs": 1.6},
                               {"do": "prop", "name": "book", "secs": 3.5}])
                return
        # eigener Kopf: abwechslungsreiche Leerlauf-Aktionen
        if self.brain(now):
            return

    def world_sig(self):
        w = self.world
        lm = w.get("last_msg")
        return (w.get("n_blocks", 0), lm and lm[3], len(w.get("rows", [])), len(w.get("panes", [])), len(self.text))

    def check_in(self, now):
        """Lange Arbeit: kurz Pause machen, schauen was sich getan hat, dann weiter."""
        m, w = self.main, self.world
        sig = self.world_sig()
        changed = self.work_sig is not None and sig != self.work_sig
        self.work_sig = sig
        lm, band = w.get("last_msg"), w.get("band")
        comp = self.composer()
        steps = []
        if changed and lm and comp and lm[3] < comp[2]:
            # Neues im Chat: hinschauen, draufzeigen, kurz lesen
            tx, ty = lm[0] + (lm[2] - lm[0]) * random.uniform(0.25, 0.6), max(lm[1] + 10, min(lm[3] - 20, comp[2] - 80))
            near = [lg for lg in self.ledges if lm[1] - 5 <= lg[2] <= lm[3] and lg[0] < tx < lg[1] and lg is not comp]
            steps = [{"do": "prop", "name": None, "secs": 0.1, "sym": "!", "color": "#3b82f6"},
                     {"do": "point", "tx": tx, "ty": ty, "secs": 2.0},
                     {"do": "prop", "name": "book", "secs": 3.0}]
            pass
        elif now - self.input_at > 20 and random.random() < 0.5:
            # Nutzer wartet auch: gemeinsam ungeduldig
            r = random.random()
            if r < 0.4:
                steps = [{"do": "prop", "name": None, "secs": 0.1, "sym": "⏱", "color": "#64748b"},
                         {"do": "wait", "secs": 1.5}]
                m.set("tap", 2.0)
            elif r < 0.7 and band:
                steps = [{"do": "point", "tx": (band[0] + band[2]) / 2, "ty": band[1] + 10, "secs": 1.5},
                         {"do": "prop", "name": None, "secs": 0.1, "sym": "…", "color": "#64748b"}]
            else:
                self.glance = (m.x, m.y + 300, now + 1.5)  # schaut zum Nutzer "runter"
                steps = [{"do": "prop", "name": None, "secs": 0.1, "sym": "♥", "color": "#D97757"}, {"do": "wait", "secs": 1.2}]
        else:
            r = random.random()
            if band and r < 0.3:
                # Fortschrittsleiste prüfen
                steps = [{"do": "point", "tx": (band[0] + band[2]) / 2, "ty": band[1] + 10, "secs": 1.8},
                         {"do": "prop", "name": None, "secs": 0.1, "sym": "✓", "color": "#22c55e"}]
            elif r < 0.5:
                steps = [{"do": "prop", "name": "coffee", "secs": 3.5, "sym": "~", "color": "#a16207"}]
            elif r < 0.7:
                m.set("stretch", 1.4)
                steps = [{"do": "wait", "secs": 1.5}]
            elif r < 0.85:
                m.set("look", 2.4)
                steps = [{"do": "wait", "secs": 2.5}]
            else:
                steps = [{"do": "prop", "name": None, "secs": 0.1, "sym": "💧", "color": "#38bdf8"},
                         {"do": "wait", "secs": 1.2}]
        self.play(steps)

    def visit_row(self, lg, kind):
        m = self.main
        sym, col = {"error": ("!", "#e5484d"), "waiting": ("?", "#f59e0b"), "unread": ("✉", "#3b82f6")}[kind]
        x = lg[1] - m.w * 1.2
        self.play([{"do": "fly", "x": x, "y": lg[2]},
                   {"do": "point", "tx": lg[0] + 90, "ty": lg[2] + 16, "secs": 2.4},
                   {"do": "prop", "name": "sign" if kind == "waiting" else None, "secs": 0.1, "sym": sym, "color": col},
                   {"do": "wave", "secs": 1.2},
                   {"do": "plant", "kind": "flag", "color": col, "life": 120},
                   {"do": "wait", "secs": 2.0},
                   {"do": "home"}])


    # ---------------------------------------------- UI-Spielereien (reine Illusion: nichts an Claude wird verändert)
    def fx(self, rect, life, fn, pad=260, padt=80, **kw):
        l, t, r, b = rect
        ww, wh = int(r - l + 2 * pad), int(b - t + padt + 120)
        d = self.add_decor("fx", (l + r) / 2, b + 60, life, None, win=(ww, wh), fn=fn, **kw)
        return d

    def end_fx(self, d, fade=0.3):
        if d:
            d.life = min(d.life, time.time() - d.t0 + fade)

    def tile(self, text, x, y):
        tw = len(text) * 8.5 + 20

        def fn(cv, now, d):
            k = min(1.0, (now - d.t0) / 0.25)
            if k < 1:  # Kopie materialisiert sich
                cv.create_rectangle(d.x - tw / 2 * k, d.y - 22 - 12 * k, d.x + tw / 2 * k, d.y - 22 + 12 * k, fill="#D97757", outline="")
                return
            x0, y0 = d.x - tw / 2, d.y - 34
            cv.create_rectangle(x0 - 2, y0 - 2, x0 + tw + 2, y0 + 26, fill="#b85f42", outline="")
            cv.create_rectangle(x0, y0, x0 + tw, y0 + 24, fill="#f1e4cc", outline="")
            cv.create_text(d.x, y0 + 12, text=text, fill="#3f3f46", font=("Segoe UI", 11, "bold"))
        return self.add_decor("fx", x, y, 120, None, win=(int(tw + 40), 150), fn=fn)

    def text_pick(self, n=1):
        texts = self.world.get("texts", [])
        if not texts:
            return []
        out = []
        for _ in range(n * 3):
            rect, txt = random.choice(texts)
            words = [w_ for w_ in _re.findall(r"[A-Za-zÄÖÜäöüß]{4,12}", txt) if txt.count(w_) == 1]
            if words:
                out.append((rect, random.choice(words), txt))
            if len(out) >= n:
                break
        return out

    def blocks(self, minw=260):
        vw = self.world.get("view")
        return [r for r, _ in self.world.get("texts", []) if r[2] - r[0] >= minw]

    def ui_scene(self, which=None):
        """(abgeschaltet: Nutzer will keine Markier-/Leucht-/Bau-Spielereien)"""
        return False
        opts = which and [which] or ["measure", "paint", "piano", "fish", "words"]
        random.shuffle(opts)
        for o in opts:
            if getattr(self, "sc_" + o)():
                return True
        return False

    def sc_enter(self):
        m, cb = self.main, getattr(self, "comp_box", None)
        if not cb:
            return False
        snd = self.world.get("send")
        sx, sy = ((snd[0] + snd[2]) / 2, (snd[1] + snd[3]) / 2) if snd else (cb[2] - 30, cb[3] - 22)
        self.keep_until = time.time() + 14
        stars = []

        def boom():
            def fn(cv, now, d):
                k = (now - d.t0) / 0.3
                for i in range(6):
                    a_ = i * 1.047 + d.t0
                    r0, r1 = 6 + 10 * k, 14 + 14 * k
                    cv.create_line(sx + math.cos(a_) * r0, sy + math.sin(a_) * r0, sx + math.cos(a_) * r1, sy + math.sin(a_) * r1,
                                   fill="#D97757", width=3, capstyle="round")
            self.fx((sx - 30, sy - 30, sx + 30, sy + 30), 0.3, fn, pad=40, padt=40)

        def pound(p):
            m.facing = 1 if sx > m.x else -1
            if m.state != "pound":
                m.set("pound")
            k = int(p * 8)
            if k not in stars:
                stars.append(k)
                boom()
        self.play([{"do": "walk", "x": max(cb[0] + m.w, min(cb[2] - m.w * 0.7, sx - m.w * 0.55))},
                   {"do": "call", "f": lambda: (m.emote("!", "#e5484d"), self.flash("determined", 3.0))},
                   {"do": "tween", "secs": 2.6, "f": pound},
                   {"do": "call", "f": lambda: (m.set("idle"), m.emote("?", "#64748b"), self.flash("question", 1.5), stars.clear(),
                                                setattr(self, "glance", (m.x, m.y + 300, time.time() + 1.2)))},
                   {"do": "wait", "secs": 1.3},
                   {"do": "call", "f": lambda: (m.emote("!", "#e5484d"), m.emote("✕", "#e5484d"), self.flash("determined", 4.0), m.hop(5))},
                   {"do": "wait", "secs": 0.5},
                   {"do": "tween", "secs": 3.2, "f": pound},
                   {"do": "call", "f": lambda: (m.set("idle"), m.emote("💧", "#38bdf8", 2.0), self.flash("sleepy", 2.5))},
                   {"do": "wait", "secs": 2.2}])
        return True

    def sc_measure(self):
        m = self.main
        bl = self.blocks(300)
        if not bl:
            return False
        l, t, r, b = random.choice(bl)
        st = {"len": 0.0}

        def fn(cv, now, d):
            L = st["len"]
            cv.create_rectangle(l - 14, t - 18, l + 2, t - 2, fill="#64748b", outline="")
            if L > 2:
                cv.create_rectangle(l, t - 12, l + L, t - 4, fill="#facc15", outline="")
                for xx in range(0, int(L), 10):
                    cv.create_line(l + xx, t - 12, l + xx, t - (6 if xx % 50 else 4), fill="#a16207")
                if L > (r - l) * 0.98:
                    cv.create_rectangle(l + L - 26, t - 32, l + L + 26, t - 14, fill="#f1e4cc", outline="#b85f42")
                    cv.create_text(l + L, t - 23, text=str(int(r - l)), fill="#3f3f46", font=("Segoe UI", 9, "bold"))
        holder = {}

        def go(p):
            st["len"] = (r - l) * p
            m.x = l + st["len"]

        def back(p):
            st["len"] = (r - l) * (1 - p)
        self.play([{"do": "fly", "x": l + 4, "y": t - 2, "hold": True},
                   {"do": "call", "f": lambda: holder.update(d=self.fx((l - 20, t - 40, r + 40, b), 30, fn))},
                   {"do": "tween", "secs": 1.8, "f": go},
                   {"do": "call", "f": lambda: m.emote("!", "#f59e0b")}, {"do": "wait", "secs": 3.2},
                   {"do": "tween", "secs": 0.5, "f": back},
                   {"do": "call", "f": lambda: self.end_fx(holder.get("d"), 0.1)}, {"do": "home"}])
        return True

    def sc_paint(self):
        m = self.main
        bl = self.blocks(260)
        if not bl:
            return False
        l, t, r, b = random.choice(bl)
        st = {"a": 0.0, "b": 0.0}
        per = 2 * ((r - l) + (b - t))

        def pt(dist):
            w_, h_ = r - l + 12, b - t + 12
            x0, y0 = l - 6, t - 6
            if dist < w_:
                return x0 + dist, y0
            dist -= w_
            if dist < h_:
                return x0 + w_, y0 + dist
            dist -= h_
            if dist < w_:
                return x0 + w_ - dist, y0 + h_
            return x0, y0 + h_ - (dist - w_)

        def fn(cv, now, d):
            a0, a1 = st["b"] * per, st["a"] * per
            if a1 - a0 < 3:
                return
            pts = []
            for i in range(41):
                pts += list(pt(a0 + (a1 - a0) * i / 40))
            cv.create_line(*pts, fill="#D97757", width=4, capstyle="round", joinstyle="round")
        holder = {}

        def paint(p):
            st["a"] = p
            m.x = l + (r - l) * min(1, p * 2.2)
        self.play([{"do": "fly", "x": l, "y": t - 6, "hold": True},
                   {"do": "call", "f": lambda: (holder.update(d=self.fx((l - 10, t - 10, r + 10, b + 10), 40, fn)),
                                                setattr(self, "prop", ("pencil", time.time() + 3)))},
                   {"do": "tween", "secs": 3.0, "f": paint},
                   {"do": "call", "f": lambda: (m.emote("♥", "#ec4899"), self.flash("love", 1.2))}, {"do": "wait", "secs": 3.0},
                   {"do": "call", "f": lambda: setattr(self, "prop", ("broom", time.time() + 1.8))},
                   {"do": "tween", "secs": 1.6, "f": lambda p: st.update(b=p)},
                   {"do": "call", "f": lambda: self.end_fx(holder.get("d"), 0.1)}, {"do": "home"}])
        return True

    def sc_piano(self):
        m, sb = self.main, self.world.get("sidebar")
        if not sb:
            return False
        rows = sorted([lg for lg in self.ledges if lg[1] <= sb[2] + 4 and lg[1] - lg[0] > 150], key=lambda g: g[2])
        if len(rows) < 5:
            return False
        i0 = random.randint(0, len(rows) - 5)
        steps = []
        cols = ["#ec4899", "#f59e0b", "#22c55e", "#3b82f6", "#8b5cf6"]
        for i, lg in enumerate(rows[i0:i0 + 5]):
            x = lg[0] + 50 + i * 55

            def note(lg=lg, c=cols[i]):
                m.emote("♪", c, 1.0)

                def fn(cv, now, d, lg=lg, c=c):
                    cv.create_rectangle(lg[0] - 4, lg[2] + 1, lg[1] + 4, lg[2] + 32, outline=c, width=2)
                self.fx((lg[0], lg[2], lg[1], lg[2] + 33), 0.35, fn, pad=20, padt=20)
            steps += [{"do": "fly", "x": x, "y": lg[2], "mode": "fly"}, {"do": "call", "f": note}, {"do": "wait", "secs": 0.25}]
        self.play(steps + [{"do": "call", "f": lambda: (m.set("dance", 1.5))}, {"do": "wait", "secs": 1.5}, {"do": "home"}])
        return True

    def sc_fish(self):
        m = self.main
        bl = [r for r in self.blocks(260) if r[1] > 200]
        pick = self.text_pick(1)
        if not bl or not pick:
            return False
        l, t, r, b = random.choice(bl)
        word = pick[0][1]
        st = {"bob": None}

        def fn(cv, now, d):
            hx, hy = m.x + m.facing * m.w * 0.55, m.y - m.h * 0.55
            tip = (hx + m.facing * 40, hy - 46)
            cv.create_line(hx, hy, tip[0], tip[1], fill="#a16207", width=3, capstyle="round")
            if st["bob"]:
                bx, by = st["bob"]
                cv.create_line(tip[0], tip[1], (tip[0] + bx) / 2, min(tip[1], by) - 10, bx, by, smooth=True, fill="#57534e")
                cv.create_oval(bx - 4, by - 4 + math.sin(now * 6) * 2, bx + 4, by + 4 + math.sin(now * 6) * 2, fill="#e5484d", outline="")
        holder = {}
        x = min(r - m.w, l + (r - l) * 0.7)
        target = (x + 60, t + 34)

        def cast(p):
            hx = m.x + m.facing * (m.w * 0.55 + 40)
            st["bob"] = (hx + (target[0] - hx) * p, m.y - m.h - 46 + (target[1] - (m.y - m.h - 46)) * p)

        def catch():
            st["bob"] = None
            holder["w"] = self.tile(word, target[0], target[1] + 20)
            m.emote("!", "#f59e0b")

        def reel(p):
            w_ = holder.get("w")
            if w_:
                w_.x = target[0] + (m.x + 30 - target[0]) * p
                w_.y = target[1] + 20 + (m.y - m.h - 10 - target[1] - 20) * p

        def toss(p):
            w_ = holder.get("w")
            if w_:
                w_.y += 6 + 10 * p
                w_.x += 2
        self.play([{"do": "fly", "x": x, "y": t, "hold": True},
                   {"do": "call", "f": lambda: holder.update(d=self.fx((l - 80, t - 120, r + 80, b), 40, fn, pad=120, padt=160))},
                   {"do": "tween", "secs": 0.8, "f": cast}, {"do": "wait", "secs": 3.5}, {"do": "call", "f": catch},
                   {"do": "tween", "secs": 0.8, "f": reel},
                   {"do": "call", "f": lambda: (self.flash("question", 1.4), setattr(self, "glance", (m.x + 30, m.y - m.h - 20, time.time() + 1.4)))},
                   {"do": "wait", "secs": 1.6},
                   {"do": "call", "f": lambda: (m.emote("~", "#64748b"), self.end_fx(holder.get("w"), 0.9))},
                   {"do": "tween", "secs": 0.8, "f": toss},
                   {"do": "call", "f": lambda: self.end_fx(holder.get("d"), 0.1)}, {"do": "home"}])
        return True

    def sc_words(self):
        m, w = self.main, self.world
        picks = self.text_pick(random.randint(3, 4))
        if len(picks) < 3:
            return False
        col_l = min(rc[0] for rc, _ in w.get("texts", []))
        col_r = max(rc[2] for rc, _ in w.get("texts", []))
        sb_r = w["sidebar"][2] if w.get("sidebar") else 0
        left, right = col_l - sb_r, self.W - col_r
        if max(left, right) < 300:
            return False
        if right >= left:
            zx0, zx1 = col_r + 40, min(self.W - 30, col_r + 460)
        else:
            zx0, zx1 = max(sb_r + 30, col_l - 460), col_l - 40
        comp = self.composer()
        py = max(220, min(self.H - 220, (comp[2] if comp else self.H) - 280))
        tiles = []
        slot = {"x": zx0 + 10, "row": 0}
        self.word_res = {}
        self.word_req = [(txt, word) for (_rc, word, txt) in picks]  # Scanner misst die echten Wortpositionen
        pos = {}
        for rect, word, txt in picks:
            pos[word] = (random.uniform(rect[0] + 30, max(rect[0] + 40, rect[2] - 140)), rect[1] + 12)  # Notlösung

        def where(word):
            wr = self.word_res.get(word)
            if wr:
                return ((wr[0] + wr[2]) / 2, (wr[1] + wr[3]) / 2, wr)
            return pos[word][0], pos[word][1], None
        steps = [{"do": "fly", "x": (zx0 + zx1) / 2, "y": py - 6, "hold": True},
                 {"do": "build", "x1": zx0, "x2": zx1, "y": py, "life": 70}]
        for rect, word, txt in picks:
            def go(word=word):
                wx, wy, _ = where(word)
                side_ = -1 if wx > m.x else 1
                m.fly_to(wx + side_ * 120, wy - 60, "fly")

            def scan(word=word):
                self.prop = ("scanner", time.time() + 1.4)
                wx, wy, wr = where(word)
                m.facing = 1 if wx > m.x else -1
                bx0, by0, bx1, by1 = wr if wr else (wx - 34, wy - 10, wx + 34, wy + 12)

                def fn(cv, now, d):
                    hx, hy = m.x + m.facing * m.w * 0.75, m.y - m.h * 0.55
                    fl = 0.5 + 0.5 * math.sin(now * 30)
                    cv.create_polygon(hx, hy, bx0 - 3, by1 + 2, bx1 + 3, by1 + 2, fill="#f6c9b6" if fl > 0.5 else "#f3b79d", outline="")
                    cv.create_rectangle(bx0 - 4, by0 - 3, bx1 + 4, by1 + 3, outline="#D97757", width=2, dash=(4, 3))
                self.fx((bx0 - 60, by0 - 200, bx1 + 60, by1 + 40), 1.0, fn, pad=320, padt=240)

            def take(word=word):
                tl = self.tile(word, m.x, m.y - m.h - 4)
                tiles.append(tl)
                self.carry = tl  # Kopie schwebt über ihm
                m.emote("✦", "#f59e0b", 0.8)

            def drop(word=word):
                tl = tiles[-1]
                self.carry = None
                tw = len(word) * 8.5 + 20
                if slot["x"] + tw > zx1:
                    slot["x"], slot["row"] = zx0 + 10, slot["row"] + 1
                tl.x, tl.y = slot["x"] + tw / 2, py - 70 - slot["row"] * 32
                slot["x"] += tw + 8
            steps += [{"do": "wait", "secs": 0.4}, {"do": "call", "f": go}, {"do": "fly_wait"},
                      {"do": "call", "f": lambda: (setattr(m, "state", "hover"), setattr(m, "hover_y", m.y))},
                      {"do": "call", "f": scan}, {"do": "wait", "secs": 1.0},
                      {"do": "call", "f": take}, {"do": "wait", "secs": 0.5},
                      {"do": "fly", "x": (zx0 + zx1) / 2, "y": py, "mode": "fly"}, {"do": "call", "f": drop}]

        def admire():
            m.facing = 1 if (zx0 + zx1) / 2 > m.x else -1
            m.back_until = time.time() + 5.0  # dreht dir den Rücken zu und betrachtet sein Werk
        steps += [{"do": "wait", "secs": 0.6}, {"do": "call", "f": admire}, {"do": "wait", "secs": 5.0},
                  {"do": "call", "f": lambda: (m.emote("♪", "#8b5cf6"), self.flash("excited", 2.0), m.hop(5))},
                  {"do": "wait", "secs": 2.5},
                  {"do": "call", "f": lambda: [self.end_fx(tl, 0.8) for tl in tiles]},
                  {"do": "wait", "secs": 0.9}, {"do": "home"}]
        self.play(steps)
        return True

    # ---------------------------------------------- Szenen (Abfolgen von Aktionen)
    def play(self, steps):
        self.scene = list(steps)
        self.step = None

    def run_scene(self, now):
        m = self.main
        if self.step is None:
            if not self.scene:
                return
            self.step = self.scene.pop(0)
            self.step["t0"] = now
            st = self.step
            kind = st["do"]
            if kind == "fly":
                m.fly_to(st["x"], st["y"], st.get("mode", "fly" if st.get("hold") else None))
            elif kind == "call":
                st["f"]()
            elif kind == "tween":
                st["f"](0.0)
            elif kind == "walk":
                m.target = st["x"]
                m.set("walk")
            elif kind == "build" and False:
                m.state = "hover"
                m.hover_y = m.y
                m.fly = None
                self.built.append({"x1": st["x1"], "x2": st["x2"], "y": st["y"], "t0": now, "dur": 0.9, "until": now + st.get("life", 12)})
                self.prop = ("hammer", now + 0.9)
            elif kind == "point":
                pass  # (Zeigen entfernt)
            elif kind == "wave":
                m.set("wave", st["secs"])
            elif kind == "home":
                comp = self.composer()
                if comp and (m.ledge is None or abs(m.ledge[2] - comp[2]) > 6):
                    x = max(comp[0] + m.w, min(comp[1] - m.w, m.x))
                    m.fly_to(x, comp[2])
                    st["do"] = "fly"
            elif kind == "replant":
                d = st.get("d")
                if d and not d.dead and d.lying:
                    d.lying = False
                    d.t0 = now  # wächst neu hoch
                    d.life = 240
                    d.ledge = m.ledge
                    d.tried, d.anchor, d.prev_y, d.role, d.base = True, None, None, None, None
                    m.emote("♥", "#ec4899")
                    self.flash("love", 1.0)
            elif kind == "plant":
                px = m.ledge and self.free_x(m.ledge, m.x - m.facing * m.w * 0.7)
                if px is not None and px is not False:
                    kd = st.get("kind") or random.choice(["flag", "flower", "flower"])
                    self.add_decor(kd, px, m.ledge[2], st.get("life", 240), m.ledge,
                                   color=st.get("color") or random.choice(["#ec4899", "#f59e0b", "#8b5cf6", "#3b82f6", "#D97757"]))
                    m.emote("✿" if kd == "flower" else "⚑", "#22c55e", 0.9)
            elif kind == "mark" and False:  # (Unterstreichen entfernt)
                lg = st.get("ledge") or m.ledge
                if lg:
                    wdt = min(320, lg[1] - lg[0] - 10)
                    cx_ = max(lg[0] + wdt / 2 + 5, min(lg[1] - wdt / 2 - 5, st.get("x", m.x)))
                    self.add_decor("mark", cx_, lg[2], st.get("life", 25), lg, w=wdt, dy=st.get("dy", 24))
                    self.prop = ("pencil", now + 0.8)
            elif kind == "tower":
                px = m.ledge and self.free_x(m.ledge, m.x + m.facing * m.w * 0.9, gap=34)
                if px is not None and px is not False:
                    self.add_decor("tower", px, m.ledge[2], 300, m.ledge, build=st.get("secs", 4))
                    self.prop = ("hammer", now + st.get("secs", 4))
            elif kind == "prop":
                if st.get("name"):
                    self.prop = (st["name"], now + st["secs"])
                if st.get("sym"):
                    m.emote(st["sym"], st.get("color", "#D97757"))
            return
        st = self.step
        el = now - st["t0"]
        kind = st["do"]
        done = el > 9
        if kind == "fly":
            done = done or (m.state != "fly" and not m.busy())
            if m.state != "fly" and st.get("hold"):
                m.state = "hover"
                m.hover_y = m.y
        elif kind == "walk":
            done = done or m.target is None
        elif kind == "build":
            done = True
            if False:
                m.y = st["y"]
                m.ledge = (st["x1"], st["x2"], st["y"])
                m.set("idle")
                m.squash = 0.2
                done = True
        elif kind == "tower":
            done = el > st.get("secs", 4)
        elif kind == "call":
            done = True
        elif kind == "fly_wait":
            done = el > 0.1 and m.state != "fly" and not m.busy()
        elif kind == "tween":
            pr = min(1.0, el / st["secs"])
            st["f"](pr * pr * (3 - 2 * pr))
            done = pr >= 1
        elif kind in ("plant", "mark", "replant"):
            done = el > 0.5
        elif kind in ("point", "wave", "wait", "prop"):
            done = done or el > st["secs"]
        elif kind == "home":
            done = True
        done = done or el > 15  # harte Obergrenze: keine Szene hängt ewig
        if done:
            self.step = None

    def composer_box(self):
        comp = self.composer()
        return comp

    def scene_error(self):
        m = self.main
        m.hop(7)
        m.emote("!", "#e5484d", 1.6)
        self.flash("scared", 2.0)
        return

    def _old_scene_error(self):
        """Fehler: neben das Eingabefeld fliegen, Plattform bauen, auf den Fehler zeigen, winken."""
        comp = self.composer()
        m = self.main
        if not comp:
            m.hop(7)
            m.emote("!", "#e5484d")
            return
        right = comp[1] + 110 < self.W
        px = comp[1] + 70 if right else comp[0] - 70
        py = comp[2] - 20
        lm = self.world.get("last_msg")
        if lm and lm[3] < comp[2]:
            target = (lm[0] + (lm[2] - lm[0]) * 0.35, max(lm[1] + 10, lm[3] - 30))  # echte letzte Nachricht
        else:
            target = (comp[0] + (comp[1] - comp[0]) * 0.3, comp[2] - 60)
        self.play([
            {"do": "fly", "x": px, "y": py - 6, "hold": True},
            {"do": "build", "x1": px - 32, "x2": px + 32, "y": py, "life": 11},
            {"do": "point", "tx": target[0], "ty": target[1], "secs": 3.2},
            {"do": "wave", "secs": 1.4},
        ])

    def update_props(self, now):
        prev = self.cur_prop
        self._update_props(now)
        if self.cur_prop and self.cur_prop != prev and self.event not in ("excited", "wow"):
            self.event_until = 0  # neue Tätigkeit: alter Gesichtsausdruck (Herzaugen, böse) endet sofort
        m = self.main
        hard = m.state in ("fly", "drag", "portal", "sleep", "hide", "sulk", "dizzy", "wave", "dance", "stretch",
                           "purr", "giggle", "kick", "pound")
        if self.cur_prop:
            self.prop_seen = now
        elif prev and not hard and now - getattr(self, "prop_seen", 0) < 0.6:
            self.cur_prop = prev  # Lücke < 0,6 s: nicht kurz verschwinden lassen

    def _update_props(self, now):
        m = self.main
        if not m.grounded and not (m.state in ("hover", "pull") and self.prop and now < self.prop[1]):
            self.cur_prop = None  # beim Fliegen/Fallen: Hände frei, kein Werkzeug
        elif m.state in ("sleep", "hide", "sulk", "dizzy", "wave", "dance", "stretch", "purr", "giggle", "kick", "pound", "drag"):
            self.cur_prop = None  # Körper ist mit etwas anderem beschäftigt: keine Requisite
        elif self.prop and now < self.prop[1]:
            self.cur_prop = self.prop[0]
        elif self.mode == "waiting":
            self.cur_prop = "sign"
        elif self.mode == "tool-use" and self.act in ("cpu", "gpu", "machine", "laptop"):
            self.cur_prop = self.act
        elif self.mode == "tool-use" and self.tool in ("Bash", "PowerShell"):
            # Befehle: pro Aufruf neu würfeln, Hammer/Amboss nur selten
            key = (self.tool, self.state.get("detail"), self.state.get("eventAt"))
            if key != getattr(self, "bash_key", None):
                self.bash_key, self.bash_since = key, now
                self.bash_prop = random.choices(["hammer", "pencil", "magnifier", "binoculars", "broom", "book"],
                                                weights=[2, 3, 2, 2, 1, 2])[0]
            self.cur_prop = "coffee" if now - self.bash_since > 25 else self.bash_prop
        elif self.mode == "tool-use":
            self.cur_prop = {"Read": "book", "Grep": "magnifier", "Glob": "magnifier", "Bash": "hammer", "PowerShell": "hammer",
                             "Edit": "pencil", "Write": "pencil", "NotebookEdit": "pencil", "WebSearch": "binoculars",
                             "WebFetch": "binoculars"}.get(self.tool)
        elif self.working and self.work_since and now - self.work_since > 40:
            self.cur_prop = "coffee"
        else:
            self.cur_prop = None
        # Maus ruht auf dem Tierchen -> wird rot
        mx, my = cursor()
        lx, ly = mx - self.rect[0], my - self.rect[1]
        if m.hit(lx, ly):
            self.hover_since = self.hover_since or now
            if now - self.hover_since > 1.5:
                self.blush_until = now + 0.5
        else:
            self.hover_since = None

    def update_badge(self, now):
        cur = self.cur_prop
        st = self.step["do"] if self.step else None
        if st in ("plant",):
            cur = "flower"
        elif st == "mark":
            cur = "mark"
        elif not cur and self.ball and now - self.ball.touched < 3:
            cur = "ball"
        old = getattr(self, "badge", None)
        if cur:
            if not old or old[0] != cur:
                self.badge = (cur, now + 0.6, now)
            else:
                self.badge = (cur, now + 0.6, old[2])
        elif old and now > old[1]:
            self.badge = None

    def draw_world(self, cv, now):
        return  # Plattformen haben jetzt eigene Fenster (siehe ensure_built)

    def ensure_built(self, now):
        """Jede gebaute Plattform bekommt ein eigenes Fenster – bleibt sichtbar, auch wenn das Tierchen wegfliegt."""
        for b in self.built:
            if "dec" in b:
                continue

            def fn(cv, now_, d, b=b):
                prog = min(1.0, (now_ - b["t0"]) / b["dur"])
                left = b["until"] - now_
                n = max(1, int((b["x2"] - b["x1"]) / 10))
                for i in range(int(n * prog)):
                    x = b["x1"] + i * 10
                    drop = 0 if left > 1 else (1 - left) * 40 * ((i % 3) + 1) / 2  # zerbröselt am Ende
                    c = "#b45309" if i % 2 else "#d97706"
                    cv.create_rectangle(x, b["y"] + drop, x + 9, b["y"] + 6 + drop, fill=c, outline="")
            b["dec"] = self.fx((b["x1"], b["y"] - 10, b["x2"], b["y"] + 10), max(0.5, b["until"] - now), fn, pad=30, padt=20)

    def _old_draw_world(self, cv, now):
        for b in self.built:
            prog = min(1.0, (now - b["t0"]) / b["dur"])
            left = b["until"] - now
            n = max(1, int((b["x2"] - b["x1"]) / 10))
            for i in range(int(n * prog)):
                x = b["x1"] + i * 10
                drop = 0 if left > 1 else (1 - left) * 40 * ((i % 3) + 1) / 2  # zerbröselt am Ende
                c = "#b45309" if i % 2 else "#d97706"
                cv.create_rectangle(x, b["y"] + drop, x + 9, b["y"] + 6 + drop, fill=c, outline="")

    # ---------------------------------------------- Hauptschleife
    def tick(self):
        t0 = time.time()
        self.beat = t0
        try:
            self._tick()
        except Exception:
            log_err()

    def _tick(self):
        now = time.time()
        pf = self.perf
        dt = (now - pf["last"]) * 1000
        pf["last"] = now
        pf["n"] += 1
        pf["sum"] += dt
        pf["max"] = max(pf["max"], dt)
        if now - pf["t"] > 1:
            try:
                json.dump({"avg_ms": round(pf["sum"] / pf["n"], 1), "max_ms": round(pf["max"], 1), "fps": round(pf["n"] / (now - pf["t"]), 1),
                           "scans": self.scans, "visible": self.visible, "main": self.main and [round(self.main.x), round(self.main.y), self.main.state], "ledges": len(self.base_ledges), "rect": self.rect, "live": round(time.time() - self.live_at, 1), "text_len": len(self.text), "caret": self.caret, "rows": [(k, n[:30]) for _, k, n in self.world.get("rows", [])], "panes": self.world.get("panes"), "popups": self.world.get("popups"), "streaming": bool(self.world.get("streaming")), "eye": self.main and self.main.eye_kind, "emotes": self.main and [e["sym"] for e in self.main.emotes], "prop": self.cur_prop, "ball": self.ball and [round(self.ball.x), round(self.ball.y), self.ball.state], "want": sorted(self.anchor_want), "word_res": {k: [round(v) for v in r] for k, r in self.word_res.items()}, "back": bool(self.main and getattr(self.main, "back_until", 0) > time.time()), "decor": [(d.kind, round(d.x), round(d.y), d.hidden) for d in self.decor], "recent": self.recent, "joy": round(self.joy, 2), "fetch": self.fetch, "scene": [st["do"] for st in self.scene] + ([self.step["do"]] if self.step else [])}, open(os.path.join(DIR, "perf.json"), "w"))
            except Exception:
                pass
            self.perf = {"n": 0, "sum": 0.0, "max": 0.0, "last": now, "t": now}
        if now - self.last_scan > 0.05:
            self.locate()
        if self.visible and self.main:
            self.built = [b for b in self.built if now < b['until']]
            self.ensure_built(now)
            self.ledges = self.base_ledges + [(b['x1'], b['x2'], b['y']) for b in self.built if now - b['t0'] > b['dur']]
            for c in self.all():
                c.t += 0.033
                c.physics(self.W, self.H)
                c.think(now)
                c.pose(now)
            self.agents = {k: c for k, c in self.agents.items() if not (c.leaving and now - c.leaving > 0.5)}
            self.decor = [d for d in self.decor if not d.dead]
            if getattr(self, "carry", None) and self.main:
                self.carry.x, self.carry.y = self.main.x + self.main.facing * 10, self.main.y - self.main.h - 4
            self.anchor_want = {o.anchor[0] for o in self.all() if getattr(o, "anchor", None)}
            self.update_props(now)
            self.update_badge(now)
            self.run_scene(now)
            self.unstick(now)
            self.explore(now)
            self.update_mouse(now)
            try:
                seq = user32.GetClipboardSequenceNumber()
            except Exception:
                seq = None
            mm = self.main
            if seq is not None and self.clip_seq is not None and seq != self.clip_seq and mm.state not in ("sleep", "drag"):
                self.papers.append({"t0": now, "dx": random.uniform(-40, 40)})  # du hast etwas kopiert
                mm.set("kick", 0.5)
                mm.emote("⧉", "#D97757", 1.2)
            self.clip_seq = seq
            self.play_ball(now)
            self.ambient(now)
            self.contexts(now)
            m = self.main
            if not self.working and not self.typing_recent and m.state == "idle" and now - self.last_act > 150 and not self.scene:
                m.set("sleep")
            elif self.working and m.state == "sleep":
                m.set("idle")
            # beim Arbeiten bleibt es in der Nähe des Eingabefelds
            self.draw(now)
        return

    def scanner(self):
        """Hintergrund: Fensterlage, Eingabefeld, Live-Text und UI-Landschaft über UI Automation – kein Screenshot."""
        last_box, last_q, box_seen = None, 0.0, 0.0
        last_w, world = 0.0, None
        anchor_els = {}
        sc = None
        while True:
            if sc is None:
                sc = UIScanner()  # erst im Thread anlegen (COM pro Thread)
            try:
                self.scan_beat = time.time()
                h = find_claude()
                fg = user32.GetForegroundWindow()
                res = {"h": h, "rect": None, "ledges": [],
                       "iconic": bool(not h or user32.IsIconic(h) or not user32.IsWindowVisible(h))}
                pid = wt.DWORD()
                user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
                mine = pid.value == os.getpid()  # eigenes Fenster (auch das unsichtbare Hauptfenster) zählt mit
                if h and not user32.IsIconic(h) and (fg == h or mine):
                    rect = client_rect(h)
                    res["rect"] = rect
                    ox, oy = rect[0], rect[1]
                    now = time.time()
                    if now - last_q > (0.25 if self.typing_recent else 0.8):
                        last_q = now
                        nb = sc.box(h)
                        if nb:
                            last_box, box_seen = nb, now
                        elif now - box_seen > 5:
                            last_box = None  # erst nach 5 s ohne Treffer aufgeben (Feld wird beim Senden neu gebaut)
                    text, caret = sc.live()
                    if caret and caret[1] != getattr(self, "_caret_top", None):
                        if getattr(self, "_caret_top", None) is not None:
                            nb = sc.box(h)  # Zeile gewechselt: Box sofort neu messen
                            if nb:
                                last_box, box_seen, last_q = nb, now, now
                        self._caret_top = caret[1]
                    if text is not None:
                        res["text"] = text
                        res["caret"] = caret and (caret[0] - ox, caret[1] - oy, caret[2] - oy)
                    if now - last_w > (1.5 if self.working else 0.7):
                        last_w = now
                        try:
                            world = sc.world(h)
                        except Exception:
                            world = None
                        if world:
                            anchor_els = {k: el for k, (_r, el) in world["anch"].items()}
                    if self.word_req:
                        req, self.word_req = self.word_req, []
                        for text, word in req:
                            try:
                                wr = sc.word_rect(h, text, word)
                            except Exception:
                                wr = None
                                sc.doc_tp = None
                            if wr:
                                self.word_res[word] = (wr[0] - ox, wr[1] - oy, wr[2] - ox, wr[3] - oy)
                    live = {}
                    for k in list(self.anchor_want):
                        el = anchor_els.get(k)
                        if el is None:
                            continue
                        try:
                            r = el.CurrentBoundingRectangle
                            if r.right > r.left and r.bottom > r.top:
                                live[k] = (r.left - ox, r.top - oy, r.right - ox, r.bottom - oy)
                        except Exception:
                            pass
                    res["anchors"] = live
                    if last_box:
                        l, t, r_, b = last_box
                        res["composer"] = (l - ox, r_ - ox, t - oy)
                        res["comp_box"] = (l - ox, t - oy, r_ - ox, b - oy)
                        res["ledges"] = [res["composer"]]
                        res["exact"] = True
                    else:
                        res["ledges"] = layout_ledges(rect[2] - rect[0], rect[3] - rect[1])
                    if world:
                        res["world"] = self.world_ledges(world, ox, oy, rect, res.get("composer"))
                with self.scan_lock:
                    self.pending = res
            except Exception:
                import traceback
                open(os.path.join(DIR, "scanner.err"), "a").write(traceback.format_exc())
            time.sleep(0.05 if self.visible else 0.25)

    def world_ledges(self, w, ox, oy, rect, comp):
        """UIA-Rohdaten -> Plattformen + interessante Stellen (alles relativ zum Claude-Fenster)."""
        H = rect[3] - rect[1]
        top_room = 80  # Platz für den Körper über einer Kante
        out = {"ledges": [], "rows": [], "panes": [], "last_msg": None, "streaming": None, "sidebar": None}
        if w.get("sidebar"):
            l, t, r, b = w["sidebar"]
            if r - l > 60:
                out["sidebar"] = (l - ox, t - oy, r - ox, b - oy)
                for (rl, rt, rr, rb), kind, name in w["rows"]:
                    if rt - oy >= max(t - oy, 0) + top_room and rb - oy <= min(b - oy, H) - 4 and rr - rl > 150:
                        lg = (rl - ox + 6, rr - ox - 6, rt - oy)
                        out["ledges"].append(lg)
                        if kind:
                            out["rows"].append((lg, kind, name))
        view = w.get("view")
        vt, vb = (view[1] - oy, view[3] - oy) if view else (0, H)
        if comp:
            vb = min(vb, comp[2] - 20)
        if w.get("band"):
            l, t, r, b = w["band"]
            if r - l > 200 and (not comp or t - oy < comp[2] - 30):
                out["ledges"].append((l - ox + 8, r - ox - 8, t - oy))
                vb = min(vb, t - oy - 10)
        for (l, t, r, b) in w["blocks"]:
            y = t - oy
            if vt + top_room <= y <= vb:
                out["ledges"].append((l - ox + 4, r - ox - 4, y))
        out["popups"] = []
        for (l, t, r, b) in w.get("popups", []):
            out["popups"].append((l - ox, t - oy, r - ox, b - oy))
            if t - oy > top_room:
                out["ledges"].append((l - ox + 6, r - ox - 6, t - oy))  # draufspringen möglich
        for pr in w["panes"]:
            l, t, r, b = pr
            if r - l > 120:
                out["panes"].append((l - ox, t - oy, r - ox, b - oy))
        for (l, t, r, b) in w["pane_blocks"]:
            y = t - oy
            if top_room <= y <= H - 30:
                out["ledges"].append((l - ox + 4, r - ox - 4, y))
        for key in ("last_msg", "streaming"):
            if w.get(key):
                l, t, r, b = w[key]
                out[key] = (l - ox, t - oy, r - ox, b - oy)
        # sehr nahe Kanten zusammenlegen (eine Plattform pro Zeile)
        merged = []
        for lg in sorted(out["ledges"], key=lambda g: (g[2], g[0])):
            if merged and abs(merged[-1][2] - lg[2]) <= 6 and lg[0] <= merged[-1][1] + 10 and lg[1] >= merged[-1][0] - 10:
                m = merged[-1]
                merged[-1] = (min(m[0], lg[0]), max(m[1], lg[1]), m[2])
            else:
                merged.append(lg)
        out["ledges"] = [lg for lg in merged if lg[1] - lg[0] > 40][:90]
        out["n_blocks"] = len(w["blocks"])
        out["texts"] = [((l - ox, t - oy, r - ox, b - oy), n) for (l, t, r, b), n in w.get("texts", [])
                        if vt + 90 <= t - oy <= vb - 40 and r - l >= 200]
        out["send"] = w.get("send") and (w["send"][0] - ox, w["send"][1] - oy, w["send"][2] - ox, w["send"][3] - oy)
        out["anchor_rects"] = {k: (r[0] - ox, r[1] - oy, r[2] - ox, r[3] - oy) for k, (r, _el) in w["anch"].items()}
        out["view"] = (vt, vb)
        out["band"] = w.get("band") and (w["band"][0] - ox, w["band"][1] - oy, w["band"][2] - ox, w["band"][3] - oy)
        return out

    def locate(self):
        self.last_scan = time.time()
        try:
            mt = os.path.getmtime(SETTINGS)
            if mt != getattr(self, "settings_mt", None):
                self.settings_mt = mt
                settings.update(json.load(open(SETTINGS, encoding="utf-8")))
        except Exception:
            pass
        with self.scan_lock:
            res, self.pending = self.pending, None
        if res is None:
            return
        if not res["rect"]:
            if self.visible and self.main and self.hide_at is None and not res.get("iconic"):
                self.hide_at = time.time() + 1.3  # schaut dir noch kurz hinterher
                self.glance = (self.W + 400, self.main.y - 200, time.time() + 1.3)
                self.main.emote("?", "#64748b", 1.2)
                return
            if self.visible and self.hide_at and time.time() < self.hide_at and not res.get("iconic"):
                return
            self.hide_at = None
            if self.visible:
                for (top, *_r) in self.wins.values():
                    top.withdraw()
                self.visible = False
                self.away_since = time.time()
            return
        self.hide_at = None
        if not self.visible:
            for (top, *_r) in self.wins.values():
                top.deiconify()
                top.attributes("-topmost", True)
            self.restack()
            self.visible = True
            self.on_show(time.time())
        rect = res["rect"]
        if rect != self.rect:
            dx, dy = rect[0] - self.rect[0], rect[1] - self.rect[1]
            if self.main and abs(dx) + abs(dy) > 30 and self.rect != (0, 0, 800, 600):
                self.flash('startle', 1.2)
                self.main.emote('!', '#f59e0b')
            if self.main and (abs((rect[2] - rect[0]) - self.W) > 40 or abs((rect[3] - rect[1]) - self.H) > 40) and abs(dx) + abs(dy) < 30:
                self.main.squash = 0.35  # Fenstergröße geändert: wird gequetscht
                self.main.emote("↔", "#64748b")
            self.rect = rect
            self.W, self.H = rect[2] - rect[0], rect[3] - rect[1]
        self.claude = res["h"]
        world = res.get("world")
        self.comp_exact = res.get("composer")
        if res.get("comp_box"):
            self.comp_box = res["comp_box"]
        if world:
            self.world = world
        fresh = res["ledges"] + (world["ledges"] if world else [])
        if "anchors" in res:
            self.anchors = res["anchors"]
        if "text" in res:
            first = self.live_at == 0
            self.live_at = time.time()
            if first:
                self.text = res["text"]
            elif res["text"] != self.text:
                self.typed(res["text"], self.text)
            self.caret = res.get("caret")
            if self.caret and self.typing_recent:
                self.caret_x = self.caret[0]
        mem = {}
        for lg in fresh:
            mem[lg] = 0
        for lg in list(mem):
            if lg not in fresh:
                if any(abs(f[2] - lg[2]) <= 4 and abs(f[0] - lg[0]) <= 12 and abs(f[1] - lg[1]) <= 12 for f in fresh):
                    del mem[lg]
                    continue
                mem[lg] += 1
                if mem[lg] > 3:
                    del mem[lg]
        self.ledge_mem = mem
        self.base_ledges = sorted(mem, key=lambda s: s[2])
        # Zeilen schätzen (umgebrochen + echte Zeilenumbrüche) -> Eingabefeld-Oberkante hebt sich
        if self.base_ledges and self.text and not res.get("exact") and not world:
            x1, x2, y = self.base_ledges[-1]
            per_line = max(20, int((x2 - x1 - 70) / 8.6))
            lines = sum(max(1, -(-len(part) // per_line)) for part in self.text.split("\n"))
            lift = min(lines - 1, 9) * 27
            self.base_ledges[-1] = (x1, x2, y - lift)
        self.comp_cache = self.composer()
        if not self.main:
            comp = self.composer()
            x = (comp[0] + comp[1]) / 2 if comp else self.W / 2
            self.main = Critter(self, x, comp[2] if comp else 40)
            self.main.emote("♥", "#D97757")
            if 5 <= time.localtime().tm_hour < 11:
                self.main.set("stretch", 1.6)  # guten Morgen
            else:
                self.main.set("wave", 1.4)

    def win_for(self, c):
        if c not in self.wins:
            ww, wh = getattr(c, "win", None) or ((440, 360) if not c.mini else (220, 200))
            top = tk.Toplevel(self.root)
            top.overrideredirect(True)
            top.attributes("-topmost", True)
            top.attributes("-transparentcolor", KEY)
            top.configure(bg=KEY)
            cv = tk.Canvas(top, width=ww, height=wh, bg=KEY, highlightthickness=0)
            cv.pack()
            cv.bind("<ButtonPress-1>", self.press)
            cv.bind("<B1-Motion>", self.move)
            cv.bind("<ButtonRelease-1>", self.release)
            cv.bind("<Double-Button-1>", self.double)
            cv.bind("<Button-3>", self.menu)
            top.update_idletasks()
            self.owns.add(user32.GetParent(top.winfo_id()) or top.winfo_id())
            self.wins[c] = (top, cv, ww, wh)
            if c is not self.main:
                self.restack()
        return self.wins[c]

    def restack(self):
        """Neue Fenster liegen oben – Ball und Tierchen wieder nach vorn holen."""
        for c in (self.ball, self.main):
            if c and c in self.wins:
                try:
                    self.wins[c][0].lift()
                except Exception:
                    pass

    def draw(self, now):
        live = set(self.all())
        for c in list(self.wins):
            if c not in live:  # Mini-Tierchen weg -> Fenster schließen
                self.wins.pop(c)[0].destroy()
                self.win_geo.pop(c, None)
                self.win_geo.pop((c, "blank"), None)
        for c in live:
            top, cv, ww, wh = self.win_for(c)
            # Das Tierchen sitzt immer an derselben Stelle im Fenster, bewegt wird nur das Fenster.
            # So können Zeichnung und Fensterlage nie auseinanderlaufen (kein Teleport-Flackern).
            ox, oy = int(round(c.x - ww / 2)), int(round(c.y - wh + 60))
            geo = f"{ww}x{wh}+{self.rect[0] + ox}+{self.rect[1] + oy}"
            if self.win_geo.get(c) != geo:
                top.geometry(geo)
                self.win_geo[c] = geo
            if getattr(c, "hidden", False):
                if self.win_geo.get((c, "blank")) is None:
                    cv.delete("all")
                    self.win_geo[(c, "blank")] = True
                continue
            self.win_geo.pop((c, "blank"), None)
            cv.delete("all")
            if c is self.main:
                self.draw_world(cv, now)
                self.draw_crumbs(cv, now)
            c.draw(cv, now)
            cv.move("all", -ox, -oy)


if __name__ == "__main__":
    app = App()
    try:
        nxt = time.perf_counter()
        while True:
            app.root.update()  # Maus/Tastatur/after()-Timer
            app.tick()
            nxt += (1 / 30) if app.visible else 0.1  # versteckt: 10 Bilder/s reichen, man sieht nichts
            wait = nxt - time.perf_counter()
            if wait > 0:
                time.sleep(wait)
            else:
                nxt = time.perf_counter()  # zu langsam gewesen: nicht nachholen, Takt neu starten
    except tk.TclError:
        pass
