#!/usr/bin/env python3
"""
Tapedeck: a personal YouTube playlist builder for Linux.

Categories sit in a two-column grid and hold ordered song lists. Drag the
handle to reorder, double-click a number to send a song to that position,
double-click a title to rename it. Playback is audio-only through mpv and
yt-dlp, with no Google account involved.

    python tapedeck.py

    python tapedeck.py --debug   # more detail in the log

Library: ~/.local/share/tapedeck/library.json
Cache:   ~/.cache/tapedeck/
Log:     ~/.local/state/tapedeck/tapedeck.log
"""
from __future__ import annotations

import base64
import collections
import contextlib
import glob
import heapq
import html
import ipaddress
import itertools
import json
import logging
import os
import random
import re
import secrets
import shutil
import sys
import tempfile
import time
import uuid
import zlib
from collections import deque
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from PySide6.QtCore import (
    QCoreApplication, QEasingCurve, QEvent, QObject, QPoint, QPointF, QProcess, QPropertyAnimation,
    QRect, QRectF, QSize, Qt, QTimer, QUrl, QVariantAnimation, Signal,
)
from PySide6.QtGui import (
    QColor, QCursor, QFont, QFontMetrics, QIcon, QIntValidator, QKeySequence,
    QPainter, QPainterPath, QPalette, QPen, QPixmap, QShortcut, QTextOption,
)
from PySide6.QtNetwork import (
    QAbstractSocket, QHostAddress, QLocalSocket, QNetworkAccessManager, QNetworkInterface,
    QNetworkReply, QNetworkRequest, QTcpServer,
)
from PySide6.QtWidgets import (
    QAbstractButton, QApplication, QFrame, QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMenu, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QStackedWidget,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "Tapedeck"
APP_ID = "tapedeck"
IS_WINDOWS = sys.platform == "win32"
VERSION = "0.9.1"

# ── Design tokens ────────────────────────────────────────────────────────────
# Strict black and white. The accent only marks actions and what is playing.
BLACK = "#000000"
WHITE = "#ffffff"
SMOKE = "#9a9a9a"        # secondary text
ASH = "#5f5f5f"          # tertiary text, idle icons
HAIRLINE = "#262626"     # card and field borders
HAIRLINE_HI = "#3a3a3a"  # hovered borders
HOVER = "#141414"        # hover fill (a state, never a surface)
# The accent is customizable (Settings). These are the default's hand-tuned
# shades; set_accent() derives matching ones for any other color.
DEFAULT_ACCENT = "#4592a2"
ACCENT = "#4592a2"
ACCENT_HI = "#56a7b8"     # hover
ACCENT_LO = "#387985"     # pressed
ACCENT_WASH = "#0c1a1d"   # accent mixed into black: playing / selected rows
ACCENT_DIM_BG = "#17292d"  # disabled accent button
ACCENT_DIM_FG = "#4f6c72"
ON_ACCENT = BLACK          # text and glyphs drawn on the accent

ROW_H = 52
ROW_H_WIDE = 62       # rows on a category's own page
SIDEBAR_W = 216
ROW_GAP = 2
MAX_VISIBLE_ROWS = 8     # longer categories scroll inside their card
CARD_ROWS = 40           # a card previews this many; the category's page shows them all

SANS = ["IBM Plex Sans", "Segoe UI", "Noto Sans", "Cantarell", "DejaVu Sans"]

def parse_hex(text: str) -> str | None:
    """"#4592a2", "4592a2" or "#49a" → "#4592a2" style; None if it isn't a color."""
    value = text.strip().lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{3}", value):
        value = "".join(ch * 2 for ch in value)
    return "#" + value.lower() if re.fullmatch(r"[0-9a-fA-F]{6}", value) else None


def _rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def mix(a: str, b: str, amount: float) -> str:
    """a moved amount (0 to 1) of the way toward b."""
    return "#" + "".join(f"{max(0, min(255, round(x + (y - x) * amount))):02x}"
                         for x, y in zip(_rgb(a), _rgb(b)))


def luminance(color: str) -> float:
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(v) for v in _rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio, 1 (none) to 21 (black on white)."""
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def accent_palette(color: str) -> dict[str, str]:
    if color == DEFAULT_ACCENT:
        return {"ACCENT": color, "ACCENT_HI": "#56a7b8", "ACCENT_LO": "#387985",
                "ACCENT_WASH": "#0c1a1d", "ACCENT_DIM_BG": "#17292d", "ACCENT_DIM_FG": "#4f6c72",
                "ON_ACCENT": BLACK}
    return {
        "ACCENT": color,
        "ACCENT_HI": mix(color, WHITE, 0.16),
        "ACCENT_LO": mix(color, BLACK, 0.18),
        "ACCENT_WASH": mix(BLACK, color, 0.17),
        "ACCENT_DIM_BG": mix(BLACK, color, 0.3),
        "ACCENT_DIM_FG": mix(color, ASH, 0.5),
        "ON_ACCENT": BLACK if contrast(color, BLACK) >= contrast(color, WHITE) else WHITE,
    }


def set_accent(color: str) -> str:
    """Use this accent everywhere (widgets built afterwards, and the stylesheet)."""
    globals().update(accent_palette(parse_hex(color) or DEFAULT_ACCENT))
    return ACCENT


# ── Zoom ─────────────────────────────────────────────────────────────────────
# Every size in this file is in "design pixels" at 100%. z() converts to real
# pixels at the current zoom (Ctrl+scroll, Ctrl+plus/minus, Ctrl+0 to reset).
ZOOM = 1.0
ZOOM_MIN, ZOOM_MAX, ZOOM_STEP = 0.7, 1.8, 0.1


def set_zoom(value: float) -> float:
    global ZOOM
    ZOOM = round(min(ZOOM_MAX, max(ZOOM_MIN, float(value))), 2)
    return ZOOM


def z(value: float) -> int:
    """Design pixels to real pixels; anything visible stays at least 1 px."""
    scaled = int(round(value * ZOOM))
    return max(1, scaled) if value > 0 else scaled


def zf(value: float) -> float:
    return value * ZOOM


def zoomed_painter(widget: QWidget) -> tuple[QPainter, float, float]:
    """A painter that draws in design pixels, plus the widget's size in design pixels."""
    painter = QPainter(widget)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(ZOOM, ZOOM)
    return painter, widget.width() / ZOOM, widget.height() / ZOOM


def draw_text(painter: QPainter, rect: QRectF, text: str, font: QFont, color: str,
              align=None) -> None:
    """Text inside a zoomed painter, rendered at its real size so it stays crisp."""
    align = align if align is not None else Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
    real = QRectF(rect.x() * ZOOM, rect.y() * ZOOM, rect.width() * ZOOM, rect.height() * ZOOM)
    painter.save()
    painter.resetTransform()
    painter.setFont(font)
    painter.setPen(QColor(color))
    elided = QFontMetrics(font).elidedText(text, Qt.TextElideMode.ElideRight, int(real.width()))
    painter.drawText(real, align, elided)
    painter.restore()


def text_width(text: str, font: QFont) -> float:
    """How wide text in a zoomed font is, in design pixels."""
    return QFontMetrics(font).horizontalAdvance(text) / ZOOM


def later(ms: int, action) -> None:
    """Run action soon; skip it quietly if its widgets were rebuilt in the meantime."""
    def run() -> None:
        try:
            action()
        except RuntimeError:  # "C++ object already deleted": the zoom rebuilt the window
            pass
    QTimer.singleShot(ms, run)


def user_dir(kind: str) -> Path:
    """"data" (library, settings), "cache" (thumbnails, saved songs), "state" (log), "tools"."""
    if IS_WINDOWS:
        home = Path.home()
        roaming = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        return {"data": roaming / APP_NAME, "cache": local / APP_NAME / "Cache",
                "state": local / APP_NAME / "Logs", "tools": local / APP_NAME / "Tools"}[kind]
    var, fallback = {"data": ("XDG_DATA_HOME", ".local/share"),
                     "cache": ("XDG_CACHE_HOME", ".cache"),
                     "state": ("XDG_STATE_HOME", ".local/state"),
                     "tools": ("XDG_DATA_HOME", ".local/share")}[kind]
    base = Path(os.environ.get(var) or str(Path.home() / fallback)) / APP_ID
    return base / "tools" if kind == "tools" else base


DATA_DIR = user_dir("data")
CACHE_DIR = user_dir("cache")
TOOLS_DIR = user_dir("tools")  # Windows: an updatable copy of yt-dlp.exe
LIBRARY_FILE = DATA_DIR / "library.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
THUMB_DIR = CACHE_DIR / "thumbs"
AUDIO_DIR = CACHE_DIR / "audio"
STATE_DIR = user_dir("state")
LOG_FILE = STATE_DIR / "tapedeck.log"

log = logging.getLogger(APP_ID)


def replace_file(tmp: Path, target: Path) -> None:
    """Atomically move tmp over target, retrying briefly if Windows has it locked."""
    for attempt in range(6):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.05 * (attempt + 1))


def setup_logging(debug: bool = False) -> None:
    """Log to the terminal and to a file, so playback problems can be diagnosed."""
    log.setLevel(logging.DEBUG if debug else logging.INFO)
    log.propagate = False
    if log.handlers:
        return
    if sys.stderr is not None:  # the windowed Windows build has no console at all
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
        log.addHandler(console)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        to_file = RotatingFileHandler(LOG_FILE, maxBytes=512_000, backupCount=2, encoding="utf-8")
        to_file.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        log.addHandler(to_file)
    except OSError as exc:
        log.warning("Can't write the log file %s: %s", LOG_FILE, exc)


# ── Small helpers ────────────────────────────────────────────────────────────
def make_font(px: int, weight: QFont.Weight = QFont.Weight.Normal, *,
              tabular: bool = False, tracking: float = 0.0) -> QFont:
    font = QFont()
    font.setFamilies(SANS)
    font.setStyleHint(QFont.StyleHint.SansSerif)
    font.setPixelSize(max(6, z(px)))
    font.setWeight(weight)
    if tracking:
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, zf(tracking))
    if tabular:
        _enable_tabular_figures(font)
    return font


def _enable_tabular_figures(font: QFont) -> None:
    """Equal-width digits so positions and durations line up (Qt 6.6+)."""
    attempts = (
        lambda: font.setFeature(QFont.Tag("tnum"), 1),
        lambda: font.setFeature(QFont.Tag.fromString("tnum"), 1),
        lambda: font.setFeature("tnum", 1),
    )
    for attempt in attempts:
        try:
            attempt()
            return
        except Exception:  # older Qt: digits stay proportional, still readable
            continue


def repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def set_flag(widget: QWidget, name: str, value: bool) -> None:
    """Set a dynamic property the stylesheet reacts to, then restyle."""
    if widget.property(name) == value:
        return
    widget.setProperty(name, value)
    repolish(widget)


def tip(text: str, hint: str = "") -> str:
    """Tooltips render rich text; escape so video titles show literally."""
    parts = [f"<span>{html.escape(text)}</span>"] if text else []
    if hint:
        parts.append(f'<span style="color:{SMOKE}">{html.escape(hint)}</span>')
    return "<br>".join(parts)


def plain_label(text: str = "", name: str = "", font: QFont | None = None) -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    if name:
        label.setObjectName(name)
    if font is not None:
        label.setFont(font)
    return label


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "–:––"
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def clean_error(text: str) -> str:
    """Turn yt-dlp / mpv stderr into one short, readable sentence."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    picked = [line for line in lines if "ERROR" in line] or lines
    message = re.sub(r"^(?:\[[^\]]*\]\s*|ERROR:\s*)+", "", picked[-1])
    message = re.sub(r"^[A-Za-z0-9_-]{11}:\s*", "", message)
    return message[:180]


def new_id() -> str:
    return uuid.uuid4().hex[:12]


# ── YouTube links ────────────────────────────────────────────────────────────
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")


def parse_video_id(text: str) -> str | None:
    """Pull the 11-character video id out of any common YouTube link."""
    raw = text.strip()
    if not raw:
        return None
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
        raw = "https://" + raw
    try:
        url = urlparse(raw)
        host = (url.hostname or "").lower()
    except ValueError:
        return None
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    candidate = ""
    if host == "youtu.be":
        candidate = url.path.lstrip("/").split("/")[0]
    elif host in ("youtube.com", "music.youtube.com", "youtube-nocookie.com"):
        parts = [part for part in url.path.split("/") if part]
        if parts[:1] == ["watch"]:
            candidate = parse_qs(url.query).get("v", [""])[0]
        elif len(parts) >= 2 and parts[0] in ("shorts", "embed", "live", "v", "e"):
            candidate = parts[1]
    return candidate if VIDEO_ID.fullmatch(candidate) else None


def bundled_tools() -> Path | None:
    """The tools folder shipped next to Tapedeck.exe (mpv.exe, yt-dlp.exe, deno.exe)."""
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    folder = base / "tools"
    return folder if folder.is_dir() else None


def setup_tools() -> None:
    """Put Tapedeck's own tools first on PATH, so it and yt-dlp (which looks for deno) find them."""
    folders = []
    bundled = bundled_tools()
    if IS_WINDOWS and bundled is not None:
        source, copy = bundled / "yt-dlp.exe", TOOLS_DIR / "yt-dlp.exe"
        try:  # yt-dlp updates itself, so it runs from a folder Tapedeck can always write to
            if source.is_file() and (not copy.is_file()
                                     or source.stat().st_mtime > copy.stat().st_mtime + 60):
                TOOLS_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, copy)
        except OSError as exc:
            log.warning("Couldn't copy yt-dlp to %s: %s", TOOLS_DIR, exc)
        if copy.is_file():
            folders.append(TOOLS_DIR)
    if bundled is not None:
        folders.append(bundled)
    if folders:
        os.environ["PATH"] = os.pathsep.join([str(f) for f in folders] + [os.environ.get("PATH", "")])


def tool(name: str) -> str:
    """Full path of mpv or yt-dlp when it can be found; the bare name otherwise."""
    return shutil.which(name) or name


def install_hint(name: str) -> str:
    if IS_WINDOWS:
        return f"{name}.exe is missing from Tapedeck's tools folder. Reinstall Tapedeck to restore it."
    package = {"deno": "deno yt-dlp-ejs"}.get(name, name)
    return f"Install it with: sudo pacman -S {package}"


def update_hint() -> str:
    if IS_WINDOWS:
        return "Tapedeck updates yt-dlp every day; if this keeps happening, try again tomorrow."
    return "Update with sudo pacman -Syu and try again."


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


PLAYLIST_ID = re.compile(r"[A-Za-z0-9_-]{2,80}")
PRIVATE_LISTS = {"WL": "Watch later", "LL": "Liked videos", "LM": "Liked music"}


def parse_playlist(text: str) -> tuple[str, str] | None:
    """(playlist id, video id or "") from a YouTube link that names a playlist."""
    raw = text.strip()
    if not raw:
        return None
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
        raw = "https://" + raw
    try:
        url = urlparse(raw)
        host = (url.hostname or "").lower()
    except ValueError:
        return None
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    if host not in ("youtube.com", "music.youtube.com", "youtu.be", "youtube-nocookie.com"):
        return None
    list_id = parse_qs(url.query).get("list", [""])[0]
    if not PLAYLIST_ID.fullmatch(list_id):
        return None
    return list_id, parse_video_id(text) or ""


def playlist_url(list_id: str, video_id: str = "") -> str:
    if list_id.startswith("RD") and video_id:  # mixes only open from one of their videos
        return f"https://www.youtube.com/watch?v={video_id}&list={list_id}"
    return f"https://www.youtube.com/playlist?list={list_id}"


# ── Data model ───────────────────────────────────────────────────────────────
@dataclass
class Song:
    id: str
    video_id: str
    title: str = ""          # your own title; empty means "use YouTube's"
    yt_title: str = ""       # the video's title on YouTube
    channel: str = ""
    duration: int | None = None
    added: float = 0.0

    @property
    def display_title(self) -> str:
        return self.title or self.yt_title

    @property
    def url(self) -> str:
        return watch_url(self.video_id)


@dataclass
class Category:
    id: str
    title: str
    subtitle: str = ""
    song_ids: list[str] = field(default_factory=list)
    share_id: str = ""  # the same in every copy made from share codes, so updates are recognized


class Library(QObject):
    """Every category and song. Saved to JSON after each change."""

    categoriesChanged = Signal()        # a category was added or deleted
    categorySongsChanged = Signal(str)  # category id: membership or order changed
    songChanged = Signal(str)           # song id: title or details changed
    videosForgotten = Signal(list)      # video ids no song uses any more
    categoryRenamed = Signal(str)       # category id: title or subtitle changed

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self.songs: dict[str, Song] = {}
        self.categories: list[Category] = []
        self._write_timer: QTimer | None = None
        self._load()

    # Persistence
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for sid, raw in data.get("songs", {}).items():
                self.songs[sid] = Song(
                    id=sid,
                    video_id=raw["video_id"],
                    title=raw.get("title", ""),
                    yt_title=raw.get("yt_title", ""),
                    channel=raw.get("channel", ""),
                    duration=raw.get("duration"),
                    added=raw.get("added", 0.0),
                )
            for raw in data.get("categories", []):
                self.categories.append(Category(
                    id=raw["id"],
                    title=raw.get("title", "Untitled"),
                    subtitle=raw.get("subtitle", ""),
                    song_ids=[sid for sid in raw.get("songs", []) if sid in self.songs],
                    share_id=str(raw.get("share_id") or ""),
                ))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            backup = self.path.with_name(f"library.broken-{int(time.time())}.json")
            try:
                self.path.replace(backup)
            except OSError:
                pass
            log.warning("Couldn't read %s (%s); moved it to %s", self.path, exc, backup)
            self.songs, self.categories = {}, []

    def save(self, soon: bool = False) -> None:
        """Write the library. soon=True batches frequent background updates into one write."""
        if soon:
            if self._write_timer is None:
                self._write_timer = QTimer(self)
                self._write_timer.setSingleShot(True)
                self._write_timer.setInterval(1500)
                self._write_timer.timeout.connect(self.save)
            self._write_timer.start()
            return
        if self._write_timer is not None:
            self._write_timer.stop()
        self._write()

    def flush(self) -> None:
        """Write anything still waiting (on quit)."""
        if self._write_timer is not None and self._write_timer.isActive():
            self.save()

    def _write(self) -> None:
        in_use = {sid for category in self.categories for sid in category.song_ids}
        dropped = [song for sid, song in self.songs.items() if sid not in in_use]
        self.songs = {sid: song for sid, song in self.songs.items() if sid in in_use}
        data = {
            "version": 1,
            "songs": {
                sid: {
                    "video_id": song.video_id,
                    "title": song.title,
                    "yt_title": song.yt_title,
                    "channel": song.channel,
                    "duration": song.duration,
                    "added": song.added,
                }
                for sid, song in self.songs.items()
            },
            "categories": [
                {"id": c.id, "title": c.title, "subtitle": c.subtitle, "songs": c.song_ids,
                 **({"share_id": c.share_id} if c.share_id else {})}
                for c in self.categories
            ],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        replace_file(tmp, self.path)
        if dropped:
            still_used = {song.video_id for song in self.songs.values()}
            gone = sorted({song.video_id for song in dropped} - still_used)
            if gone:
                self.videosForgotten.emit(gone)

    # Lookups
    def category(self, category_id: str) -> Category | None:
        return next((c for c in self.categories if c.id == category_id), None)

    def song(self, song_id: str) -> Song | None:
        return self.songs.get(song_id)

    # Categories
    def add_category(self, title: str, subtitle: str = "") -> Category:
        category = Category(id=new_id(), title=title.strip(), subtitle=subtitle.strip())
        self.categories.append(category)
        self.save()
        self.categoriesChanged.emit()
        return category

    def rename_category(self, category_id: str, *, title: str | None = None,
                        subtitle: str | None = None) -> None:
        """An empty title is ignored; an empty subtitle removes it."""
        category = self.category(category_id)
        if category is None:
            return
        changed = False
        if title is not None:
            title = " ".join(title.split())[:80]
            if title and title != category.title:
                category.title, changed = title, True
        if subtitle is not None:
            subtitle = " ".join(subtitle.split())[:140]
            if subtitle != category.subtitle:
                category.subtitle, changed = subtitle, True
        if changed:
            self.save()
            self.categoryRenamed.emit(category_id)

    def delete_category(self, category_id: str) -> None:
        category = self.category(category_id)
        if category is None:
            return
        self.categories.remove(category)
        self.save()
        self.categoriesChanged.emit()

    # Songs
    def add_song(self, video_id: str, title: str, category_ids: list[str], *,
                 yt_title: str = "", channel: str = "", duration: int | None = None) -> Song:
        """Create one song and put it at the top of every chosen category."""
        song = Song(id=new_id(), video_id=video_id, title=title.strip(), yt_title=yt_title,
                    channel=channel, duration=duration, added=time.time())
        self.songs[song.id] = song
        touched = []
        for category_id in category_ids:
            category = self.category(category_id)
            if category is not None:
                category.song_ids.insert(0, song.id)
                touched.append(category_id)
        self.save()
        for category_id in touched:
            self.categorySongsChanged.emit(category_id)
        return song

    def import_songs(self, entries: list[dict], category_ids: list[str]) -> dict[str, int]:
        """Put a playlist's videos at the top of each category, in playlist order.

        Videos a category already has are skipped there. A video already in the
        library elsewhere reuses that song, so its title and details stay shared.
        Returns how many songs each category gained.
        """
        by_video: dict[str, Song] = {}
        for song in self.songs.values():
            by_video.setdefault(song.video_id, song)
        ordered: list[str] = []
        now = time.time()
        for entry in entries:
            video_id = entry["video_id"]
            song = by_video.get(video_id)
            if song is None:
                song = Song(id=new_id(), video_id=video_id, title=entry.get("custom") or "",
                            yt_title=entry.get("title") or "", channel=entry.get("channel") or "",
                            duration=entry.get("duration"), added=now)
                self.songs[song.id] = song
                by_video[video_id] = song
            if song.id not in ordered:
                ordered.append(song.id)
        added: dict[str, int] = {}
        for category_id in category_ids:
            category = self.category(category_id)
            if category is None or category_id in added:
                continue
            present = {self.songs[sid].video_id for sid in category.song_ids if sid in self.songs}
            block = [sid for sid in ordered if self.songs[sid].video_id not in present]
            category.song_ids[0:0] = block
            added[category_id] = len(block)
        self.save()  # also drops any new songs that ended up in no category
        for category_id in added:
            self.categorySongsChanged.emit(category_id)
        return added

    def category_by_share(self, share_id: str) -> Category | None:
        return next((c for c in self.categories if share_id and c.share_id == share_id), None)

    def ensure_share_id(self, category_id: str) -> str:
        category = self.category(category_id)
        if category is None:
            return ""
        if not category.share_id:
            category.share_id = new_id()
            self.save()
        return category.share_id

    def replace_category_songs(self, category_id: str, entries: list[dict], *,
                               title: str = "", subtitle: str | None = None) -> tuple[int, int]:
        """Make a category match a newer share code exactly: its songs, order, titles and name.

        Returns (songs added, songs removed).
        """
        category = self.category(category_id)
        if category is None:
            return 0, 0
        here = {self.songs[sid].video_id: self.songs[sid] for sid in category.song_ids
                if sid in self.songs}
        elsewhere: dict[str, Song] = {}
        for song in self.songs.values():
            elsewhere.setdefault(song.video_id, song)
        new_ids: list[str] = []
        added = 0
        for entry in entries:
            video_id = entry["video_id"]
            song = here.get(video_id) or elsewhere.get(video_id)
            if song is None:
                song = Song(id=new_id(), video_id=video_id, title=entry.get("custom") or "",
                            yt_title=entry.get("title") or "", channel=entry.get("channel") or "",
                            duration=entry.get("duration"), added=time.time())
                self.songs[song.id] = song
            else:
                song.title = entry.get("custom") or ""  # their renames come along
                if song.duration is None and entry.get("duration"):
                    song.duration = entry["duration"]
            if video_id not in here:
                added += 1
            if song.id not in new_ids:
                new_ids.append(song.id)
        kept = {entry["video_id"] for entry in entries}
        removed = sum(1 for video_id in here if video_id not in kept)
        category.song_ids = new_ids
        renamed = False
        if title and title != category.title:
            category.title, renamed = title[:80], True
        if subtitle is not None and subtitle != category.subtitle:
            category.subtitle, renamed = subtitle[:140], True
        self.save()
        self.categorySongsChanged.emit(category_id)
        if renamed:
            self.categoryRenamed.emit(category_id)
        for song_id in new_ids:
            self.songChanged.emit(song_id)
        return added, removed

    def has_video(self, category_id: str, video_id: str) -> bool:
        category = self.category(category_id)
        return category is not None and any(
            sid in self.songs and self.songs[sid].video_id == video_id for sid in category.song_ids)

    def set_song_categories(self, song: Song, category_ids: list[str]) -> tuple[list[str], list[str]]:
        """Put this song's video in exactly these categories.

        Newly chosen categories get it at the top. Returns (added to, removed from).
        """
        if song.id not in self.songs:  # e.g. it's playing but was removed from everywhere
            self.songs[song.id] = song
        wanted = set(category_ids)
        added: list[str] = []
        removed: list[str] = []
        for category in self.categories:
            here = [sid for sid in category.song_ids
                    if sid in self.songs and self.songs[sid].video_id == song.video_id]
            if category.id in wanted and not here:
                category.song_ids.insert(0, song.id)
                added.append(category.id)
            elif category.id not in wanted and here:
                category.song_ids = [sid for sid in category.song_ids if sid not in here]
                removed.append(category.id)
        if added or removed:
            self.save()
            for category_id in added + removed:
                self.categorySongsChanged.emit(category_id)
        return added, removed

    def remove_song(self, category_id: str, song_id: str) -> None:
        category = self.category(category_id)
        if category is None or song_id not in category.song_ids:
            return
        category.song_ids.remove(song_id)
        self.save()
        self.categorySongsChanged.emit(category_id)

    def move_song(self, category_id: str, song_id: str, position: int) -> None:
        """Send a song to a 1-based position; the songs in between shift by one."""
        category = self.category(category_id)
        if category is None or song_id not in category.song_ids:
            return
        ids = category.song_ids
        current = ids.index(song_id)
        target = max(1, min(position, len(ids))) - 1
        if target == current:
            return
        ids.insert(target, ids.pop(current))
        self.save()
        self.categorySongsChanged.emit(category_id)

    def reorder(self, category_id: str, song_ids: list[str]) -> None:
        category = self.category(category_id)
        if category is None or song_ids == category.song_ids:
            return
        if sorted(song_ids) != sorted(category.song_ids):
            return  # the list changed under us; ignore the stale order
        category.song_ids = list(song_ids)
        self.save()
        self.categorySongsChanged.emit(category_id)

    def set_song_title(self, song_id: str, text: str) -> None:
        """An empty title (or YouTube's own) falls back to the video's title."""
        song = self.song(song_id)
        if song is None:
            return
        text = text.strip()
        new_title = "" if not text or text == song.yt_title else text
        if new_title != song.title:
            song.title = new_title
            self.save()
        self.songChanged.emit(song_id)

    def apply_metadata(self, video_id: str, title: str, channel: str,
                       duration: int | None) -> None:
        matches = [song for song in self.songs.values() if song.video_id == video_id]
        changed = False
        for song in matches:
            if title and song.yt_title != title:
                song.yt_title, changed = title, True
            if channel and song.channel != channel:
                song.channel, changed = channel, True
            if duration is not None and song.duration != duration:
                song.duration, changed = duration, True
        if changed:
            self.save(soon=True)  # many arrive at once after an import
        for song in matches:
            self.songChanged.emit(song.id)


# ── Video details: oEmbed for a fast title, yt-dlp for the length ────────────
class MetaFetcher(QObject):
    """Looks up title, channel and duration for video ids, without blocking."""

    updated = Signal(str)   # video id: new details (or a final failure) available
    notice = Signal(str)    # one-time hints for the user (e.g. yt-dlp missing)
    MAX_PROCESSES = 3

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.info: dict[str, dict] = {}
        self._pending: dict[str, int] = {}
        self._queue: deque[str] = deque()
        self._processes: dict[str, QProcess] = {}
        self._network = QNetworkAccessManager(self)
        self.has_ytdlp = shutil.which("yt-dlp") is not None
        self._warned = False

    def is_pending(self, video_id: str) -> bool:
        return self._pending.get(video_id, 0) > 0

    def request(self, video_id: str, need_duration: bool = True) -> None:
        if self.is_pending(video_id):
            return
        entry = self.info.get(video_id)
        if entry and entry["title"] and (entry["duration"] is not None or not self.has_ytdlp
                                         or not need_duration):
            return
        if entry is None:
            entry = self.info[video_id] = {
                "title": None, "channel": None, "duration": None, "error": None}
        entry["error"] = None
        self._pending[video_id] = 0
        self._start_oembed(video_id)
        if not need_duration:
            return
        if self.has_ytdlp:
            self._pending[video_id] += 1
            self._queue.append(video_id)
            self._pump()
        elif not self._warned:
            self._warned = True
            self.notice.emit("Song lengths need yt-dlp. " + install_hint("yt-dlp"))

    # oEmbed answers in a fraction of a second, so the title shows up quickly.
    def _start_oembed(self, video_id: str) -> None:
        url = ("https://www.youtube.com/oembed?format=json&url="
               + quote(watch_url(video_id), safe=""))
        request = QNetworkRequest(QUrl(url))
        request.setTransferTimeout(12000)
        reply = self._network.get(request)
        self._pending[video_id] += 1
        reply.finished.connect(lambda r=reply, v=video_id: self._oembed_done(r, v))

    def _oembed_done(self, reply: QNetworkReply, video_id: str) -> None:
        entry = self.info[video_id]
        try:
            if reply.error() == QNetworkReply.NetworkError.NoError:
                data = json.loads(bytes(reply.readAll()).decode("utf-8"))
                entry["title"] = entry["title"] or data.get("title")
                entry["channel"] = entry["channel"] or data.get("author_name")
            else:
                status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
                if status in (400, 401, 403, 404):
                    entry["error"] = "This video is private or no longer available"
                else:
                    entry["error"] = entry["error"] or "Couldn't reach YouTube. Check your connection."
        except ValueError:
            pass
        finally:
            reply.deleteLater()
            self._finish(video_id)

    # yt-dlp is slower but knows the duration.
    def _pump(self) -> None:
        while self._queue and len(self._processes) < self.MAX_PROCESSES:
            video_id = self._queue.popleft()
            if video_id in self._processes:
                continue
            process = QProcess(self)
            process.setProgram(tool("yt-dlp"))
            process.setArguments([
                "-J", "--no-playlist", "--no-warnings", "--ignore-no-formats-error",
                "--socket-timeout", "15", watch_url(video_id),
            ])
            process.finished.connect(
                lambda code, _status, p=process, v=video_id: self._ytdlp_done(p, v, code))
            process.errorOccurred.connect(
                lambda error, p=process, v=video_id: self._ytdlp_error(p, v, error))
            self._processes[video_id] = process
            QTimer.singleShot(90_000, lambda p=process, v=video_id: self._timeout(p, v))
            process.start()

    def _timeout(self, process: QProcess, video_id: str) -> None:
        if self._processes.get(video_id) is process:
            process.kill()

    def _ytdlp_done(self, process: QProcess, video_id: str, code: int) -> None:
        if self._processes.get(video_id) is not process:
            return
        del self._processes[video_id]
        out = bytes(process.readAllStandardOutput()).decode("utf-8", "replace")
        err = bytes(process.readAllStandardError()).decode("utf-8", "replace")
        process.deleteLater()
        entry = self.info[video_id]
        if code == 0 and out.strip():
            try:
                data = json.loads(out)
                entry["title"] = data.get("title") or entry["title"]
                entry["channel"] = data.get("channel") or data.get("uploader") or entry["channel"]
                if data.get("duration") is not None:
                    entry["duration"] = int(round(float(data["duration"])))
            except (ValueError, TypeError):
                pass
        elif not entry["title"]:
            entry["error"] = clean_error(err) or "yt-dlp couldn't read this video"
        self._finish(video_id)
        self._pump()

    def _ytdlp_error(self, process: QProcess, video_id: str, error) -> None:
        if error != QProcess.ProcessError.FailedToStart:
            return  # crashes and kills still arrive through finished()
        if self._processes.get(video_id) is process:
            del self._processes[video_id]
        process.deleteLater()
        self.has_ytdlp = False
        self._finish(video_id)
        self._pump()

    def _finish(self, video_id: str) -> None:
        left = self._pending.get(video_id, 1) - 1
        if left > 0:
            self._pending[video_id] = left
        else:
            self._pending.pop(video_id, None)
        self.updated.emit(video_id)


# ── Playlists ────────────────────────────────────────────────────────────────
UNAVAILABLE_TITLES = {"[Deleted video]", "[Private video]", "[Unavailable video]"}
HIDDEN_AVAILABILITY = {"private", "needs_auth", "premium_only", "subscriber_only"}


def playlist_entries(data: dict) -> tuple[list[dict], int]:
    """Playable videos from yt-dlp's flat playlist JSON, and how many were unavailable."""
    entries: list[dict] = []
    unavailable = 0
    seen: set[str] = set()
    for item in data.get("entries") or []:
        if not isinstance(item, dict):
            continue
        video_id = str(item.get("id") or "")
        if not VIDEO_ID.fullmatch(video_id):
            video_id = parse_video_id(str(item.get("url") or "")) or ""
        if not video_id:
            continue
        title = str(item.get("title") or "")
        if title in UNAVAILABLE_TITLES or item.get("availability") in HIDDEN_AVAILABILITY:
            unavailable += 1
            continue
        if video_id in seen:
            continue
        seen.add(video_id)
        duration = item.get("duration")
        try:
            duration = int(round(float(duration))) if duration is not None else None
        except (TypeError, ValueError):
            duration = None
        entries.append({"video_id": video_id, "title": title,
                        "channel": str(item.get("channel") or item.get("uploader") or ""),
                        "duration": duration})
    return entries, unavailable


def describe_playlist_failure(lines: list[str]) -> str:
    text = "\n".join(lines)
    if not re.search(r"sign in to confirm you", text, re.IGNORECASE) and \
            re.search(r"does not exist|private|unavailable|not found|404|unviewable", text,
                      re.IGNORECASE):
        return ("That playlist is private or doesn't exist. Public and unlisted playlists "
                "can be imported.")
    return describe_failure(lines)


class PlaylistJob(QObject):
    """Reads a playlist's videos with yt-dlp: titles and lengths only, nothing downloaded."""

    done = Signal(object)  # the job itself
    TIMEOUT_MS = 120_000

    def __init__(self, list_id: str, video_id: str = "", parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.list_id = list_id
        self.video_id = video_id
        self.title = ""
        self.channel = ""
        self.entries: list[dict] = []
        self.unavailable = 0
        self.kind = "playlist"  # the same fields as an ImportBatch, so either can be imported
        self.subtitle = ""
        self.share_id = ""
        self.error = ""
        self.cancelled = False
        self._process: QProcess | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._timed_out)

    def start(self) -> None:
        args = ["-J", "--flat-playlist", "--socket-timeout", "15",
                playlist_url(self.list_id, self.video_id)]
        process = QProcess(self)
        process.setProgram(tool("yt-dlp"))
        process.setArguments(args)
        process.finished.connect(
            lambda code, status, p=process: self._finished(p, code, status))
        process.errorOccurred.connect(lambda error, p=process: self._start_failed(p, error))
        self._process = process
        log.info("Reading playlist %s", self.list_id)
        self._timer.start(self.TIMEOUT_MS)
        process.start()

    def cancel(self) -> None:
        self.cancelled = True
        self._timer.stop()
        process, self._process = self._process, None
        if process is not None:
            process.kill()
            process.waitForFinished(500)
            process.deleteLater()

    def _finished(self, process: QProcess, code: int, status) -> None:
        if process is not self._process:
            return
        self._process = None
        self._timer.stop()
        out = bytes(process.readAllStandardOutput()).decode("utf-8", "replace")
        err = bytes(process.readAllStandardError()).decode("utf-8", "replace")
        process.deleteLater()
        lines = [redact(line.strip()) for line in err.splitlines() if line.strip()]
        for line in lines:
            log.info("yt-dlp: %s", line)
        data = None
        if status == QProcess.ExitStatus.NormalExit and code == 0:
            try:
                data = json.loads(out)
            except ValueError:
                data = None
        if isinstance(data, dict):
            self.title = str(data.get("title") or "Imported playlist")
            self.channel = str(data.get("channel") or data.get("uploader") or "")
            self.entries, self.unavailable = playlist_entries(data)
            if not self.entries:
                self.error = "This playlist has no videos that can be played."
            log.info("Playlist %s has %d playable songs (%d unavailable)", self.list_id,
                     len(self.entries), self.unavailable)
        else:
            self.error = (describe_playlist_failure(lines) if lines
                          else "yt-dlp couldn't read the playlist.")
        self.done.emit(self)

    def _start_failed(self, process: QProcess, error) -> None:
        if error != QProcess.ProcessError.FailedToStart or process is not self._process:
            return
        self._process = None
        self._timer.stop()
        process.deleteLater()
        self.error = "Importing needs yt-dlp. " + install_hint("yt-dlp")
        self.done.emit(self)

    def _timed_out(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        process.kill()
        process.waitForFinished(500)
        process.deleteLater()
        self.error = "YouTube took too long to list the playlist. Try again in a moment."
        self.done.emit(self)


def song_count(count: int) -> str:
    return f"{count} song" + ("" if count == 1 else "s")


def import_summary(library: Library, added: dict[str, int], total: int, unavailable: int,
                   created: Category | None) -> str:
    if created is not None and len(added) == 1:
        text = f"Imported {song_count(added[created.id])} into “{created.title}”."
    elif len(added) == 1:
        category_id, count = next(iter(added.items()))
        category = library.category(category_id)
        text = f"Added {song_count(count)} to “{category.title if category else 'the category'}”."
        if count < total:
            skipped = total - count
            text += f" {skipped} {'was' if skipped == 1 else 'were'} already there."
    else:
        text = f"Added {song_count(total)} to {len(added)} categories."
    if unavailable:
        text += f" {unavailable} unavailable video{'' if unavailable == 1 else 's'} left out."
    return text


# ── Sharing ──────────────────────────────────────────────────────────────────
# A share code is the category as compact JSON, compressed and made text-safe:
#   tapedeck:1:<base64url of zlib(JSON)>
# JSON: {"i": share id, "n": name, "d": subtitle, "s": [[video id, seconds, renamed title], …]}
SHARE_PREFIX = "tapedeck:1:"
MAX_SHARED_SONGS = 5000
MAX_SHARE_BYTES = 2_000_000  # after decompression; refuses "zip bombs"


@dataclass
class ImportBatch:
    """Songs to import that didn't come from a YouTube playlist: a share code or pasted links."""
    title: str
    channel: str
    entries: list[dict]
    kind: str               # "code" or "links"
    subtitle: str = ""
    share_id: str = ""
    unavailable: int = 0


def make_share_code(library: Library, category: Category) -> str:
    songs = []
    for song in (library.song(sid) for sid in category.song_ids):
        if song is None:
            continue
        item: list = [song.video_id, int(song.duration or 0)]
        if song.title:
            item.append(song.title)
        songs.append(item)
    payload = {"i": category.share_id, "n": category.title, "d": category.subtitle, "s": songs}
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    packed = base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode("ascii").rstrip("=")
    return SHARE_PREFIX + packed


def looks_like_share_code(text: str) -> bool:
    return "".join(text.split())[:len(SHARE_PREFIX)].lower() == SHARE_PREFIX


def _unpack_code(text: str, prefix: str):
    """The JSON inside a code, or None if it's damaged, incomplete or suspiciously big."""
    compact = "".join(text.split())  # chat apps sometimes wrap long codes
    if compact[:len(prefix)].lower() != prefix:
        return None
    body = compact[len(prefix):]
    if not body or not re.fullmatch(r"[A-Za-z0-9_-]+", body):
        return None
    try:
        packed = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        inflater = zlib.decompressobj()
        raw = inflater.decompress(packed, MAX_SHARE_BYTES)
        if inflater.unconsumed_tail:
            return None
        return json.loads(raw.decode("utf-8"))
    except (ValueError, zlib.error, UnicodeDecodeError):
        return None


def _text_of(value, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _shared_entries(items, full: bool) -> list[dict]:
    """Songs from a code. Share codes carry renamed titles; library codes carry every
    song's title and channel (full=True)."""
    entries: list[dict] = []
    seen: set[str] = set()
    for item in items if isinstance(items, list) else []:
        if isinstance(item, str):
            item = [item]
        if not isinstance(item, list) or not item or not isinstance(item[0], str):
            continue
        video_id = item[0]
        if not VIDEO_ID.fullmatch(video_id) or video_id in seen:
            continue
        seen.add(video_id)
        seconds = item[1] if len(item) > 1 else 0
        valid = isinstance(seconds, int) and not isinstance(seconds, bool) and 0 < seconds < 360_000
        third = _text_of(item[2], 200) if len(item) > 2 else ""
        entries.append({"video_id": video_id, "duration": seconds if valid else None,
                        "title": third if full else "", "custom": "" if full else third,
                        "channel": _text_of(item[3], 120) if full and len(item) > 3 else ""})
        if len(entries) >= MAX_SHARED_SONGS:
            break
    return entries


def _share_id(value) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{12}", value) else ""


def read_share_code(text: str) -> ImportBatch | None:
    """The category in a share code, or None if it's damaged or incomplete."""
    data = _unpack_code(text, SHARE_PREFIX)
    if not isinstance(data, dict) or not isinstance(data.get("s"), list):
        return None
    return ImportBatch(title=_text_of(data.get("n"), 80) or "Shared playlist",
                       channel="a Tapedeck share code", entries=_shared_entries(data["s"], False),
                       kind="code", subtitle=_text_of(data.get("d"), 140),
                       share_id=_share_id(data.get("i")))


# A library code is every category at once, for moving to the phone app or another computer:
#   tapedeck:lib:1:<base64url of zlib(JSON)>
# JSON: {"a": accent, "c": [{"i", "n", "d", "s": [[video id, seconds, title, channel], …]}, …]}
LIBRARY_PREFIX = "tapedeck:lib:1:"
MAX_SHARED_CATEGORIES = 500


def make_library_code(library: Library) -> str:
    categories = []
    for category in library.categories:
        songs = [[song.video_id, int(song.duration or 0), song.display_title, song.channel]
                 for song in (library.song(sid) for sid in category.song_ids) if song is not None]
        categories.append({"i": category.share_id, "n": category.title, "d": category.subtitle,
                           "s": songs})
    raw = json.dumps({"a": ACCENT, "c": categories}, separators=(",", ":"),
                     ensure_ascii=False).encode("utf-8")
    return LIBRARY_PREFIX + base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode("ascii").rstrip("=")


def looks_like_library_code(text: str) -> bool:
    return "".join(text.split())[:len(LIBRARY_PREFIX)].lower() == LIBRARY_PREFIX


def read_library_code(text: str) -> list[ImportBatch] | None:
    data = _unpack_code(text, LIBRARY_PREFIX)
    if not isinstance(data, dict) or not isinstance(data.get("c"), list):
        return None
    batches = []
    for item in data["c"][:MAX_SHARED_CATEGORIES]:
        if isinstance(item, dict):
            batches.append(ImportBatch(title=_text_of(item.get("n"), 80) or "Imported playlist",
                                       channel="a Tapedeck library",
                                       entries=_shared_entries(item.get("s"), True), kind="library",
                                       subtitle=_text_of(item.get("d"), 140),
                                       share_id=_share_id(item.get("i"))))
    return batches


def music_url(video_id: str) -> str:
    return f"https://music.youtube.com/watch?v={video_id}"


def copy_text(ctx: Ctx, text: str, confirmation: str) -> None:
    QApplication.clipboard().setText(text)
    if ctx.window is not None:
        ctx.window.notify(confirmation)


def add_link_actions(menu: QMenu, ctx: Ctx, video_id: str) -> None:
    menu.addAction("Copy YouTube link").triggered.connect(
        lambda: copy_text(ctx, watch_url(video_id), "YouTube link copied"))
    menu.addAction("Copy YouTube Music link").triggered.connect(
        lambda: copy_text(ctx, music_url(video_id), "YouTube Music link copied"))


def song_links(library: Library, category: Category) -> str:
    return "\n".join(watch_url(song.video_id) for song in
                     (library.song(sid) for sid in category.song_ids) if song is not None)


def parse_link_list(text: str) -> list[str]:
    """Video ids from pasted links (one per line, or separated by spaces or commas)."""
    found: list[str] = []
    for token in re.split(r"[\s,;]+", text.strip()):
        video_id = parse_video_id(token) if token else None
        if video_id and video_id not in found:
            found.append(video_id)
    return found


def update_summary(added: int, removed: int, name: str) -> str:
    if not added and not removed:
        return f"“{name}” already matched."
    parts = []
    if added:
        parts.append(f"added {song_count(added)}")
    if removed:
        parts.append(f"removed {song_count(removed)}")
    return f"Updated “{name}”: " + " and ".join(parts) + "."


# ── QR codes ─────────────────────────────────────────────────────────────────
# A small QR Code (Model 2) encoder: byte mode, error correction L or M, any
# version 1 to 40. Tapedeck shows one so the phone app can scan its way to
# the library; this way there's no extra library to install.

_QR_ECC_PER_BLOCK = {  # error correction codewords per block, by version
    "L": (-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28,
          28, 28, 30, 30, 26, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    "M": (-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26,
          26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28),
}
_QR_BLOCKS = {  # number of error correction blocks, by version
    "L": (-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7, 8,
          8, 9, 9, 10, 12, 12, 12, 13, 14, 15, 16, 17, 18, 19, 19, 20, 21, 22, 24, 25),
    "M": (-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16,
          17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49),
}
_QR_FORMAT_BITS = {"L": 1, "M": 0}
_QR_MASKS = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
)


def _qr_raw_modules(version: int) -> int:
    """Modules left for data and error correction once the fixed patterns are drawn."""
    result = (16 * version + 128) * version + 64
    if version >= 2:
        aligns = version // 7 + 2
        result -= (25 * aligns - 10) * aligns - 55
        if version >= 7:
            result -= 36
    return result


def _qr_data_codewords(version: int, level: str) -> int:
    return _qr_raw_modules(version) // 8 - _QR_ECC_PER_BLOCK[level][version] * _QR_BLOCKS[level][version]


def _gf_multiply(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_multiply(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_multiply(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for byte in data:
        factor = byte ^ result.pop(0)
        result.append(0)
        for i, coefficient in enumerate(divisor):
            result[i] ^= _gf_multiply(coefficient, factor)
    return result


def _qr_alignment_positions(version: int) -> list[int]:
    if version == 1:
        return []
    count = version // 7 + 2
    step = (version * 8 + count * 3 + 5) // (count * 4 - 4) * 2
    size = version * 4 + 17
    return [6] + [size - 7 - i * step for i in reversed(range(count - 1))]


def _qr_codewords(data: bytes, version: int, level: str) -> list[int]:
    """The data bit stream, padded, split into blocks and interleaved with error correction."""
    capacity = _qr_data_codewords(version, level)
    bits: list[int] = []

    def put(value: int, length: int) -> None:
        bits.extend((value >> i) & 1 for i in reversed(range(length)))

    put(0b0100, 4)  # byte mode
    put(len(data), 8 if version < 10 else 16)
    for byte in data:
        put(byte, 8)
    put(0, min(4, capacity * 8 - len(bits)))
    put(0, -len(bits) % 8)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(words) < capacity:
        words.append(pad)
        pad ^= 0xEC ^ 0x11
    block_count = _QR_BLOCKS[level][version]
    ecc_length = _QR_ECC_PER_BLOCK[level][version]
    raw = _qr_raw_modules(version) // 8
    short_blocks = block_count - raw % block_count
    short_length = raw // block_count
    divisor = _rs_divisor(ecc_length)
    blocks, start = [], 0
    for index in range(block_count):
        length = short_length - ecc_length + (0 if index < short_blocks else 1)
        chunk = words[start:start + length]
        start += length
        ecc = _rs_remainder(chunk, divisor)
        if index < short_blocks:
            chunk = chunk + [0]  # placeholder so every block has the same length
        blocks.append(chunk + ecc)
    result = []
    for i in range(len(blocks[0])):
        for j, block in enumerate(blocks):
            if i != short_length - ecc_length or j >= short_blocks:  # skip placeholders
                result.append(block[i])
    return result


def _qr_penalty(modules: list[list[bool]]) -> int:
    """How hard a masked symbol is to read (lower is better), per the QR standard's rules."""
    size = len(modules)
    lines = ["".join("1" if cell else "0" for cell in row) for row in modules]
    lines += ["".join(lines[y][x] for y in range(size)) for x in range(size)]
    score = 0
    for line in lines:
        for _, run in itertools.groupby(line):
            length = sum(1 for _ in run)
            if length >= 5:
                score += length - 2
        padded = "0000" + line + "0000"
        score += 40 * (padded.count("10111010000") + padded.count("00001011101"))
    for y in range(size - 1):
        top, bottom = modules[y], modules[y + 1]
        for x in range(size - 1):
            if top[x] == top[x + 1] == bottom[x] == bottom[x + 1]:
                score += 3
    dark = sum(row.count(True) for row in modules)
    total = size * size
    score += 10 * ((abs(dark * 20 - total * 10) + total - 1) // total - 1)
    return score


def qr_matrix(text: str, level: str = "M", max_version: int = 40,
              mask: int | None = None) -> list[list[bool]]:
    """Encode text (UTF-8) as a QR code; True is a dark module. Raises ValueError if it's too long.

    The mask is normally chosen by the standard's penalty rules; tests can force one.
    """
    data = text.encode("utf-8")
    for version in range(1, max_version + 1):
        header = 4 + (8 if version < 10 else 16)
        if header + len(data) * 8 <= _qr_data_codewords(version, level) * 8:
            break
    else:
        raise ValueError("too much text for one QR code")
    size = version * 4 + 17
    modules = [[False] * size for _ in range(size)]
    fixed = [[False] * size for _ in range(size)]

    def set_fixed(x: int, y: int, dark: bool) -> None:
        modules[y][x] = dark
        fixed[y][x] = True

    for i in range(size):  # timing patterns
        set_fixed(6, i, i % 2 == 0)
        set_fixed(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):  # finder patterns and separators
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < size and 0 <= y < size:
                    set_fixed(x, y, max(abs(dx), abs(dy)) not in (2, 4))
    positions = _qr_alignment_positions(version)
    last = len(positions) - 1
    for i, ax in enumerate(positions):
        for j, ay in enumerate(positions):
            if (i, j) in ((0, 0), (0, last), (last, 0)):
                continue  # those corners hold finder patterns
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    set_fixed(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)

    def draw_format(mask: int) -> None:
        value = _QR_FORMAT_BITS[level] << 3 | mask
        remainder = value
        for _ in range(10):
            remainder = (remainder << 1) ^ ((remainder >> 9) * 0x537)
        bits = (value << 10 | remainder) ^ 0x5412
        bit = lambda i: (bits >> i) & 1 == 1
        for i in range(6):
            set_fixed(8, i, bit(i))
        set_fixed(8, 7, bit(6))
        set_fixed(8, 8, bit(7))
        set_fixed(7, 8, bit(8))
        for i in range(9, 15):
            set_fixed(14 - i, 8, bit(i))
        for i in range(8):
            set_fixed(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            set_fixed(8, size - 15 + i, bit(i))
        set_fixed(8, size - 8, True)  # the module that's always dark

    draw_format(0)  # reserves the format areas; redrawn once the mask is chosen
    if version >= 7:
        remainder = version
        for _ in range(12):
            remainder = (remainder << 1) ^ ((remainder >> 11) * 0x1F25)
        bits = version << 12 | remainder
        for i in range(18):
            dark = (bits >> i) & 1 == 1
            a, b = size - 11 + i % 3, i // 3
            set_fixed(a, b, dark)
            set_fixed(b, a, dark)

    codewords = _qr_codewords(data, version, level)
    index = 0
    for right in range(size - 1, 0, -2):  # zigzag through column pairs, skipping the timing column
        if right <= 6:
            right -= 1
        upward = (right + 1) & 2 == 0
        for vertical in range(size):
            y = size - 1 - vertical if upward else vertical
            for x in (right, right - 1):
                if not fixed[y][x] and index < len(codewords) * 8:
                    modules[y][x] = (codewords[index >> 3] >> (7 - (index & 7))) & 1 == 1
                    index += 1

    def apply_mask(mask: int) -> None:
        rule = _QR_MASKS[mask]
        for y in range(size):
            row, fixed_row = modules[y], fixed[y]
            for x in range(size):
                if not fixed_row[x] and rule(x, y):
                    row[x] = not row[x]

    best, best_score = 0, None
    for candidate in (range(8) if mask is None else [mask]):
        apply_mask(candidate)
        draw_format(candidate)
        score = _qr_penalty(modules)
        if best_score is None or score < best_score:
            best, best_score = candidate, score
        apply_mask(candidate)  # masking twice undoes it
    apply_mask(best)
    draw_format(best)
    return modules


# ── Copying to the phone ─────────────────────────────────────────────────────
def lan_address() -> str:
    """This computer's address on the home network, preferring Wi-Fi, then Ethernet."""
    found: list[tuple[int, str]] = []
    for interface in QNetworkInterface.allInterfaces():
        flags = interface.flags()
        if not (flags & QNetworkInterface.InterfaceFlag.IsUp) or \
                not (flags & QNetworkInterface.InterfaceFlag.IsRunning) or \
                flags & QNetworkInterface.InterfaceFlag.IsLoopBack:
            continue
        kind = interface.type()
        rank = {QNetworkInterface.InterfaceType.Wifi: 0,
                QNetworkInterface.InterfaceType.Ethernet: 1}.get(kind, 2)
        if kind == QNetworkInterface.InterfaceType.Virtual:
            continue
        for entry in interface.addressEntries():
            address = entry.ip()
            if address.protocol() != QAbstractSocket.NetworkLayerProtocol.IPv4Protocol:
                continue
            text = address.toString()
            try:
                ip = ipaddress.ip_address(text)
            except ValueError:
                continue
            if ip.is_private and not ip.is_link_local and not ip.is_loopback:
                found.append((rank * 10 + (0 if text.startswith("192.168.") else 1), text))
    return min(found)[1] if found else ""


AUDIO_TYPES = {".webm": "audio/webm", ".weba": "audio/webm", ".m4a": "audio/mp4", ".mp4": "audio/mp4",
               ".opus": "audio/ogg", ".ogg": "audio/ogg", ".mp3": "audio/mpeg"}


class _Connection:
    """One request from the phone: the bytes read so far, then the file being sent."""

    __slots__ = ("socket", "timer", "buffer", "answered", "handle", "left", "video_id", "to_end")

    def __init__(self, socket, timer) -> None:
        self.socket, self.timer = socket, timer
        self.buffer = bytearray()
        self.answered = False
        self.handle = None
        self.left = 0
        self.video_id = ""
        self.to_end = False


class PhoneTransfer(QObject):
    """Hands the library to the phone over Wi-Fi, but only while the prompt is open.

    The phone fetches the library code, then sends the list of songs it still needs; the ones
    saved on this computer are copied straight from here instead of coming from YouTube again.
    Every address carries a random one-time token, and nothing else is served.
    """

    sent = Signal()        # the phone has the library
    progressed = Signal()  # a song finished sending, or the phone asked for songs

    CHUNK = 256 * 1024
    MAX_LIST = 1024 * 1024  # the phone's list of songs it needs
    IDLE_MS = 15_000

    def __init__(self, code: str, songs: dict[str, Path] | None = None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.body = code.encode("utf-8")
        self.songs = dict(songs or {})  # video id → saved audio file
        self.token = secrets.token_urlsafe(12)
        self.url = ""
        self.wanted: set[str] = set()  # songs the phone asked this computer for
        self.done: set[str] = set()    # …and has received in full
        self._sent_bytes = 0
        self._samples: deque[tuple[float, int]] = deque(maxlen=400)
        self._connections: dict = {}
        self.server = QTcpServer(self)
        self.server.newConnection.connect(self._accept)
        address = lan_address()
        if address and self.server.listen(QHostAddress(QHostAddress.SpecialAddress.AnyIPv4), 0):
            self.url = f"http://{address}:{self.server.serverPort()}/t/{self.token}"
            log.info("Library ready for the phone at %s", self.url.rsplit("/", 1)[0] + "/…")

    @property
    def speed(self) -> float:
        """Bytes per second over the last three seconds; 0 when nothing is moving."""
        now = time.monotonic()
        recent = [(when, total) for when, total in self._samples if now - when <= 3.0]
        if len(recent) < 2 or now - recent[-1][0] > 1.5:
            return 0.0
        (first_time, first_total), (last_time, last_total) = recent[0], recent[-1]
        return (last_total - first_total) / max(last_time - first_time, 0.25)

    def close(self) -> None:
        if self.wanted:
            log.info("Phone transfer closed: sent %d of %d songs",
                     len(self.done & self.wanted), len(self.wanted))
        self.server.close()
        for connection in list(self._connections.values()):
            connection.socket.abort()

    def _accept(self) -> None:
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            timer = QTimer(socket)  # drop connections that go quiet
            timer.setSingleShot(True)
            timer.setInterval(self.IDLE_MS)
            timer.timeout.connect(socket.abort)
            connection = _Connection(socket, timer)
            self._connections[socket] = connection
            socket.readyRead.connect(lambda c=connection: self._read(c))
            socket.bytesWritten.connect(lambda _count, c=connection: self._pump(c))
            socket.disconnected.connect(lambda c=connection: self._closed(c))
            timer.start()

    def _read(self, connection: _Connection) -> None:
        connection.timer.start()  # it's active
        data = bytes(connection.socket.readAll())
        if connection.answered:
            return
        connection.buffer += data
        end = connection.buffer.find(b"\r\n\r\n")
        if end < 0:
            if len(connection.buffer) > 65536:
                self._reply(connection, "431 Request Header Fields Too Large", b"")
            return  # the request isn't complete yet
        lines = bytes(connection.buffer[:end]).decode("latin-1", "replace").split("\r\n")
        request = lines[0].split(" ")
        method = request[0]
        path = request[1] if len(request) > 1 else ""
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        body = bytes(connection.buffer[end + 4:])
        if method == "POST":
            try:
                length = int(headers.get("content-length", "0"))
            except ValueError:
                length = -1
            if not 0 <= length <= self.MAX_LIST:
                self._reply(connection, "413 Payload Too Large", b"")
                return
            if len(body) < length:
                return  # wait for the rest of the list
            body = body[:length]
        connection.answered = True
        self._route(connection, method, path, headers, body)

    def _route(self, connection: _Connection, method: str, path: str, headers: dict, body: bytes) -> None:
        token = self.token
        shown = path.replace(token, "…") if token else path  # the log never shows the secret part
        log.info("Phone: %s %s%s", method, shown,
                 f" ({headers['range']})" if "range" in headers else "")
        if path == f"/t/{token}" and method in ("GET", "HEAD"):
            self._reply(connection, "200 OK", self.body, head_only=method == "HEAD")
            if method == "GET":
                log.info("Sent the library to the phone")
                self.sent.emit()
        elif path == f"/s/{token}" and method == "POST":
            asked = {line.strip() for line in body.decode("utf-8", "replace").splitlines()}
            offer = sorted(video_id for video_id in asked if video_id in self.songs)
            self.wanted.update(offer)
            log.info("The phone asked for %d songs; %d are saved here", len(asked), len(offer))
            self._reply(connection, "200 OK", "\n".join(offer).encode("ascii"))
            self.progressed.emit()
        elif path.startswith(f"/a/{token}/") and method in ("GET", "HEAD"):
            video_id = path.rsplit("/", 1)[-1]
            target = self.songs.get(video_id) if VIDEO_ID.fullmatch(video_id) else None
            if target is None:
                self._reply(connection, "404 Not Found", b"Not found")
            else:
                self._send_file(connection, video_id, target, method == "HEAD", headers.get("range", ""))
        else:
            self._reply(connection, "404 Not Found", b"Not found")

    def _reply(self, connection: _Connection, status: str, body: bytes, head_only: bool = False,
               extra: str = "") -> None:
        connection.answered = True
        head = (f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\n{extra}Cache-Control: no-store\r\n"
                "Connection: close\r\n\r\n")
        connection.socket.write(head.encode("ascii") + (b"" if head_only else body))
        connection.socket.disconnectFromHost()

    def _send_file(self, connection: _Connection, video_id: str, path: Path, head_only: bool,
                   wanted_range: str) -> None:
        try:
            size = path.stat().st_size
        except OSError:
            self._reply(connection, "404 Not Found", b"Not found")
            return
        start, end, status = 0, size - 1, "200 OK"
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", wanted_range.replace(" ", ""))
        if match and (match.group(1) or match.group(2)):  # resuming partway through
            if match.group(1):
                start = int(match.group(1))
                if match.group(2):
                    end = min(int(match.group(2)), size - 1)
            else:  # "the last N bytes"
                start = max(0, size - int(match.group(2)))
            if start > end:
                self._reply(connection, "416 Range Not Satisfiable", b"",
                            extra=f"Content-Range: bytes */{size}\r\n")
                return
            status = "206 Partial Content"
        length = max(0, end - start + 1)
        kind = AUDIO_TYPES.get(path.suffix.lower(), "application/octet-stream")
        head = (f"HTTP/1.1 {status}\r\nContent-Type: {kind}\r\nContent-Length: {length}\r\n"
                "Accept-Ranges: bytes\r\n"
                + (f"Content-Range: bytes {start}-{end}/{size}\r\n" if status.startswith("206") else "")
                + "Cache-Control: no-store\r\nConnection: close\r\n\r\n")
        connection.socket.write(head.encode("ascii"))
        if head_only or length == 0:
            connection.socket.disconnectFromHost()
            return
        try:
            handle = open(path, "rb")
            handle.seek(start)
        except OSError:
            connection.socket.abort()
            return
        connection.handle, connection.left = handle, length
        connection.video_id, connection.to_end = video_id, end == size - 1
        self._pump(connection)

    def _pump(self, connection: _Connection) -> None:
        """Feed the file to the socket a chunk at a time, so a whole song never sits in memory."""
        connection.timer.start()  # it's active
        handle = connection.handle
        if handle is None:
            return
        socket = connection.socket
        complete = True
        while connection.left > 0 and socket.bytesToWrite() < 2 * self.CHUNK:
            data = handle.read(min(self.CHUNK, connection.left))
            if not data:  # the file got shorter while sending; the phone will try again
                connection.left, complete = 0, False
                break
            socket.write(data)
            connection.left -= len(data)
            self._sent_bytes += len(data)
            self._samples.append((time.monotonic(), self._sent_bytes))
        if connection.left > 0:
            return
        handle.close()
        connection.handle = None
        if complete and connection.to_end:
            self.done.add(connection.video_id)
            log.info("Phone: received %s (%d of %d)", connection.video_id,
                     len(self.done & self.wanted), len(self.wanted))
            self.progressed.emit()
        socket.disconnectFromHost()  # after everything queued has gone out

    def _closed(self, connection: _Connection) -> None:
        if connection.handle is not None:
            connection.handle.close()
            connection.handle = None
        self._connections.pop(connection.socket, None)
        connection.socket.deleteLater()


# ── Thumbnails ───────────────────────────────────────────────────────────────
class ThumbCache(QObject):
    """YouTube thumbnails, cached on disk and in memory."""

    ready = Signal(str)  # video id

    def __init__(self, folder: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.folder = folder
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # thumbnails still work, just without the disk cache
        # Recently used thumbnails, downscaled: about 130 KB each instead of 230 KB,
        # and at most MEMORY_LIMIT of them (the rest reload from disk in a moment).
        self._memory: collections.OrderedDict[str, QPixmap] = collections.OrderedDict()
        self._pending: set[str] = set()
        self._failed: dict[str, float] = {}  # video id → when to try again
        self._network = QNetworkAccessManager(self)

    MEMORY_LIMIT = 240
    RETRY_AFTER = 10 * 60
    KEEP = QSize(240, 135)  # sharp enough for the largest thumbnail at high zoom

    def _remember(self, video_id: str, pixmap: QPixmap) -> QPixmap:
        if pixmap.width() > self.KEEP.width():
            pixmap = pixmap.scaled(self.KEEP, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                   Qt.TransformationMode.SmoothTransformation)
        self._memory[video_id] = pixmap
        self._memory.move_to_end(video_id)
        while len(self._memory) > self.MEMORY_LIMIT:
            self._memory.popitem(last=False)
        return pixmap

    def get(self, video_id: str) -> QPixmap | None:
        """Return the thumbnail now if we have it; otherwise fetch it and emit ready()."""
        if not video_id:
            return None
        if video_id in self._memory:
            self._memory.move_to_end(video_id)
            return self._memory[video_id]
        path = self.folder / f"{video_id}.jpg"
        if path.exists():
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                return self._remember(video_id, pixmap)
        if time.monotonic() < self._failed.get(video_id, 0.0):
            return None
        if video_id not in self._pending:
            self._pending.add(video_id)
            # mqdefault is 16:9 with no letterboxing.
            url = QUrl(f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg")
            request = QNetworkRequest(url)
            request.setTransferTimeout(15000)
            reply = self._network.get(request)
            reply.finished.connect(lambda r=reply, v=video_id: self._done(r, v))
        return None

    def _done(self, reply: QNetworkReply, video_id: str) -> None:
        self._pending.discard(video_id)
        data = b""
        if reply.error() == QNetworkReply.NetworkError.NoError:
            data = bytes(reply.readAll())
        reply.deleteLater()
        pixmap = QPixmap()
        if data and pixmap.loadFromData(data):
            self._failed.pop(video_id, None)
            self._remember(video_id, pixmap)
            try:
                (self.folder / f"{video_id}.jpg").write_bytes(data)
            except OSError:
                pass
            self.ready.emit(video_id)
        else:  # offline, or no thumbnail: try again in a while rather than never
            self._failed[video_id] = time.monotonic() + self.RETRY_AFTER


# ── Settings ─────────────────────────────────────────────────────────────────
class Settings:
    """Small preferences file: volume, mute, and whether songs get saved."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict = {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._data = data
        except (OSError, ValueError):
            pass

    def get(self, key: str, default):
        value = self._data.get(key, default)
        if isinstance(default, bool):
            return value if isinstance(value, bool) else default
        if isinstance(default, (int, float)):
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            return value if ok else default
        return value if default is None or isinstance(value, type(default)) else default

    def set(self, key: str, value) -> None:
        if self._data.get(key) == value:
            return
        self._data[key] = value
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            replace_file(tmp, self.path)
        except OSError as exc:
            log.warning("Couldn't save settings: %s", exc)


# ── Saved songs ──────────────────────────────────────────────────────────────
# YouTube makes every player wait about five seconds before a song's audio
# is available, and that can't be skipped. So each song's audio is saved to
# disk once (when you paste its link, when it's coming up next, and in the
# background for the rest of the library). Saved songs start straight away
# and play back to back without a gap. Each one is about 3 to 5 MB.

class Priority:
    NOW = 0         # clicked, or its link was just pasted
    UPCOMING = 1    # next in the queue
    VISIBLE = 2     # on the category page you opened
    BACKGROUND = 3  # everything else in the library


PARTIAL_SUFFIXES = (".part", ".ytdl", ".temp", ".tmp")


def find_saved_file(folder: Path, video_id: str, printed: str = "") -> Path | None:
    """The file yt-dlp saved, from what it printed or by looking in the folder."""
    for line in reversed(printed.splitlines()):
        line = line.strip()
        if line:
            path = Path(line)
            try:
                if path.is_file() and path.stat().st_size > 0:
                    return path
            except OSError:
                pass
            break
    try:
        found = [path for path in folder.glob(glob.escape(video_id) + ".*")
                 if path.is_file() and not path.name.endswith(PARTIAL_SUFFIXES)
                 and path.stat().st_size > 0]
    except OSError:
        return None
    return max(found, key=lambda path: path.stat().st_size) if found else None


class DownloadJob(QObject):
    """Saves one song's audio with yt-dlp."""

    done = Signal(object)  # the job itself
    TIMEOUT_MS = 5 * 60_000

    def __init__(self, video_id: str, folder: Path, priority: int,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.video_id = video_id
        self.folder = folder
        self.priority = priority
        self.path: Path | None = None
        self.error = ""
        self.cancelled = False
        self._process: QProcess | None = None
        self._started = 0.0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._timed_out)

    def start(self) -> None:
        args = ["-f", AUDIO_FORMAT, "--no-playlist", "--no-progress", "--quiet", "--no-mtime",
                "--socket-timeout", "15", "--retries", "3",
                "-P", str(self.folder), "-o", "%(id)s.%(ext)s",
                "--print", "after_move:filepath", "--no-simulate", watch_url(self.video_id)]
        process = QProcess(self)
        process.setProgram(tool("yt-dlp"))
        process.setArguments(args)
        process.finished.connect(
            lambda code, status, p=process: self._finished(p, code, status))
        process.errorOccurred.connect(lambda error, p=process: self._start_failed(p, error))
        self._process = process
        self._started = time.monotonic()
        log.debug("yt-dlp %s", " ".join(args))
        self._timer.start(self.TIMEOUT_MS)
        process.start()

    def cancel(self) -> None:
        self.cancelled = True
        self._timer.stop()
        process, self._process = self._process, None
        if process is not None:
            process.kill()
            process.waitForFinished(500)
            process.deleteLater()
        self.deleteLater()

    def _finished(self, process: QProcess, code: int, status) -> None:
        if process is not self._process:
            return
        self._process = None
        self._timer.stop()
        out = bytes(process.readAllStandardOutput()).decode("utf-8", "replace")
        err = bytes(process.readAllStandardError()).decode("utf-8", "replace")
        process.deleteLater()
        lines = [redact(line.strip()) for line in err.splitlines() if line.strip()]
        for line in lines:
            log.info("yt-dlp: %s", line)
        if status == QProcess.ExitStatus.NormalExit and code == 0:
            self.path = find_saved_file(self.folder, self.video_id, out)
        if self.path is not None:
            try:
                size = self.path.stat().st_size / 1e6
            except OSError:
                size = 0.0
            log.info("Saved %s (%.1f MB) in %.1f s", self.video_id, size,
                     time.monotonic() - self._started)
        else:
            self.error = describe_failure(lines) if lines else "yt-dlp didn't save the song."
            log.warning("Couldn't save %s: %s", self.video_id, self.error)
        self.done.emit(self)

    def _start_failed(self, process: QProcess, error) -> None:
        if error != QProcess.ProcessError.FailedToStart or process is not self._process:
            return
        self._process = None
        self._timer.stop()
        process.deleteLater()
        self.error = "Saving songs needs yt-dlp. " + install_hint("yt-dlp")
        self.done.emit(self)

    def _timed_out(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        process.kill()
        process.waitForFinished(500)
        process.deleteLater()
        self.error = "Saving the song took too long."
        log.warning("Saving %s took too long", self.video_id)
        self.done.emit(self)


class AudioCache(QObject):
    """Songs saved on disk, plus a queue of songs to save, most urgent first."""

    changed = Signal()          # counts, size or activity changed
    saved = Signal(str)         # video id that's now saved
    failed = Signal(str, str)   # video id, why it couldn't be saved

    MAX_ACTIVE = 2
    RETRY_AFTER = 15 * 60
    BACKOFF = 20 * 60           # after YouTube's bot check or rate limit

    def __init__(self, folder: Path, settings: Settings, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.folder = folder
        self.settings = settings
        self.enabled = settings.get("save_songs", True)
        self.files: dict[str, Path] = {}
        self._sizes: dict[str, int] = {}
        self._heap: list[tuple[int, int, str]] = []
        self._queued: dict[str, int] = {}
        self._seq = itertools.count()
        self._active: dict[str, DownloadJob] = {}
        self._retry_at: dict[str, float] = {}
        self._paused_until = 0.0
        self._resume = QTimer(self)
        self._resume.setSingleShot(True)
        self._resume.timeout.connect(self._pump)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("Can't create %s: %s", folder, exc)
        self._scan()

    # What's saved
    def _scan(self) -> None:
        try:
            entries = list(self.folder.iterdir())
        except OSError:
            return
        for path in entries:
            if not path.is_file():
                continue
            if path.name.endswith(PARTIAL_SUFFIXES):  # left over from an interrupted save
                with contextlib.suppress(OSError):
                    path.unlink()
                continue
            video_id = path.name.split(".", 1)[0]
            if VIDEO_ID.fullmatch(video_id):
                self._remember(video_id, path)

    def _remember(self, video_id: str, path: Path) -> None:
        self.files[video_id] = path
        try:
            self._sizes[video_id] = path.stat().st_size
        except OSError:
            self._sizes[video_id] = 0

    def path_for(self, video_id: str) -> Path | None:
        path = self.files.get(video_id)
        if path is None:
            return None
        if path.is_file():
            return path
        self.files.pop(video_id, None)
        self._sizes.pop(video_id, None)
        return None

    def size_bytes(self) -> int:
        return sum(self._sizes.values())

    def has(self, video_id: str) -> bool:
        """Saved, as far as we know (no disk check; path_for checks before playing)."""
        return video_id in self.files

    def busy(self) -> bool:
        return bool(self._active or self._queued)

    # Asking for songs
    def want(self, video_id: str, priority: int, *, force: bool = False) -> None:
        """Save this song. Lower priority numbers go first."""
        if not video_id or video_id in self.files or video_id in self._active:
            return
        if not (self.enabled or force):
            return
        if priority > Priority.NOW and time.monotonic() < self._retry_at.get(video_id, 0.0):
            return
        best = self._queued.get(video_id)
        if best is not None and best <= priority:
            return
        self._queued[video_id] = priority
        heapq.heappush(self._heap, (priority, next(self._seq), video_id))
        self._pump()

    def want_library(self, library: Library, priority: int = Priority.BACKGROUND) -> None:
        """Queue every song, a few from each category in turn, top songs first."""
        columns = [category.song_ids for category in library.categories]
        for row in itertools.zip_longest(*columns):
            for song_id in row:
                song = library.song(song_id) if song_id else None
                if song is not None:
                    self.want(song.video_id, priority)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        self.settings.set("save_songs", enabled)
        if not enabled:
            for video_id, job in list(self._active.items()):
                if job.priority > Priority.NOW:
                    self._active.pop(video_id).cancel()
            self._heap = [item for item in self._heap if item[0] <= Priority.NOW]
            heapq.heapify(self._heap)
            self._queued = {v: p for v, p in self._queued.items() if p <= Priority.NOW}
        self.changed.emit()

    def forget(self, video_ids: list[str]) -> None:
        """Delete these songs' audio (they were removed from the library)."""
        for video_id in video_ids:
            self._queued.pop(video_id, None)
            job = self._active.pop(video_id, None)
            if job is not None:
                job.cancel()
            path = self.files.pop(video_id, None)
            self._sizes.pop(video_id, None)
            if path is not None:
                with contextlib.suppress(OSError):
                    path.unlink()
        self.changed.emit()
        self._pump()

    def keep_only(self, video_ids: set[str]) -> None:
        stale = [video_id for video_id in self.files if video_id not in video_ids]
        if stale:
            log.info("Deleting %d saved songs that aren't in the library", len(stale))
            self.forget(stale)

    def clear(self) -> None:
        for job in list(self._active.values()):
            job.cancel()
        self._active.clear()
        self._heap.clear()
        self._queued.clear()
        self.forget(list(self.files))

    # Working through the queue
    def _pump(self) -> None:
        now = time.monotonic()
        while len(self._active) < self.MAX_ACTIVE and self._heap:
            priority, _, video_id = self._heap[0]
            if self._queued.get(video_id) != priority:  # superseded by a more urgent request
                heapq.heappop(self._heap)
                continue
            if now < self._paused_until and priority > Priority.NOW:
                self._resume.start(int((self._paused_until - now) * 1000) + 200)
                break
            heapq.heappop(self._heap)
            del self._queued[video_id]
            if video_id in self.files or video_id in self._active:
                continue
            job = DownloadJob(video_id, self.folder, priority, self)
            job.done.connect(self._done)
            self._active[video_id] = job
            job.start()
        self.changed.emit()

    def _done(self, job: DownloadJob) -> None:
        if self._active.get(job.video_id) is job:
            del self._active[job.video_id]
        job.deleteLater()
        if job.cancelled:
            return
        if job.path is not None:
            self._remember(job.video_id, job.path)
            self._retry_at.pop(job.video_id, None)
            self.saved.emit(job.video_id)
        else:
            self._retry_at[job.video_id] = time.monotonic() + self.RETRY_AFTER
            if re.search(r"not a bot|limiting|too many requests", job.error, re.IGNORECASE):
                self._paused_until = time.monotonic() + self.BACKOFF
                log.warning("YouTube is limiting requests; background saving pauses for "
                            "%d minutes", self.BACKOFF // 60)
            self.failed.emit(job.video_id, job.error)
        self._pump()


# ── Playback ─────────────────────────────────────────────────────────────────
# One mpv runs in the background for the whole session and is driven over its
# IPC socket, so there's no start-up cost per song, and volume, seeking and
# pause are instant. Saved songs play from disk. A song that isn't saved yet
# is looked up with yt-dlp; YouTube refuses its stream (HTTP 403) until a few
# seconds after the lookup ("available_at"), so Tapedeck waits that out.

AUDIO_FORMAT = "bestaudio/best"
FORBIDDEN = re.compile(r"\b403\b|forbidden", re.IGNORECASE)
STREAM_URL = re.compile(r"https?://[^\s'\"]*googlevideo\.com[^\s'\"]*")
GENERIC_LINE = re.compile(
    r"^(exiting\.\.\.|failed to recognize file format|playing:|\(\+\)|ao:|a:|audio:|file tags:)",
    re.IGNORECASE)
FAILURE_HINTS = (
    (r"confirm your age|age[- ]restricted",
     "This video is age-restricted, which needs a signed-in YouTube account."),
    (r"sign in to confirm you",
     "YouTube wants a sign-in to prove you're not a bot. This mostly happens on VPNs "
     "and shared networks."),
    (r"private video", "This video is private."),
    (r"video unavailable|this video is unavailable|has been removed|no longer available",
     "This video is no longer available on YouTube."),
    (r"no supported javascript runtime|challenge solving failed",
     "yt-dlp needs Deno to read YouTube. " + install_hint("deno")),
    (r"requested format is not available|only images are available|no video formats found",
     "YouTube didn't offer a playable audio stream. " + update_hint()),
    (r"\b403\b|forbidden",
     "YouTube refused the audio stream (HTTP 403). " + update_hint()),
    (r"\b429\b|too many requests",
     "YouTube is limiting requests from this connection. Wait a while, then try again."),
    (r"could not open/initialize audio device|failed to initialize audio|no audio output",
     "mpv couldn't open an audio device. Check your sound settings."),
    (r"name resolution|network is unreachable|timed out|connection refused",
     "Couldn't reach YouTube. Check your connection."),
)


def redact(text: str) -> str:
    """Stream URLs embed your IP address; keep them out of logs and messages."""
    return STREAM_URL.sub("<stream URL>", text)


def pretty_path(path: Path) -> str:
    home, text = str(Path.home()), str(path)
    return "~" + text[len(home):] if text.startswith(home + os.sep) else text


def log_hint() -> str:
    return f"Details are in {pretty_path(LOG_FILE)}"


def describe_failure(lines: list[str]) -> str:
    """Explain a yt-dlp or mpv failure in one or two plain sentences."""
    text = "\n".join(lines)
    for pattern, message in FAILURE_HINTS:
        if re.search(pattern, text, re.IGNORECASE):
            return message
    useful = [line for line in lines
              if re.search(r"error|failed|cannot|unable", line, re.IGNORECASE)
              and not GENERIC_LINE.match(line.strip())]
    detail = clean_error(useful[-1]).rstrip(". ") if useful else ""
    return f"{detail}. {log_hint()}." if detail else f"{log_hint()}."


def missing_tools() -> str:
    for name in ("mpv", "yt-dlp"):
        if shutil.which(name) is None:
            return f"Playback needs {name}. {install_hint(name)}"
    return ""


def video_details(data: dict) -> dict:
    duration = data.get("duration")
    try:
        duration = int(round(float(duration))) if duration is not None else None
    except (TypeError, ValueError):
        duration = None
    return {
        "title": str(data.get("title") or ""),
        "channel": str(data.get("channel") or data.get("uploader") or ""),
        "duration": duration,
    }


def pick_stream(info: dict) -> dict | None:
    """Find the audio stream yt-dlp chose (run with -J -f bestaudio/best)."""
    requested = [f for f in info.get("requested_formats") or [] if isinstance(f, dict)]
    if requested:  # a merged pick; use its audio half
        with_audio = [f for f in requested if f.get("acodec") not in (None, "none")]
        audio_only = [f for f in with_audio if f.get("vcodec") in (None, "none")]
        chosen = (audio_only or with_audio or requested)[0]
    else:
        format_id = info.get("format_id")
        chosen = next((f for f in info.get("formats") or []
                       if isinstance(f, dict) and format_id and f.get("format_id") == format_id),
                      info)
    url = str(chosen.get("url") or info.get("url") or "")
    protocol = str(chosen.get("protocol") or info.get("protocol") or "")
    if not url.startswith(("https://", "http://")) or "dash" in protocol or "sabr" in protocol:
        return None  # nothing mpv can open directly
    headers = chosen.get("http_headers") or info.get("http_headers") or {}
    available = chosen.get("available_at") or info.get("available_at")
    try:
        available = float(available) if available else None
    except (TypeError, ValueError):
        available = None
    if available and available > 1e11:  # milliseconds, just in case
        available /= 1000.0
    return {
        "url": url,
        "headers": {str(k): str(v) for k, v in headers.items()} if isinstance(headers, dict) else {},
        "available_at": available,
        "format": str(chosen.get("format_id") or info.get("format_id") or "?"),
        "codec": str(chosen.get("acodec") or "unknown codec"),
    }


class ResolveJob(QObject):
    """Asks yt-dlp where a song's audio stream is, without downloading it."""

    done = Signal(object)  # the job itself
    TIMEOUT_MS = 60_000

    def __init__(self, video_id: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.video_id = video_id
        self.stream: dict | None = None
        self.info: dict = {}
        self.error = ""
        self.lines: list[str] = []
        self.cancelled = False
        self._process: QProcess | None = None
        self._started = 0.0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._timed_out)

    def start(self) -> None:
        args = ["-J", "--no-playlist", "-f", AUDIO_FORMAT, "--socket-timeout", "15",
                watch_url(self.video_id)]
        process = QProcess(self)
        process.setProgram(tool("yt-dlp"))
        process.setArguments(args)
        process.finished.connect(
            lambda code, status, p=process: self._finished(p, code, status))
        process.errorOccurred.connect(lambda error, p=process: self._start_failed(p, error))
        self._process = process
        self._started = time.monotonic()
        log.debug("yt-dlp %s", " ".join(args))
        self._timer.start(self.TIMEOUT_MS)
        process.start()

    def cancel(self) -> None:
        self.cancelled = True
        self._timer.stop()
        process, self._process = self._process, None
        if process is not None:
            process.kill()
            process.waitForFinished(500)
            process.deleteLater()
        self.deleteLater()

    def _finished(self, process: QProcess, code: int, status) -> None:
        if process is not self._process:
            return
        self._process = None
        self._timer.stop()
        out = bytes(process.readAllStandardOutput()).decode("utf-8", "replace")
        err = bytes(process.readAllStandardError()).decode("utf-8", "replace")
        process.deleteLater()
        self.lines = [redact(line.strip()) for line in err.splitlines() if line.strip()]
        for line in self.lines:
            log.info("yt-dlp: %s", line)
        ok = status == QProcess.ExitStatus.NormalExit and code == 0
        data = None
        if ok:
            try:
                data = json.loads(out)
            except ValueError:
                data = None
        if isinstance(data, dict):
            self.info = video_details(data)
            self.stream = pick_stream(data)
            found = f"format {self.stream['format']}" if self.stream else "no direct stream"
            log.info("yt-dlp found %s for %s in %.1f s", found, self.video_id,
                     time.monotonic() - self._started)
        elif ok:
            self.error = f"yt-dlp gave an answer Tapedeck couldn't read. {log_hint()}."
        else:
            log.warning("yt-dlp exited with code %s for %s", code, self.video_id)
            self.error = describe_failure(self.lines)
        self.done.emit(self)

    def _start_failed(self, process: QProcess, error) -> None:
        if error != QProcess.ProcessError.FailedToStart or process is not self._process:
            return
        self._process = None
        self._timer.stop()
        process.deleteLater()
        self.error = "Playback needs yt-dlp. " + install_hint("yt-dlp")
        self.done.emit(self)

    def _timed_out(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        process.kill()
        process.waitForFinished(500)
        process.deleteLater()
        log.warning("yt-dlp took too long for %s", self.video_id)
        self.error = "YouTube took too long to answer. Try again in a moment."
        self.done.emit(self)


def mpv_launch_args(socket_path: str, volume: int, muted: bool, loop: bool = False) -> list[str]:
    """One background mpv for the whole session, audio only, driven over IPC."""
    return [
        "--idle=yes", f"--input-ipc-server={socket_path}",
        "--no-video", "--ytdl=no", "--input-terminal=no", "--msg-level=all=warn",
        "--force-window=no", "--audio-display=no", "--keep-open=no",
        f"--loop-file={'inf' if loop else 'no'}", "--loop-playlist=no", "--shuffle=no",
        "--save-position-on-quit=no", "--resume-playback=no",
        "--gapless-audio=weak", "--prefetch-playlist=yes", "--network-timeout=20",
        f"--audio-client-name={APP_NAME}", "--volume-max=100",
        f"--volume={volume}", f"--mute={'yes' if muted else 'no'}",
        # mpv's optional autoload script would add every saved song to the playlist
        "--script-opts-append=autoload-disabled=yes",
    ]


class MpvIpc(QObject):
    """mpv kept running in the background, controlled over its JSON IPC socket."""

    received = Signal(object)  # each mpv event, as a dict
    ready = Signal()
    died = Signal(str)      # why, in a sentence

    OBSERVED = ("time-pos", "duration", "pause", "volume", "mute", "seekable",
                "paused-for-cache", "loop-file")
    CONNECT_TRIES = 75
    RETRY_MS = 80

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        if IS_WINDOWS:  # mpv and QLocalSocket both speak named pipes there
            self.socket_path = rf"\\.\pipe\tapedeck-mpv-{os.getpid()}"
        else:
            runtime = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
            self.socket_path = str(Path(runtime) / f"tapedeck-mpv-{os.getpid()}.sock")
        self.volume, self.muted, self.loop = 80, False, False  # used whenever mpv (re)starts
        self._process: QProcess | None = None
        self._socket: QLocalSocket | None = None
        self._connected = False
        self._quitting = False
        self._buffer = b""
        self._outbox: list[bytes] = []
        self._callbacks: dict[int, object] = {}
        self._ids = itertools.count(1)
        self._tries = 0
        self._output: deque[str] = deque(maxlen=40)
        self._retry = QTimer(self)
        self._retry.setSingleShot(True)
        self._retry.timeout.connect(self._connect)

    @property
    def running(self) -> bool:
        return self._process is not None

    def _remove_socket(self) -> None:
        if not IS_WINDOWS:  # named pipes vanish with the process
            with contextlib.suppress(OSError):
                os.unlink(self.socket_path)

    def start(self) -> None:
        if self._process is not None or self._quitting:
            return
        if shutil.which("mpv") is None:
            self.died.emit("Playback needs mpv. " + install_hint("mpv"))
            return
        self._remove_socket()
        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.setProgram(tool("mpv"))
        process.setArguments(mpv_launch_args(self.socket_path, self.volume, self.muted, self.loop))
        process.readyReadStandardOutput.connect(lambda p=process: self._output_ready(p))
        process.finished.connect(lambda code, _status, p=process: self._finished(p, code))
        process.errorOccurred.connect(lambda error, p=process: self._error(p, error))
        self._process = process
        self._connected = False
        self._buffer = b""
        self._tries = 0
        self._output.clear()
        log.info("Starting mpv in the background")
        process.start()
        self._retry.start(self.RETRY_MS)

    def command(self, *args, callback=None) -> int:
        """Send a command; callback(reply) runs when mpv answers."""
        request_id = next(self._ids)
        if callback is not None:
            self._callbacks[request_id] = callback
        line = (json.dumps({"command": list(args), "request_id": request_id}) + "\n").encode()
        if self._connected and self._socket is not None:
            self._socket.write(line)
        else:
            self._outbox.append(line)
            self.start()
        return request_id

    def set_property(self, name: str, value) -> int:
        return self.command("set_property", name, value)

    def quit(self) -> None:
        self._quitting = True
        self._retry.stop()
        process, self._process = self._process, None
        if process is None:
            return
        if self._connected and self._socket is not None:
            self._socket.write(b'{"command": ["quit"]}\n')
            self._socket.flush()
        if not process.waitForFinished(1500):
            process.kill()
            process.waitForFinished(500)
        self._remove_socket()

    # Connection
    def _connect(self) -> None:
        if self._process is None or self._connected:
            return
        self._tries += 1
        if self._socket is None:
            socket = QLocalSocket(self)
            socket.connected.connect(self._on_connected)
            socket.readyRead.connect(self._read)
            socket.errorOccurred.connect(self._socket_error)
            socket.disconnected.connect(self._on_disconnected)
            self._socket = socket
        self._socket.abort()
        self._socket.connectToServer(self.socket_path)

    def _socket_error(self, _error=None) -> None:
        if self._connected or self._process is None:
            return
        if self._tries < self.CONNECT_TRIES:  # mpv hasn't created the socket yet
            self._retry.start(self.RETRY_MS)
        else:
            log.warning("mpv's control socket never appeared at %s", self.socket_path)
            self._process.kill()

    def _on_connected(self) -> None:
        if self._process is None or self._socket is None:
            return
        self._connected = True
        log.info("mpv is ready")
        setup = [["request_log_messages", "warn"]]
        setup += [["observe_property", number, name]
                  for number, name in enumerate(self.OBSERVED, start=1)]
        for args in setup:
            self._socket.write((json.dumps({"command": args}) + "\n").encode())
        for line in self._outbox:
            self._socket.write(line)
        self._outbox.clear()
        self.ready.emit()

    def _on_disconnected(self) -> None:
        if self._connected and self._process is not None and not self._quitting:
            log.warning("Lost the connection to mpv; it will restart on the next song")
            self._connected = False
            self._process.kill()

    def _read(self) -> None:
        if self._socket is None:
            return
        self._buffer += bytes(self._socket.readAll())
        *lines, self._buffer = self._buffer.split(b"\n")
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except ValueError:
                log.debug("Unreadable message from mpv: %r", raw[:200])
                continue
            if not isinstance(message, dict):
                continue
            if "event" in message:
                self.received.emit(message)
            elif "request_id" in message:
                callback = self._callbacks.pop(message.get("request_id"), None)
                if message.get("error") not in (None, "success"):
                    log.info("mpv answered: %s", message.get("error"))
                if callback is not None:
                    callback(message)

    # The process
    def _output_ready(self, process: QProcess) -> None:
        if process is not self._process:
            return
        text = bytes(process.readAllStandardOutput()).decode("utf-8", "replace")
        for raw in text.splitlines():
            line = redact(raw.strip())
            if line:
                self._output.append(line)
                log.info("mpv: %s", line)

    def _finished(self, process: QProcess, code: int) -> None:
        if process is not self._process:
            return  # we quit it ourselves
        self._output_ready(process)
        self._process = None
        self._connected = False
        self._retry.stop()
        if self._socket is not None:
            self._socket.abort()
        self._buffer = b""
        self._callbacks.clear()
        self._outbox.clear()
        process.deleteLater()
        self._remove_socket()
        log.warning("mpv exited with code %s", code)
        lines = list(self._output)
        self.died.emit(describe_failure(lines) if lines
                       else f"mpv stopped unexpectedly. {log_hint()}.")

    def _error(self, process: QProcess, error) -> None:
        if error == QProcess.ProcessError.FailedToStart and process is self._process:
            self._process = None
            self._retry.stop()
            self._outbox.clear()
            self._callbacks.clear()
            process.deleteLater()
            self.died.emit("mpv failed to start. Try running mpv in a terminal to see why.")


def reply_entry(reply: dict) -> int | None:
    """The playlist entry id in mpv's answer to loadfile."""
    data = reply.get("data") if isinstance(reply, dict) else None
    entry = data.get("playlist_entry_id") if isinstance(data, dict) else None
    return entry if isinstance(entry, int) and not isinstance(entry, bool) else None


class SongEngine(QObject):
    """Plays songs through one long-running mpv.

    A saved song loads from disk and starts at once; the next saved song
    waits in mpv's playlist, so it follows without a gap. A song that isn't
    saved yet streams after YouTube's wait and gets saved for next time.
    """

    phaseChanged = Signal(str, str)        # song id, "loading" or "playing"
    advanced = Signal(str)                 # mpv moved on to the queued next song
    ended = Signal(str)                    # song id: finished, nothing queued after it
    failed = Signal(str, str)              # song id, what went wrong
    infoResolved = Signal(str, object)     # video id, {title, channel, duration}
    notice = Signal(str)
    positionChanged = Signal(object, object)  # seconds, duration (None when unknown)
    stateChanged = Signal()                # pause, volume, mute or seekability changed

    LOOKAHEAD = 25.0          # look up an unsaved next song this close to the end
    STREAM_SECONDS = 45 * 60  # how long a looked-up stream URL is trusted
    MAX_WAIT = 30.0

    def __init__(self, cache: AudioCache, settings: Settings, mpv: MpvIpc | None = None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.cache = cache
        self.settings = settings
        self.volume = int(max(0, min(100, settings.get("volume", 80))))
        self.muted = bool(settings.get("muted", False))
        self.repeat_one = bool(settings.get("repeat_one", False))
        self.mpv = mpv if mpv is not None else MpvIpc(self)
        self.mpv.volume, self.mpv.muted, self.mpv.loop = self.volume, self.muted, self.repeat_one
        self.mpv.received.connect(self._event)
        self.mpv.died.connect(self._mpv_died)
        cache.saved.connect(self._saved)
        cache.failed.connect(self._save_failed)

        self.song: Song | None = None
        self.source = ""          # what mpv is playing for the current song
        self.source_kind = ""     # "file" (saved) or "stream"
        self.next_song: Song | None = None
        self.next_source = ""
        self._next_kind = ""
        self.paused = False
        self.position: float | None = None
        self.duration: float | None = None
        self.seekable = False
        self.buffering = False
        self.prefer_download = False  # set if YouTube refuses direct streams
        self._stage = "idle"      # idle, resolving, waiting, saving, loading, playing
        self._cur_entry: int | None = None
        self._next_entry: int | None = None
        self._dead: set[int] = set()  # playlist entries we've moved away from
        self._awaiting = ""
        self._retried: set[str] = set()
        self._jobs: dict[str, ResolveJob] = {}
        self._streams: dict[str, dict] = {}
        self._pending_stream: dict | None = None
        self._lines: deque[str] = deque(maxlen=60)
        self._last_report: tuple[float, float] | None = None
        self._wait = QTimer(self)
        self._wait.setSingleShot(True)
        self._wait.timeout.connect(self._load_pending_stream)
        self._next_timer = QTimer(self)
        self._next_timer.setSingleShot(True)
        self._next_timer.timeout.connect(self._sync_next)
        self._watchdog = QTimer(self)
        self._watchdog.setSingleShot(True)
        self._watchdog.timeout.connect(self._timed_out)
        self._persist = QTimer(self)
        self._persist.setSingleShot(True)
        self._persist.setInterval(600)
        self._persist.timeout.connect(self._save_volume)

    @property
    def slow_start(self) -> bool:
        """True while a song that isn't saved yet waits on YouTube."""
        return self._stage in ("resolving", "waiting", "saving")

    # Public
    def warm_up(self) -> None:
        if shutil.which("mpv") is not None:
            self.mpv.start()

    def play(self, song: Song) -> None:
        self._forget_current()
        self.song = song
        self.duration = float(song.duration) if song.duration else None
        for video_id in [v for v in self._jobs if v != song.video_id]:
            self._jobs.pop(video_id).cancel()
        log.info("Play %s (%s)", song.video_id, song.display_title or "untitled")
        self.phaseChanged.emit(song.id, "loading")
        self.positionChanged.emit(None, self.duration)
        path = self.cache.path_for(song.video_id)
        if path is not None:
            self._load(str(path), "file")
            return
        self._silence()
        self.cache.want(song.video_id, Priority.NOW)  # saved for next time
        if self.prefer_download:
            self._await_save(song)
            return
        stream = self._take_stream(song.video_id)
        if stream is not None:
            self._schedule(stream)
        else:
            self._stage = "resolving"
            self._resolve(song.video_id)

    def set_next(self, song: Song | None) -> None:
        """What should follow the current song."""
        if song is not None and self.next_song is not None and song.id == self.next_song.id:
            self._sync_next()
            return
        self.next_song = song
        if song is not None and self.cache.path_for(song.video_id) is None:
            self.cache.want(song.video_id, Priority.UPCOMING)
            if not self.cache.enabled:
                self._look_up(song)
        self._sync_next()

    def stop(self) -> None:
        for video_id in list(self._jobs):
            self._jobs.pop(video_id).cancel()
        had_song = self.song is not None
        self._forget_current()
        if had_song:
            self._silence()
        self.positionChanged.emit(None, None)

    def toggle_pause(self) -> None:
        if self.song is None:
            return
        self.paused = not self.paused
        self.mpv.set_property("pause", self.paused)
        self.stateChanged.emit()

    def seek(self, seconds: float) -> None:
        if self.song is None or self._stage != "playing":
            return
        if self.duration:
            seconds = min(seconds, max(0.0, self.duration - 0.5))
        seconds = max(0.0, float(seconds))
        self.mpv.command("seek", seconds, "absolute")
        self.position = seconds
        self.positionChanged.emit(self.position, self.duration)

    def set_volume(self, value: float) -> None:
        value = int(max(0, min(100, round(value))))
        if value == self.volume:
            return
        self.volume = self.mpv.volume = value
        if self.mpv.running:
            self.mpv.set_property("volume", value)
        self._persist.start()
        self.stateChanged.emit()

    def set_muted(self, muted: bool) -> None:
        if muted == self.muted:
            return
        self.muted = self.mpv.muted = muted
        if self.mpv.running:
            self.mpv.set_property("mute", muted)
        self._persist.start()
        self.stateChanged.emit()

    def set_repeat(self, on: bool) -> None:
        """Play the current song over and over (mpv loops it; nothing else changes)."""
        if on == self.repeat_one:
            return
        self.repeat_one = self.mpv.loop = on
        if self.mpv.running:
            self.mpv.set_property("loop-file", "inf" if on else "no")
        self.settings.set("repeat_one", on)
        self.stateChanged.emit()

    def shutdown(self) -> None:
        self._persist.stop()
        self._save_volume()
        self.mpv.quit()

    # Loading into mpv
    def _load(self, source: str, kind: str, headers: dict | None = None) -> None:
        song = self.song
        self.source, self.source_kind = source, kind
        self._stage = "loading"
        if headers is not None:
            self._apply_headers(headers)
        if self.paused:
            self.paused = False
            self.mpv.set_property("pause", False)
            self.stateChanged.emit()
        self._retire_entries()
        self.next_source = ""
        self.mpv.command("loadfile", source, "replace",
                         callback=lambda reply, s=song, src=source: self._load_reply(reply, s, src))
        self._watchdog.start(10_000 if kind == "file" else 30_000)
        log.info("Loading %s from %s", song.video_id if song else "?",
                 "its saved copy" if kind == "file" else "a stream")

    def _load_reply(self, reply: dict, song: Song | None, source: str) -> None:
        entry = reply_entry(reply)
        if song is not self.song or source != self.source:
            if entry is not None:
                self._dead.add(entry)  # superseded before mpv answered
            return
        if reply.get("error") != "success":
            self._fail(f"mpv couldn't open the song ({reply.get('error')}). {log_hint()}.")
            return
        self._cur_entry = entry

    def _apply_headers(self, headers: dict) -> None:
        lowered = {str(key).lower(): str(value) for key, value in headers.items()}
        if lowered.get("user-agent"):
            self.mpv.set_property("user-agent", lowered["user-agent"])
        fields = [f"{name}: {lowered[name.lower()]}" for name in ("Referer", "Cookie", "X-Forwarded-For")
                  if lowered.get(name.lower())]
        self.mpv.set_property("http-header-fields", fields)

    def _sync_next(self) -> None:
        """Keep mpv's playlist as [current, next] whenever the next can start instantly."""
        if self.song is None or self._stage != "playing" or not self.mpv.running:
            return
        song = self.next_song
        source, kind, headers = "", "", None
        if song is not None:
            path = self.cache.path_for(song.video_id)
            if path is not None:
                source, kind = str(path), "file"
            else:
                stream = self._streams.get(song.video_id)
                if stream is not None and self._stream_fresh(stream):
                    wait = (stream.get("available_at") or 0.0) - time.time()
                    if wait > 0:
                        self._next_timer.start(int(wait * 1000) + 400)
                    else:
                        source, kind, headers = stream["url"], "stream", stream["headers"]
        if source == self.next_source:
            return
        if self._next_entry is not None:
            self._dead.add(self._next_entry)
        self.mpv.command("playlist-clear")  # mpv keeps only the current song, then gets the new next
        self._next_entry = None
        self.next_source, self._next_kind = source, kind
        if source:
            if headers is not None:
                self._apply_headers(headers)  # used when mpv opens the next stream
                self._streams.pop(song.video_id, None)
            self.mpv.command("loadfile", source, "append",
                             callback=lambda reply, src=source: self._next_reply(reply, src))

    def _next_reply(self, reply: dict, source: str) -> None:
        entry = reply_entry(reply)
        if source != self.next_source:
            if entry is not None:
                self._dead.add(entry)
            return
        if reply.get("error") == "success" and entry is not None:
            self._next_entry = entry
        else:  # couldn't queue it; the next song starts the ordinary way instead
            self.next_source = ""
            if reply.get("error") == "success":
                self.mpv.command("playlist-clear")

    def _retire_entries(self) -> None:
        for entry in (self._cur_entry, self._next_entry):
            if entry is not None:
                self._dead.add(entry)
        if len(self._dead) > 500:
            self._dead = set(sorted(self._dead)[-100:])
        self._cur_entry = self._next_entry = None

    def _silence(self) -> None:
        if self.mpv.running:
            self.mpv.command("stop")

    def _forget_current(self) -> None:
        self._wait.stop()
        self._next_timer.stop()
        self._watchdog.stop()
        self._retire_entries()
        self.song = None
        self.source = self.source_kind = ""
        self.next_song = None
        self.next_source = self._next_kind = ""
        self.position = self.duration = None
        self._pending_stream = None
        self._awaiting = ""
        self._stage = "idle"
        self._lines.clear()

    # mpv's side of the story
    def _event(self, message: dict) -> None:
        name = message.get("event")
        if name == "property-change":
            self._property(message.get("name"), message.get("data"))
        elif name == "log-message":
            text = redact(str(message.get("text") or "").strip())
            if text:
                line = f"[{message.get('prefix') or 'mpv'}] {text}"
                self._lines.append(line)
                log.debug("mpv: %s", line)
        elif name == "start-file":
            entry = message.get("playlist_entry_id")
            if entry is not None and entry == self._next_entry:
                self._advance(entry)
        elif name == "playback-restart":
            self._started()
        elif name == "end-file":
            self._end_file(message.get("playlist_entry_id"), message.get("reason"),
                           message.get("file_error"))

    def _started(self) -> None:
        if self.song is None or self._stage != "loading":
            return
        self._stage = "playing"
        self._watchdog.stop()
        self.phaseChanged.emit(self.song.id, "playing")
        self._sync_next()

    def _advance(self, entry: int) -> None:
        song = self.next_song
        if song is None:
            return
        if self._cur_entry is not None:
            self._dead.add(self._cur_entry)
        self.song, self.source, self.source_kind = song, self.next_source, self._next_kind
        self._cur_entry, self._next_entry = entry, None
        self.next_song = None
        self.next_source = self._next_kind = ""
        self.position = 0.0
        self.duration = float(song.duration) if song.duration else None
        self._stage = "playing"
        self._lines.clear()
        self._watchdog.stop()
        log.info("Next song %s started without a gap", song.video_id)
        self.advanced.emit(song.id)
        self.positionChanged.emit(self.position, self.duration)

    def _end_file(self, entry, reason, file_error) -> None:
        if self.song is None or (entry is not None and entry in self._dead):
            return
        if entry is not None and self._cur_entry is not None and entry != self._cur_entry:
            if entry == self._next_entry and reason == "error":
                log.info("The queued next song couldn't be opened; it will start normally")
                self._next_entry, self.next_source = None, ""
            return
        if reason == "eof":
            if self._cur_entry is None or self._next_entry is not None:
                return  # mpv hasn't confirmed this song yet, or it's going straight on
            song = self.song
            log.info("Finished %s", song.video_id)
            self._forget_current()
            self.positionChanged.emit(None, None)
            self.ended.emit(song.id)
        elif reason == "error":
            self._current_failed(str(file_error or ""))
        # "stop", "quit" and "redirect" are our own doing

    def _current_failed(self, file_error: str) -> None:
        song = self.song
        lines = list(self._lines) + ([file_error] if file_error else [])
        log.warning("mpv couldn't play %s (%s): %s", song.video_id, self.source_kind or "?",
                    file_error or "no reason given")
        if self.source_kind == "file" and song.video_id not in self._retried:
            self._retried.add(song.video_id)
            log.warning("The saved copy of %s didn't play; deleting it and streaming instead",
                        song.video_id)
            self.cache.forget([song.video_id])
            self.play(song)
            return
        if self.source_kind == "stream" and not self.prefer_download \
                and any(FORBIDDEN.search(line) for line in lines):
            self.prefer_download = True
            log.warning("YouTube refused the direct stream; saving songs before playing them")
            self.notice.emit("YouTube refused the direct stream, so songs are now saved before "
                             "they play. A song's first play takes a few seconds longer.")
            self._await_save(song)
            return
        self._fail(describe_failure(lines))

    def _property(self, name, value) -> None:
        number = isinstance(value, (int, float)) and not isinstance(value, bool)
        if name == "time-pos":
            if self.song is not None and self._stage == "playing":
                self.position = float(value) if number else None
                now = time.monotonic()
                last = self._last_report
                if self.position is None or last is None or now - last[0] >= 0.25 \
                        or abs(self.position - last[1]) >= 1.0:  # a seek shows at once
                    self._last_report = (now, self.position or 0.0)
                    self.positionChanged.emit(self.position, self.duration)
                    self._look_ahead()
        elif name == "duration":
            if self.song is not None and number and value > 0:
                self.duration = float(value)
                self.positionChanged.emit(self.position, self.duration)
        elif name == "pause":
            if bool(value) != self.paused:
                self.paused = bool(value)
                self.stateChanged.emit()
        elif name == "volume":
            if number and int(round(value)) != self.volume:
                self.volume = self.mpv.volume = int(round(value))
                self._persist.start()
                self.stateChanged.emit()
        elif name == "mute":
            if bool(value) != self.muted:
                self.muted = self.mpv.muted = bool(value)
                self._persist.start()
                self.stateChanged.emit()
        elif name == "seekable":
            self.seekable = bool(value)
            self.stateChanged.emit()
        elif name == "paused-for-cache":
            self.buffering = bool(value)
            self.stateChanged.emit()
        elif name == "loop-file":  # e.g. changed through media controls
            on = value not in (False, None, "no", 0)
            if on != self.repeat_one:
                self.repeat_one = self.mpv.loop = on
                self.settings.set("repeat_one", on)
                self.stateChanged.emit()

    def _look_ahead(self) -> None:
        """Near the end of a song, make sure an unsaved next song can still start quickly."""
        song = self.next_song
        if song is None or self.next_source or self.position is None or not self.duration:
            return
        if self.duration - self.position <= self.LOOKAHEAD \
                and self.cache.path_for(song.video_id) is None:
            self._look_up(song)

    def _mpv_died(self, reason: str) -> None:
        self._cur_entry = self._next_entry = None
        self.next_source = ""
        self.paused = self.seekable = self.buffering = False
        song = self.song
        if song is not None and self._stage in ("loading", "playing"):
            self._forget_current()
            self.positionChanged.emit(None, None)
            self.failed.emit(song.id, reason)
        self.stateChanged.emit()

    # Songs that aren't saved yet
    def _look_up(self, song: Song) -> None:
        stream = self._streams.get(song.video_id)
        if (stream is not None and self._stream_fresh(stream)) or song.video_id in self._jobs:
            return
        log.info("Looking up %s ahead of time", song.video_id)
        self._resolve(song.video_id)

    def _resolve(self, video_id: str) -> None:
        if video_id in self._jobs:
            return
        job = ResolveJob(video_id, self)
        job.done.connect(self._resolved)
        self._jobs[video_id] = job
        job.start()

    def _stream_fresh(self, stream: dict) -> bool:
        return time.monotonic() - stream.get("resolved", 0.0) < self.STREAM_SECONDS

    def _take_stream(self, video_id: str) -> dict | None:
        stream = self._streams.pop(video_id, None)
        return stream if stream is not None and self._stream_fresh(stream) else None

    def _resolved(self, job: ResolveJob) -> None:
        if self._jobs.get(job.video_id) is job:
            del self._jobs[job.video_id]
        job.deleteLater()
        if job.cancelled:
            return
        if job.info:
            self.infoResolved.emit(job.video_id, job.info)
        if job.stream is not None:
            job.stream["resolved"] = time.monotonic()
        song = self.song
        if song is not None and song.video_id == job.video_id and self._stage == "resolving":
            self._lines.extend(job.lines)
            if job.error:
                self._fail(job.error)
            elif job.stream is not None:
                self._schedule(job.stream)
            else:
                log.info("No direct stream for %s; saving it before playing", job.video_id)
                self._await_save(song)
            return
        if job.stream is not None:  # looked up ahead of time for the next song
            self._streams[job.video_id] = job.stream
            while len(self._streams) > 6:
                self._streams.pop(next(iter(self._streams)))
            if self.next_song is not None and self.next_song.video_id == job.video_id:
                self._sync_next()

    def _schedule(self, stream: dict) -> None:
        """Load the stream once YouTube allows it; asking earlier gets a 403."""
        self._pending_stream = stream
        self._stage = "waiting"
        delay = 0.0
        if stream.get("available_at"):
            delay = stream["available_at"] - time.time() + 0.4
        delay = max(0.0, min(delay, self.MAX_WAIT))
        log.info("Stream ready (format %s, %s); YouTube allows it in %.1f s",
                 stream["format"], stream["codec"], delay)
        self._wait.start(int(delay * 1000))

    def _load_pending_stream(self) -> None:
        stream = self._pending_stream
        if self.song is None or stream is None or self._stage != "waiting":
            return
        self._pending_stream = None
        self._load(stream["url"], "stream", stream["headers"])

    def _await_save(self, song: Song) -> None:
        if self.source:
            self._silence()
        self.source = self.source_kind = ""
        self._stage = "saving"
        path = self.cache.path_for(song.video_id)
        if path is not None:
            self._load(str(path), "file")
            return
        self._awaiting = song.video_id
        self.cache.want(song.video_id, Priority.NOW, force=True)
        self._watchdog.start(120_000)

    def _saved(self, video_id: str) -> None:
        song = self.song
        if self._awaiting and video_id == self._awaiting and song is not None \
                and song.video_id == video_id:
            self._awaiting = ""
            path = self.cache.path_for(video_id)
            if path is not None:
                self._load(str(path), "file")
            return
        if self.next_song is not None and self.next_song.video_id == video_id:
            self._sync_next()

    def _save_failed(self, video_id: str, message: str) -> None:
        if self._awaiting and video_id == self._awaiting:
            self._awaiting = ""
            self._fail(message)

    def _timed_out(self) -> None:
        if self.song is None:
            return
        if self._stage == "saving":
            self._fail("Saving the song took too long. Try again in a moment.")
        elif self._stage == "loading":
            self._current_failed("mpv took too long to start the song")

    def _fail(self, message: str) -> None:
        song = self.song
        had_source = bool(self.source)
        self._forget_current()
        if had_source:
            self._silence()
        log.warning("Couldn't play %s: %s", song.video_id if song else "?", message)
        self.positionChanged.emit(None, None)
        if song is not None:
            self.failed.emit(song.id, message)

    def _save_volume(self) -> None:
        self.settings.set("volume", self.volume)
        self.settings.set("muted", self.muted)


@dataclass
class PlayQueue:
    category_id: str
    mode: str          # "single", "order" or "shuffle"
    order: list[str]   # song ids in play order
    pos: int = 0

    @property
    def current(self) -> str:
        return self.order[self.pos] if 0 <= self.pos < len(self.order) else ""


class Player(QObject):
    """What plays next: one song, a category in order, or a category shuffled."""

    changed = Signal()
    notice = Signal(str)                # messages for the user
    position = Signal(object, object)   # seconds, duration
    state = Signal()                    # pause, volume, mute or seekability changed
    MAX_FAILURES = 3
    RESTART_AFTER = 3.0  # "previous" restarts a song that's further in than this

    def __init__(self, library: Library, engine: SongEngine, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.library = library
        self.engine = engine
        self.queue: PlayQueue | None = None
        self.current = ""
        self.current_song: Song | None = None  # kept even if it's removed while playing
        self.phase = "idle"  # idle, loading or playing
        self._failures = 0
        engine.phaseChanged.connect(self._engine_phase)
        engine.advanced.connect(self._engine_advanced)
        engine.ended.connect(self._engine_ended)
        engine.failed.connect(self._engine_failed)
        engine.notice.connect(lambda text: self.notice.emit(text))
        engine.positionChanged.connect(lambda seconds, total: self.position.emit(seconds, total))
        engine.stateChanged.connect(lambda: self.state.emit())
        engine.infoResolved.connect(
            lambda video_id, info: library.apply_metadata(
                video_id, info.get("title") or "", info.get("channel") or "",
                info.get("duration")))
        library.categorySongsChanged.connect(self._category_songs_changed)
        library.categoriesChanged.connect(self._categories_changed)

    # What the interface asks
    @property
    def paused(self) -> bool:
        return self.engine.paused

    @property
    def volume(self) -> int:
        return self.engine.volume

    @property
    def muted(self) -> bool:
        return self.engine.muted

    @property
    def repeat_one(self) -> bool:
        return self.engine.repeat_one

    def toggle_repeat(self) -> None:
        self.engine.set_repeat(not self.engine.repeat_one)

    def toggle_shuffle(self) -> None:
        """The bottom bar's shuffle: flips shuffle for the category that's playing."""
        q = self.queue
        if q is not None and q.mode != "single":
            self.set_shuffle(q.category_id, q.mode != "shuffle")

    def timing(self) -> tuple[float | None, float | None]:
        """Where the current song is, and how long it is."""
        return self.engine.position, self.engine.duration

    def mode_in(self, category_id: str) -> str:
        """'order' or 'shuffle' while this whole category is playing, else ''."""
        q = self.queue
        if q is not None and q.category_id == category_id and q.mode != "single":
            return q.mode
        return ""

    def song_phase(self, song_id: str) -> str:
        return self.phase if song_id and song_id == self.current else "idle"

    def has_next(self) -> bool:
        return self._step(1) is not None

    def has_previous(self) -> bool:
        return self.queue is not None  # at worst it restarts the song

    def progress(self) -> tuple[int, int]:
        q = self.queue
        if q is None:
            return 0, 0
        playable = [sid for sid in q.order if sid == q.current or self._valid(sid)]
        number = playable.index(q.current) + 1 if q.current in playable else 1
        return number, len(playable)

    # Actions
    def play_category(self, category_id: str, shuffle: bool = False) -> None:
        category = self.library.category(category_id)
        if category is None or not category.song_ids or not self._ready():
            return
        order = list(category.song_ids)
        if shuffle:
            random.shuffle(order)
        self.queue = PlayQueue(category_id, "shuffle" if shuffle else "order", order)
        self._failures = 0
        self._start()

    def play_song(self, song_id: str, category_id: str) -> None:
        """A row's play button: stop it, jump to it in the playing category, or play it alone."""
        if song_id == self.current and self.phase != "idle":
            self.stop()
            return
        if not self._ready():
            return
        q = self.queue
        if q is not None and q.mode != "single" and q.category_id == category_id \
                and song_id in q.order:
            index = q.order.index(song_id)
            if q.mode == "order":
                q.pos = index
            else:  # play it next and keep the rest of the shuffle
                q.order.pop(index)
                if index < q.pos:
                    q.pos -= 1
                q.pos += 1
                q.order.insert(q.pos, song_id)
        else:
            self.queue = PlayQueue(category_id, "single", [song_id])
        self._failures = 0
        self._start()

    def set_shuffle(self, category_id: str, on: bool) -> None:
        """Switch modes mid-play without interrupting the current song."""
        q = self.queue
        category = self.library.category(category_id)
        if q is None or q.mode == "single" or q.category_id != category_id or category is None:
            return
        if (q.mode == "shuffle") == on:
            return
        current = q.current
        if on:
            rest = [sid for sid in category.song_ids if sid != current]
            random.shuffle(rest)
            q.order, q.pos, q.mode = [current] + rest, 0, "shuffle"
        else:
            ids = list(category.song_ids)
            q.mode = "order"
            if current in ids:
                q.order, q.pos = ids, ids.index(current)
            else:
                q.order, q.pos = [current] + ids, 0
        self.changed.emit()
        self._update_next()

    def next(self) -> None:
        index = self._step(1)
        if index is not None:
            self.queue.pos = index
            self._failures = 0
            self._start()

    def previous(self) -> None:
        if self.queue is None:
            return
        index = self._step(-1)
        position = self.engine.position or 0.0
        if index is None or position > self.RESTART_AFTER:
            self.seek(0.0)
            return
        self.queue.pos = index
        self._failures = 0
        self._start()

    def toggle_pause(self) -> None:
        if self.queue is not None:
            self.engine.toggle_pause()

    def seek(self, seconds: float) -> None:
        if self.queue is not None:
            self.engine.seek(max(0.0, seconds))

    def seek_by(self, delta: float) -> None:
        if self.queue is not None and self.engine.position is not None:
            self.seek(self.engine.position + delta)

    def set_volume(self, value: float) -> None:
        self.engine.set_volume(value)

    def set_muted(self, muted: bool) -> None:
        self.engine.set_muted(muted)

    def stop(self) -> None:
        self.engine.stop()
        active = self.queue is not None
        self.queue, self.current, self.current_song, self.phase = None, "", None, "idle"
        if active:
            self.changed.emit()

    def shutdown(self) -> None:
        self.stop()
        self.engine.shutdown()

    # Internals
    def _ready(self) -> bool:
        problem = missing_tools()
        if problem:
            self.notice.emit(problem)
        return not problem

    def _valid(self, song_id: str) -> bool:
        if self.library.song(song_id) is None:
            return False
        q = self.queue
        if q is None or q.mode == "single":
            return True
        category = self.library.category(q.category_id)
        return category is not None and song_id in category.song_ids

    def _step(self, direction: int) -> int | None:
        q = self.queue
        if q is None or q.mode == "single":
            return None
        index = q.pos + direction
        while 0 <= index < len(q.order):
            if self._valid(q.order[index]):
                return index
            index += direction
        return None

    def _upcoming(self, count: int) -> list[str]:
        q = self.queue
        if q is None or q.mode == "single":
            return []
        found, index = [], q.pos + 1
        while index < len(q.order) and len(found) < count:
            if self._valid(q.order[index]):
                found.append(q.order[index])
            index += 1
        return found

    def _update_next(self) -> None:
        """Tell the engine what follows, and get the next few songs saved."""
        songs = [s for s in (self.library.song(sid) for sid in self._upcoming(3)) if s is not None]
        self.engine.set_next(songs[0] if songs else None)
        for song in songs[1:]:
            self.engine.cache.want(song.video_id, Priority.UPCOMING)

    def _start(self) -> None:
        q = self.queue
        song = self.library.song(q.current) if q is not None else None
        if song is None:  # it was removed; move along
            index = self._step(1)
            if index is None:
                self.stop()
            else:
                q.pos = index
                self._start()
            return
        self.current, self.current_song, self.phase = song.id, song, "loading"
        self.changed.emit()
        self.engine.play(song)
        if self.queue is q and self.current == song.id:
            self._update_next()

    def _engine_phase(self, song_id: str, phase: str) -> None:
        if song_id != self.current or self.queue is None or phase == self.phase:
            return
        self.phase = phase
        if phase == "playing":
            self._failures = 0
        self.changed.emit()
        if phase == "playing":
            self._update_next()

    def _engine_advanced(self, song_id: str) -> None:
        q = self.queue
        if q is None:
            return
        index = self._step(1)
        if index is not None and q.order[index] == song_id:
            q.pos = index
        elif song_id in q.order:
            q.pos = q.order.index(song_id)
        else:
            return
        self.current = song_id
        self.current_song = self.library.song(song_id) or self.engine.song
        self.phase = "playing"
        self._failures = 0
        self.changed.emit()
        self._update_next()

    def _engine_ended(self, song_id: str) -> None:
        if song_id != self.current:
            return
        index = self._step(1)
        if index is None:
            self.stop()  # that was the last song
        else:
            self.queue.pos = index
            self._start()

    def _engine_failed(self, song_id: str, message: str) -> None:
        if song_id != self.current:
            return
        song = self.current_song
        title = song.display_title if song is not None and song.display_title else "this song"
        index = self._step(1)
        if index is None:
            self.stop()
            self.notice.emit(f"Couldn't play “{title}”. {message}")
            return
        self._failures += 1
        if self._failures >= self.MAX_FAILURES:
            self.stop()
            self.notice.emit(f"Stopped after {self.MAX_FAILURES} songs in a row couldn't play. "
                             f"{message}")
            return
        self.notice.emit(f"Skipped “{title}”. {message}")
        self.queue.pos = index
        self._start()

    def _category_songs_changed(self, category_id: str) -> None:
        """Keep the queue in step when songs are moved, added or removed mid-play."""
        q = self.queue
        if q is None or q.mode == "single" or q.category_id != category_id or not q.current:
            return
        category = self.library.category(category_id)
        if category is None:
            return
        ids, current = list(category.song_ids), q.current
        if q.mode == "order":
            if current in ids:
                q.order, q.pos = ids, ids.index(current)
            else:  # it left the category; the song that took its place plays next
                spot = min(q.pos, len(ids))
                q.order, q.pos = ids[:spot] + [current] + ids[spot:], spot
        else:
            present, known = set(ids), set(q.order)
            kept = [sid for sid in q.order if sid in present or sid == current]
            pos = kept.index(current)
            for sid in ids:  # songs added mid-shuffle join the songs still to come
                if sid not in known:
                    kept.insert(random.randint(pos + 1, len(kept)), sid)
            q.order, q.pos = kept, pos
        self.changed.emit()
        self._update_next()

    def _categories_changed(self) -> None:
        q = self.queue
        if q is not None and self.library.category(q.category_id) is None:
            if q.mode != "single":  # the category was deleted: finish this song, then stop
                q.order, q.pos, q.mode = [q.current], 0, "single"
            self.changed.emit()
            self._update_next()


# ── Painted pieces ───────────────────────────────────────────────────────────
def painted_icon(kind: str, color: str | None = None, size: int = 18) -> QIcon:
    """Small icons for the + menu, drawn in code so no icon theme is needed."""
    ratio = 3
    pixmap = QPixmap(size * ratio, size * ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(ratio * size / 18, ratio * size / 18)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color or ACCENT))
    if kind == "song":  # an eighth note
        painter.drawEllipse(QRectF(2.6, 11.0, 7.4, 5.6))
        painter.drawRect(QRectF(8.4, 2.4, 1.7, 11.6))
        flag = QPainterPath(QPointF(10.1, 2.4))
        flag.cubicTo(QPointF(11.0, 5.0), QPointF(15.6, 5.0), QPointF(14.6, 9.9))
        flag.cubicTo(QPointF(13.8, 7.6), QPointF(12.0, 6.9), QPointF(10.1, 6.8))
        flag.closeSubpath()
        painter.drawPath(flag)
    elif kind == "playlist":  # a list with a play triangle
        for y in (3.0, 7.6, 12.2):
            painter.drawRoundedRect(QRectF(2.0, y, 8.4, 2.6), 1.3, 1.3)
        painter.drawPath(triangle(QPointF(11.8, 6.0), QPointF(11.8, 15.4), QPointF(17.2, 10.7)))
    else:  # a 2×2 grid, like the main page
        for x in (2.0, 10.0):
            for y in (2.0, 10.0):
                painter.drawRoundedRect(QRectF(x, y, 6.0, 6.0), 1.8, 1.8)
    painter.end()
    pixmap.setDevicePixelRatio(ratio)
    return QIcon(pixmap)


def app_icon() -> QIcon:
    """Accent tile with a list and a play triangle, matching tapedeck.svg."""
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(size / 64, size / 64)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(ACCENT))
        painter.drawRoundedRect(QRectF(4, 4, 56, 56), 15, 15)
        painter.setBrush(QColor(ON_ACCENT))
        for y in (21.75, 30.25, 38.75):
            painter.drawRoundedRect(QRectF(14, y, 18, 3.5), 1.75, 1.75)
        triangle = QPainterPath(QPointF(38, 21))
        triangle.lineTo(38, 43)
        triangle.lineTo(52, 32)
        triangle.closeSubpath()
        painter.drawPath(triangle)
        painter.end()
        icon.addPixmap(pixmap)
    return icon


class FabButton(QAbstractButton):
    """The rounded square with a plus, bottom right. Turns into × while open."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setFixedSize(z(58), z(58))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip("Add a song or category")
        self._angle = 0.0
        self._spin = QVariantAnimation(self)
        self._spin.setDuration(160)
        self._spin.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._spin.valueChanged.connect(self._set_angle)
        glow = QGraphicsDropShadowEffect(self)
        glow.setBlurRadius(z(30))
        glow.setOffset(0, z(6))
        glow_color = QColor(ACCENT)
        glow_color.setAlpha(95)
        glow.setColor(glow_color)
        self.setGraphicsEffect(glow)

    def _set_angle(self, value) -> None:
        self._angle = float(value)
        self.update()

    def set_open(self, is_open: bool) -> None:
        self._spin.stop()
        self._spin.setStartValue(self._angle)
        self._spin.setEndValue(45.0 if is_open else 0.0)
        self._spin.start()

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        painter, w, h = zoomed_painter(self)
        fill = ACCENT_LO if self.isDown() else ACCENT_HI if self.underMouse() else ACCENT
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(fill))
        painter.drawRoundedRect(QRectF(0, 0, w, h), 17, 17)
        painter.translate(w / 2, h / 2)
        painter.rotate(self._angle)
        painter.setPen(QPen(QColor(ON_ACCENT), 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        arm = 10.0
        painter.drawLine(QPointF(-arm, 0), QPointF(arm, 0))
        painter.drawLine(QPointF(0, -arm), QPointF(0, arm))


class DragHandle(QWidget):
    """Three horizontal lines. Hold and drag to move a song up or down."""

    pressed = Signal()
    moved = Signal(QPoint)
    released = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(z(22), z(32))
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip("Drag to reorder")
        self._held = False

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        painter, w, h = zoomed_painter(self)
        color = ACCENT if self._held else WHITE if self.underMouse() else ASH
        painter.setPen(QPen(QColor(color), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        cx, cy = w / 2, h / 2
        for dy in (-4.5, 0.0, 4.5):
            painter.drawLine(QPointF(cx - 6, cy + dy), QPointF(cx + 6, cy + dy))

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._held = True
            self.update()
            self.pressed.emit()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._held:
            self.moved.emit(event.globalPosition().toPoint())

    def mouseReleaseEvent(self, event) -> None:
        if self._held and event.button() == Qt.MouseButton.LeftButton:
            self._held = False
            self.update()
            self.released.emit()


class PlayButton(QAbstractButton):
    """A rightward triangle. Spins while the song starts, shows stop while it plays."""

    TIPS = {"idle": "Play", "loading": "Cancel", "playing": "Stop"}

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(z(32), z(32))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._phase = "idle"
        self._angle = 0.0
        self._spin = QVariantAnimation(self)
        self._spin.setStartValue(0.0)
        self._spin.setEndValue(360.0)
        self._spin.setDuration(1000)
        self._spin.setLoopCount(-1)
        self._spin.valueChanged.connect(self._turn)
        self._spinning = False
        self._spin_delay = QTimer(self)  # saved songs start before a spinner would show
        self._spin_delay.setSingleShot(True)
        self._spin_delay.timeout.connect(self._begin_spin)
        self.setToolTip(self.TIPS["idle"])

    def _turn(self, value) -> None:
        self._angle = float(value)
        self.update()

    def _begin_spin(self) -> None:
        if self._phase == "loading":
            self._spinning = True
            self._spin.start()
            self.update()

    def set_phase(self, phase: str) -> None:
        if phase == self._phase:
            return
        self._phase = phase
        self._spinning = False
        self._spin.stop()
        if phase == "loading":
            self._spin_delay.start(220)
        else:
            self._spin_delay.stop()
        self.setToolTip(self.TIPS.get(phase, "Play"))
        self.update()

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        painter, w, h = zoomed_painter(self)
        ring = QRectF(0, 0, w, h).adjusted(2, 2, -2, -2)
        hover = self.underMouse()
        center = ring.center()
        if self._phase == "playing" or (self._phase == "loading" and not self._spinning):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(ACCENT_HI if hover else ACCENT))
            painter.drawEllipse(ring)
            draw_glyph(painter, "stop", center, QColor(ON_ACCENT), scale=0.9)
        elif self._phase == "loading":
            track = ring.adjusted(1, 1, -1, -1)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(HAIRLINE_HI), 1.6))
            painter.drawEllipse(track)
            painter.setPen(QPen(QColor(ACCENT), 1.9, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap))
            painter.drawArc(track, int(-self._angle * 16), -100 * 16)
            draw_glyph(painter, "stop", center, QColor(WHITE if hover else SMOKE), scale=0.7)
        else:
            if hover:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(ACCENT))
                painter.drawEllipse(ring)
            else:
                painter.setPen(QPen(QColor(HAIRLINE_HI), 1))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(ring.adjusted(0.5, 0.5, -0.5, -0.5))
            draw_glyph(painter, "play", center, QColor(ON_ACCENT if hover else WHITE), scale=0.92)


def triangle(a: QPointF, b: QPointF, c: QPointF) -> QPainterPath:
    path = QPainterPath(a)
    path.lineTo(b)
    path.lineTo(c)
    path.closeSubpath()
    return path


def draw_glyph(painter: QPainter, kind: str, center: QPointF, color: QColor,
               scale: float = 1.0) -> None:
    """play, stop, next, prev or shuffle, drawn in a 16 px box around center."""
    painter.save()
    painter.translate(center)
    painter.scale(scale, scale)
    if kind == "shuffle":
        painter.setPen(QPen(color, 1.7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                            Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for sign in (1.0, -1.0):
            y = 4.5 * sign
            path = QPainterPath(QPointF(-7.0, -y))
            path.lineTo(-3.5, -y)
            path.cubicTo(QPointF(0.0, -y), QPointF(0.0, y), QPointF(3.5, y))
            path.lineTo(6.5, y)
            painter.drawPath(path)
            head = QPainterPath(QPointF(4.2, y - 2.3))
            head.lineTo(6.6, y)
            head.lineTo(4.2, y + 2.3)
            painter.drawPath(head)
    else:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        if kind == "stop":
            painter.drawRoundedRect(QRectF(-4.5, -4.5, 9.0, 9.0), 1.6, 1.6)
        elif kind == "play":
            painter.drawPath(triangle(QPointF(-3.4, -5.6), QPointF(-3.4, 5.6), QPointF(5.6, 0.0)))
        elif kind == "next":
            painter.drawPath(triangle(QPointF(-5.2, -5.4), QPointF(-5.2, 5.4), QPointF(3.2, 0.0)))
            painter.drawRoundedRect(QRectF(3.6, -5.4, 2.0, 10.8), 0.8, 0.8)
        elif kind == "prev":
            painter.drawPath(triangle(QPointF(5.2, -5.4), QPointF(5.2, 5.4), QPointF(-3.2, 0.0)))
            painter.drawRoundedRect(QRectF(-5.6, -5.4, 2.0, 10.8), 0.8, 0.8)
        elif kind == "repeat":
            painter.setPen(QPen(color, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                                Qt.PenJoinStyle.RoundJoin))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            top = QPainterPath(QPointF(-6.5, 1.0))
            top.lineTo(-6.5, -1.5)
            top.quadTo(QPointF(-6.5, -4.2), QPointF(-3.8, -4.2))
            top.lineTo(5.6, -4.2)
            painter.drawPath(top)
            painter.drawPath(triangle(QPointF(3.6, -6.2), QPointF(5.8, -4.2), QPointF(3.6, -2.2)))
            bottom = QPainterPath(QPointF(6.5, -1.0))
            bottom.lineTo(6.5, 1.5)
            bottom.quadTo(QPointF(6.5, 4.2), QPointF(3.8, 4.2))
            bottom.lineTo(-5.6, 4.2)
            painter.drawPath(bottom)
            painter.drawPath(triangle(QPointF(-3.6, 2.2), QPointF(-5.8, 4.2), QPointF(-3.6, 6.2)))
            painter.setPen(QPen(color, 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawLine(QPointF(0.5, -1.9), QPointF(0.5, 1.9))   # the "1"
            painter.drawLine(QPointF(-0.7, -0.9), QPointF(0.5, -1.9))
        elif kind == "share":
            painter.setPen(QPen(color, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                                Qt.PenJoinStyle.RoundJoin))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            tray = QPainterPath(QPointF(-5.6, 0.6))
            tray.lineTo(-5.6, 5.4)
            tray.lineTo(5.6, 5.4)
            tray.lineTo(5.6, 0.6)
            painter.drawPath(tray)
            painter.drawLine(QPointF(0.0, 2.2), QPointF(0.0, -6.0))
            head = QPainterPath(QPointF(-3.1, -3.0))
            head.lineTo(0.0, -6.1)
            head.lineTo(3.1, -3.0)
            painter.drawPath(head)
        elif kind == "phone":
            painter.setPen(QPen(color, 1.6))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(-4.6, -7.2, 9.2, 14.4), 2.2, 2.2)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(QPointF(0.0, 4.6), 1.1, 1.1)
        elif kind == "settings":
            ring = QPainterPath()
            ring.setFillRule(Qt.FillRule.OddEvenFill)
            ring.addEllipse(QPointF(0.0, 0.0), 5.0, 5.0)
            ring.addEllipse(QPointF(0.0, 0.0), 2.2, 2.2)
            painter.drawPath(ring)
            for tooth in range(8):
                painter.save()
                painter.rotate(45 * tooth)
                painter.drawRoundedRect(QRectF(-1.5, -7.3, 3.0, 3.2), 0.7, 0.7)
                painter.restore()
        elif kind == "pause":
            painter.drawRoundedRect(QRectF(-4.6, -5.6, 3.4, 11.2), 1.0, 1.0)
            painter.drawRoundedRect(QRectF(1.2, -5.6, 3.4, 11.2), 1.0, 1.0)
        elif kind.startswith("volume"):
            speaker = QPainterPath(QPointF(-7.5, -2.6))
            for x, y in ((-4.6, -2.6), (-0.8, -6.2), (-0.8, 6.2), (-4.6, 2.6), (-7.5, 2.6)):
                speaker.lineTo(x, y)
            speaker.closeSubpath()
            painter.drawPath(speaker)
            painter.setPen(QPen(color, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            if kind == "volume-mute":
                painter.drawLine(QPointF(2.2, -3.0), QPointF(7.4, 3.0))
                painter.drawLine(QPointF(2.2, 3.0), QPointF(7.4, -3.0))
            else:
                painter.drawArc(QRectF(-4.8, -4.0, 8.0, 8.0), -50 * 16, 100 * 16)
                if kind == "volume-high":
                    painter.drawArc(QRectF(-8.0, -7.2, 14.4, 14.4), -50 * 16, 100 * 16)
    painter.restore()


class GlyphButton(QAbstractButton):
    """Painted icon button.

    outline: hairline square; when active, an accent square (like the + button)
    toggle:  hairline square; when active, accent outline and accent glyph
    bare:    no outline (transport controls)
    primary: always accent (play / pause)
    """

    def __init__(self, kind: str, *, size: int = 34, radius: float = 10,
                 variant: str = "outline", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.kind = kind
        self.variant = variant
        self._radius = radius
        self._active = False
        self.setFixedSize(z(size), z(size))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)

    def set_kind(self, kind: str) -> None:
        if kind != self.kind:
            self.kind = kind
            self.update()

    def set_active(self, active: bool) -> None:
        if active != self._active:
            self._active = active
            self.update()

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.EnabledChange:
            self.setCursor(Qt.CursorShape.PointingHandCursor if self.isEnabled()
                           else Qt.CursorShape.ArrowCursor)
            self.update()

    def paintEvent(self, _event) -> None:
        painter, w, h = zoomed_painter(self)
        frame = QRectF(0, 0, w, h).adjusted(0.5, 0.5, -0.5, -0.5)
        enabled = self.isEnabled()
        hover = enabled and self.underMouse()
        fill: QColor | None = None
        edge: QColor | None = None
        glyph = QColor(WHITE)
        if self.variant == "primary":
            if enabled:
                fill = QColor(ACCENT_LO if self.isDown() else ACCENT_HI if hover else ACCENT)
                glyph = QColor(ON_ACCENT)
            else:
                fill, glyph = QColor(ACCENT_DIM_BG), QColor(ACCENT_DIM_FG)
        elif not enabled:
            glyph = QColor(HAIRLINE_HI)
            edge = QColor(HAIRLINE) if self.variant != "bare" else None
        elif self._active and self.variant == "outline":
            fill, glyph = QColor(ACCENT_HI if hover else ACCENT), QColor(ON_ACCENT)
        elif self._active and self.variant == "toggle":
            fill, edge, glyph = QColor(ACCENT_WASH), QColor(ACCENT), QColor(ACCENT)
        elif self._active and self.variant == "bare":
            glyph = QColor(ACCENT_HI if hover else ACCENT)
            fill = QColor(HOVER) if hover else None
        else:
            if hover:
                fill = QColor(HOVER)
            if self.variant != "bare":
                edge = QColor("#5a5a5a" if hover else HAIRLINE_HI)
        if self.hasFocus():
            edge = QColor(WHITE)
        painter.setPen(QPen(edge, 1) if edge is not None else Qt.PenStyle.NoPen)
        painter.setBrush(fill if fill is not None else Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(frame, self._radius, self._radius)
        draw_glyph(painter, self.kind, QRectF(0, 0, w, h).center(), glyph)
        if self._active and self.variant == "bare" and enabled:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(glyph)
            painter.drawEllipse(QPointF(w / 2, h - 4.0), 1.6, 1.6)


class SeekBar(QWidget):
    """The locator: click or drag anywhere on it to jump within the song."""

    seekRequested = Signal(float)
    previewChanged = Signal(object)  # seconds while dragging, None when let go
    PAD = 8.0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(z(20))
        self.setMinimumWidth(z(120))
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._position = 0.0
        self._duration = 0.0
        self._drag: float | None = None
        self._hover = False

    @property
    def dragging(self) -> bool:
        return self._drag is not None

    def set_state(self, position, duration) -> None:
        number = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
        self._position = float(position) if number(position) else 0.0
        self._duration = float(duration) if number(duration) and duration > 0 else 0.0
        if self._drag is None:
            self.update()

    def _fraction_at(self, x: float) -> float:
        span = max(1.0, self.width() / ZOOM - 2 * self.PAD)
        return max(0.0, min(1.0, (x / ZOOM - self.PAD) / span))

    def _fraction(self) -> float:
        if self._drag is not None:
            return self._drag
        return max(0.0, min(1.0, self._position / self._duration)) if self._duration else 0.0

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or not self.isEnabled() \
                or not self._duration:
            super().mousePressEvent(event)
            return
        self._drag = self._fraction_at(event.position().x())
        self.previewChanged.emit(self._drag * self._duration)
        self.update()

    def mouseMoveEvent(self, event) -> None:
        if self._drag is not None:
            self._drag = self._fraction_at(event.position().x())
            self.previewChanged.emit(self._drag * self._duration)
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if self._drag is None or event.button() != Qt.MouseButton.LeftButton:
            return
        target = self._drag * self._duration
        self._drag = None
        self._position = target
        self.previewChanged.emit(None)
        self.seekRequested.emit(target)
        self.update()

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        painter, w, h = zoomed_painter(self)
        active = self.isEnabled() and bool(self._duration) and (self._hover or self._drag is not None)
        thick = 6.0 if active else 4.0
        mid = h / 2
        track = QRectF(self.PAD, mid - thick / 2, max(1.0, w - 2 * self.PAD), thick)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(HAIRLINE_HI))
        painter.drawRoundedRect(track, thick / 2, thick / 2)
        if not self.isEnabled() or not self._duration:
            return
        fraction = self._fraction()
        painter.setBrush(QColor(ACCENT))
        painter.drawRoundedRect(QRectF(track.left(), track.top(), track.width() * fraction, thick),
                                thick / 2, thick / 2)
        if active:
            painter.setBrush(QColor(WHITE))
            painter.drawEllipse(QPointF(track.left() + track.width() * fraction, mid), 6.5, 6.5)


class VolumeSlider(QWidget):
    """Drag, click or scroll to set the volume."""

    valueChanged = Signal(int)
    PAD = 7.0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(z(104), z(20))
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip("Volume")
        self._value = 80
        self._muted = False
        self._drag = False
        self._hover = False

    @property
    def value(self) -> int:
        return self._value

    def set_value(self, value: int, muted: bool) -> None:
        self._value, self._muted = int(value), bool(muted)
        self.update()

    def _value_at(self, x: float) -> int:
        span = max(1.0, self.width() / ZOOM - 2 * self.PAD)
        return int(round(max(0.0, min(1.0, (x / ZOOM - self.PAD) / span)) * 100))

    def _change(self, value: int) -> None:
        value = max(0, min(100, value))
        if value != self._value or self._muted:
            self._value = value
            self.valueChanged.emit(value)
            self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = True
            self._change(self._value_at(event.position().x()))
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag:
            self._change(self._value_at(event.position().x()))
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        self._drag = False
        self.update()

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            event.ignore()  # Ctrl+scroll zooms the window instead
            return
        steps = event.angleDelta().y() / 120
        if steps:
            self._change(self._value + int(round(steps * 5)))
        event.accept()

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        painter, w, h = zoomed_painter(self)
        active = self._hover or self._drag
        thick = 6.0 if active else 4.0
        mid = h / 2
        track = QRectF(self.PAD, mid - thick / 2, max(1.0, w - 2 * self.PAD), thick)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(HAIRLINE_HI))
        painter.drawRoundedRect(track, thick / 2, thick / 2)
        fraction = self._value / 100
        painter.setBrush(QColor(ASH if self._muted else WHITE))
        painter.drawRoundedRect(QRectF(track.left(), track.top(), track.width() * fraction, thick),
                                thick / 2, thick / 2)
        if active:
            painter.setBrush(QColor(WHITE))
            painter.drawEllipse(QPointF(track.left() + track.width() * fraction, mid), 6.0, 6.0)


class Thumb(QWidget):
    """A rounded, center-cropped video thumbnail with a quiet placeholder."""

    def __init__(self, width: int, height: int, radius: float,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(z(width), z(height))
        self._radius = zf(radius)
        self._scaled: QPixmap | None = None

    def set_pixmap(self, pixmap: QPixmap | None) -> None:
        if pixmap is None or pixmap.isNull():
            self._scaled = None
        else:
            ratio = max(1.0, self.devicePixelRatioF())
            target = QSize(round(self.width() * ratio), round(self.height() * ratio))
            scaled = pixmap.scaled(target, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                   Qt.TransformationMode.SmoothTransformation)
            x = (scaled.width() - target.width()) // 2
            y = (scaled.height() - target.height()) // 2
            self._scaled = scaled.copy(x, y, target.width(), target.height())
            self._scaled.setDevicePixelRatio(ratio)
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing
                               | QPainter.RenderHint.SmoothPixmapTransform)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(self.rect()), self._radius, self._radius)
        painter.setClipPath(clip)
        if self._scaled is not None:
            painter.drawPixmap(0, 0, self._scaled)
            return
        painter.fillRect(self.rect(), QColor(HOVER))
        center = QRectF(self.rect()).center()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(HAIRLINE_HI))
        painter.drawPath(triangle(QPointF(center.x() - zf(3), center.y() - zf(4.5)),
                                  QPointF(center.x() - zf(3), center.y() + zf(4.5)),
                                  QPointF(center.x() + zf(4.5), center.y())))


class ClickLabel(QLabel):
    """Plain-text label that reports double-clicks."""

    doubleClicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setTextFormat(Qt.TextFormat.PlainText)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.doubleClicked.emit()
        else:
            super().mouseDoubleClickEvent(event)


class ElidedLabel(ClickLabel):
    """Single line that ends in … when it doesn't fit; full text in the tooltip."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full = ""
        self.hint = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(z(16))

    @property
    def full_text(self) -> str:
        return self._full

    def set_full_text(self, text: str) -> None:
        self._full = text
        self.setToolTip(tip(text, self.hint))
        self._elide()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._elide()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange:
            self._elide()

    def _elide(self) -> None:
        width = max(z(16), self.contentsRect().width())
        elided = QFontMetrics(self.font()).elidedText(
            getattr(self, "_full", ""), Qt.TextElideMode.ElideRight, width)
        if elided != self.text():
            self.setText(elided)


class EditableText(QWidget):
    """A label that becomes a text field when double-clicked. Enter saves, Esc cancels."""

    committed = Signal(str)

    def __init__(self, name: str, font: QFont, *, max_length: int = 80,
                 allow_empty: bool = False, hint: str = "Double-click to rename",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.allow_empty = allow_empty
        self._font = font
        self._max_length = max_length
        self.label = ElidedLabel()
        self.label.setObjectName(name)
        self.label.setFont(font)
        self.label.hint = hint
        self.label.doubleClicked.connect(self.begin)
        self.editor: InlineEditor | None = None  # created on first use
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._layout.addWidget(self.label)

    @property
    def text(self) -> str:
        return self.label.full_text

    def set_text(self, text: str) -> None:
        self.label.set_full_text(text)

    @property
    def editing(self) -> bool:
        return self.editor is not None and not self.editor.isHidden()

    def begin(self) -> None:
        if self.editor is None:
            self.editor = InlineEditor(self)
            self.editor.setFont(self._font)
            self.editor.setMaxLength(self._max_length)
            self.editor.committed.connect(self._committed)
            self.editor.cancelled.connect(self._close)
            self._layout.addWidget(self.editor)
        self.label.hide()
        self.editor.begin(self.label.full_text)

    def _close(self) -> None:
        if self.editor is not None:
            self.editor.hide()
        self.label.show()

    def _committed(self, text: str) -> None:
        self._close()
        text = " ".join(text.split())
        if (text or self.allow_empty) and text != self.label.full_text:
            self.committed.emit(text)


class InlineEditor(QLineEdit):
    """Edits a value in place. Enter or clicking away saves; Esc cancels."""

    committed = Signal(str)
    cancelled = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("InlineEditor")
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self._open = False

    def begin(self, text: str) -> None:
        self._open = True
        self.setText(text)
        self.show()
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self.selectAll()

    def _finish(self, accept: bool) -> None:
        if not self._open:
            return
        self._open = False
        if accept:
            self.committed.emit(self.text())
        else:
            self.cancelled.emit()

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._finish(True)
            return
        if event.key() == Qt.Key.Key_Escape:
            self._finish(False)
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        if event.reason() != Qt.FocusReason.PopupFocusReason:
            self._finish(True)


class CheckRow(QAbstractButton):
    """A category you can tick: square checkbox, title and optional subtitle."""

    def __init__(self, category_id: str, title: str, subtitle: str,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.category_id = category_id
        self._title = title
        self._subtitle = subtitle
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setFixedHeight(z(58 if subtitle else 46))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._title_font = make_font(14, QFont.Weight.Medium)
        self._subtitle_font = make_font(12)
        self.toggled.connect(lambda _on: self.update())

    def sizeHint(self) -> QSize:
        return QSize(z(320), self.height())

    def set_text(self, title: str, subtitle: str = "") -> None:
        self._title, self._subtitle = title, subtitle
        self.update()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.EnabledChange:
            self.update()

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        painter, w, h = zoomed_painter(self)
        if not self.isEnabled():
            painter.setOpacity(0.35)
        frame = QRectF(0, 0, w, h).adjusted(0.5, 0.5, -0.5, -0.5)
        checked, hover = self.isChecked(), self.underMouse()
        if checked:
            fill, edge = QColor(ACCENT_WASH), QColor(ACCENT)
        elif hover:
            fill, edge = QColor(HOVER), QColor(HAIRLINE_HI)
        else:
            fill, edge = QColor(BLACK), QColor(HAIRLINE)
        if self.hasFocus():
            edge = QColor(WHITE)
        painter.setPen(QPen(edge, 1))
        painter.setBrush(fill)
        painter.drawRoundedRect(frame, 10, 10)

        box = QRectF(14, (h - 18) / 2, 18, 18)
        if checked:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(ACCENT))
            painter.drawRoundedRect(box, 5, 5)
            painter.setPen(QPen(QColor(ON_ACCENT), 2.2, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            tick = QPainterPath(QPointF(box.left() + 4.6, box.center().y() + 0.3))
            tick.lineTo(box.left() + 7.8, box.bottom() - 4.8)
            tick.lineTo(box.right() - 4.2, box.top() + 5.0)
            painter.drawPath(tick)
        else:
            painter.setPen(QPen(QColor(ASH), 1.4))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(box.adjusted(0.7, 0.7, -0.7, -0.7), 5, 5)

        x = box.right() + 14
        width = max(10.0, w - x - 14)
        if self._subtitle:
            draw_text(painter, QRectF(x, 9, width, 21), self._title, self._title_font, WHITE)
            draw_text(painter, QRectF(x, 30, width, 19), self._subtitle, self._subtitle_font, SMOKE)
        else:
            draw_text(painter, QRectF(x, 0, width, h), self._title, self._title_font, WHITE)


class Toast(QLabel):
    """A short message that fades in above the bottom edge, then fades away."""

    def __init__(self, host: QWidget) -> None:
        super().__init__(host)
        self.setObjectName("Toast")
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setFont(make_font(13, QFont.Weight.Medium))
        self.setWordWrap(True)
        self.setMaximumWidth(z(560))
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._opacity = QGraphicsOpacityEffect(self)
        self._opacity.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity)
        self._fade = QPropertyAnimation(self._opacity, b"opacity", self)
        self._fade.setDuration(180)
        self._fade.finished.connect(self._fade_finished)
        self._hiding = False
        self.lift = 34  # distance above the bottom of its area
        self.area: QRect | None = None  # where to center it (defaults to the whole window)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fade_out)
        self.hide()

    def show_message(self, text: str, ms: int = 2600) -> None:
        self.setText(text)
        self.adjustSize()
        self.place()
        self.show()
        self.raise_()
        self._hiding = False
        self._fade.stop()
        self._fade.setStartValue(self._opacity.opacity())
        self._fade.setEndValue(1.0)
        self._fade.start()
        self._timer.start(max(ms, 1800 + 40 * len(text)))

    def place(self) -> None:
        host = self.parentWidget()
        if host is None:
            return
        area = self.area if self.area is not None else host.rect()
        self.move(area.x() + (area.width() - self.width()) // 2,
                  area.y() + area.height() - self.height() - z(self.lift))

    def _fade_out(self) -> None:
        self._hiding = True
        self._fade.stop()
        self._fade.setStartValue(self._opacity.opacity())
        self._fade.setEndValue(0.0)
        self._fade.start()

    def _fade_finished(self) -> None:
        if self._hiding:
            self.hide()


# ── Songs inside a category ──────────────────────────────────────────────────
class SongRow(QFrame):
    """☰  position  thumbnail  title  length  ▶"""

    def __init__(self, ctx: Ctx, song_list: SongList, song_id: str, wide: bool = False) -> None:
        super().__init__(song_list)
        self.ctx = ctx
        self.song_list = song_list
        self.song_id = song_id
        self.wide = wide
        song = ctx.library.song(song_id)
        self.video_id = song.video_id if song else ""
        self.setObjectName("SongRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(z(ROW_H_WIDE if wide else ROW_H))

        layout = self._row_layout = QHBoxLayout(self)
        layout.setContentsMargins(z(4), 0, z(12 if wide else 8), 0)
        layout.setSpacing(z(14 if wide else 10))

        self.handle = DragHandle(self)
        self.handle.pressed.connect(lambda: song_list.begin_drag(self))
        self.handle.moved.connect(song_list.drag_to)
        self.handle.released.connect(song_list.end_drag)

        number_font = make_font(13, QFont.Weight.Medium, tabular=True)
        number_metrics = QFontMetrics(number_font)

        self.pos_label = ClickLabel(self)
        self.pos_label.setObjectName("SongPos")
        self.pos_label.setFont(number_font)
        self.pos_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.pos_label.setFixedWidth(number_metrics.horizontalAdvance("000") + z(4))
        self.pos_label.setToolTip("Double-click to move this song to a position")
        self.pos_label.doubleClicked.connect(self._edit_position)

        self.pos_edit: InlineEditor | None = None  # created on first double-click

        self.thumb = Thumb(80, 45, 5, self) if wide else Thumb(64, 36, 4, self)
        title_font = make_font(15 if wide else 14)

        self.title_label = ElidedLabel(self)
        self.title_label.setObjectName("SongTitle")
        self.title_label.setFont(title_font)
        self.title_label.doubleClicked.connect(self._edit_title)

        self.title_edit: InlineEditor | None = None

        duration_font = make_font(13, tabular=True)
        self.duration = plain_label(name="SongDuration", font=duration_font)
        self.duration.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.duration.setFixedWidth(QFontMetrics(duration_font).horizontalAdvance("0:00:00") + z(4))

        self.play_button = PlayButton(self)
        self.play_button.clicked.connect(self._toggle_play)

        for widget in (self.handle, self.pos_label, self.thumb):
            layout.addWidget(widget)
        layout.addWidget(self.title_label, 3)
        self.channel: ElidedLabel | None = None
        if wide:
            self.channel = ElidedLabel(self)
            self.channel.setObjectName("SongChannel")
            self.channel.setFont(make_font(13))
            layout.addWidget(self.channel, 2)
        layout.addWidget(self.duration)
        layout.addWidget(self.play_button)

        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)
        self.refresh()
        self.load_thumb()

    def song(self) -> Song | None:
        return self.ctx.library.song(self.song_id)

    def refresh(self) -> None:
        song = self.song()
        if song is None:
            return
        title = song.display_title
        if not title:
            title = ("Looking up title…" if self.ctx.fetcher.is_pending(song.video_id)
                     else "Untitled video")
        self.title_label.set_full_text(title)
        set_flag(self.title_label, "pending", not song.display_title)
        if self.channel is not None:
            self.channel.set_full_text(song.channel)
        self.duration.setText(format_duration(song.duration))
        self.set_phase(self.ctx.player.song_phase(self.song_id))

    def load_thumb(self) -> None:
        pixmap = self.ctx.thumbs.get(self.video_id)
        if pixmap is not None:
            self.thumb.set_pixmap(pixmap)

    def set_position(self, number: int) -> None:
        self.pos_label.setText(str(number))

    def set_phase(self, phase: str) -> None:
        """idle, loading (starting up) or playing."""
        self.play_button.set_phase(phase)
        active = phase != "idle"
        set_flag(self, "playing", active)
        set_flag(self.pos_label, "playing", active)

    # Double-click the number: jump to a position
    def _editor_after(self, anchor: QWidget, stretch: int = 0) -> InlineEditor:
        editor = InlineEditor(self)
        editor.setFont(anchor.font())
        self._row_layout.insertWidget(self._row_layout.indexOf(anchor) + 1, editor, stretch)
        return editor

    def _edit_position(self) -> None:
        if self.pos_edit is None:
            self.pos_edit = self._editor_after(self.pos_label)
            self.pos_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.pos_edit.setFixedWidth(
                QFontMetrics(self.pos_label.font()).horizontalAdvance("000") + z(24))
            self.pos_edit.committed.connect(self._position_committed)
            self.pos_edit.cancelled.connect(self._end_position_edit)
        category = self.ctx.library.category(self.song_list.category_id)
        count = len(category.song_ids) if category is not None else len(self.song_list.rows)
        self.pos_edit.setValidator(QIntValidator(1, max(1, count), self.pos_edit))
        self.pos_label.hide()
        self.pos_edit.begin(self.pos_label.text())

    def _end_position_edit(self) -> None:
        if self.pos_edit is not None:
            self.pos_edit.hide()
        self.pos_label.show()

    def _position_committed(self, text: str) -> None:
        self._end_position_edit()
        if text.strip().isdigit():
            self.song_list.moveRequested.emit(self.song_id, int(text))

    # Double-click the title: rename
    def _edit_title(self) -> None:
        song = self.song()
        if song is None:
            return
        if self.title_edit is None:
            self.title_edit = self._editor_after(self.title_label, 3)
            self.title_edit.setMaxLength(200)
            self.title_edit.committed.connect(self._title_committed)
            self.title_edit.cancelled.connect(self._end_title_edit)
        self.title_label.hide()
        self.title_edit.setPlaceholderText(song.yt_title or "Title")
        self.title_edit.begin(song.display_title)

    def _end_title_edit(self) -> None:
        if self.title_edit is not None:
            self.title_edit.hide()
        self.title_label.show()

    def _title_committed(self, text: str) -> None:
        self._end_title_edit()
        self.ctx.library.set_song_title(self.song_id, text)

    def _toggle_play(self) -> None:
        if self.song() is not None:
            self.ctx.player.play_song(self.song_id, self.song_list.category_id)

    def _context_menu(self, pos: QPoint) -> None:
        menu = QMenu(self)
        add = menu.addAction("Add to categories…")
        add_link_actions(menu, self.ctx, self.video_id)
        menu.addSeparator()
        remove = menu.addAction("Remove from this category")
        chosen = menu.exec(self.mapToGlobal(pos))
        if chosen == add and self.ctx.window is not None:
            self.ctx.window.open_song_categories(self.song_id)
        elif chosen == remove:
            self.song_list.removeRequested.emit(self.song_id)


class SongList(QWidget):
    """The ordered rows of one category, with live drag-to-reorder."""

    reordered = Signal(list)          # song ids in their new order
    moveRequested = Signal(str, int)  # song id, 1-based position
    removeRequested = Signal(str)     # song id

    def __init__(self, ctx: Ctx, category_id: str, parent: QWidget | None = None,
                 wide: bool = False) -> None:
        super().__init__(parent)
        self.setObjectName("SongList")
        self.ctx = ctx
        self.category_id = category_id
        self.wide = wide
        self.rows: list[SongRow] = []
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(z(ROW_GAP))
        self._layout.addStretch(1)
        self._drag_row: SongRow | None = None
        self._autoscroll = QTimer(self)
        self._autoscroll.setInterval(16)
        self._autoscroll.timeout.connect(self._autoscroll_tick)

    def row_for(self, song_id: str) -> SongRow | None:
        return next((row for row in self.rows if row.song_id == song_id), None)

    def sync(self, song_ids: list[str]) -> None:
        """Match the rows to the library order, reusing rows that still exist."""
        if self._drag_row is not None:
            return
        existing = {row.song_id: row for row in self.rows}
        rows = [existing.pop(sid, None) or SongRow(self.ctx, self, sid, self.wide)
                for sid in song_ids]
        for gone in existing.values():
            self._layout.removeWidget(gone)
            gone.hide()
            gone.deleteLater()
        for row in rows:
            self._layout.removeWidget(row)
        for index, row in enumerate(rows):
            self._layout.insertWidget(index, row)
            row.show()
        self.rows = rows
        self.renumber()

    def renumber(self) -> None:
        for number, row in enumerate(self.rows, start=1):
            row.set_position(number)

    # Dragging: the held row moves live as the cursor passes other rows' centers.
    def begin_drag(self, row: SongRow) -> None:
        self._drag_row = row
        set_flag(row, "dragging", True)
        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.ClosedHandCursor))
        self._autoscroll.start()

    def drag_to(self, global_pos: QPoint) -> None:
        row = self._drag_row
        if row is None:
            return
        y = self.mapFromGlobal(global_pos).y()
        target = sum(1 for other in self.rows
                     if other is not row and y > other.geometry().center().y())
        current = self.rows.index(row)
        if target == current:
            return
        self.rows.insert(target, self.rows.pop(current))
        self._layout.removeWidget(row)
        self._layout.insertWidget(target, row)
        self._layout.activate()
        self.renumber()

    def end_drag(self) -> None:
        row = self._drag_row
        if row is None:
            return
        self._drag_row = None
        self._autoscroll.stop()
        QApplication.restoreOverrideCursor()
        set_flag(row, "dragging", False)
        self.reordered.emit([r.song_id for r in self.rows])

    def _autoscroll_tick(self) -> None:
        """Scroll the card (then the page) while a drag hovers near an edge."""
        if self._drag_row is None:
            self._autoscroll.stop()
            return
        cursor = QCursor.pos()
        widget = self.parentWidget()
        while widget is not None:
            if isinstance(widget, QScrollArea):
                bar = widget.verticalScrollBar()
                view = widget.viewport()
                y = view.mapFromGlobal(cursor).y()
                edge, step = z(36), 0
                if y < edge:
                    step = -min(z(22), 2 + (edge - y) // 3)
                elif y > view.height() - edge:
                    step = min(z(22), 2 + (y - view.height() + edge) // 3)
                if (step < 0 and bar.value() > bar.minimum()) or \
                        (step > 0 and bar.value() < bar.maximum()):
                    bar.setValue(bar.value() + step)
                    self.drag_to(cursor)
                    return
            widget = widget.parentWidget()


def sync_category_controls(ctx: Ctx, category_id: str, play_button: GlyphButton,
                           shuffle_button: GlyphButton) -> None:
    category = ctx.library.category(category_id)
    has_songs = bool(category and category.song_ids)
    mode = ctx.player.mode_in(category_id)
    play_button.set_kind("stop" if mode else "play")
    play_button.set_active(bool(mode))
    play_button.setToolTip("Stop" if mode else "Play in order")
    play_button.setEnabled(has_songs or bool(mode))
    shuffle_button.set_active(mode == "shuffle")
    shuffle_button.setToolTip("Turn off shuffle" if mode == "shuffle"
                              else "Turn on shuffle" if mode else "Shuffle")
    shuffle_button.setEnabled(has_songs)


def toggle_category_play(player: Player, category_id: str) -> None:
    if player.mode_in(category_id):
        player.stop()
    else:
        player.play_category(category_id)


def toggle_category_shuffle(player: Player, category_id: str) -> None:
    mode = player.mode_in(category_id)
    if mode:  # already playing: flip shuffle, keep the current song going
        player.set_shuffle(category_id, mode != "shuffle")
    else:
        player.play_category(category_id, shuffle=True)


def format_total(seconds: float) -> str:
    minutes = int(round(seconds / 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def runtime_text(songs: list[Song]) -> str:
    """Total length, saying "at least" while some lengths are still being looked up."""
    known = [song.duration for song in songs if song.duration]
    seconds = sum(known)
    if seconds < 60:
        return ""
    return ("at least " if len(known) < len(songs) else "") + format_total(seconds)


def songs_summary(library: Library, category: Category) -> str:
    songs = [song for song in (library.song(sid) for sid in category.song_ids) if song]
    runtime = runtime_text(songs)
    return song_count(len(songs)) + (f", {runtime}" if runtime else "")


def wire_song_list(song_list: SongList, library: Library, category_id: str) -> None:
    def reordered(ids: list) -> None:  # a card shows only the top songs; the rest keep their order
        category = library.category(category_id)
        if category is not None:
            shown = set(ids)
            library.reorder(category_id, list(ids) + [sid for sid in category.song_ids
                                                      if sid not in shown])
    song_list.reordered.connect(reordered)
    song_list.moveRequested.connect(lambda sid, pos: library.move_song(category_id, sid, pos))
    song_list.removeRequested.connect(lambda sid: library.remove_song(category_id, sid))


class CategoryCard(QFrame):
    """One cell of the grid: a category's title, subtitle and song list."""

    def __init__(self, ctx: Ctx, category_id: str) -> None:
        super().__init__()
        self.ctx = ctx
        self.category_id = category_id
        self.setObjectName("Card")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)  # clicking here closes inline edits
        self.setMinimumWidth(z(330))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(z(18), z(18), z(12), z(12))
        layout.setSpacing(z(14))

        top = QHBoxLayout()
        top.setContentsMargins(z(4), 0, z(7), 0)  # play lines up with the rows' play buttons
        top.setSpacing(z(8))
        heading = QVBoxLayout()
        heading.setContentsMargins(0, 0, 0, 0)
        heading.setSpacing(z(4))
        self.title = EditableText("CardTitle", make_font(20, QFont.Weight.DemiBold, tracking=-0.3))
        self.subtitle = EditableText("CardSubtitle", make_font(13), max_length=140,
                                     allow_empty=True, hint="Double-click to edit")
        self.title.committed.connect(
            lambda text: ctx.library.rename_category(self.category_id, title=text))
        self.subtitle.committed.connect(
            lambda text: ctx.library.rename_category(self.category_id, subtitle=text))
        self.meta = plain_label(name="CardMeta", font=make_font(12, tabular=True))
        heading.addWidget(self.title)
        heading.addWidget(self.subtitle)
        heading.addWidget(self.meta)
        top.addLayout(heading, 1)
        self.shuffle_button = GlyphButton("shuffle", variant="toggle")
        self.play_button = GlyphButton("play", variant="outline")
        top.addWidget(self.shuffle_button, 0, Qt.AlignmentFlag.AlignTop)
        top.addWidget(self.play_button, 0, Qt.AlignmentFlag.AlignTop)
        self.shuffle_button.clicked.connect(self._shuffle_clicked)
        self.play_button.clicked.connect(self._play_clicked)
        layout.addLayout(top)

        self.list = SongList(ctx, category_id)
        self.scroller = QScrollArea()
        self.scroller.setObjectName("CardScroll")
        self.scroller.setWidgetResizable(True)
        self.scroller.setFrameShape(QFrame.Shape.NoFrame)
        self.scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroller.viewport().setObjectName("CardViewport")
        self.scroller.viewport().setAutoFillBackground(False)
        self.scroller.setWidget(self.list)
        layout.addWidget(self.scroller)

        self.see_all = QPushButton("")
        self.see_all.setObjectName("Ghost")
        self.see_all.setFont(make_font(12, QFont.Weight.Medium))
        self.see_all.setCursor(Qt.CursorShape.PointingHandCursor)
        self.see_all.clicked.connect(
            lambda: ctx.window.show_page(category_id) if ctx.window is not None else None)
        self.see_all.hide()
        layout.addWidget(self.see_all, 0, Qt.AlignmentFlag.AlignLeft)
        self.empty = plain_label("Empty. Use the + button to add a song.", "CardEmpty",
                                 make_font(13))
        self.empty.setContentsMargins(z(4), 0, 0, z(6))
        layout.addWidget(self.empty)
        layout.addStretch(1)

        wire_song_list(self.list, ctx.library, category_id)

        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)
        self.refresh_heading()
        self.sync()

    def refresh_heading(self) -> None:
        category = self.ctx.library.category(self.category_id)
        if category is None:
            return
        self.title.set_text(category.title)
        self.subtitle.set_text(category.subtitle)
        self.subtitle.setVisible(bool(category.subtitle))

    def sync(self) -> None:
        category = self.ctx.library.category(self.category_id)
        if category is None:
            return
        self.list.sync(category.song_ids[:CARD_ROWS])
        count = len(category.song_ids)
        self.see_all.setVisible(count > CARD_ROWS)
        self.see_all.setText(f"See all {count} songs")
        self.empty.setVisible(count == 0)
        self.scroller.setVisible(count > 0)
        shown = min(count, MAX_VISIBLE_ROWS, CARD_ROWS)
        self.scroller.setFixedHeight(max(0, shown * z(ROW_H) + max(0, shown - 1) * z(ROW_GAP)))
        self.meta.setText(songs_summary(self.ctx.library, category))
        self._update_controls()

    def song_changed(self, song_id: str) -> None:
        row = self.list.row_for(song_id)
        if row is not None:
            row.refresh()
            category = self.ctx.library.category(self.category_id)
            if category is not None:  # a length that just arrived changes the total
                self.meta.setText(songs_summary(self.ctx.library, category))

    def thumb_ready(self, video_id: str) -> None:
        for row in self.list.rows:
            if row.video_id == video_id:
                row.load_thumb()

    def player_changed(self) -> None:
        player = self.ctx.player
        for row in self.list.rows:
            row.set_phase(player.song_phase(row.song_id))
        self._update_controls()

    def _update_controls(self) -> None:
        sync_category_controls(self.ctx, self.category_id, self.play_button, self.shuffle_button)

    def _play_clicked(self) -> None:
        toggle_category_play(self.ctx.player, self.category_id)

    def _shuffle_clicked(self) -> None:
        toggle_category_shuffle(self.ctx.player, self.category_id)

    def _context_menu(self, pos: QPoint) -> None:
        category = self.ctx.library.category(self.category_id)
        menu = QMenu(self)
        rename = menu.addAction("Rename category")
        share = menu.addAction("Share or export…")
        share.setEnabled(bool(category and category.song_ids))
        menu.addSeparator()
        delete = menu.addAction("Delete category…")
        chosen = menu.exec(self.mapToGlobal(pos))
        if chosen == rename:
            self.title.begin()
        elif chosen == share and self.ctx.window is not None:
            self.ctx.window.open_share(self.category_id)
        elif chosen == delete and self.ctx.window is not None:
            self.ctx.window.confirm_delete_category(self.category_id)


# ── Prompts ──────────────────────────────────────────────────────────────────
class Overlay(QWidget):
    """Dims the window and holds one floating card. Esc or a click outside closes it."""

    dismissed = Signal()

    def __init__(self, host: QWidget, dim: int = 190) -> None:
        super().__init__(host)
        self._dim = dim
        self._closed = False
        self.card: QWidget | None = None
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setGeometry(host.rect())
        host.installEventFilter(self)

    def set_card(self, card: QWidget) -> None:
        self.card = card
        card.setParent(self)
        card.installEventFilter(self)
        card.show()
        self.place_card()

    def place_card(self) -> None:
        if self.card is None or self._closed:
            return
        self.card.adjustSize()
        x = (self.width() - self.card.width()) // 2
        y = max(z(24), (self.height() - self.card.height()) // 2 - z(24))
        self.card.move(x, y)

    def open(self) -> None:
        self.show()
        self.raise_()
        fade = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(fade)
        animation = QPropertyAnimation(fade, b"opacity", self)
        animation.setDuration(140)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.finished.connect(lambda: self.setGraphicsEffect(None))
        animation.start()
        target = self.card.focus_target() if hasattr(self.card, "focus_target") else self
        target.setFocus(Qt.FocusReason.OtherFocusReason)

    def dismiss(self) -> None:
        if self._closed:
            return
        self._closed = True
        host = self.parentWidget()
        if host is not None:
            host.removeEventFilter(self)
        self.hide()
        self.dismissed.emit()
        self.deleteLater()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self.setGeometry(self.parentWidget().rect())
            self.place_card()
        elif watched is self.card and event.type() == QEvent.Type.LayoutRequest:
            QTimer.singleShot(0, self.place_card)
        return False

    def paintEvent(self, _event) -> None:
        QPainter(self).fillRect(self.rect(), QColor(0, 0, 0, self._dim))

    def mousePressEvent(self, event) -> None:
        if self.card is None or not self.card.geometry().contains(event.position().toPoint()):
            self.dismiss()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.dismiss()
        else:
            super().keyPressEvent(event)


class MenuOverlay(Overlay):
    """The two choices that open from the + button."""

    chosen = Signal(str)  # "song" or "category"

    def __init__(self, host: QWidget, anchor: QWidget) -> None:
        super().__init__(host, dim=120)
        self.anchor = anchor
        card = QFrame()
        card.setObjectName("MenuCard")
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        card.setFixedWidth(z(214))
        layout = QVBoxLayout(card)
        layout.setContentsMargins(z(6), z(6), z(6), z(6))
        layout.setSpacing(z(2))
        for kind, label in (("song", "Add Song"), ("category", "Add Category"),
                            ("playlist", "Import Playlist")):
            button = QPushButton("  " + label)
            button.setObjectName("MenuItem")
            button.setIcon(painted_icon(kind))
            button.setIconSize(QSize(z(18), z(18)))
            button.setFont(make_font(14, QFont.Weight.Medium))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            button.clicked.connect(lambda _checked=False, k=kind: self._choose(k))
            layout.addWidget(button)
        self.set_card(card)

    def place_card(self) -> None:
        if self.card is None or self._closed:
            return
        self.card.adjustSize()
        anchor = self.anchor.geometry()
        self.card.move(anchor.right() - self.card.width() + 1,
                       anchor.top() - self.card.height() - z(12))

    def _choose(self, kind: str) -> None:
        self.dismiss()
        self.chosen.emit(kind)


def make_field(label: str, placeholder: str, *, optional: bool = False,
               hint: str = "") -> tuple[QWidget, QLineEdit, QLabel | None]:
    box = QWidget()
    column = QVBoxLayout(box)
    column.setContentsMargins(0, 0, 0, 0)
    column.setSpacing(z(7))
    top = QHBoxLayout()
    top.setSpacing(z(8))
    top.addWidget(plain_label(label, "FieldLabel", make_font(13, QFont.Weight.Medium)))
    top.addStretch(1)
    if optional:
        top.addWidget(plain_label("Optional", "FieldOptional", make_font(12)))
    column.addLayout(top)
    edit = QLineEdit()
    edit.setPlaceholderText(placeholder)
    edit.setFont(make_font(14))
    column.addWidget(edit)
    hint_label = None
    if hint:
        hint_label = plain_label(hint, "FieldHint", make_font(12))
        hint_label.setWordWrap(True)
        column.addWidget(hint_label)
    return box, edit, hint_label


class CategoryPicker(QWidget):
    """Categories with checkboxes, as in Add Song, Import Playlist and Add to categories."""

    changed = Signal()

    def __init__(self, library: Library, checked=(), visible_rows: int = 5,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        picks = QWidget()
        picks.setObjectName("PickList")
        pick_layout = QVBoxLayout(picks)
        pick_layout.setContentsMargins(0, 0, z(4), 0)
        pick_layout.setSpacing(z(8))
        self.checks: list[CheckRow] = []
        for category in library.categories:
            check = CheckRow(category.id, category.title, category.subtitle)
            check.setChecked(category.id in checked)
            check.toggled.connect(lambda _on: self.changed.emit())
            pick_layout.addWidget(check)
            self.checks.append(check)
        pick_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setObjectName("PickScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.viewport().setObjectName("PickViewport")
        scroll.viewport().setAutoFillBackground(False)
        scroll.setWidget(picks)
        shown = self.checks[:visible_rows]
        height = sum(c.height() for c in shown) + z(8) * max(0, len(shown) - 1)
        scroll.setFixedHeight(height + (z(28) if len(self.checks) > visible_rows else 0))
        layout.addWidget(scroll)

    def selected(self) -> list[str]:
        return [check.category_id for check in self.checks if check.isChecked()]


class Sheet(QFrame):
    """A prompt card: title, optional step counter, body, and two buttons."""

    cancelled = Signal()

    def __init__(self, title: str, step: str = "", primary: str = "Next",
                 secondary: str = "Cancel") -> None:
        super().__init__()
        self.setObjectName("Sheet")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedWidth(z(460))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(z(26), z(24), z(26), z(22))
        outer.setSpacing(z(20))

        top = QHBoxLayout()
        self.title_label = plain_label(title, "SheetTitle",
                                       make_font(20, QFont.Weight.DemiBold, tracking=-0.3))
        self.step_label = plain_label(step, "SheetStep", make_font(12, tabular=True))
        top.addWidget(self.title_label)
        top.addStretch(1)
        top.addWidget(self.step_label)
        outer.addLayout(top)

        self.body = QVBoxLayout()
        self.body.setSpacing(z(16))
        outer.addLayout(self.body)

        buttons = QHBoxLayout()
        buttons.setSpacing(z(10))
        buttons.addStretch(1)
        self.secondary = QPushButton(secondary)
        self.secondary.setObjectName("Ghost")
        self.secondary.setFont(make_font(13, QFont.Weight.Medium))
        self.primary = QPushButton(primary)
        self.primary.setObjectName("Primary")
        self.primary.setFont(make_font(13, QFont.Weight.DemiBold))
        for button in (self.secondary, self.primary):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            buttons.addWidget(button)
        outer.addLayout(buttons)

        self.primary.clicked.connect(lambda: self.on_primary())
        self.secondary.clicked.connect(lambda: self.on_secondary())

    def on_primary(self) -> None:
        pass

    def on_secondary(self) -> None:
        self.cancelled.emit()

    def focus_target(self) -> QWidget:
        return self

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            focused = QApplication.focusWidget()
            if isinstance(focused, QPushButton) and focused.isEnabled():
                focused.click()
            elif self.primary.isEnabled():
                self.primary.click()
            return
        super().keyPressEvent(event)


class CategorySheet(Sheet):
    submitted = Signal(str, str)  # title, subtitle

    def __init__(self) -> None:
        super().__init__("New category")
        title_box, self.title_input, _ = make_field("Title", "Late-night drives")
        subtitle_box, self.subtitle_input, _ = make_field(
            "Subtitle", "Slow songs for empty roads", optional=True)
        self.title_input.setMaxLength(80)
        self.subtitle_input.setMaxLength(140)
        self.body.addWidget(title_box)
        self.body.addWidget(subtitle_box)
        self.title_input.textChanged.connect(self._validate)
        self._validate()

    def _validate(self) -> None:
        self.primary.setEnabled(bool(self.title_input.text().strip()))

    def focus_target(self) -> QWidget:
        return self.title_input

    def on_primary(self) -> None:
        if self.primary.isEnabled():
            self.submitted.emit(self.title_input.text().strip(),
                                self.subtitle_input.text().strip())


class VideoPreview(QFrame):
    """Thumbnail, title and length of the pasted link, so you know it's the right one."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("Preview")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(z(10), z(10), z(14), z(10))
        layout.setSpacing(z(12))
        self.thumb = Thumb(96, 54, 5)
        text = QVBoxLayout()
        text.setSpacing(z(3))
        self.title = ElidedLabel()
        self.title.setObjectName("PreviewTitle")
        self.title.setFont(make_font(14, QFont.Weight.Medium))
        self.channel = ElidedLabel()
        self.channel.setObjectName("PreviewMeta")
        self.channel.setFont(make_font(12))
        text.addStretch(1)
        text.addWidget(self.title)
        text.addWidget(self.channel)
        text.addStretch(1)
        self.duration = plain_label(name="PreviewMeta", font=make_font(13, tabular=True))
        layout.addWidget(self.thumb)
        layout.addLayout(text, 1)
        layout.addWidget(self.duration)


LINK_HINT = "Paste a link to a YouTube video."
LINK_ERROR = "That isn't a YouTube video link. Copy it from the video's Share button."
TITLE_PLACEHOLDER = "Leave empty to use the video's title"
LIST_NOTE = ("Only this video is added. To add the whole playlist, use Import Playlist "
             "in the + menu.")
PLAYLIST_LINK_ERROR = ("That's a playlist link. Use Import Playlist in the + menu to add all "
                       "of its songs.")


class SongSheet(Sheet):
    """Step 1: title and link. Step 2: which categories get the song."""

    submitted = Signal(str, str, list)  # video id, custom title, category ids

    def __init__(self, ctx: Ctx, preselect: str = "") -> None:
        super().__init__("Add song", "1 of 2")
        self.ctx = ctx
        self.video_id: str | None = None
        self._step = 1

        self.step1 = QWidget()
        first = QVBoxLayout(self.step1)
        first.setContentsMargins(0, 0, 0, 0)
        first.setSpacing(z(16))
        title_box, self.title_input, _ = make_field("Title", TITLE_PLACEHOLDER, optional=True)
        self.title_input.setMaxLength(200)
        link_box, self.link_input, self.link_hint = make_field(
            "YouTube link", "https://www.youtube.com/watch?v=…", hint=LINK_HINT)
        self.preview = VideoPreview()
        self.preview.hide()
        self.list_hint = plain_label(LIST_NOTE, "FieldHint", make_font(12))
        self.list_hint.setWordWrap(True)
        self.list_hint.hide()
        first.addWidget(title_box)
        first.addWidget(link_box)
        first.addWidget(self.preview)
        first.addWidget(self.list_hint)

        self.step2 = QWidget()
        second = QVBoxLayout(self.step2)
        second.setContentsMargins(0, 0, 0, 0)
        second.setSpacing(z(12))
        intro = plain_label("Tick one or more. The song goes to the top of each.",
                            "FieldHint", make_font(13))
        intro.setWordWrap(True)
        second.addWidget(intro)
        categories = ctx.library.categories  # pre-tick the open page's category, or the only one
        ticked = {categories[0].id} if len(categories) == 1 else {preselect}
        self.picker = CategoryPicker(ctx.library, checked=ticked, visible_rows=5)
        self.picker.changed.connect(self._validate)
        self.checks = self.picker.checks
        second.addWidget(self.picker)

        self.body.addWidget(self.step1)
        self.body.addWidget(self.step2)
        self.step2.hide()

        self.link_input.textChanged.connect(self._link_changed)
        ctx.fetcher.updated.connect(self._details_updated)
        ctx.thumbs.ready.connect(self._thumb_ready)
        self._validate()

    def focus_target(self) -> QWidget:
        return self.title_input

    def _link_changed(self, text: str) -> None:
        video_id = parse_video_id(text)
        self.video_id = video_id
        in_playlist = parse_playlist(text) is not None
        self.list_hint.setVisible(bool(video_id) and in_playlist)
        if video_id:
            self.link_hint.hide()
            self.preview.show()
            self.preview.thumb.set_pixmap(self.ctx.thumbs.get(video_id))
            self.ctx.fetcher.request(video_id)
            self.ctx.cache.want(video_id, Priority.NOW)  # likely to be played right after adding
            self._refresh_preview()
        else:
            self.preview.hide()
            self.link_hint.show()
            if in_playlist:
                message = PLAYLIST_LINK_ERROR
            else:
                message = LINK_ERROR if text.strip() else LINK_HINT
            self.link_hint.setText(message)
            set_flag(self.link_hint, "error", bool(text.strip()))
            self.title_input.setPlaceholderText(TITLE_PLACEHOLDER)
        self._validate()

    def _refresh_preview(self) -> None:
        if not self.video_id:
            return
        info = self.ctx.fetcher.info.get(self.video_id, {})
        pending = self.ctx.fetcher.is_pending(self.video_id)
        title = info.get("title")
        if title:
            self.preview.title.set_full_text(title)
            self.title_input.setPlaceholderText(title)
        elif pending:
            self.preview.title.set_full_text("Looking up the video…")
        else:
            self.preview.title.set_full_text(
                info.get("error") or "Couldn't load this video's details. You can still add it.")
        set_flag(self.preview.title, "pending", not title)
        self.preview.channel.set_full_text(info.get("channel") or "")
        duration = info.get("duration")
        self.preview.duration.setText(format_duration(duration) if duration is not None
                                      else ("…" if pending else ""))

    def _details_updated(self, video_id: str) -> None:
        if video_id == self.video_id:
            self._refresh_preview()

    def _thumb_ready(self, video_id: str) -> None:
        if video_id == self.video_id:
            self.preview.thumb.set_pixmap(self.ctx.thumbs.get(video_id))

    def _validate(self) -> None:
        if self._step == 1:
            self.primary.setEnabled(self.video_id is not None)
        else:
            self.primary.setEnabled(any(check.isChecked() for check in self.checks))

    def _show_step(self, step: int) -> None:
        self._step = step
        self.step1.setVisible(step == 1)
        self.step2.setVisible(step == 2)
        self.title_label.setText("Add song" if step == 1 else "Add to categories")
        self.step_label.setText(f"{step} of 2")
        self.secondary.setText("Cancel" if step == 1 else "Back")
        (self.link_input if step == 1 else self).setFocus()
        self._validate()

    def on_primary(self) -> None:
        if not self.primary.isEnabled():
            return
        if self._step == 1:
            self._show_step(2)
        else:
            chosen = [check.category_id for check in self.checks if check.isChecked()]
            self.submitted.emit(self.video_id, self.title_input.text().strip(), chosen)

    def on_secondary(self) -> None:
        if self._step == 2:
            self._show_step(1)
        else:
            self.cancelled.emit()


IMPORT_HINT = ("Paste a YouTube playlist link, a Tapedeck share code, or YouTube links (one per "
               "line). Public and unlisted playlists work.")
SINGLE_VIDEO_HINT = ("That's a link to a single video. Use Add Song for that, or paste more "
                     "links, one per line.")
NOT_IMPORTABLE_HINT = "That isn't a playlist link, a share code or a list of YouTube links."
BROKEN_CODE_HINT = "That share code is incomplete or damaged. Ask for it again, and copy all of it."


class PasteBox(QPlainTextEdit):
    """A few lines of text: long share codes and lists of links fit without scrolling sideways."""

    def __init__(self, placeholder: str = "", read_only: bool = False,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("PasteBox")
        self.setFont(make_font(13))
        self.setPlaceholderText(placeholder)
        self.setReadOnly(read_only)
        self.setTabChangesFocus(True)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setFixedHeight(z(88))


def share_diff(library: Library, category: Category, entries: list[dict]) -> tuple[int, int]:
    """(new songs, removed songs) if this category were updated to match entries."""
    here = {song.video_id for song in (library.song(sid) for sid in category.song_ids) if song}
    there = {entry["video_id"] for entry in entries}
    return len(there - here), len(here - there)


def diff_text(new: int, removed: int) -> str:
    if not new and not removed:
        return "no song changes"
    parts = ([f"{new} new"] if new else []) + ([f"{removed} removed"] if removed else [])
    return ", ".join(parts)


class PlaylistSheet(Sheet):
    """Import a YouTube playlist, a share code or pasted links, then choose where they go."""

    # source (PlaylistJob or ImportBatch), new category name or "", category ids, category to update
    submitted = Signal(object, str, list, str)

    def __init__(self, ctx: Ctx, preselect: str = "") -> None:
        super().__init__("Import playlist", "1 of 2")
        self.ctx = ctx
        self.job: PlaylistJob | None = None
        self.result = None          # a finished PlaylistJob, or an ImportBatch
        self.match: Category | None = None  # the category a share code is a newer copy of
        self._key = None
        self._thumb_id = ""
        self._named = False  # the new category's name was typed by hand
        self._step = 1

        self.step1 = QWidget()
        first = QVBoxLayout(self.step1)
        first.setContentsMargins(0, 0, 0, 0)
        first.setSpacing(z(16))
        box = QWidget()
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(z(7))
        column.addWidget(plain_label("Playlist link, share code or links", "FieldLabel",
                                     make_font(13, QFont.Weight.Medium)))
        self.link_input = PasteBox("https://www.youtube.com/playlist?list=…")
        column.addWidget(self.link_input)
        self.link_hint = plain_label(IMPORT_HINT, "FieldHint", make_font(12))
        self.link_hint.setWordWrap(True)
        column.addWidget(self.link_hint)
        self.preview = VideoPreview()
        self.preview.hide()
        first.addWidget(box)
        first.addWidget(self.preview)

        self.step2 = QWidget()
        second = QVBoxLayout(self.step2)
        second.setContentsMargins(0, 0, 0, 0)
        second.setSpacing(z(10))
        self.intro = plain_label("Its songs go to the top of each category you tick, in order.",
                                 "FieldHint", make_font(13))
        self.intro.setWordWrap(True)
        second.addWidget(self.intro)
        self.library_note = plain_label(
            "Categories you already have from this library are updated to match; the rest are "
            "added as new categories.", "SheetText", make_font(13))
        self.library_note.setWordWrap(True)
        self.library_note.hide()
        second.addWidget(self.library_note)
        self.update_check = CheckRow("", "Update", "Match the share code")
        self.update_check.hide()
        self.new_check = CheckRow("", "New category", "Named after the playlist")
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Category name")
        self.name_input.setFont(make_font(14))
        self.name_input.setMaxLength(80)
        second.addWidget(self.update_check)
        second.addWidget(self.new_check)
        second.addWidget(self.name_input)
        self.picker = CategoryPicker(ctx.library, checked={preselect}, visible_rows=4)
        self.picker.changed.connect(self._validate)
        self.checks = self.picker.checks
        self.picker_label = plain_label("Or add them to", "FieldLabel",
                                        make_font(13, QFont.Weight.Medium))
        if self.checks:
            second.addSpacing(z(6))
            second.addWidget(self.picker_label)
            second.addWidget(self.picker)
        self.new_check.setChecked(not any(check.isChecked() for check in self.checks))
        self.new_check.toggled.connect(lambda _on: self._validate())
        self.update_check.toggled.connect(lambda _on: self._validate())

        self.body.addWidget(self.step1)
        self.body.addWidget(self.step2)
        self.step2.hide()
        self.link_input.textChanged.connect(lambda: self._link_changed(self.link_input.toPlainText()))
        self.name_input.textEdited.connect(self._name_edited)
        ctx.thumbs.ready.connect(self._thumb_ready)
        self._validate()

    def focus_target(self) -> QWidget:
        return self.link_input

    def stop(self) -> None:
        """The prompt closed: stop reading the playlist."""
        if self.job is not None:
            self.job.cancel()
            self.job = None

    # What was pasted
    def _link_changed(self, text: str) -> None:
        stripped = text.strip()
        if looks_like_library_code(stripped):
            batches = read_library_code(stripped)
            if batches is None:
                self._problem(BROKEN_CODE_HINT, text)
            elif not any(batch.entries for batch in batches):
                self._problem("That library code has no songs in it.", text)
            else:
                self._use_library(batches, ("library", "".join(stripped.split())))
            return
        if looks_like_share_code(stripped):
            batch = read_share_code(stripped)
            if batch is None:
                self._problem(BROKEN_CODE_HINT, text)
            elif not batch.entries:
                self._problem("That share code has no songs in it.", text)
            else:
                self._use_batch(batch, ("code", "".join(stripped.split())))
            return
        ids = parse_link_list(stripped)
        if len(ids) >= 2:
            entries = [{"video_id": video_id, "title": "", "channel": "", "duration": None,
                        "custom": ""} for video_id in ids]
            self._use_batch(ImportBatch(title="Imported songs", channel="pasted links",
                                        entries=entries, kind="links"), ("links", tuple(ids)))
            return
        parsed = parse_playlist(stripped)
        if parsed is None:
            self._problem(SINGLE_VIDEO_HINT if ids else
                          NOT_IMPORTABLE_HINT if stripped else IMPORT_HINT, text)
            return
        if parsed[0] in PRIVATE_LISTS:
            self._problem(f"“{PRIVATE_LISTS[parsed[0]]}” belongs to your account, so it can't be "
                          "imported without signing in.", text)
            return
        if parsed != self._key:
            self.stop()
            self._key, self.result, self.match = parsed, None, None
            self.link_hint.hide()
            self.preview.show()
            self.preview.thumb.set_pixmap(None)
            self.preview.title.set_full_text("Looking up the playlist…")
            set_flag(self.preview.title, "pending", True)
            self.preview.channel.set_full_text("")
            self.preview.duration.setText("")
            self.job = PlaylistJob(*parsed, parent=self)
            self.job.done.connect(self._job_done)
            self.job.start()
        self._validate()

    def _problem(self, message: str, text: str) -> None:
        self.stop()
        self._key, self.result, self.match = None, None, None
        self.preview.hide()
        self.link_hint.show()
        self.link_hint.setText(message)
        set_flag(self.link_hint, "error", bool(text.strip()))
        self._validate()

    def _use_batch(self, batch: ImportBatch, key) -> None:
        if key == self._key:
            return
        self.stop()
        self._key, self.result = key, batch
        library = self.ctx.library
        self.match = library.category_by_share(batch.share_id) if batch.share_id else None
        seconds = sum(entry["duration"] or 0 for entry in batch.entries)
        meta = song_count(len(batch.entries))
        meta += f", {format_total(seconds)}" if seconds >= 60 else ""
        meta += f", from {batch.channel}"
        if self.match is not None:
            new, removed = share_diff(library, self.match, batch.entries)
            meta = f"Your “{self.match.title}”, shared back: {diff_text(new, removed)}"
        self._show_preview(batch.title, meta, batch.entries[0]["video_id"])
        self._validate()

    def _use_library(self, batches: list[ImportBatch], key) -> None:
        if key == self._key:
            return
        self.stop()
        self._key, self.result, self.match = key, batches, None
        library = self.ctx.library
        known = sum(1 for batch in batches if batch.share_id and library.category_by_share(batch.share_id))
        songs = sum(len(batch.entries) for batch in batches)
        meta = f"{len(batches)} categor{'y' if len(batches) == 1 else 'ies'}, {song_count(songs)}"
        if known:
            meta += f"; {known} already here will be updated to match"
        first = next(batch for batch in batches if batch.entries)
        self._show_preview("A whole Tapedeck library", meta, first.entries[0]["video_id"])
        self._validate()

    @property
    def is_library(self) -> bool:
        return isinstance(self.result, list)

    def _job_done(self, job: PlaylistJob) -> None:
        if job is not self.job:
            return
        self.job = None
        job.deleteLater()
        if job.error:
            self.result = None
            self.preview.title.set_full_text(job.error)
            set_flag(self.preview.title, "pending", True)
        else:
            self.result = job
            seconds = sum(entry["duration"] or 0 for entry in job.entries)
            meta = song_count(len(job.entries))
            meta += f", {format_total(seconds)}" if seconds >= 60 else ""
            meta += f", by {job.channel}" if job.channel else ""
            if job.unavailable:
                meta += f". {job.unavailable} unavailable will be left out"
            self._show_preview(job.title, meta, job.entries[0]["video_id"])
        self._validate()

    def _show_preview(self, title: str, meta: str, thumb_id: str) -> None:
        self.link_hint.hide()
        self.preview.show()
        self.preview.title.set_full_text(title)
        set_flag(self.preview.title, "pending", False)
        self.preview.channel.set_full_text(meta)
        self.preview.duration.setText("")
        self._thumb_id = thumb_id
        self.preview.thumb.set_pixmap(self.ctx.thumbs.get(thumb_id))
        if not self._named:
            self.name_input.setText(title[:80])

    def _thumb_ready(self, video_id: str) -> None:
        if video_id and video_id == self._thumb_id:
            self.preview.thumb.set_pixmap(self.ctx.thumbs.get(video_id))

    def _name_edited(self, _text: str) -> None:
        self._named = True
        self._validate()

    # Where the songs go
    @property
    def updating(self) -> bool:
        return self.match is not None and self.update_check.isChecked()

    def _validate(self) -> None:
        updating = self.updating
        for widget in (self.new_check, self.name_input, self.picker, self.picker_label):
            widget.setEnabled(not updating)
        if not updating:
            self.name_input.setEnabled(self.new_check.isChecked())
        if self._step == 1:
            self.primary.setEnabled(self.result is not None)
            return
        if self.is_library:
            count = len(self.result)
            self.primary.setEnabled(True)
            self.primary.setText(f"Import {count} categor{'y' if count == 1 else 'ies'}")
            return
        if updating:
            self.primary.setEnabled(True)
            self.primary.setText(f"Update “{self.match.title}”")
            return
        wants_new = self.new_check.isChecked()
        named = bool(self.name_input.text().strip())
        existing = any(check.isChecked() for check in self.checks)
        # somewhere to put the songs, and a name if that includes a new category
        self.primary.setEnabled((wants_new or existing) and (named or not wants_new))
        if self.result is not None:
            self.primary.setText(f"Import {song_count(len(self.result.entries))}")

    def _show_step(self, step: int) -> None:
        self._step = step
        self.step1.setVisible(step == 1)
        self.step2.setVisible(step == 2)
        self.title_label.setText("Import playlist" if step == 1 else "Choose categories")
        self.step_label.setText(f"{step} of 2")
        self.secondary.setText("Cancel" if step == 1 else "Back")
        library_mode = step == 2 and self.is_library
        for widget in (self.new_check, self.name_input, self.picker, self.picker_label, self.intro):
            widget.setVisible(not library_mode)
        self.library_note.setVisible(library_mode)
        if step == 2 and not library_mode:
            match = self.match
            self.update_check.setVisible(match is not None)
            if match is not None:
                new, removed = share_diff(self.ctx.library, match, self.result.entries)
                self.update_check.set_text(f"Update “{match.title}” to match",
                                           f"{diff_text(new, removed).capitalize()}; "
                                           "uses their order and renames")
                self.update_check.setChecked(True)
        else:
            self.primary.setText("Next")
        (self.link_input if step == 1 else self).setFocus()
        self._validate()

    def on_primary(self) -> None:
        if not self.primary.isEnabled() or self.result is None:
            return
        if self._step == 1:
            self._show_step(2)
            return
        if self.is_library:
            self.submitted.emit(self.result, "", [], "")
            return
        if self.updating:
            self.submitted.emit(self.result, "", [], self.match.id)
            return
        name = self.name_input.text().strip() if self.new_check.isChecked() else ""
        self.submitted.emit(self.result, name, self.picker.selected(), "")

    def on_secondary(self) -> None:
        if self._step == 2:
            self._show_step(1)
        else:
            self.cancelled.emit()


class ShareSheet(Sheet):
    """Share a category: a code for other Tapedeck users, or plain YouTube links."""

    copied = Signal(str)  # the confirmation to show

    def __init__(self, ctx: Ctx, category: Category) -> None:
        super().__init__(f"Share “{category.title}”", primary="Copy share code",
                         secondary="Copy links")
        library = ctx.library
        library.ensure_share_id(category.id)
        self.code = make_share_code(library, category)
        self.links = song_links(library, category)
        box = QWidget()
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(z(7))
        column.addWidget(plain_label("Share code", "FieldLabel", make_font(13, QFont.Weight.Medium)))
        self.code_box = PasteBox(read_only=True)
        self.code_box.setPlainText(self.code)
        column.addWidget(self.code_box)
        about = plain_label("Your friend pastes it into + → Import Playlist. It keeps the order, "
                            "the subtitle and any titles you renamed. When you send each other "
                            "newer codes for this playlist, Tapedeck offers to update the copy "
                            "instead of making another.", "FieldHint", make_font(12))
        about.setWordWrap(True)
        column.addWidget(about)
        self.body.addWidget(box)
        links = plain_label(f"Copy links puts its {song_count(len(self.links.splitlines()))} on "
                            "the clipboard as YouTube links, one per line, for anywhere else.",
                            "FieldHint", make_font(12))
        links.setWordWrap(True)
        self.body.addWidget(links)

    def on_primary(self) -> None:
        QApplication.clipboard().setText(self.code)
        self.copied.emit("Share code copied")

    def on_secondary(self) -> None:
        QApplication.clipboard().setText(self.links)
        self.copied.emit("Links copied")


ACCENT_PRESETS = (("#4592a2", "Teal (default)"), ("#4f7cff", "Blue"), ("#8b6cf6", "Violet"),
                  ("#e2609b", "Pink"), ("#e5534b", "Red"), ("#ea8a3a", "Orange"),
                  ("#d4b43a", "Gold"), ("#4fb36a", "Green"))
ACCENT_HINT = "Type a hex code like #4592a2, or pick a color."


def saved_counts(ctx: Ctx) -> tuple[int, int]:
    videos = {song.video_id for song in ctx.library.songs.values()}
    return sum(1 for video_id in videos if ctx.cache.has(video_id)), len(videos)


class Swatch(QAbstractButton):
    """A preset accent color to pick."""

    def __init__(self, color: str, name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.color = color
        self._selected = False
        self.setFixedSize(z(26), z(26))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setToolTip(f"{name}  {color}")

    def set_selected(self, selected: bool) -> None:
        if selected != self._selected:
            self._selected = selected
            self.update()

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        painter, w, h = zoomed_painter(self)
        center = QPointF(w / 2, h / 2)
        if self._selected or self.underMouse() or self.hasFocus():
            painter.setPen(QPen(QColor(WHITE if self._selected or self.hasFocus() else HAIRLINE_HI),
                                1.6))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(center, 11.6, 11.6)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(self.color))
        painter.drawEllipse(center, 8.6, 8.6)


class AccentPreview(QWidget):
    """How an accent color looks on Tapedeck's controls, before saving it."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(z(48))
        self.color: str | None = ACCENT

    def set_color(self, color: str | None) -> None:
        self.color = color
        self.update()

    def paintEvent(self, _event) -> None:
        painter, w, h = zoomed_painter(self)
        if self.color is None:  # not a color (yet): show the shapes as outlines
            painter.setPen(QPen(QColor(HAIRLINE_HI), 1.2, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(0.5, 6.5, w - 1, 35), 11, 11)
            return
        colors = accent_palette(self.color)
        accent, on_accent = QColor(colors["ACCENT"]), QColor(colors["ON_ACCENT"])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(accent)  # the + button
        painter.drawRoundedRect(QRectF(0, 6, 36, 36), 11, 11)
        painter.setPen(QPen(on_accent, 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(QPointF(11, 24), QPointF(25, 24))
        painter.drawLine(QPointF(18, 17), QPointF(18, 31))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(accent)  # play
        painter.drawEllipse(QPointF(64, 24), 16, 16)
        draw_glyph(painter, "play", QPointF(64, 24), on_accent, scale=0.92)
        painter.setBrush(accent)  # a ticked box
        box = QRectF(94, 15, 18, 18)
        painter.drawRoundedRect(box, 5, 5)
        painter.setPen(QPen(on_accent, 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                            Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        tick = QPainterPath(QPointF(box.left() + 4.6, box.center().y() + 0.3))
        tick.lineTo(box.left() + 7.8, box.bottom() - 4.8)
        tick.lineTo(box.right() - 4.2, box.top() + 5.0)
        painter.drawPath(tick)
        left, right = 128.0, max(140.0, w - 6)  # the locator, part played
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(HAIRLINE_HI))
        painter.drawRoundedRect(QRectF(left, 22, right - left, 4), 2, 2)
        painter.setBrush(accent)
        played = left + (right - left) * 0.6
        painter.drawRoundedRect(QRectF(left, 22, played - left, 4), 2, 2)
        painter.setBrush(QColor(WHITE))
        painter.drawEllipse(QPointF(played, 24), 6.5, 6.5)


class SettingsSheet(Sheet):
    """Accent color and instant-play saving."""

    submitted = Signal(str, bool)  # accent color, save songs for instant play

    def __init__(self, ctx: Ctx) -> None:
        super().__init__("Settings", primary="Save")
        self.ctx = ctx
        box = QWidget()
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(z(9))
        column.addWidget(plain_label("Accent color", "FieldLabel", make_font(13, QFont.Weight.Medium)))
        row = QHBoxLayout()
        row.setSpacing(z(6))
        self.hex_input = QLineEdit()
        self.hex_input.setFont(make_font(14, tabular=True))
        self.hex_input.setPlaceholderText(DEFAULT_ACCENT)
        self.hex_input.setMaxLength(7)
        self.hex_input.setFixedWidth(z(112))
        self.hex_input.setText(ACCENT)
        row.addWidget(self.hex_input)
        row.addSpacing(z(6))
        self.swatches = [Swatch(color, name) for color, name in ACCENT_PRESETS]
        for swatch in self.swatches:
            swatch.clicked.connect(lambda _checked=False, c=swatch.color: self.hex_input.setText(c))
            row.addWidget(swatch)
        row.addStretch(1)
        column.addLayout(row)
        self.preview = AccentPreview()
        column.addWidget(self.preview)
        self.accent_hint = plain_label(ACCENT_HINT, "FieldHint", make_font(12))
        self.accent_hint.setWordWrap(True)
        column.addWidget(self.accent_hint)
        self.body.addWidget(box)

        saved, total = saved_counts(ctx)
        self.save_check = CheckRow("", "Save songs for instant play",
                                   f"{saved} of {song_count(total)} saved, "
                                   f"{size_text(ctx.cache.size_bytes())}")
        self.save_check.setChecked(ctx.cache.enabled)
        self.body.addWidget(self.save_check)
        zoom = plain_label(f"Zoom is {round(ZOOM * 100)}%. Change it with Ctrl+scroll or "
                           "Ctrl+plus and minus; Ctrl+0 resets it.", "FieldHint", make_font(12))
        zoom.setWordWrap(True)
        self.body.addWidget(zoom)
        self.hex_input.textChanged.connect(self._hex_changed)
        self._hex_changed(self.hex_input.text())

    def focus_target(self) -> QWidget:
        return self.hex_input

    @property
    def color(self) -> str | None:
        return parse_hex(self.hex_input.text())

    def _hex_changed(self, _text: str) -> None:
        color = self.color
        self.preview.set_color(color)
        for swatch in self.swatches:
            swatch.set_selected(swatch.color == color)
        if color is None:
            hint, warn = "That isn't a hex color yet: six digits, like #4592a2.", True
        elif contrast(color, BLACK) < 3:
            hint, warn = "That's hard to see on Tapedeck's black background. A lighter color works better.", True
        else:
            hint, warn = ACCENT_HINT, False
        self.accent_hint.setText(hint)
        set_flag(self.accent_hint, "error", warn)
        self.primary.setEnabled(color is not None)

    def on_primary(self) -> None:
        color = self.color
        if color is not None:
            self.submitted.emit(color, self.save_check.isChecked())


SONG_CATEGORIES_HINT = ("The song is in the ticked categories. Tick others to add it to their "
                        "top, or untick one to take it out.")
KEEP_ONE_HINT = ("Keep at least one ticked. To delete the song, use Remove from this category "
                 "in its last category.")


class SongCategoriesSheet(Sheet):
    """Right-click a song, Add to categories: tick every category it should be in."""

    submitted = Signal(list)  # the categories the song should be in

    def __init__(self, ctx: Ctx, song: Song) -> None:
        super().__init__("Add to categories", primary="Save")
        self.ctx = ctx
        self.song = song
        library = ctx.library
        self.initial = {category.id for category in library.categories
                        if library.has_video(category.id, song.video_id)}
        self.preview = VideoPreview()
        self.preview.title.set_full_text(song.display_title or "Untitled video")
        self.preview.channel.set_full_text(song.channel)
        self.preview.duration.setText(format_duration(song.duration) if song.duration else "")
        self.preview.thumb.set_pixmap(ctx.thumbs.get(song.video_id))
        self.hint = plain_label(SONG_CATEGORIES_HINT, "FieldHint", make_font(13))
        self.hint.setWordWrap(True)
        self.picker = CategoryPicker(library, checked=self.initial, visible_rows=6)
        self.picker.changed.connect(self._validate)
        self.checks = self.picker.checks
        self.body.addWidget(self.preview)
        self.body.addWidget(self.hint)
        self.body.addWidget(self.picker)
        ctx.thumbs.ready.connect(self._thumb_ready)
        self._validate()

    def _thumb_ready(self, video_id: str) -> None:
        if video_id == self.song.video_id:
            self.preview.thumb.set_pixmap(self.ctx.thumbs.get(video_id))

    def _validate(self) -> None:
        chosen = set(self.picker.selected())
        self.primary.setEnabled(bool(chosen) and chosen != self.initial)
        self.hint.setText(SONG_CATEGORIES_HINT if chosen else KEEP_ONE_HINT)
        set_flag(self.hint, "error", not chosen)

    def on_primary(self) -> None:
        if self.primary.isEnabled():
            self.submitted.emit(self.picker.selected())


def join_names(library: Library, category_ids: list[str]) -> str:
    names = [f"“{library.category(cid).title}”" for cid in category_ids if library.category(cid)]
    if len(names) > 2:
        return f"{len(names)} categories"
    return " and ".join(names)


def membership_summary(library: Library, added: list[str], removed: list[str]) -> str:
    parts = []
    if added:
        parts.append(f"Added to {join_names(library, added)}")
    if removed:
        parts.append(f"{'removed' if added else 'Removed'} from {join_names(library, removed)}")
    return ", and ".join(parts) + "."


def wrapped_label(text: str, name: str, font: QFont, width: int, lines: int = 0) -> QLabel:
    """A word-wrapped label sized for the width it will actually get.

    Qt sizes wrapped labels for a width it guesses; beside other widgets the real width is
    narrower, and the text gets squeezed into lines that overlap. lines reserves room for
    text that changes later.
    """
    label = plain_label(text, name, font)
    label.setWordWrap(True)
    needed = label.heightForWidth(width)
    if lines:
        needed = max(needed, QFontMetrics(font).lineSpacing() * lines)
    if isinstance(needed, int) and needed > 0:
        label.setMinimumHeight(needed)
    return label


class QrView(QWidget):
    """A QR code: dark modules on white, inside the quiet zone scanners need."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.matrix: list[list[bool]] = []
        self.setFixedSize(z(200), z(200))

    def set_text(self, text: str) -> None:
        self.matrix = qr_matrix(text, "M") if text else []
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(WHITE))
        painter.drawRoundedRect(QRectF(self.rect()), zf(14), zf(14))
        if not self.matrix:
            return
        cells = len(self.matrix) + 8  # four light modules on every side
        module = max(1, min(self.width(), self.height()) // cells)
        left = (self.width() - module * cells) // 2 + 4 * module
        top = (self.height() - module * cells) // 2 + 4 * module
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)  # crisp squares
        painter.setBrush(QColor(BLACK))
        for y, row in enumerate(self.matrix):
            for x, dark in enumerate(row):
                if dark:
                    painter.drawRect(left + x * module, top + y * module, module, module)


class PhoneSheet(Sheet):
    """Copy every category to the Android app: scan the QR code, or paste a code."""

    copied = Signal(str)

    def __init__(self, ctx: Ctx) -> None:
        super().__init__("Copy to your phone", primary="Done", secondary="Copy code")
        library = ctx.library
        for category in library.categories:  # so sending again later updates the phone's copies
            library.ensure_share_id(category.id)
        self.code = make_library_code(library)
        video_ids = {song.video_id for category in library.categories
                     for song in (library.song(sid) for sid in category.song_ids) if song}
        saved = {video_id: path for video_id in video_ids
                 if (path := ctx.cache.path_for(video_id)) is not None}
        self.transfer = PhoneTransfer(self.code, saved, self)
        self.transfer.sent.connect(self._sent)
        self.transfer.progressed.connect(self._show_progress)
        self._ticker = QTimer(self)  # the speed, once a second while songs are moving
        self._ticker.setInterval(1000)
        self._ticker.timeout.connect(self._show_progress)
        self._library_sent = False
        songs = sum(len(category.song_ids) for category in library.categories)
        count = len(library.categories)
        inner = z(460) - 2 * z(26)  # the prompt's width inside its margins
        qr_size, gap = z(200), z(18)
        # Beside the QR code, only the two short steps; the explanation runs full width below,
        # where it has room. (Long text squeezed next to it is what made the lines overlap.)
        row = QHBoxLayout()
        row.setSpacing(gap)
        self.qr = QrView()
        row.addWidget(self.qr, 0, Qt.AlignmentFlag.AlignTop)
        steps = QVBoxLayout()
        steps.setSpacing(z(10))
        for text in ("1. Install Tapedeck on your Android phone (the APK in your GitHub release).",
                     "2. Open it, tap Import, then Scan QR code."):
            steps.addWidget(wrapped_label(text, "SheetText", make_font(13), inner - qr_size - gap))
        steps.addStretch(1)
        row.addLayout(steps, 1)
        self.body.addLayout(row)
        about = (f"This copies your {count} categor{'y' if count == 1 else 'ies'} and "
                 f"{song_count(songs)}; categories the phone already has from here are updated "
                 "to match. ")
        if saved:
            about += (f"The {song_count(len(saved))} saved on this computer come straight from "
                      "here, much faster than YouTube, so keep this open until they're sent.")
        else:
            about += "Your phone saves the songs from YouTube."
        self.body.addWidget(wrapped_label(about, "SheetText", make_font(13), inner))
        self.status = wrapped_label("", "FieldHint", make_font(12), inner, lines=2)
        self.body.addWidget(self.status)
        if self.transfer.url:
            self.qr.set_text(self.transfer.url)
            self.status.setText("Your phone needs to be on the same Wi-Fi. If your computer asks "
                                "whether Tapedeck may use the network, allow it.")
        else:
            self.qr.hide()
            self.status.setText("This computer isn't on a network your phone can reach. Use Copy "
                                "code and paste it into the app's Import.")

    def _sent(self) -> None:
        self._library_sent = True
        self._show_progress()

    def _show_progress(self) -> None:
        transfer = self.transfer
        wanted, done = len(transfer.wanted), len(transfer.done & transfer.wanted)
        if wanted and done >= wanted:
            self._ticker.stop()
            text = f"All {song_count(wanted)} sent. You can close this."
        elif wanted:
            if not self._ticker.isActive():
                self._ticker.start()
            speed = transfer.speed
            rate = f", {speed / 1_048_576:.1f} MB/s" if speed > 0 else ""
            text = (f"Sending songs to your phone: {done} of {wanted}{rate}. "
                    "Keep this open until it's done.")
        elif self._library_sent:
            text = "Sent. The library is on your phone now."
        else:
            return
        self.status.setText(text)
        set_flag(self.status, "error", False)

    def stop(self) -> None:
        self._ticker.stop()
        self.transfer.close()

    def on_primary(self) -> None:
        self.cancelled.emit()

    def on_secondary(self) -> None:
        QApplication.clipboard().setText(self.code)
        self.copied.emit("Library code copied. Paste it into Import in the phone app.")


class ConfirmSheet(Sheet):
    confirmed = Signal()

    def __init__(self, title: str, message: str, action: str) -> None:
        super().__init__(title, primary=action)
        text = plain_label(message, "SheetText", make_font(14))
        text.setWordWrap(True)
        self.body.addWidget(text)

    def on_primary(self) -> None:
        self.confirmed.emit()


# ── Main window ──────────────────────────────────────────────────────────────
class EmptyArt(QWidget):
    """A 2×2 grid with one filled cell: the page waiting for its first category."""

    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(z(64), z(64))

    def paintEvent(self, _event) -> None:
        painter, w, h = zoomed_painter(self)
        size, gap = 26.0, 8.0
        for index, (x, y) in enumerate(((1, 1), (1 + size + gap, 1),
                                        (1, 1 + size + gap), (1 + size + gap, 1 + size + gap))):
            cell = QRectF(x, y, size, size)
            if index == 0:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(ACCENT))
            else:
                painter.setPen(QPen(QColor(HAIRLINE_HI), 1.5))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                cell.adjust(0.75, 0.75, -0.75, -0.75)
            painter.drawRoundedRect(cell, 7, 7)


class EmptyState(QWidget):
    def __init__(self) -> None:
        super().__init__()
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(z(10))
        column.addWidget(EmptyArt(), 0, Qt.AlignmentFlag.AlignHCenter)
        column.addSpacing(z(10))
        title = plain_label("Start with a category", "EmptyTitle",
                            make_font(22, QFont.Weight.DemiBold, tracking=-0.4))
        hint = plain_label("Categories hold your songs. Use the + button in the bottom "
                           "right to create one.", "EmptyHint", make_font(14))
        for label in (title, hint):
            label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            column.addWidget(label)


class ClickArea(QWidget):
    clicked = Signal()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


def page_scroller(name: str = "PageScroll", sideways: bool = True) -> QScrollArea:
    """Pages scroll sideways only when zoomed-in content is wider than the window."""
    scroller = QScrollArea()
    scroller.setObjectName(name)
    scroller.setWidgetResizable(True)
    scroller.setFrameShape(QFrame.Shape.NoFrame)
    scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded if sideways
                                          else Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroller.viewport().setObjectName(name.replace("Scroll", "Viewport"))
    scroller.viewport().setAutoFillBackground(False)
    return scroller


def size_text(size_bytes: int) -> str:
    megabytes = size_bytes / 1e6
    return f"{megabytes / 1000:.1f} GB" if megabytes >= 1000 else f"{megabytes:.0f} MB"


class CategoryPage(QWidget):
    """A category on its own page: a large heading and every song in wide rows."""

    def __init__(self, ctx: Ctx, category_id: str) -> None:
        super().__init__()
        self.ctx = ctx
        self.category_id = category_id
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroller = page_scroller()
        container = QWidget()
        container.setObjectName("PageContainer")
        container.setFocusPolicy(Qt.FocusPolicy.ClickFocus)  # clicking here closes inline edits
        column = QVBoxLayout(container)
        column.setContentsMargins(z(40), z(34), z(40), z(120))  # bottom room so + never covers a row
        column.setSpacing(0)

        header = QHBoxLayout()
        header.setContentsMargins(z(4), 0, z(12), 0)
        header.setSpacing(z(10))
        heading = QVBoxLayout()
        heading.setSpacing(z(6))
        self.title = EditableText("PageTitle", make_font(34, QFont.Weight.DemiBold, tracking=-0.8))
        self.subtitle = EditableText("PageSubtitle", make_font(15), max_length=140,
                                     allow_empty=True, hint="Double-click to edit")
        self.title.committed.connect(
            lambda text: ctx.library.rename_category(category_id, title=text))
        self.subtitle.committed.connect(
            lambda text: ctx.library.rename_category(category_id, subtitle=text))
        self.meta = plain_label(name="PageMeta", font=make_font(13, tabular=True))
        heading.addWidget(self.title)
        heading.addWidget(self.subtitle)
        heading.addSpacing(z(4))
        heading.addWidget(self.meta)
        header.addLayout(heading, 1)
        self.share_button = GlyphButton("share", size=44, radius=13, variant="outline")
        self.share_button.setToolTip("Share or export")
        self.shuffle_button = GlyphButton("shuffle", size=44, radius=13, variant="toggle")
        self.play_button = GlyphButton("play", size=44, radius=13, variant="outline")
        header.addWidget(self.share_button, 0, Qt.AlignmentFlag.AlignBottom)
        header.addWidget(self.shuffle_button, 0, Qt.AlignmentFlag.AlignBottom)
        header.addWidget(self.play_button, 0, Qt.AlignmentFlag.AlignBottom)
        column.addLayout(header)
        column.addSpacing(z(26))

        self.list = SongList(ctx, category_id, wide=True)
        column.addWidget(self.list)
        self.empty = plain_label("No songs here yet. Use the + button to add one.", "CardEmpty",
                                 make_font(14))
        self.empty.setContentsMargins(z(4), z(6), 0, 0)
        column.addWidget(self.empty)
        column.addStretch(1)
        self.scroller.setWidget(container)
        outer.addWidget(self.scroller)

        wire_song_list(self.list, ctx.library, category_id)
        self.play_button.clicked.connect(lambda: toggle_category_play(ctx.player, category_id))
        self.shuffle_button.clicked.connect(
            lambda: toggle_category_shuffle(ctx.player, category_id))
        self.share_button.clicked.connect(
            lambda: ctx.window.open_share(category_id) if ctx.window is not None else None)
        self.refresh_heading()
        self.sync()

    def refresh_heading(self) -> None:
        category = self.ctx.library.category(self.category_id)
        if category is None:
            return
        self.title.set_text(category.title)
        self.subtitle.set_text(category.subtitle)
        self.subtitle.setVisible(bool(category.subtitle))

    def sync(self) -> None:
        category = self.ctx.library.category(self.category_id)
        if category is None:
            return
        self.list.sync(list(category.song_ids))
        self.empty.setVisible(not category.song_ids)
        self.list.setVisible(bool(category.song_ids))
        self.share_button.setEnabled(bool(category.song_ids))
        self._update_meta()
        sync_category_controls(self.ctx, self.category_id, self.play_button, self.shuffle_button)

    def _update_meta(self) -> None:
        category = self.ctx.library.category(self.category_id)
        if category is not None:
            self.meta.setText(songs_summary(self.ctx.library, category))

    def song_changed(self, song_id: str) -> None:
        row = self.list.row_for(song_id)
        if row is not None:
            row.refresh()
            self._update_meta()  # a newly known length changes the total

    def thumb_ready(self, video_id: str) -> None:
        for row in self.list.rows:
            if row.video_id == video_id:
                row.load_thumb()

    def player_changed(self) -> None:
        player = self.ctx.player
        for row in self.list.rows:
            row.set_phase(player.song_phase(row.song_id))
        sync_category_controls(self.ctx, self.category_id, self.play_button, self.shuffle_button)

    def reveal(self, song_id: str) -> None:
        row = self.list.row_for(song_id)
        if row is not None:
            self.scroller.ensureWidgetVisible(row, 0, z(120))


class SideTab(QAbstractButton):
    """A sidebar entry: all categories, or one category's page."""

    def __init__(self, key: str, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.key = key
        self._text = ""
        self._count = ""
        self._activity = ""  # "", "playing" or "paused"
        self.setCheckable(True)
        self.setFixedHeight(z(38))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self._font = make_font(14, QFont.Weight.Medium)
        self._count_font = make_font(12, tabular=True)
        self.toggled.connect(lambda _on: self.update())
        self.set_text(text)

    def set_text(self, text: str) -> None:
        if text != self._text:
            self._text = text
            if self.key:
                self.setToolTip(tip(text))
            self.update()

    def set_count(self, count: str) -> None:
        if count != self._count:
            self._count = count
            self.update()

    def set_activity(self, activity: str) -> None:
        if activity != self._activity:
            self._activity = activity
            self.update()

    def sizeHint(self) -> QSize:
        return QSize(z(SIDEBAR_W), z(38))

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        painter, w, h = zoomed_painter(self)
        rect = QRectF(0, 0, w, h).adjusted(10, 1, -10, -1)
        checked, hover = self.isChecked(), self.underMouse()
        painter.setPen(Qt.PenStyle.NoPen)
        if checked or hover:
            painter.setBrush(QColor(HOVER))
            painter.drawRoundedRect(rect, 9, 9)
        if checked:
            painter.setBrush(QColor(ACCENT))
            painter.drawRoundedRect(QRectF(rect.left(), rect.center().y() - 8, 3, 16), 1.5, 1.5)
        if self.hasFocus():
            painter.setPen(QPen(QColor(WHITE), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 9, 9)
            painter.setPen(Qt.PenStyle.NoPen)
        bright = checked or hover
        icon = QPointF(rect.left() + 22, rect.center().y())
        if not self.key:  # all categories: the grid, like the main page
            painter.setBrush(QColor(WHITE if bright else SMOKE))
            for dx in (-6.2, 0.6):
                for dy in (-6.2, 0.6):
                    painter.drawRoundedRect(QRectF(icon.x() + dx, icon.y() + dy, 5.6, 5.6), 1.6, 1.6)
        elif self._activity:  # this category is playing
            painter.setBrush(QColor(ACCENT if self._activity == "playing" else SMOKE))
            left = icon.x() - 5.8
            for index, height in enumerate((7.0, 12.0, 9.0)):
                painter.drawRoundedRect(QRectF(left + index * 4.5, icon.y() + 6 - height, 2.6, height),
                                        1.0, 1.0)
        right = rect.right() - 12
        reserved = 0.0
        if self._count:
            width = text_width(self._count, self._count_font) + 2  # slack for rounding
            draw_text(painter, QRectF(right - width, rect.top(), width, rect.height()), self._count,
                      self._count_font, ASH, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            reserved = width + 8
        x = rect.left() + 40
        width = max(10.0, right - reserved - x)
        draw_text(painter, QRectF(x, rect.top(), width, rect.height()), self._text, self._font,
                  WHITE if bright else SMOKE)


class SavedStatus(QAbstractButton):
    """Sidebar footer: how many songs are saved for instant play. Click for options."""

    def __init__(self, ctx: Ctx, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setFixedHeight(z(60))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setToolTip("Saved songs start instantly. Click for options.")
        self.line1 = ""
        self.line2 = ""
        self.progress: float | None = None
        self._font1 = make_font(12, QFont.Weight.Medium)
        self._font2 = make_font(12, tabular=True)
        self.clicked.connect(self._menu)
        self.refresh()

    def counts(self) -> tuple[int, int]:
        videos = {song.video_id for song in self.ctx.library.songs.values()}
        saved = sum(1 for video_id in videos if self.ctx.cache.has(video_id))
        return saved, len(videos)

    def refresh(self) -> None:
        cache = self.ctx.cache
        saved, total = self.counts()
        self.setVisible(total > 0)
        if not cache.enabled:
            self.line1 = "Saving songs is off"
        elif saved >= total:
            self.line1 = "Ready for instant play"
        elif cache.busy():
            self.line1 = "Saving for instant play"
        else:
            self.line1 = "Some songs couldn't be saved"
        self.line2 = f"{saved} of {total} songs, {size_text(cache.size_bytes())}"
        self.progress = saved / total if cache.enabled and total and saved < total else None
        self.update()

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        painter, w, h = zoomed_painter(self)
        rect = QRectF(0, 0, w, h).adjusted(10, 4, -10, -6)
        painter.setPen(Qt.PenStyle.NoPen)
        if self.underMouse():
            painter.setBrush(QColor(HOVER))
            painter.drawRoundedRect(rect, 9, 9)
        if self.hasFocus():
            painter.setPen(QPen(QColor(WHITE), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 9, 9)
            painter.setPen(Qt.PenStyle.NoPen)
        x, width = rect.left() + 12, rect.width() - 24
        if self.progress is not None:
            bar = QRectF(x, rect.bottom() - 8, width, 2)
            painter.setBrush(QColor(HAIRLINE_HI))
            painter.drawRoundedRect(bar, 1, 1)
            painter.setBrush(QColor(ACCENT))
            painter.drawRoundedRect(QRectF(bar.left(), bar.top(), bar.width() * self.progress, 2), 1, 1)
        draw_text(painter, QRectF(x, rect.top() + 7, width, 18), self.line1, self._font1, WHITE)
        draw_text(painter, QRectF(x, rect.top() + 25, width, 18), self.line2, self._font2, ASH)

    def _menu(self) -> None:
        cache = self.ctx.cache
        menu = QMenu(self)
        toggle = menu.addAction("Save songs for instant play")
        toggle.setCheckable(True)
        toggle.setChecked(cache.enabled)
        menu.addSeparator()
        clear = menu.addAction(f"Delete saved songs ({size_text(cache.size_bytes())})")
        clear.setEnabled(bool(cache.files))
        where = self.mapToGlobal(QPoint(10, 0)) - QPoint(0, menu.sizeHint().height())
        chosen = menu.exec(where)
        if chosen == toggle:
            cache.set_enabled(not cache.enabled)
            if cache.enabled:
                cache.want_library(self.ctx.library)
        elif chosen == clear and self.ctx.window is not None:
            self.ctx.window.confirm_clear_saved()


class SideBar(QFrame):
    """Vertical tabs on the left: all categories, then a page per category."""

    selected = Signal(str)  # "" for all categories

    def __init__(self, ctx: Ctx, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setObjectName("Sidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(z(SIDEBAR_W))
        column = QVBoxLayout(self)
        column.setContentsMargins(0, z(22), 0, z(8))
        column.setSpacing(z(2))
        top = QHBoxLayout()
        top.setContentsMargins(z(24), 0, z(14), 0)
        top.addWidget(plain_label("tapedeck", "Wordmark",
                                  make_font(22, QFont.Weight.Bold, tracking=-0.6)))
        top.addStretch(1)
        self.phone_button = GlyphButton("phone", size=30, radius=9, variant="bare")
        self.phone_button.setToolTip("Copy to your phone")
        self.phone_button.clicked.connect(
            lambda: ctx.window.open_phone() if ctx.window is not None else None)
        top.addWidget(self.phone_button)
        self.settings_button = GlyphButton("settings", size=30, radius=9, variant="bare")
        self.settings_button.setToolTip("Settings  (Ctrl+,)")
        self.settings_button.clicked.connect(
            lambda: ctx.window.open_settings() if ctx.window is not None else None)
        top.addWidget(self.settings_button)
        column.addLayout(top)
        column.addSpacing(z(22))
        self.all_tab = SideTab("", "All categories")
        self.all_tab.clicked.connect(lambda _checked=False: self._clicked(""))
        column.addWidget(self.all_tab)
        column.addSpacing(z(10))
        divider = QFrame()
        divider.setObjectName("SideDivider")
        divider.setFixedHeight(z(1))
        holder = QHBoxLayout()
        holder.setContentsMargins(z(24), 0, z(24), 0)
        holder.addWidget(divider)
        column.addLayout(holder)
        column.addSpacing(z(10))
        scroller = page_scroller("SideScroll", sideways=False)
        tabs = QWidget()
        tabs.setObjectName("SideHolder")
        self._tabs = QVBoxLayout(tabs)
        self._tabs.setContentsMargins(0, 0, 0, 0)
        self._tabs.setSpacing(z(2))
        self._tabs.addStretch(1)
        scroller.setWidget(tabs)
        column.addWidget(scroller, 1)
        self.status = SavedStatus(ctx)
        column.addWidget(self.status)
        self.tabs: dict[str, SideTab] = {}
        self.current = ""
        self.rebuild()

    def rebuild(self) -> None:
        categories = self.ctx.library.categories
        alive = {category.id for category in categories}
        for category_id in list(self.tabs):
            if category_id not in alive:
                tab = self.tabs.pop(category_id)
                self._tabs.removeWidget(tab)
                tab.hide()
                tab.deleteLater()
        for tab in self.tabs.values():
            self._tabs.removeWidget(tab)
        for index, category in enumerate(categories):
            tab = self.tabs.get(category.id)
            if tab is None:
                tab = SideTab(category.id, category.title)
                tab.clicked.connect(lambda _checked=False, key=category.id: self._clicked(key))
                tab.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                tab.customContextMenuRequested.connect(lambda pos, t=tab: self._tab_menu(t, pos))
                self.tabs[category.id] = tab
            tab.set_text(category.title)
            self._tabs.insertWidget(index, tab)
            tab.show()
        self.refresh_counts()
        self.player_changed()
        self.set_current(self.current if self.current in alive else "")

    def set_current(self, key: str) -> None:
        self.current = key
        self.all_tab.setChecked(key == "")
        for category_id, tab in self.tabs.items():
            tab.setChecked(category_id == key)

    def refresh_counts(self) -> None:
        for category_id, tab in self.tabs.items():
            category = self.ctx.library.category(category_id)
            tab.set_count(str(len(category.song_ids)) if category else "")

    def player_changed(self) -> None:
        player = self.ctx.player
        playing = player.queue.category_id if player.queue is not None else ""
        activity = "paused" if player.paused else "playing"
        for category_id, tab in self.tabs.items():
            tab.set_activity(activity if category_id == playing else "")

    def _clicked(self, key: str) -> None:
        self.set_current(key)
        self.selected.emit(key)

    def _tab_menu(self, tab: SideTab, pos: QPoint) -> None:
        window = self.ctx.window
        category = self.ctx.library.category(tab.key)
        menu = QMenu(self)
        rename = menu.addAction("Rename category")
        share = menu.addAction("Share or export…")
        share.setEnabled(bool(category and category.song_ids))
        menu.addSeparator()
        delete = menu.addAction("Delete category…")
        chosen = menu.exec(tab.mapToGlobal(pos))
        if window is None:
            return
        if chosen == rename:
            window.rename_category(tab.key)
        elif chosen == share:
            window.open_share(tab.key)
        elif chosen == delete:
            window.confirm_delete_category(tab.key)


class TransportBar(QFrame):
    """Docked along the bottom: the song, transport controls, the locator and volume."""

    revealRequested = Signal()
    SLOW_AFTER = 0.35  # only say "Starting…" if starting takes longer than this

    def __init__(self, ctx: Ctx, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.video_id = ""
        self._preview: float | None = None
        self._position_value: float | None = None
        self._loading_song = ""
        self._loading_since = 0.0
        self.setObjectName("Transport")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(z(84))
        row = QHBoxLayout(self)
        row.setContentsMargins(z(14), z(10), z(22), z(10))
        row.setSpacing(z(20))

        self.info = ClickArea()
        self.info.setFixedWidth(z(300))
        self.info.setToolTip("Show this song in its category")
        info_row = QHBoxLayout(self.info)
        info_row.setContentsMargins(0, 0, 0, 0)
        info_row.setSpacing(z(12))
        self.thumb = Thumb(72, 40, 5)
        texts = QVBoxLayout()
        texts.setSpacing(z(3))
        self.title = ElidedLabel()
        self.title.setObjectName("NowTitle")
        self.title.setFont(make_font(14, QFont.Weight.Medium))
        self.status = ElidedLabel()
        self.status.setObjectName("NowStatus")
        self.status.setFont(make_font(12, tabular=True))
        texts.addStretch(1)
        texts.addWidget(self.title)
        texts.addWidget(self.status)
        texts.addStretch(1)
        info_row.addWidget(self.thumb)
        info_row.addLayout(texts, 1)
        self.info.clicked.connect(lambda: self.revealRequested.emit())
        self.info.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.info.customContextMenuRequested.connect(self._info_menu)
        row.addWidget(self.info)

        player = ctx.player
        middle = QVBoxLayout()
        middle.setSpacing(z(2))
        controls = QHBoxLayout()
        controls.setSpacing(z(16))
        self.shuffle_button = GlyphButton("shuffle", size=34, radius=17, variant="bare")
        self.prev_button = GlyphButton("prev", size=34, radius=17, variant="bare")
        self.play_button = GlyphButton("pause", size=40, radius=20, variant="primary")
        self.next_button = GlyphButton("next", size=34, radius=17, variant="bare")
        self.repeat_button = GlyphButton("repeat", size=34, radius=17, variant="bare")
        controls.addStretch(1)
        for button, tooltip, action in ((self.shuffle_button, "Shuffle", player.toggle_shuffle),
                                        (self.prev_button, "Previous", player.previous),
                                        (self.play_button, "Pause", player.toggle_pause),
                                        (self.next_button, "Next", player.next),
                                        (self.repeat_button, "Repeat this song", player.toggle_repeat)):
            button.setToolTip(tooltip)
            button.clicked.connect(lambda _checked=False, act=action: act())
            controls.addWidget(button)
        controls.addStretch(1)
        seek_row = QHBoxLayout()
        seek_row.setSpacing(z(10))
        time_font = make_font(12, tabular=True)
        time_width = QFontMetrics(time_font).horizontalAdvance("0:00:00") + z(6)
        self.elapsed = plain_label("0:00", "TimeLabel", time_font)
        self.elapsed.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.elapsed.setFixedWidth(time_width)
        self.seek = SeekBar()
        self.total = plain_label("–:––", "TimeLabel", time_font)
        self.total.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.total.setFixedWidth(time_width)
        seek_row.addWidget(self.elapsed)
        seek_row.addWidget(self.seek, 1)
        seek_row.addWidget(self.total)
        middle.addLayout(controls)
        middle.addLayout(seek_row)
        row.addLayout(middle, 1)

        right = QWidget()
        right.setFixedWidth(z(170))
        volume_row = QHBoxLayout(right)
        volume_row.setContentsMargins(0, 0, 0, 0)
        volume_row.setSpacing(z(6))
        volume_row.addStretch(1)
        self.mute_button = GlyphButton("volume-high", size=34, radius=17, variant="bare")
        self.volume = VolumeSlider()
        volume_row.addWidget(self.mute_button)
        volume_row.addWidget(self.volume)
        row.addWidget(right)

        self.seek.seekRequested.connect(player.seek)
        self.seek.previewChanged.connect(self._preview_changed)
        self.mute_button.clicked.connect(lambda: player.set_muted(not player.muted))
        self.volume.valueChanged.connect(self._volume_changed)
        player.position.connect(self.show_position)
        player.state.connect(self.state_changed)
        self._slow_timer = QTimer(self)
        self._slow_timer.setSingleShot(True)
        self._slow_timer.timeout.connect(self.refresh)
        self.refresh()
        self.state_changed()
        self.show_position(*player.timing())

    def refresh(self) -> None:
        player = self.ctx.player
        song = player.current_song if player.queue is not None else None
        active = song is not None
        if player.phase == "loading" and player.current != self._loading_song:
            self._loading_song = player.current
            self._loading_since = time.monotonic()
            self._slow_timer.start(int(self.SLOW_AFTER * 1000) + 20)
        elif player.phase != "loading":
            self._loading_song = ""
        if active:
            self.title.set_full_text(song.display_title or "Untitled video")
        else:
            self.title.set_full_text("Nothing playing")
        set_flag(self.title, "idle", not active)
        self.status.set_full_text(self.status_text())
        video_id = song.video_id if song else ""
        if video_id != self.video_id:
            self.video_id = video_id
            self.thumb.set_pixmap(self.ctx.thumbs.get(video_id) if video_id else None)
        self.info.setCursor(Qt.CursorShape.PointingHandCursor if active
                            else Qt.CursorShape.ArrowCursor)
        self.prev_button.setEnabled(active and player.has_previous())
        self.next_button.setEnabled(active and player.has_next())
        self.play_button.setEnabled(active)
        shuffling = player.queue is not None and player.queue.mode == "shuffle"
        self.shuffle_button.setEnabled(active and player.queue.mode != "single")
        self.shuffle_button.set_active(shuffling)
        self.shuffle_button.setToolTip("Turn off shuffle" if shuffling else "Shuffle this category")
        self._sync_play_button()
        if not active:
            self.show_position(None, None)

    def status_text(self) -> str:
        player = self.ctx.player
        queue = player.queue
        if queue is None:
            return ""
        if player.phase == "loading" and time.monotonic() - self._loading_since >= self.SLOW_AFTER:
            if player.engine.slow_start:
                return "Starting… a song's first play takes a few seconds"
            return "Starting…"
        if player.engine.buffering:
            return "Buffering…"
        category = self.ctx.library.category(queue.category_id)
        name = category.title if category else ""
        if queue.mode == "single":
            text = f"From {name}" if name else "Playing"
            return text + (", on repeat" if player.repeat_one else "")
        number, total = player.progress()
        text = f"{number} of {total}" + (f" in {name}" if name else "")
        text += ", shuffled" if queue.mode == "shuffle" else ""
        return text + (", on repeat" if player.repeat_one else "")

    def state_changed(self) -> None:
        player = self.ctx.player
        self.volume.set_value(player.volume, player.muted)
        if player.muted or player.volume == 0:
            kind = "volume-mute"
        else:
            kind = "volume-low" if player.volume < 50 else "volume-high"
        self.mute_button.set_kind(kind)
        self.mute_button.setToolTip("Unmute" if player.muted else "Mute")
        self.repeat_button.set_active(player.repeat_one)
        self.repeat_button.setToolTip("Stop repeating this song" if player.repeat_one
                                      else "Repeat this song")
        self._sync_play_button()
        self.status.set_full_text(self.status_text())

    def _sync_play_button(self) -> None:
        player = self.ctx.player
        resting = player.paused or player.queue is None
        self.play_button.set_kind("play" if resting else "pause")
        self.play_button.setToolTip("Play" if resting else "Pause")

    def show_position(self, seconds, total) -> None:
        player = self.ctx.player
        self._position_value = seconds
        shown = self._preview if self._preview is not None else seconds
        self.elapsed.setText(format_duration(shown) if shown is not None else "0:00")
        self.total.setText(format_duration(total) if total else "–:––")
        self.seek.set_state(seconds, total)
        self.seek.setEnabled(player.queue is not None and bool(total) and player.phase == "playing")

    def _preview_changed(self, seconds) -> None:
        self._preview = seconds
        shown = seconds if seconds is not None else self._position_value
        self.elapsed.setText(format_duration(shown) if shown is not None else "0:00")

    def _volume_changed(self, value: int) -> None:
        player = self.ctx.player
        if player.muted and value > 0:
            player.set_muted(False)
        player.set_volume(value)

    def thumb_ready(self, video_id: str) -> None:
        if video_id and video_id == self.video_id:
            self.thumb.set_pixmap(self.ctx.thumbs.get(video_id))

    def _info_menu(self, pos: QPoint) -> None:
        song = self.ctx.player.current_song
        window = self.ctx.window
        if song is None or window is None:
            return
        menu = QMenu(self)
        add = menu.addAction("Add to categories…")
        show = menu.addAction("Show in its category")
        add_link_actions(menu, self.ctx, song.video_id)
        chosen = menu.exec(self.info.mapToGlobal(pos))
        if chosen == add:
            window.open_song_categories(song.id)
        elif chosen == show:
            self.revealRequested.emit()


class Root(QWidget):
    resized = Signal()
    zoomed = Signal(int)  # Ctrl+scroll anywhere that isn't a scroll area (notches)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("Root")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self._wheel = 0  # trackpads scroll in small steps; zoom once per full notch

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.resized.emit()

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._wheel += event.angleDelta().y()
            notches = int(self._wheel / 120)
            if notches:
                self._wheel -= notches * 120
                self.zoomed.emit(notches)
            event.accept()
        else:
            super().wheelEvent(event)


class MainWindow(QMainWindow):
    def __init__(self, ctx: Ctx) -> None:
        super().__init__()
        self.ctx = ctx
        self.cards: dict[str, CategoryCard] = {}
        self.pages: dict[str, CategoryPage] = {}
        self.current_page = ""
        self._overlay: Overlay | None = None
        self._pending_zoom: float | None = None
        self._wheel = 0  # trackpads scroll in small steps; zoom once per full notch
        self.setWindowTitle(APP_NAME)
        self.resize(1320, 860)
        self.setMinimumSize(800, 560)  # content scrolls sideways if zoomed past this

        self._status_timer = QTimer(self)  # saving progress updates come in bursts
        self._status_timer.setSingleShot(True)
        self._status_timer.setInterval(250)
        self._status_timer.timeout.connect(self._refresh_saved_status)
        self._meta_timer = QTimer(self)  # song details arrive in bursts; total once per burst
        self._meta_timer.setSingleShot(True)
        self._meta_timer.setInterval(200)
        self._meta_timer.timeout.connect(self._update_home_meta)
        self._zoom_timer = QTimer(self)  # several notches in a row rebuild once
        self._zoom_timer.setSingleShot(True)
        self._zoom_timer.setInterval(90)
        self._zoom_timer.timeout.connect(self._apply_zoom)

        # Long-lived services talk to the window, which finds whichever widgets
        # are current; that way a zoom can rebuild every widget safely.
        library = ctx.library
        library.categoriesChanged.connect(self.rebuild)
        library.categorySongsChanged.connect(self._category_songs_changed)
        library.categoryRenamed.connect(self._category_renamed)
        library.songChanged.connect(self._song_changed)
        ctx.thumbs.ready.connect(self._thumb_ready)
        ctx.player.changed.connect(self._player_changed)
        ctx.player.state.connect(self._player_state)
        ctx.player.notice.connect(self._notice)
        ctx.fetcher.updated.connect(self._details_updated)
        ctx.fetcher.notice.connect(self._notice)
        ctx.cache.changed.connect(self._status_timer.start)

        for keys, action in (("Ctrl+Q", self.close), ("Space", self._toggle_pause),
                             ("Left", lambda: self._seek_by(-5.0)),
                             ("Right", lambda: self._seek_by(5.0)),
                             ("F11", self._toggle_fullscreen),
                             ("Ctrl++", lambda: self.zoom_by(1)), ("Ctrl+=", lambda: self.zoom_by(1)),
                             ("Ctrl+-", lambda: self.zoom_by(-1)), ("Ctrl+0", self.reset_zoom),
                             ("Ctrl+,", self.open_settings)):
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.activated.connect(action)

        self._build()
        self._fetch_missing_details()

    def _build(self) -> None:
        """Create every widget at the current zoom. Runs again whenever the zoom changes."""
        ctx = self.ctx
        self.cards, self.pages = {}, {}
        self.root = Root()
        outer = QVBoxLayout(self.root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self.sidebar = SideBar(ctx)
        self.sidebar.selected.connect(self.show_page)
        body.addWidget(self.sidebar)
        self.stack = QStackedWidget()
        self.stack.setObjectName("Stack")
        body.addWidget(self.stack, 1)
        outer.addLayout(body, 1)
        self.transport = TransportBar(ctx)
        self.transport.revealRequested.connect(self.reveal_playing)
        outer.addWidget(self.transport)

        # All categories: a heading and the two-column grid
        self.page = page_scroller()
        container = QWidget()
        container.setObjectName("PageContainer")
        container.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        content = QVBoxLayout(container)
        content.setContentsMargins(z(40), z(34), z(40), z(120))  # bottom room so + never covers a row
        content.setSpacing(0)
        heading = QVBoxLayout()
        heading.setContentsMargins(z(4), 0, 0, 0)
        heading.setSpacing(z(6))
        heading.addWidget(plain_label("All categories", "PageTitle",
                                      make_font(34, QFont.Weight.DemiBold, tracking=-0.8)))
        heading.addSpacing(z(4))
        self.home_meta = plain_label(name="PageMeta", font=make_font(13, tabular=True))
        heading.addWidget(self.home_meta)
        content.addLayout(heading)
        content.addSpacing(z(26))
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(z(18))
        self.grid.setVerticalSpacing(z(18))
        self.grid.setColumnStretch(0, 1)
        self.grid.setColumnStretch(1, 1)
        content.addLayout(self.grid)
        content.addStretch(1)
        self.empty = EmptyState()
        content.addWidget(self.empty)
        content.addStretch(1)
        self.page.setWidget(container)
        self.stack.addWidget(self.page)

        self.fab = FabButton(self.root)
        self.fab.clicked.connect(self.toggle_menu)
        self.toast = Toast(self.root)
        self.root.resized.connect(self._place_floating)
        self.root.zoomed.connect(self.zoom_by)
        self.stack.installEventFilter(self)
        self.setCentralWidget(self.root)  # the previous widgets, if any, are deleted
        self.rebuild()
        self.watch_wheel(self.root)

    def watch_wheel(self, widget: QWidget) -> None:
        """Ctrl+scroll over a scroll area zooms instead of scrolling it."""
        areas = widget.findChildren(QScrollArea)
        for area in areas if isinstance(areas, list) else []:
            area.viewport().installEventFilter(self)

    # Zoom
    def zoom_by(self, notches: int) -> None:
        base = self._pending_zoom if self._pending_zoom is not None else ZOOM
        self._request_zoom(base + notches * ZOOM_STEP)

    def reset_zoom(self) -> None:
        self._request_zoom(1.0)

    def _request_zoom(self, value: float) -> None:
        if self._overlay is not None or self._dragging():
            return  # never rebuild under an open prompt or a drag
        self._pending_zoom = round(min(ZOOM_MAX, max(ZOOM_MIN, value)), 2)
        self._zoom_timer.start()

    def _apply_zoom(self) -> None:
        target, self._pending_zoom = self._pending_zoom, None
        if target is None:
            return
        if abs(target - ZOOM) > 1e-6:
            set_zoom(target)
            self.ctx.settings.set("zoom", ZOOM)
            self._rebuild_ui()
            log.info("Zoom %d%%", round(ZOOM * 100))
        self.toast.show_message(f"Zoom {round(ZOOM * 100)}%")

    def _rebuild_ui(self) -> None:
        """Build every widget again (new zoom or accent), keeping the page and scroll position."""
        page, fraction = self.current_page, self._scroll_fraction()
        old = self.takeCentralWidget()
        if old is not None:  # delete the old widgets now, so restyling skips them
            old.deleteLater()
            QCoreApplication.sendPostedEvents(old, QEvent.Type.DeferredDelete)
        apply_app_style()
        self._build()
        self.show_page(page)
        later(0, lambda: self._restore_scroll(fraction))

    def _current_scroller(self) -> QScrollArea:
        page = self.pages.get(self.current_page)
        return page.scroller if page is not None else self.page

    def _scroll_fraction(self) -> float:
        bar = self._current_scroller().verticalScrollBar()
        return bar.value() / bar.maximum() if bar.maximum() > 0 else 0.0

    def _restore_scroll(self, fraction: float) -> None:
        bar = self._current_scroller().verticalScrollBar()
        bar.setValue(int(round(fraction * bar.maximum())))

    def _dragging(self) -> bool:
        return any(view.list._drag_row is not None
                   for view in (*self.cards.values(), *self.pages.values()))

    def _owns(self, watched) -> bool:
        return isinstance(watched, QWidget) and watched.window() is self

    # Layout
    def eventFilter(self, watched, event) -> bool:
        kind = event.type()
        if kind == QEvent.Type.Wheel:
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier and self._owns(watched):
                self._wheel += event.angleDelta().y()
                notches = int(self._wheel / 120)
                if notches:
                    self._wheel -= notches * 120
                    self.zoom_by(notches)
                return True  # Ctrl+scroll zooms instead of scrolling
        elif kind in (QEvent.Type.Resize, QEvent.Type.Move) and watched is getattr(self, "stack", None):
            self._place_floating()
        return False  # let everything else through untouched

    def _place_floating(self) -> None:
        """The + button and messages sit over the page area, above the transport bar."""
        area = QRect(self.stack.mapTo(self.root, QPoint(0, 0)), self.stack.size())
        margin = z(26)
        self.fab.move(area.x() + area.width() - self.fab.width() - margin,
                      area.y() + area.height() - self.fab.height() - margin)
        self.toast.area = area
        self.toast.place()

    def rebuild(self) -> None:
        """Lay the categories out two per row, in the order they were created."""
        categories = self.ctx.library.categories
        alive = {category.id for category in categories}
        for category_id in list(self.cards):
            if category_id not in alive:
                card = self.cards.pop(category_id)
                self.grid.removeWidget(card)
                card.hide()
                card.deleteLater()
        for card in self.cards.values():
            self.grid.removeWidget(card)
        for index, category in enumerate(categories):
            card = self.cards.get(category.id)
            if card is None:
                card = CategoryCard(self.ctx, category.id)
                self.cards[category.id] = card
                self.watch_wheel(card)
            self.grid.addWidget(card, index // 2, index % 2)
        for category_id in list(self.pages):
            if category_id not in alive:
                page = self.pages.pop(category_id)
                self.stack.removeWidget(page)
                page.deleteLater()
        self.empty.setVisible(not categories)
        self.sidebar.rebuild()
        self._update_home_meta()
        self.sidebar.status.refresh()
        if self.current_page and self.current_page not in alive:
            self.show_page("")

    def show_page(self, key: str) -> None:
        """"" shows all categories; a category id shows that category's own page."""
        library = self.ctx.library
        category = library.category(key) if key else None
        if category is None:
            key = ""
        if key and key not in self.pages:
            page = CategoryPage(self.ctx, key)
            self.pages[key] = page
            self.stack.addWidget(page)
            self.watch_wheel(page)
        self.stack.setCurrentWidget(self.pages[key] if key else self.page)
        self.current_page = key
        self.sidebar.set_current(key)
        if category is not None:  # its songs are the likeliest to be played next
            for song_id in category.song_ids:
                song = library.song(song_id)
                if song is not None:
                    self.ctx.cache.want(song.video_id, Priority.VISIBLE)
        QTimer.singleShot(0, self._place_floating)

    def _update_home_meta(self) -> None:
        categories = len(self.ctx.library.categories)
        songs = list(self.ctx.library.songs.values())
        runtime = runtime_text(songs)
        self.home_meta.setText(f"{categories} categor{'y' if categories == 1 else 'ies'}, "
                               f"{song_count(len(songs))}" + (f", {runtime}" if runtime else ""))

    # Library and service updates
    def _category_songs_changed(self, category_id: str) -> None:
        card = self.cards.get(category_id)
        if card is not None:
            card.sync()
        page = self.pages.get(category_id)
        if page is not None:
            page.sync()
        self.sidebar.refresh_counts()
        self._update_home_meta()
        self._status_timer.start()

    def _song_changed(self, song_id: str) -> None:
        for view in (*self.cards.values(), *self.pages.values()):
            view.song_changed(song_id)
        self._meta_timer.start()  # a length that arrived changes the overall total
        if song_id == self.ctx.player.current:
            self.transport.refresh()

    def _thumb_ready(self, video_id: str) -> None:
        for view in (*self.cards.values(), *self.pages.values()):
            view.thumb_ready(video_id)
        self.transport.thumb_ready(video_id)

    def _player_changed(self) -> None:
        for view in (*self.cards.values(), *self.pages.values()):
            view.player_changed()
        self.transport.refresh()
        self.sidebar.player_changed()

    def reveal_playing(self) -> None:
        """Clicking the song in the transport bar opens its category's page at that song."""
        player = self.ctx.player
        if player.queue is None or self.ctx.library.category(player.queue.category_id) is None:
            return
        key = player.queue.category_id
        self.show_page(key)
        page = self.pages.get(key)
        if page is not None:
            later(30, lambda p=page, s=player.current: p.reveal(s))

    def _details_updated(self, video_id: str) -> None:
        info = self.ctx.fetcher.info.get(video_id) or {}
        self.ctx.library.apply_metadata(video_id, info.get("title") or "",
                                        info.get("channel") or "", info.get("duration"))

    def _fetch_missing_details(self) -> None:
        asked: set[str] = set()
        for song in self.ctx.library.songs.values():
            if song.video_id not in asked and (not song.yt_title or song.duration is None):
                asked.add(song.video_id)
                self.ctx.fetcher.request(song.video_id)

    def _category_renamed(self, category_id: str) -> None:
        for view in (self.cards.get(category_id), self.pages.get(category_id)):
            if view is not None:
                view.refresh_heading()
        self.sidebar.rebuild()
        self.transport.refresh()  # "3 of 12 in …" names the category

    def rename_category(self, category_id: str) -> None:
        """From a menu: open the category's page with its title ready to edit."""
        if self.ctx.library.category(category_id) is None:
            return
        self.show_page(category_id)
        page = self.pages.get(category_id)
        if page is not None:
            later(0, page.title.begin)

    def _notice(self, text: str) -> None:
        self.toast.show_message(text)

    def notify(self, text: str) -> None:
        """A short message at the bottom of the window."""
        self.toast.show_message(text)

    def _player_state(self) -> None:
        self.sidebar.player_changed()

    def _refresh_saved_status(self) -> None:
        self.sidebar.status.refresh()

    # Keyboard
    def _toggle_pause(self) -> None:
        if self._overlay is None:
            self.ctx.player.toggle_pause()

    def _seek_by(self, seconds: float) -> None:
        if self._overlay is None:
            self.ctx.player.seek_by(seconds)

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    # Prompts
    def _show_overlay(self, overlay: Overlay) -> None:
        if self._overlay is not None:
            self._overlay.dismiss()
        self._overlay = overlay
        overlay.dismissed.connect(lambda o=overlay: self._overlay_closed(o))
        overlay.open()

    def _overlay_closed(self, overlay: Overlay) -> None:
        if self._overlay is overlay:
            self._overlay = None
        self.fab.set_open(False)

    def _open_sheet(self, sheet: Sheet) -> Overlay:
        overlay = Overlay(self.root)
        overlay.set_card(sheet)
        sheet.cancelled.connect(overlay.dismiss)
        self._show_overlay(overlay)
        return overlay

    def toggle_menu(self) -> None:
        if isinstance(self._overlay, MenuOverlay):
            self._overlay.dismiss()
            return
        menu = MenuOverlay(self.root, self.fab)
        menu.chosen.connect(self._menu_chosen)
        self._show_overlay(menu)
        self.fab.raise_()  # stays clickable above the dim layer, as the close button
        self.fab.set_open(True)

    def _menu_chosen(self, kind: str) -> None:
        if kind == "category":
            self.open_add_category()
        elif kind == "playlist":
            self.open_import_playlist()
        elif not self.ctx.library.categories:
            self.toast.show_message("Add a category first")
        else:
            self.open_add_song()

    def open_add_category(self) -> None:
        sheet = CategorySheet()
        overlay = self._open_sheet(sheet)

        def create(title: str, subtitle: str) -> None:
            category = self.ctx.library.add_category(title, subtitle)
            overlay.dismiss()
            card = self.cards.get(category.id)
            if card is not None and not self.current_page:
                later(60, lambda: self.page.ensureWidgetVisible(card, 0, z(40)))

        sheet.submitted.connect(create)

    def open_add_song(self) -> None:
        sheet = SongSheet(self.ctx, preselect=self.current_page)
        overlay = self._open_sheet(sheet)

        def create(video_id: str, title: str, category_ids: list) -> None:
            fetcher = self.ctx.fetcher
            info = fetcher.info.get(video_id) or {}
            self.ctx.library.add_song(
                video_id, title, list(category_ids),
                yt_title=info.get("title") or "", channel=info.get("channel") or "",
                duration=info.get("duration"))
            overlay.dismiss()
            self.ctx.cache.want(video_id, Priority.NOW)
            if not fetcher.is_pending(video_id):
                fetcher.request(video_id)  # no-op when the details are already complete

        sheet.submitted.connect(create)

    def open_settings(self) -> None:
        sheet = SettingsSheet(self.ctx)
        overlay = self._open_sheet(sheet)

        def save(color: str, saving: bool) -> None:
            overlay.dismiss()
            cache = self.ctx.cache
            if saving != cache.enabled:
                cache.set_enabled(saving)
                if saving:
                    cache.want_library(self.ctx.library)
            if color != ACCENT:  # after this click finishes: the rebuild replaces the button
                later(0, lambda: self.apply_accent(color))

        sheet.submitted.connect(save)

    def apply_accent(self, color: str) -> None:
        set_accent(color)
        self.ctx.settings.set("accent", ACCENT)
        self._rebuild_ui()
        log.info("Accent color %s", ACCENT)
        self.toast.show_message("Accent color updated")

    def open_phone(self) -> None:
        if not self.ctx.library.categories:
            self.notify("Add a category first")
            return
        sheet = PhoneSheet(self.ctx)
        overlay = self._open_sheet(sheet)
        overlay.dismissed.connect(sheet.stop)
        sheet.copied.connect(self.notify)

    def open_share(self, category_id: str) -> None:
        category = self.ctx.library.category(category_id)
        if category is None or not category.song_ids:
            return
        sheet = ShareSheet(self.ctx, category)
        overlay = self._open_sheet(sheet)

        def copied(message: str) -> None:
            overlay.dismiss()
            self.toast.show_message(message)

        sheet.copied.connect(copied)

    def open_song_categories(self, song_id: str) -> None:
        """The checkbox prompt for one song's categories (from any right-click menu)."""
        player = self.ctx.player
        song = self.ctx.library.song(song_id) or (
            player.current_song if player.current == song_id else None)
        if song is None or not self.ctx.library.categories:
            return
        sheet = SongCategoriesSheet(self.ctx, song)
        overlay = self._open_sheet(sheet)

        def save(category_ids: list) -> None:
            added, removed = self.ctx.library.set_song_categories(song, list(category_ids))
            overlay.dismiss()
            if added or removed:
                self.toast.show_message(membership_summary(self.ctx.library, added, removed))

        sheet.submitted.connect(save)

    def open_import_playlist(self) -> None:
        sheet = PlaylistSheet(self.ctx, preselect=self.current_page)
        overlay = self._open_sheet(sheet)
        overlay.dismissed.connect(sheet.stop)

        def import_songs(source, new_title: str, category_ids: list, update_id: str) -> None:
            library = self.ctx.library
            if isinstance(source, list):  # a whole library
                added = updated = 0
                for batch in source:
                    match = library.category_by_share(batch.share_id) if batch.share_id else None
                    if match is not None:
                        library.replace_category_songs(match.id, batch.entries, title=batch.title,
                                                       subtitle=batch.subtitle)
                        updated += 1
                    else:
                        created = library.add_category(batch.title, batch.subtitle)
                        created.share_id = batch.share_id
                        library.import_songs(batch.entries, [created.id])
                        added += 1
                overlay.dismiss()
                parts = ([f"added {added} categor{'y' if added == 1 else 'ies'}"] if added else []) + \
                        ([f"updated {updated}"] if updated else [])
                self.toast.show_message("Library imported: " + " and ".join(parts) + ".")
                for batch in source:
                    self._fill_in(batch.entries)
                return
            if update_id:  # a newer share code of a playlist we already have
                category = library.category(update_id)
                added, removed = library.replace_category_songs(
                    update_id, source.entries, title=source.title, subtitle=source.subtitle)
                overlay.dismiss()
                self.toast.show_message(update_summary(added, removed,
                                                       category.title if category else source.title))
                self.show_page(update_id)
                self._fill_in(source.entries)
                return
            targets = list(category_ids)
            created = None
            if new_title:
                created = library.add_category(new_title, source.subtitle)
                if source.share_id and library.category_by_share(source.share_id) is None:
                    created.share_id = source.share_id  # later codes of it update this copy
                    library.save()
                targets.insert(0, created.id)
            added = library.import_songs(source.entries, targets)
            overlay.dismiss()
            self.toast.show_message(import_summary(library, added, len(source.entries),
                                                   source.unavailable, created))
            if created is not None:
                self.show_page(created.id)
            self._fill_in(source.entries)

        sheet.submitted.connect(import_songs)

    def _fill_in(self, entries: list[dict]) -> None:
        """Save imported songs for instant play, and look up the details a source lacked."""
        known = {song.video_id: song for song in self.ctx.library.songs.values()}
        for entry in entries:  # playlist order first
            video_id = entry["video_id"]
            self.ctx.cache.want(video_id, Priority.BACKGROUND)
            song = known.get(video_id)
            if song is not None and song.yt_title and song.duration is not None:
                continue
            if not entry.get("title") or entry.get("duration") is None:
                self.ctx.fetcher.request(video_id, need_duration=entry.get("duration") is None)

    def confirm_delete_category(self, category_id: str) -> None:
        category = self.ctx.library.category(category_id)
        if category is None:
            return
        sheet = ConfirmSheet(
            "Delete this category?",
            f"“{category.title}” and its song order will be deleted. "
            "Songs that are also in other categories stay there.",
            "Delete category")
        overlay = self._open_sheet(sheet)

        def delete() -> None:
            self.ctx.library.delete_category(category_id)
            overlay.dismiss()

        sheet.confirmed.connect(delete)

    def confirm_clear_saved(self) -> None:
        cache = self.ctx.cache
        sheet = ConfirmSheet(
            "Delete saved songs?",
            f"This frees {size_text(cache.size_bytes())} and turns saving off. Songs will "
            "stream instead, so each one takes a few seconds to start. You can turn saving "
            "back on from the same menu.",
            "Delete saved songs")
        overlay = self._open_sheet(sheet)

        def clear() -> None:
            cache.set_enabled(False)
            cache.clear()
            overlay.dismiss()
            self.toast.show_message("Deleted saved songs")

        sheet.confirmed.connect(clear)

    def closeEvent(self, event) -> None:
        self.ctx.player.shutdown()
        self.ctx.library.flush()
        super().closeEvent(event)


# ── Theme ────────────────────────────────────────────────────────────────────
def stylesheet_source() -> str:
    """Written at 100% zoom; build_stylesheet() scales it."""
    return f"""
#Root {{ background: {BLACK}; }}
QToolTip {{ background: {BLACK}; color: {WHITE}; border: 1px solid {HAIRLINE_HI};
           border-radius: 6px; padding: 6px 8px; }}

#PageScroll, #PageViewport, #PageContainer,
#CardScroll, #CardViewport, #SongList,
#PickScroll, #PickViewport, #PickList {{ background: transparent; border: none; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px 1px 2px 1px; }}
QScrollBar::handle:vertical {{ background: {HAIRLINE_HI}; border-radius: 4px; min-height: 32px; }}
QScrollBar::handle:vertical:hover, QScrollBar::handle:vertical:pressed {{ background: {ACCENT}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; border: none; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 1px 2px 1px 2px; }}
QScrollBar::handle:horizontal {{ background: {HAIRLINE_HI}; border-radius: 4px; min-width: 32px; }}
QScrollBar::handle:horizontal:hover, QScrollBar::handle:horizontal:pressed {{ background: {ACCENT}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; border: none; }}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: none; }}

#Wordmark {{ color: {WHITE}; }}
#EmptyTitle {{ color: {WHITE}; }}
#EmptyHint {{ color: {SMOKE}; }}

#Card {{ background: {BLACK}; border: 1px solid {HAIRLINE}; border-radius: 14px; }}
#CardTitle {{ color: {WHITE}; }}
#CardSubtitle {{ color: {SMOKE}; }}
#CardMeta {{ color: {ASH}; }}
#CardEmpty {{ color: {ASH}; }}

#SongRow {{ background: transparent; border: 1px solid transparent; border-radius: 8px; }}
#SongRow:hover {{ background: {HOVER}; }}
#SongRow[playing="true"] {{ background: {ACCENT_WASH}; }}
#SongRow[dragging="true"] {{ background: {ACCENT_WASH}; border: 1px solid {ACCENT}; }}
#SongPos {{ color: {ASH}; }}
#SongPos[playing="true"] {{ color: {ACCENT}; }}
#SongTitle {{ color: {WHITE}; }}
#SongTitle[pending="true"] {{ color: {ASH}; }}
#SongDuration {{ color: {SMOKE}; }}

QLineEdit {{ background: {BLACK}; color: {WHITE}; border: 1px solid {HAIRLINE_HI};
            border-radius: 9px; padding: 9px 12px;
            selection-background-color: {ACCENT}; selection-color: {ON_ACCENT}; }}
QLineEdit:focus {{ border: 1px solid {ACCENT}; }}
QPlainTextEdit#PasteBox {{ background: {BLACK}; color: {WHITE}; border: 1px solid {HAIRLINE_HI};
            border-radius: 9px; padding: 5px 7px;
            selection-background-color: {ACCENT}; selection-color: {ON_ACCENT}; }}
QPlainTextEdit#PasteBox:focus {{ border: 1px solid {ACCENT}; }}
#InlineEditor {{ padding: 3px 7px; border-radius: 6px; border: 1px solid {ACCENT}; }}

#Sheet {{ background: {BLACK}; border: 1px solid {HAIRLINE_HI}; border-radius: 18px; }}
#SheetTitle {{ color: {WHITE}; }}
#SheetStep {{ color: {SMOKE}; }}
#SheetText {{ color: {SMOKE}; }}
#FieldLabel {{ color: {WHITE}; }}
#FieldOptional {{ color: {ASH}; }}
#FieldHint {{ color: {SMOKE}; }}
#FieldHint[error="true"] {{ color: {WHITE}; }}

#Preview {{ background: {BLACK}; border: 1px solid {HAIRLINE}; border-radius: 12px; }}
#PreviewTitle {{ color: {WHITE}; }}
#PreviewTitle[pending="true"] {{ color: {SMOKE}; }}
#PreviewMeta {{ color: {SMOKE}; }}

QPushButton#Primary {{ background: {ACCENT}; color: {ON_ACCENT}; border: 2px solid {ACCENT};
                      border-radius: 9px; padding: 8px 20px; min-width: 56px; }}
QPushButton#Primary:hover {{ background: {ACCENT_HI}; border-color: {ACCENT_HI}; }}
QPushButton#Primary:pressed {{ background: {ACCENT_LO}; border-color: {ACCENT_LO}; }}
QPushButton#Primary:focus {{ border-color: {WHITE}; }}
QPushButton#Primary:disabled {{ background: {ACCENT_DIM_BG}; border-color: {ACCENT_DIM_BG};
                                color: {ACCENT_DIM_FG}; }}
QPushButton#Ghost {{ background: transparent; color: {SMOKE}; border: 1px solid {HAIRLINE_HI};
                    border-radius: 9px; padding: 9px 18px; min-width: 56px; }}
QPushButton#Ghost:hover {{ color: {WHITE}; border-color: #5a5a5a; }}
QPushButton#Ghost:focus {{ color: {WHITE}; border-color: {WHITE}; }}

#MenuCard {{ background: {BLACK}; border: 1px solid {HAIRLINE_HI}; border-radius: 14px; }}
QPushButton#MenuItem {{ background: transparent; color: {WHITE}; border: 1px solid transparent;
                       border-radius: 9px; padding: 11px 12px; text-align: left; }}
QPushButton#MenuItem:hover {{ background: {HOVER}; }}
QPushButton#MenuItem:focus {{ border-color: {WHITE}; }}

#Sidebar {{ background: {BLACK}; border: none; border-right: 1px solid {HAIRLINE}; }}
#SideScroll, #SideViewport, #SideHolder, #Stack {{ background: transparent; border: none; }}
#SideDivider {{ background: {HAIRLINE}; }}
#Transport {{ background: {BLACK}; border: none; border-top: 1px solid {HAIRLINE}; }}
#PageTitle {{ color: {WHITE}; }}
#PageSubtitle {{ color: {SMOKE}; }}
#PageMeta {{ color: {ASH}; }}
#SongChannel {{ color: {SMOKE}; }}
#TimeLabel {{ color: {SMOKE}; }}
#NowTitle {{ color: {WHITE}; }}
#NowTitle[idle="true"] {{ color: {ASH}; }}
#NowStatus {{ color: {SMOKE}; }}

#Toast {{ background: {WHITE}; color: {BLACK}; border-radius: 10px; padding: 11px 18px; }}

QMenu {{ background: {BLACK}; color: {WHITE}; border: 1px solid {HAIRLINE_HI}; padding: 6px; }}
QMenu::item {{ padding: 8px 16px; border-radius: 6px; }}
QMenu::item:selected {{ background: {ACCENT}; color: {ON_ACCENT}; }}
"""


def build_stylesheet() -> str:
    def scale(match: re.Match) -> str:
        value = float(match.group(1))
        return f"{max(1, round(value * ZOOM)) if value else 0}px"
    return re.sub(r"(\d+(?:\.\d+)?)px", scale, stylesheet_source())


def apply_app_style(app: QApplication | None = None) -> None:
    app = app or QApplication.instance()
    if app is None:
        return
    app.setPalette(build_palette())
    app.setFont(make_font(13))
    app.setStyleSheet(build_stylesheet())
    app.setWindowIcon(app_icon())  # the icon wears the accent too


def build_palette() -> QPalette:
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: BLACK,
        QPalette.ColorRole.WindowText: WHITE,
        QPalette.ColorRole.Base: BLACK,
        QPalette.ColorRole.AlternateBase: HOVER,
        QPalette.ColorRole.Text: WHITE,
        QPalette.ColorRole.Button: BLACK,
        QPalette.ColorRole.ButtonText: WHITE,
        QPalette.ColorRole.Highlight: ACCENT,
        QPalette.ColorRole.HighlightedText: ON_ACCENT,
        QPalette.ColorRole.ToolTipBase: BLACK,
        QPalette.ColorRole.ToolTipText: WHITE,
        QPalette.ColorRole.PlaceholderText: ASH,
        QPalette.ColorRole.Link: ACCENT,
    }
    for role, color in roles.items():
        palette.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText,
                 QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(ASH))
    return palette


# ── Startup ──────────────────────────────────────────────────────────────────
class Ctx:
    """Shared services, handed to every widget that needs them."""

    def __init__(self) -> None:
        self.settings = Settings(SETTINGS_FILE)
        self.library = Library(LIBRARY_FILE)
        self.fetcher = MetaFetcher()
        self.thumbs = ThumbCache(THUMB_DIR)
        self.cache = AudioCache(AUDIO_DIR, self.settings)
        self.player = Player(self.library, SongEngine(self.cache, self.settings))
        self.library.videosForgotten.connect(self.cache.forget)
        self.window: MainWindow | None = None


class YtDlpUpdater(QObject):
    """Windows only: keep Tapedeck's copy of yt-dlp current, since YouTube changes often.

    (On Linux, the package manager updates yt-dlp.)
    """

    EVERY = 20 * 3600  # check about once a day

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._process: QProcess | None = None
        self.stamp = TOOLS_DIR / "last-update-check"

    def due(self) -> bool:
        try:
            return time.time() - self.stamp.stat().st_mtime > self.EVERY
        except OSError:
            return True

    def start(self) -> None:
        copy = TOOLS_DIR / "yt-dlp.exe"
        if not IS_WINDOWS or self._process is not None or not copy.is_file() or not self.due():
            return
        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.setProgram(str(copy))
        process.setArguments(["-U"])
        process.finished.connect(lambda code, _status, p=process: self._done(p, code))
        self._process = process
        log.info("Checking for a newer yt-dlp")
        process.start()

    def _done(self, process: QProcess, code: int) -> None:
        text = bytes(process.readAllStandardOutput()).decode("utf-8", "replace")
        for line in text.splitlines():
            if line.strip():
                log.info("yt-dlp update: %s", line.strip())
        process.deleteLater()
        self._process = None
        if code == 0:
            with contextlib.suppress(OSError):
                self.stamp.touch()


def set_windows_app_id() -> None:
    """Group Tapedeck's window under its own taskbar icon instead of Python's."""
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Tapedeck.Tapedeck")
    except (AttributeError, OSError):
        pass


def log_tool_versions(parent: QObject) -> None:
    """Record yt-dlp and mpv versions: the first thing to check when playback breaks."""
    for tool in ("yt-dlp", "mpv"):
        if shutil.which(tool) is None:
            log.warning("%s wasn't found. %s", tool, install_hint(tool))
            continue
        process = QProcess(parent)
        process.finished.connect(lambda _code, _status, p=process, t=tool: _log_version(p, t))
        process.start(tool, ["--version"])
    if shutil.which("deno") is None:
        log.warning("Deno wasn't found; yt-dlp needs it for YouTube. %s", install_hint("deno"))


def _log_version(process: QProcess, tool: str) -> None:
    text = bytes(process.readAllStandardOutput()).decode("utf-8", "replace").strip()
    log.info("%s %s", tool, text.splitlines()[0] if text else "(version unknown)")
    process.deleteLater()


def main() -> int:
    import signal

    debug = "--debug" in sys.argv[1:]
    setup_logging(debug)
    log.info("%s %s starting on %s. Log file: %s", APP_NAME, VERSION, sys.platform, LOG_FILE)
    setup_tools()
    set_windows_app_id()

    QApplication.setApplicationName(APP_NAME)
    QApplication.setDesktopFileName(APP_ID)
    app = QApplication([arg for arg in sys.argv if arg != "--debug"])
    app.setStyle("Fusion")

    ctx = Ctx()
    set_zoom(ctx.settings.get("zoom", 1.0))
    set_accent(ctx.settings.get("accent", DEFAULT_ACCENT))
    apply_app_style(app)
    if ctx.library.songs:  # never treat an unreadable library as "delete every saved song"
        ctx.cache.keep_only({song.video_id for song in ctx.library.songs.values()})
    window = MainWindow(ctx)
    ctx.window = window
    app.aboutToQuit.connect(ctx.player.shutdown)  # never leave mpv playing on its own
    app.aboutToQuit.connect(ctx.library.flush)

    # Ctrl+C in a terminal closes the window properly (and stops playback).
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: window.close())
    heartbeat = QTimer()
    heartbeat.timeout.connect(lambda: None)  # lets Python notice signals
    heartbeat.start(300)

    log_tool_versions(app)
    updater = YtDlpUpdater(app)
    QTimer.singleShot(1500, updater.start)
    window.show()
    ctx.player.engine.warm_up()  # mpv waits in the background, ready to play
    QTimer.singleShot(2500, lambda: ctx.cache.want_library(ctx.library))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
