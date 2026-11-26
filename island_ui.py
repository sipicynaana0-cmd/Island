import ctypes
from dataclasses import dataclass
import math
import os
import random
import re
import time
from typing import Callable, Dict, List, Optional, Tuple

from PyQt6.QtCore import (
    QMimeData,
    QPointF,
    QRect,
    QRectF,
    Qt,
    QTimer,
    QUrl,
)
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QDrag,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PyQt6.QtWidgets import QApplication, QFileDialog, QLineEdit, QWidget

from backend import (
    RUST_CORE,
    AppAudioSession,
    AudioEngine,
    ClipboardEntry,
    ConsentManager,
    GitHubUpdater,
    LiveActivitiesWatcher,
    LyricsEngine,
    MediaSessionWorker,
    SpotlightEngine,
    SystemTelemetry,
    TrackInfo,
    TrackLikesManager,
    WeatherWorker,
    boost_free_ram,
    focus_media_player_window,
    get_active_keyboard_layout,
    get_cursor_pixel_hex,
    get_foreground_app_name,
    launch_shell_target,
    lock_workstation,
    set_keep_awake,
    simulate_ctrl_v,
    sleep_workstation,
    toggle_windows_dark_theme,
    transform_text_tool,
)


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


@dataclass
class ClickRipple:
    center: QPointF
    clip_rect: QRectF
    clip_radius: float
    progress: float = 0.0
    max_radius: float = 90.0
    color: Tuple[int, int, int] = (255, 255, 255)


class SpringValue:
    """Critically-tuned analytical/semi-implicit spring oscillator for 120 FPS zero-glitch motion."""

    def __init__(self, initial: float, stiffness: float = 420.0, damping: float = 34.0):
        self.value = float(initial)
        self.target = float(initial)
        self.velocity = 0.0
        self.stiffness = stiffness
        self.damping = damping

    def set_target(
        self,
        target: float,
        stiffness: Optional[float] = None,
        damping: Optional[float] = None,
    ):
        self.target = float(target)
        if stiffness is not None:
            self.stiffness = stiffness
        if damping is not None:
            self.damping = damping

    def impulse(self, dv: float):
        self.velocity = clamp(self.velocity + dv, -420.0, 420.0)

    def step(self, dt: float) -> float:
        dt = min(dt, 0.032)
        sub_dt = dt * 0.5
        for _ in range(2):
            disp = self.value - self.target
            force = -self.stiffness * disp - self.damping * self.velocity
            self.velocity += force * sub_dt
            self.value += self.velocity * sub_dt
        if abs(self.value - self.target) < 0.04 and abs(self.velocity) < 0.22:
            self.value = self.target
            self.velocity = 0.0
        return self.value

    def is_moving(self) -> bool:
        return abs(self.value - self.target) >= 0.04 or abs(self.velocity) >= 0.22


class ScreenDimmerOverlay(QWidget):
    """Click-through fullscreen dimmer controlled from 'Управление' brightness slider."""

    def __init__(self):
        super().__init__()
        self.brightness_pct = 85
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

    def set_brightness(self, pct: int):
        self.brightness_pct = int(clamp(pct, 20, 100))
        if self.brightness_pct >= 98:
            self.hide()
        else:
            screen = QGuiApplication.primaryScreen()
            if screen:
                self.setGeometry(screen.geometry())
            self.show()
            self.raise_()
            try:
                GWL_EXSTYLE = -20
                WS_EX_LAYERED = 0x00080000
                WS_EX_TRANSPARENT = 0x00000020
                hwnd = int(self.winId())
                style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT)
            except Exception:
                pass
            self.update()

    def paintEvent(self, event):
        if self.brightness_pct >= 98:
            return
        alpha = int((100 - self.brightness_pct) * 2.1)
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, alpha))


class RegionSnipOverlay(QWidget):
    """Interactive Lightshot-style screen region selector for 'Область' quick action."""

    def __init__(self, on_captured: Callable[[QPixmap], None]):
        super().__init__()
        self.on_captured = on_captured
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setCursor(Qt.CursorShape.CrossCursor)
        self._full_pix: Optional[QPixmap] = None
        self._start_pt: Optional[QPointF] = None
        self._curr_pt: Optional[QPointF] = None

    def start_capture(self):
        screen = QGuiApplication.primaryScreen()
        if not screen:
            return
        self._full_pix = screen.grabWindow(0)
        self._start_pt = None
        self._curr_pt = None
        self.setGeometry(screen.geometry())
        self.showFullScreen()
        self.raise_()
        self.activateWindow()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.hide()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self.hide()
            return
        self._start_pt = event.position()
        self._curr_pt = event.position()
        self.update()

    def mouseMoveEvent(self, event):
        if self._start_pt is not None:
            self._curr_pt = event.position()
            self.update()

    def mouseReleaseEvent(self, event):
        if self._start_pt is not None and self._curr_pt is not None and self._full_pix is not None:
            x1 = int(min(self._start_pt.x(), self._curr_pt.x()))
            y1 = int(min(self._start_pt.y(), self._curr_pt.y()))
            x2 = int(max(self._start_pt.x(), self._curr_pt.x()))
            y2 = int(max(self._start_pt.y(), self._curr_pt.y()))
            self.hide()
            if x2 - x1 > 10 and y2 - y1 > 10:
                cropped = self._full_pix.copy(QRect(x1, y1, x2 - x1, y2 - y1))
                self.on_captured(cropped)
        else:
            self.hide()

    def paintEvent(self, event):
        if not self._full_pix:
            return
        p = QPainter(self)
        p.drawPixmap(0, 0, self._full_pix)
        p.fillRect(self.rect(), QColor(0, 0, 0, 125))
        if self._start_pt and self._curr_pt:
            r = QRectF(self._start_pt, self._curr_pt).normalized()
            p.drawPixmap(r.toRect(), self._full_pix, r.toRect())
            p.setPen(QPen(QColor(236, 72, 128), 2.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(r)


class VectorIcons:
    """100% Pure QPainterPath vector iconography. Zero emojis or dingbats."""

    @staticmethod
    def draw_home(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.42
        p.setPen(QPen(col, 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath()
        path.moveTo(cx - r, cy - r * 0.1)
        path.lineTo(cx, cy - r * 0.95)
        path.lineTo(cx + r, cy - r * 0.1)
        path.lineTo(cx + r * 0.78, cy - r * 0.1)
        path.lineTo(cx + r * 0.78, cy + r * 0.85)
        path.lineTo(cx - r * 0.78, cy + r * 0.85)
        path.lineTo(cx - r * 0.78, cy - r * 0.1)
        path.closeSubpath()
        p.drawPath(path)
        p.restore()

    @staticmethod
    def draw_clipboard(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.40
        p.setPen(QPen(col, 1.45, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(cx - r * 0.78, cy - r * 0.75, r * 1.56, r * 1.68), 2.5, 2.5)
        p.drawRoundedRect(QRectF(cx - r * 0.38, cy - r * 0.96, r * 0.76, r * 0.36), 1.2, 1.2)
        p.drawLine(QPointF(cx - r * 0.38, cy - r * 0.1), QPointF(cx + r * 0.38, cy - r * 0.1))
        p.drawLine(QPointF(cx - r * 0.38, cy + r * 0.35), QPointF(cx + r * 0.25, cy + r * 0.35))
        p.restore()

    @staticmethod
    def draw_shelf_box(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.42
        p.setPen(QPen(col, 1.45, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath()
        path.moveTo(cx - r * 0.65, cy - r * 0.75)
        path.lineTo(cx + r * 0.65, cy - r * 0.75)
        path.lineTo(cx + r * 0.95, cy + r * 0.1)
        path.lineTo(cx + r * 0.95, cy + r * 0.78)
        path.lineTo(cx - r * 0.95, cy + r * 0.78)
        path.lineTo(cx - r * 0.95, cy + r * 0.1)
        path.closeSubpath()
        p.drawPath(path)
        p.drawLine(QPointF(cx - r * 0.92, cy + r * 0.12), QPointF(cx - r * 0.35, cy + r * 0.12))
        p.drawLine(QPointF(cx + r * 0.35, cy + r * 0.12), QPointF(cx + r * 0.92, cy + r * 0.12))
        p.restore()

    @staticmethod
    def draw_text_t(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.38
        p.setPen(QPen(col, 1.55, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.drawLine(QPointF(cx - r * 0.82, cy - r * 0.78), QPointF(cx + r * 0.82, cy - r * 0.78))
        p.drawLine(QPointF(cx - r * 0.82, cy - r * 0.78), QPointF(cx - r * 0.82, cy - r * 0.48))
        p.drawLine(QPointF(cx + r * 0.82, cy - r * 0.78), QPointF(cx + r * 0.82, cy - r * 0.48))
        p.drawLine(QPointF(cx, cy - r * 0.78), QPointF(cx, cy + r * 0.82))
        p.drawLine(QPointF(cx - r * 0.35, cy + r * 0.82), QPointF(cx + r * 0.35, cy + r * 0.82))
        p.restore()

    @staticmethod
    def draw_note_edit(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.40
        p.setPen(QPen(col, 1.45, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(cx - r * 0.82, cy - r * 0.78, r * 1.45, r * 1.62), 2.5, 2.5)
        p.drawLine(QPointF(cx + r * 0.1, cy + r * 0.15), QPointF(cx + r * 0.95, cy - r * 0.70))
        p.restore()

    @staticmethod
    def draw_weather_cloud(p: QPainter, c: QPointF, s: float, col: QColor, filled: bool = False, sun_behind: bool = False):
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.40
        if sun_behind:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(250, 195, 65, 235))
            p.drawEllipse(QPointF(cx - r * 0.45, cy - r * 0.42), r * 0.46, r * 0.46)
        p.setPen(QPen(col, 1.45, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(col if filled else Qt.BrushStyle.NoBrush)
        path = QPainterPath()
        path.setFillRule(Qt.FillRule.WindingFill)
        path.addRoundedRect(QRectF(cx - r * 0.88, cy - r * 0.15, r * 1.76, r * 0.82), r * 0.4, r * 0.4)
        path.addEllipse(QPointF(cx - r * 0.22, cy - r * 0.25), r * 0.48, r * 0.48)
        path.addEllipse(QPointF(cx + r * 0.28, cy - r * 0.12), r * 0.36, r * 0.36)
        p.drawPath(path)
        p.restore()

    @staticmethod
    def draw_sliders_h(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        cx, cy = c.x(), c.y()
        w = s * 0.76
        pen = QPen(col, 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        for yo, kx in ((-0.26, -0.18), (0.0, 0.22), (0.26, -0.08)):
            y = cy + s * yo
            p.setPen(pen)
            p.drawLine(QPointF(cx - w * 0.5, y), QPointF(cx + w * 0.5, y))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(col)
            p.drawEllipse(QPointF(cx + w * kx, y), 1.9, 1.9)
        p.restore()

    @staticmethod
    def draw_pushpin(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        p.translate(c)
        p.rotate(42.0)
        r = s * 0.38
        p.setPen(QPen(col, 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(col)
        p.drawRoundedRect(QRectF(-r * 0.48, -r * 0.85, r * 0.96, r * 0.26), 1.0, 1.0)
        p.drawRect(QRectF(-r * 0.28, -r * 0.60, r * 0.56, r * 0.65))
        p.drawRoundedRect(QRectF(-r * 0.65, 0.0, r * 1.30, r * 0.24), 1.0, 1.0)
        p.drawLine(QPointF(0, r * 0.24), QPointF(0, r * 0.98))
        p.restore()

    @staticmethod
    def draw_voice_bars(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        cx, cy = c.x(), c.y()
        p.setPen(QPen(col, 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        for i, h_m in enumerate((0.22, 0.48, 0.78, 0.48, 0.22)):
            x = cx + (i - 2) * (s * 0.18)
            p.drawLine(QPointF(x, cy - s * h_m * 0.5), QPointF(x, cy + s * h_m * 0.5))
        p.restore()

    @staticmethod
    def draw_play(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        r = s * 0.5
        path = QPainterPath()
        cx, cy = c.x() + s * 0.06, c.y()
        path.moveTo(cx - r * 0.55, cy - r * 0.72)
        path.lineTo(cx + r * 0.75, cy)
        path.lineTo(cx - r * 0.55, cy + r * 0.72)
        path.closeSubpath()
        p.drawPath(path)
        p.restore()

    @staticmethod
    def draw_pause(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        bw, bh, gap = s * 0.22, s * 0.74, s * 0.15
        cx, cy = c.x(), c.y()
        p.drawRoundedRect(QRectF(cx - gap - bw, cy - bh * 0.5, bw, bh), bw * 0.4, bw * 0.4)
        p.drawRoundedRect(QRectF(cx + gap, cy - bh * 0.5, bw, bh), bw * 0.4, bw * 0.4)
        p.restore()

    @staticmethod
    def draw_prev(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.44
        p.setPen(QPen(col, 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath()
        path.moveTo(cx + r * 0.7, cy - r * 0.68)
        path.lineTo(cx - r * 0.35, cy)
        path.lineTo(cx + r * 0.7, cy + r * 0.68)
        path.closeSubpath()
        p.drawPath(path)
        p.drawLine(QPointF(cx - r * 0.62, cy - r * 0.65), QPointF(cx - r * 0.62, cy + r * 0.65))
        p.restore()

    @staticmethod
    def draw_next(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.44
        p.setPen(QPen(col, 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath()
        path.moveTo(cx - r * 0.7, cy - r * 0.68)
        path.lineTo(cx + r * 0.35, cy)
        path.lineTo(cx - r * 0.7, cy + r * 0.68)
        path.closeSubpath()
        p.drawPath(path)
        p.drawLine(QPointF(cx + r * 0.62, cy - r * 0.65), QPointF(cx + r * 0.62, cy + r * 0.65))
        p.restore()

    @staticmethod
    def draw_speaker(p: QPainter, c: QPointF, s: float, col: QColor, vol: float = 0.7, muted: bool = False):
        p.save()
        cx, cy = c.x() - s * 0.1, c.y()
        r = s * 0.42
        p.setPen(QPen(col, 1.45, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        cone = QPainterPath()
        cone.moveTo(cx - r * 0.72, cy - r * 0.32)
        cone.lineTo(cx - r * 0.30, cy - r * 0.32)
        cone.lineTo(cx + r * 0.22, cy - r * 0.76)
        cone.lineTo(cx + r * 0.22, cy + r * 0.76)
        cone.lineTo(cx - r * 0.30, cy + r * 0.32)
        cone.lineTo(cx - r * 0.72, cy + r * 0.32)
        cone.closeSubpath()
        p.drawPath(cone)
        if muted or vol <= 0.01:
            p.setPen(QPen(QColor(235, 85, 100), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(cx + r * 0.48, cy - r * 0.38), QPointF(cx + r * 1.0, cy + r * 0.38))
            p.drawLine(QPointF(cx + r * 1.0, cy - r * 0.38), QPointF(cx + r * 0.48, cy + r * 0.38))
        else:
            if vol > 0.05:
                p.drawArc(QRectF(cx + r * 0.05, cy - r * 0.42, r * 0.62, r * 0.84), -45 * 16, 90 * 16)
            if vol > 0.50:
                p.drawArc(QRectF(cx + r * 0.10, cy - r * 0.75, r * 1.0, r * 1.50), -45 * 16, 90 * 16)
        p.restore()

    @staticmethod
    def draw_speaker_box(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.42
        p.setPen(QPen(col, 1.35))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(cx - r * 0.68, cy - r * 0.90, r * 1.36, r * 1.80), 2.2, 2.2)
        p.drawEllipse(QPointF(cx, cy + r * 0.25), r * 0.36, r * 0.36)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawEllipse(QPointF(cx, cy - r * 0.42), r * 0.14, r * 0.14)
        p.restore()

    @staticmethod
    def draw_music_note_single(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.40
        p.setPen(QPen(col, 1.45, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(col)
        p.drawEllipse(QPointF(cx - r * 0.25, cy + r * 0.42), r * 0.34, r * 0.26)
        p.drawLine(QPointF(cx + r * 0.06, cy + r * 0.38), QPointF(cx + r * 0.06, cy - r * 0.68))
        p.drawLine(QPointF(cx + r * 0.06, cy - r * 0.68), QPointF(cx + r * 0.55, cy - r * 0.42))
        p.restore()

    @staticmethod
    def draw_chevron_down(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.28
        p.setPen(QPen(col, 1.45, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.drawLine(QPointF(cx - r, cy - r * 0.45), QPointF(cx, cy + r * 0.55))
        p.drawLine(QPointF(cx, cy + r * 0.55), QPointF(cx + r, cy - r * 0.45))
        p.restore()

    @staticmethod
    def draw_area_select(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.40
        p.setPen(QPen(col, 1.4, Qt.PenStyle.DashLine))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(cx - r * 0.85, cy - r * 0.75, r * 1.5, r * 1.35), 2.5, 2.5)
        p.setPen(QPen(col, 1.4))
        p.drawLine(QPointF(cx + r * 0.35, cy + r * 0.25), QPointF(cx + r * 0.88, cy + r * 0.78))
        p.restore()

    @staticmethod
    def draw_monitor(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.42
        p.setPen(QPen(col, 1.45, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(cx - r * 0.9, cy - r * 0.75, r * 1.8, r * 1.22), 2.5, 2.5)
        p.drawLine(QPointF(cx, cy + r * 0.47), QPointF(cx, cy + r * 0.80))
        p.drawLine(QPointF(cx - r * 0.45, cy + r * 0.80), QPointF(cx + r * 0.45, cy + r * 0.80))
        p.restore()

    @staticmethod
    def draw_ocr_scan(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.38
        d = r * 0.45
        p.setPen(QPen(col, 1.45, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
            x0, y0 = cx + sx * r, cy + sy * r
            p.drawLine(QPointF(x0, y0), QPointF(x0 - sx * d, y0))
            p.drawLine(QPointF(x0, y0), QPointF(x0, y0 - sy * d))
        p.drawLine(QPointF(cx - r * 0.55, cy - r * 0.2), QPointF(cx + r * 0.55, cy - r * 0.2))
        p.drawLine(QPointF(cx - r * 0.55, cy + r * 0.2), QPointF(cx + r * 0.35, cy + r * 0.2))
        p.restore()

    @staticmethod
    def draw_qr(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.38
        p.setPen(QPen(col, 1.35))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(QRectF(cx - r, cy - r, r * 0.72, r * 0.72))
        p.drawRect(QRectF(cx + r * 0.28, cy - r, r * 0.72, r * 0.72))
        p.drawRect(QRectF(cx - r, cy + r * 0.28, r * 0.72, r * 0.72))
        p.setBrush(col)
        p.drawRect(QRectF(cx + r * 0.35, cy + r * 0.35, r * 0.55, r * 0.55))
        p.restore()

    @staticmethod
    def draw_pipette(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        p.translate(c)
        p.rotate(45.0)
        r = s * 0.42
        p.setPen(QPen(col, 1.45, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(-r * 0.24, -r * 0.85, r * 0.48, r * 1.45), 2.0, 2.0)
        p.drawLine(QPointF(-r * 0.45, -r * 0.32), QPointF(r * 0.45, -r * 0.32))
        p.drawLine(QPointF(0, r * 0.60), QPointF(0, r * 0.92))
        p.restore()

    @staticmethod
    def draw_eraser(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        p.translate(c)
        p.rotate(-38.0)
        r = s * 0.38
        p.setPen(QPen(col, 1.45, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(-r * 0.85, -r * 0.45, r * 1.7, r * 0.9), 2.5, 2.5)
        p.drawLine(QPointF(-r * 0.1, -r * 0.45), QPointF(-r * 0.1, r * 0.45))
        p.restore()

    @staticmethod
    def draw_video_cam(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.40
        p.setPen(QPen(col, 1.45, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(cx - r * 0.95, cy - r * 0.55, r * 1.32, r * 1.1), 2.5, 2.5)
        path = QPainterPath()
        path.moveTo(cx + r * 0.37, cy - r * 0.22)
        path.lineTo(cx + r * 0.92, cy - r * 0.52)
        path.lineTo(cx + r * 0.92, cy + r * 0.52)
        path.lineTo(cx + r * 0.37, cy + r * 0.22)
        p.drawPath(path)
        p.restore()

    @staticmethod
    def draw_search(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.36
        p.setPen(QPen(col, 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(cx - r * 0.18, cy - r * 0.18), r * 0.68, r * 0.68)
        p.drawLine(QPointF(cx + r * 0.32, cy + r * 0.32), QPointF(cx + r * 0.88, cy + r * 0.88))
        p.restore()

    @staticmethod
    def draw_trash_bin(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.38
        p.setPen(QPen(col, 1.35, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(cx - r * 0.85, cy - r * 0.52), QPointF(cx + r * 0.85, cy - r * 0.52))
        p.drawLine(QPointF(cx - r * 0.32, cy - r * 0.78), QPointF(cx + r * 0.32, cy - r * 0.78))
        p.drawRoundedRect(QRectF(cx - r * 0.65, cy - r * 0.52, r * 1.30, r * 1.38), 2.0, 2.0)
        p.drawLine(QPointF(cx - r * 0.22, cy - r * 0.18), QPointF(cx - r * 0.22, cy + r * 0.50))
        p.drawLine(QPointF(cx + r * 0.22, cy - r * 0.18), QPointF(cx + r * 0.22, cy + r * 0.50))
        p.restore()

    @staticmethod
    def draw_drag_grip(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        cx, cy = c.x(), c.y()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        for ox in (-2.4, 2.4):
            for oy in (-4.4, 0.0, 4.4):
                p.drawEllipse(QPointF(cx + ox, cy + oy), 1.25, 1.25)
        p.restore()

    @staticmethod
    def draw_sparkle(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.42
        path = QPainterPath()
        path.moveTo(cx, cy - r)
        path.quadTo(cx, cy, cx + r, cy)
        path.quadTo(cx, cy, cx, cy + r)
        path.quadTo(cx, cy, cx - r, cy)
        path.quadTo(cx, cy, cx, cy - r)
        p.setPen(QPen(col, 1.25, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.restore()

    @staticmethod
    def draw_location_pin(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.40
        p.setPen(QPen(col, 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(cx, cy - r * 0.18), r * 0.58, r * 0.58)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawEllipse(QPointF(cx, cy - r * 0.18), r * 0.18, r * 0.18)
        p.restore()

    @staticmethod
    def draw_refresh(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.36
        p.setPen(QPen(col, 1.45, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(QRectF(cx - r, cy - r, r * 2, r * 2), 35 * 16, 285 * 16)
        p.drawLine(QPointF(cx + r * 0.78, cy - r * 0.65), QPointF(cx + r * 0.88, cy - r * 0.10))
        p.drawLine(QPointF(cx + r * 0.32, cy - r * 0.25), QPointF(cx + r * 0.88, cy - r * 0.10))
        p.restore()

    @staticmethod
    def draw_wifi(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        cx, cy = c.x(), c.y() + s * 0.18
        p.setPen(QPen(col, 1.75, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        for rad in (s * 0.24, s * 0.44, s * 0.64):
            p.drawArc(QRectF(cx - rad, cy - rad, rad * 2, rad * 2), 45 * 16, 90 * 16)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawEllipse(QPointF(cx, cy + 1.0), 1.8, 1.8)
        p.restore()

    @staticmethod
    def draw_bluetooth(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.42
        p.setPen(QPen(col, 1.65, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath()
        path.moveTo(cx - r * 0.55, cy - r * 0.45)
        path.lineTo(cx + r * 0.55, cy + r * 0.48)
        path.lineTo(cx, cy + r * 0.95)
        path.lineTo(cx, cy - r * 0.95)
        path.lineTo(cx + r * 0.55, cy - r * 0.48)
        path.lineTo(cx - r * 0.55, cy + r * 0.45)
        p.drawPath(path)
        p.restore()

    @staticmethod
    def draw_theme_contrast(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.40
        p.setPen(QPen(col, 1.65))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(c, r, r)
        half = QPainterPath()
        half.moveTo(cx, cy - r * 0.68)
        half.arcTo(QRectF(cx - r * 0.68, cy - r * 0.68, r * 1.36, r * 1.36), 90, -180)
        half.closeSubpath()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawPath(half)
        p.restore()

    @staticmethod
    def draw_battery_icon(p: QPainter, c: QPointF, s: float, col: QColor, pct: float = 0.82):
        p.save()
        cx, cy = c.x(), c.y()
        w, h = s * 0.88, s * 0.48
        p.setPen(QPen(col, 1.35))
        p.setBrush(Qt.BrushStyle.NoBrush)
        body = QRectF(cx - w * 0.52, cy - h * 0.5, w, h)
        p.drawRoundedRect(body, 2.5, 2.5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(QRectF(body.right() + 1.5, cy - h * 0.22, 2.2, h * 0.44), 1.0, 1.0)
        fill_w = max(2.0, (w - 4.0) * clamp(pct))
        p.drawRoundedRect(QRectF(body.left() + 2.0, body.top() + 2.0, fill_w, h - 4.0), 1.5, 1.5)
        p.restore()

    @staticmethod
    def draw_sun(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.22
        p.setPen(QPen(col, 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(c, r, r)
        for i in range(8):
            ang = i * (math.pi / 4.0)
            p.drawLine(
                QPointF(cx + math.cos(ang) * (r + 2.2), cy + math.sin(ang) * (r + 2.2)),
                QPointF(cx + math.cos(ang) * (r + 4.4), cy + math.sin(ang) * (r + 4.4)),
            )
        p.restore()

    @staticmethod
    def draw_moon_crescent(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.38
        p1 = QPainterPath()
        p1.addEllipse(c, r, r)
        p2 = QPainterPath()
        p2.addEllipse(QPointF(cx + r * 0.42, cy - r * 0.30), r * 0.80, r * 0.80)
        cres = p1.subtracted(p2)
        p.setPen(QPen(col, 1.35, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(cres)
        p.restore()

    @staticmethod
    def draw_padlock(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.38
        p.setPen(QPen(col, 1.45, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(cx - r * 0.78, cy - r * 0.08, r * 1.56, r * 1.05), 2.5, 2.5)
        p.drawArc(QRectF(cx - r * 0.45, cy - r * 0.82, r * 0.90, r * 1.05), 0, 180 * 16)
        p.restore()

    @staticmethod
    def draw_power_off(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.38
        p.setPen(QPen(col, 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(QRectF(cx - r, cy - r + 1.0, r * 2, r * 2), 130 * 16, 280 * 16)
        p.drawLine(QPointF(cx, cy - r * 0.95), QPointF(cx, cy - r * 0.05))
        p.restore()

    @staticmethod
    def draw_heart(p: QPainter, c: QPointF, s: float, col: QColor, filled: bool = False):
        """Pure vector Heart icon for Like (Нравится)."""
        p.save()
        cx, cy = c.x(), c.y() + s * 0.04
        r = s * 0.44
        path = QPainterPath()
        path.moveTo(cx, cy + r * 0.82)
        path.cubicTo(cx - r * 1.15, cy + r * 0.15, cx - r * 0.95, cy - r * 0.85, cx, cy - r * 0.28)
        path.cubicTo(cx + r * 0.95, cy - r * 0.85, cx + r * 1.15, cy + r * 0.15, cx, cy + r * 0.82)
        p.setPen(QPen(col, 1.45, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(col if filled else Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.restore()

    @staticmethod
    def draw_dislike(p: QPainter, c: QPointF, s: float, col: QColor, active: bool = False):
        """Pure vector Dislike (crossed heart) icon for Не нравится."""
        p.save()
        VectorIcons.draw_heart(p, c, s, col, filled=False)
        cx, cy = c.x(), c.y()
        r = s * 0.44
        p.setPen(QPen(QColor(235, 85, 100) if active else col, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(cx - r * 0.75, cy - r * 0.65), QPointF(cx + r * 0.75, cy + r * 0.65))
        p.restore()

    @staticmethod
    def draw_telegram(p: QPainter, c: QPointF, s: float, col: QColor):
        """Pure vector Telegram Paper Plane icon."""
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.46
        path = QPainterPath()
        path.moveTo(cx + r * 0.95, cy - r * 0.72)
        path.lineTo(cx - r * 0.95, cy + r * 0.04)
        path.lineTo(cx - r * 0.22, cy + r * 0.30)
        path.lineTo(cx + r * 0.56, cy - r * 0.36)
        path.lineTo(cx - r * 0.02, cy + r * 0.40)
        path.lineTo(cx + r * 0.45, cy + r * 0.80)
        path.closeSubpath()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawPath(path)
        p.restore()

    @staticmethod
    def draw_discord(p: QPainter, c: QPointF, s: float, col: QColor):
        """Pure vector Discord controller/mascot emblem."""
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.45
        body = QPainterPath()
        body.addRoundedRect(QRectF(cx - r * 0.92, cy - r * 0.58, r * 1.84, r * 1.18), r * 0.48, r * 0.48)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawPath(body)
        p.setBrush(QColor(30, 35, 65, 255))
        p.drawEllipse(QPointF(cx - r * 0.36, cy + r * 0.02), r * 0.20, r * 0.22)
        p.drawEllipse(QPointF(cx + r * 0.36, cy + r * 0.02), r * 0.20, r * 0.22)
        p.restore()

    @staticmethod
    def draw_microphone(p: QPainter, c: QPointF, s: float, col: QColor):
        """Pure vector Condenser Microphone icon."""
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.42
        p.setPen(QPen(col, 1.45, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(cx - r * 0.36, cy - r * 0.92, r * 0.72, r * 1.18), r * 0.36, r * 0.36)
        p.drawArc(QRectF(cx - r * 0.68, cy - r * 0.45, r * 1.36, r * 1.18), 180 * 16, 180 * 16)
        p.drawLine(QPointF(cx, cy + r * 0.73), QPointF(cx, cy + r * 1.0))
        p.drawLine(QPointF(cx - r * 0.38, cy + r * 1.0), QPointF(cx + r * 0.38, cy + r * 1.0))
        p.restore()

    @staticmethod
    def draw_headphones(p: QPainter, c: QPointF, s: float, col: QColor):
        """Pure vector Over-Ear Headphones icon."""
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.42
        p.setPen(QPen(col, 1.55, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(QRectF(cx - r * 0.85, cy - r * 0.85, r * 1.7, r * 1.6), 0, 180 * 16)
        p.setBrush(col)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(cx - r * 0.98, cy - r * 0.02, r * 0.42, r * 0.85), 2.2, 2.2)
        p.drawRoundedRect(QRectF(cx + r * 0.56, cy - r * 0.02, r * 0.42, r * 0.85), 2.2, 2.2)
        p.restore()

    @staticmethod
    def draw_lightning(p: QPainter, c: QPointF, s: float, col: QColor):
        """Pure vector Lightning Bolt icon for RAM Boost (Буст ОЗУ)."""
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.46
        path = QPainterPath()
        path.moveTo(cx + r * 0.14, cy - r * 0.95)
        path.lineTo(cx - r * 0.62, cy + r * 0.10)
        path.lineTo(cx - r * 0.04, cy + r * 0.10)
        path.lineTo(cx - r * 0.16, cy + r * 0.95)
        path.lineTo(cx + r * 0.62, cy - r * 0.12)
        path.lineTo(cx + r * 0.04, cy - r * 0.12)
        path.closeSubpath()
        p.setPen(QPen(col, 1.1, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(col)
        p.drawPath(path)
        p.restore()

    @staticmethod
    def draw_sparkle(p: QPainter, c: QPointF, s: float, col: QColor):
        """Pure vector 4-point Sparkle icon for Clean Text (Очистка текста)."""
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.46
        path = QPainterPath()
        path.moveTo(cx, cy - r)
        path.quadTo(cx, cy, cx + r, cy)
        path.quadTo(cx, cy, cx, cy + r)
        path.quadTo(cx, cy, cx - r, cy)
        path.quadTo(cx, cy, cx, cy - r)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawPath(path)
        p.drawEllipse(QPointF(cx + r * 0.68, cy - r * 0.62), 1.4, 1.4)
        p.restore()

    @staticmethod
    def draw_eyes_mini(p: QPainter, c: QPointF, s: float, col: QColor):
        """Pure vector twin-eye glyph for Character toggle in header."""
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.36
        p.setPen(QPen(col, 1.35))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(cx - r * 0.62, cy), r * 0.44, r * 0.62)
        p.drawEllipse(QPointF(cx + r * 0.62, cy), r * 0.44, r * 0.62)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawEllipse(QPointF(cx - r * 0.54, cy), r * 0.22, r * 0.28)
        p.drawEllipse(QPointF(cx + r * 0.70, cy), r * 0.22, r * 0.28)
        p.restore()

    @staticmethod
    def draw_chevron_up(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.34
        p.setPen(QPen(col, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.drawLine(QPointF(cx - r, cy + r * 0.45), QPointF(cx, cy - r * 0.45))
        p.drawLine(QPointF(cx, cy - r * 0.45), QPointF(cx + r, cy + r * 0.45))
        p.restore()

    @staticmethod
    def draw_download_arrow(p: QPainter, c: QPointF, s: float, col: QColor):
        """Pure vector download/update arrow into tray icon."""
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.42
        p.setPen(QPen(col, 1.55, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.drawLine(QPointF(cx, cy - r * 0.85), QPointF(cx, cy + r * 0.35))
        p.drawLine(QPointF(cx - r * 0.45, cy - r * 0.05), QPointF(cx, cy + r * 0.42))
        p.drawLine(QPointF(cx + r * 0.45, cy - r * 0.05), QPointF(cx, cy + r * 0.42))
        p.drawLine(QPointF(cx - r * 0.72, cy + r * 0.82), QPointF(cx + r * 0.72, cy + r * 0.82))
        p.restore()

    @staticmethod
    def draw_gear(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.36
        p.setPen(QPen(col, 1.55, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(c, r * 0.56, r * 0.56)
        for ti in range(8):
            a = ti * (math.pi / 4.0)
            p.drawLine(
                QPointF(cx + math.cos(a) * r * 0.72, cy + math.sin(a) * r * 0.72),
                QPointF(cx + math.cos(a) * r * 1.12, cy + math.sin(a) * r * 1.12),
            )
        p.restore()

    @staticmethod
    def draw_scissors(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.42
        p.setPen(QPen(col, 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(cx - r * 0.55, cy + r * 0.50), r * 0.32, r * 0.32)
        p.drawEllipse(QPointF(cx + r * 0.55, cy + r * 0.50), r * 0.32, r * 0.32)
        p.drawLine(QPointF(cx - r * 0.35, cy + r * 0.25), QPointF(cx + r * 0.55, cy - r * 0.72))
        p.drawLine(QPointF(cx + r * 0.35, cy + r * 0.25), QPointF(cx - r * 0.55, cy - r * 0.72))
        p.restore()

    @staticmethod
    def draw_calc(p: QPainter, c: QPointF, s: float, col: QColor):
        p.save()
        cx, cy = c.x(), c.y()
        r = s * 0.42
        p.setPen(QPen(col, 1.45, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(cx - r * 0.78, cy - r * 0.88, r * 1.56, r * 1.76), 2.8, 2.8)
        p.drawLine(QPointF(cx - r * 0.48, cy - r * 0.42), QPointF(cx + r * 0.48, cy - r * 0.42))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        for gx in (-0.36, 0.0, 0.36):
            for gy in (0.04, 0.46):
                p.drawEllipse(QPointF(cx + gx * r, cy + gy * r), 1.35, 1.35)
        p.restore()


def peak_from_bands(bands: List[float]) -> float:
    """Kick-drum energy from the lowest spectrum bands (0..1)."""
    if not bands:
        return 0.0
    low = bands[:5]
    return max(low) * 0.6 + (sum(low) / len(low)) * 0.4


def make_rounded_pixmap(img_bytes: Optional[bytes], size: int, radius: int) -> Optional[QPixmap]:
    if not img_bytes:
        return None
    img = QImage.fromData(img_bytes)
    if img.isNull():
        return None
    scaled = img.scaled(
        size,
        size,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    x = max(0, (scaled.width() - size) // 2)
    y = max(0, (scaled.height() - size) // 2)
    cropped = scaled.copy(x, y, size, size)

    out = QPixmap(size, size)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, size, size), radius, radius)
    p.setClipPath(path)
    p.drawImage(0, 0, cropped)
    p.end()
    return out


def format_time(sec: float) -> str:
    if sec <= 0 or math.isnan(sec):
        return "0:00"
    s = int(sec)
    return f"{s // 60}:{s % 60:02d}"


class DynamicIslandWidget(QWidget):
    """
    Ultra-Smooth 120 FPS Ferrofluid Dynamic Island for Windows.
    - Direct A->B tab cross-fade/slide transitions (never flies through intermediate tabs!).
    - Every button in the Player Card & Volume Bar is 100% interactive and functional.
    - Redesigned iOS-18 / Apple Weather experience with city pills, Bezier hourly curve, and high-detail gauges.
    """

    MAX_CANVAS_W = 800
    MAX_CANVAS_H = 510
    EXPANDED_W = 696.0
    EXPANDED_H = 428.0

    def __init__(self, media: MediaSessionWorker, audio: AudioEngine, telemetry: SystemTelemetry):
        super().__init__()
        self.media = media
        self.audio = audio
        self.telemetry = telemetry
        self.weather_worker = WeatherWorker()
        self.likes_mgr = TrackLikesManager()
        self.consent_mgr = ConsentManager()
        self.updater = GitHubUpdater()
        self.lyrics_engine = LyricsEngine()
        self.live_watcher = LiveActivitiesWatcher()
        self.spotlight = SpotlightEngine()
        self.show_lyrics_view: bool = False
        self._last_lyric_idx: int = -2
        self._cached_lyric_line: str = ""
        self.sp_lyric_slide = SpringValue(0.0, stiffness=380.0, damping=34.0)
        self.legal_doc_mode: str = ""  # "", "privacy", "consent"
        self._exe_icon_cache: Dict[str, Optional[QPixmap]] = {}
        self._suppress_hover_until: float = 0.0
        self._notif_hovered: bool = False
        self._active_download: Optional[Dict[str, object]] = None
        self.spotlight_open: bool = False
        self._spotlight_results: List[Dict[str, str]] = []
        self._spotlight_sel: int = 0
        self._spot_input = QLineEdit(self)
        self._spot_input.setPlaceholderText("Поиск программ, 2450*0.85, 100$, yt ...")
        self._spot_input.setStyleSheet(
            "QLineEdit { background: transparent; color: #F8FAFC; border: none; "
            "font-family: 'Segoe UI Variable Display'; font-size: 14px; font-weight: 600; "
            "selection-background-color: #EC4880; }"
        )
        self._spot_input.hide()
        self._spot_input.textChanged.connect(self._on_spotlight_text_changed)
        self._spot_input.returnPressed.connect(self._execute_spotlight_selected)
        self._spot_input.installEventFilter(self)
        self.dimmer_overlay = ScreenDimmerOverlay()
        self.snip_overlay = RegionSnipOverlay(self._on_region_captured)
        self.standby_win = self.snip_overlay

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)

        self._screen_w = 1920
        self._screen_h = 1080
        self._reposition_canvas()

        # Core Capsule Springs (Critically damped: 2*sqrt(420) ~= 41.0 -> zero overshoot jitter, locked phase!)
        self.sp_w = SpringValue(210.0, stiffness=420.0, damping=41.0)
        self.sp_h = SpringValue(36.0, stiffness=420.0, damping=41.0)
        self.sp_r = SpringValue(18.0, stiffness=420.0, damping=41.0)
        self.sp_expand = SpringValue(0.0, stiffness=420.0, damping=41.0)
        self.sp_osd = SpringValue(0.0, stiffness=440.0, damping=42.0)
        self.sp_cascade = SpringValue(0.0, stiffness=400.0, damping=40.0)
        self.sp_top_y = SpringValue(9.0 if self.consent_mgr.island_float_mode == 1 else 0.0, stiffness=400.0, damping=40.0)
        self._notif_subtitle: str = ""
        self._notif_app: str = ""
        self._notif_target: str = ""
        self._osd_started_at: float = 0.0
        self._osd_duration: float = 1.5

        # Game Auto-Hide + Arrow Pull-Tab + 3-Stage Reveal ("Точка -> 2 глазика -> Остров")
        self.game_hide_state: str = "revealing"  # "visible", "hidden_arrow", "revealing"
        self._reveal_started_at: float = time.monotonic()
        self._reveal_duration: float = 1.72
        self._game_summoned_override: bool = False
        self._last_game_fs: bool = False
        self._arrow_hovered: bool = False
        self._dl_sparks: List[List[float]] = [
            [random.uniform(0.0, 1.0), random.uniform(-6.0, 6.0), random.uniform(1.0, 2.4), random.uniform(0.6, 1.4)]
            for _ in range(14)
        ]

        # Ferrofluid magnetic bottom pull & smooth cursor tracking
        self.sp_ferro_pull = SpringValue(0.0, stiffness=340.0, damping=26.0)
        self.sp_ferro_x = SpringValue(self.MAX_CANVAS_W * 0.5, stiffness=360.0, damping=28.0)
        self.sp_mx = SpringValue(self.MAX_CANVAS_W * 0.5, stiffness=420.0, damping=32.0)
        self.sp_my = SpringValue(120.0, stiffness=420.0, damping=32.0)

        # Spring-inertia eye gaze tracking
        self.sp_eye_dx = SpringValue(0.0, stiffness=280.0, damping=24.0)
        self.sp_eye_dy = SpringValue(0.0, stiffness=280.0, damping=24.0)

        # Direct A->B Tab Transition (Eliminates flying across intermediate tabs!)
        self.prev_tab = 0
        self.active_tab = 0
        self.sp_tab_trans = SpringValue(1.0, stiffness=420.0, damping=34.0)
        self.tab_open_sp: List[SpringValue] = [
            SpringValue(1.0 if i == 0 else 0.0, stiffness=460.0, damping=34.0) for i in range(8)
        ]
        self.is_updating_collapse = False
        self.sp_update_prog = SpringValue(0.0, stiffness=180.0, damping=26.0)

        self.character_mode = 1  # Default: Living Expressive Eyes (1: Глазки, 2: Котик, 0: Погода)
        self._blink_timer = 0.0
        self._is_blinking = False
        self._eye_emotion: str = "neutral"  # neutral, happy, sleepy, surprised, vibe, curious
        self._eye_happy_until: float = 0.0
        self._eye_surprised_until: float = 0.0
        self._pet_last_x: float = 0.0
        self._pet_dir: int = 0
        self._pet_flips: int = 0
        self._pet_last_ts: float = 0.0
        self._last_clip_seq: int = RUST_CORE.clipboard_seq()

        # Player & Volume Bar Functional Modes (Screenshot 2 upgrades!)
        self.visualizer_mode = 0  # 0: Круговой спектр + Аврора, 1: Живая Волна, 2: Винил
        self.vol_control_mode = 0  # 0: Общий звук системы (🔊), 1: Звук только плеера (♪)
        self.audio_fx_mode = 0  # 0: Динамики, 1: Лимитер, 2: Ночной (45%)
        self._vinyl_angle = 0.0

        # Smooth Album Accent Color Springs
        self.sp_r1 = SpringValue(236.0, stiffness=180.0, damping=26.0)
        self.sp_g1 = SpringValue(72.0, stiffness=180.0, damping=26.0)
        self.sp_b1 = SpringValue(128.0, stiffness=180.0, damping=26.0)

        # Hover & Press Springs + Click Ripple Shockwaves
        self.btn_hover_sp: Dict[str, SpringValue] = {}
        self.btn_press_sp: Dict[str, SpringValue] = {}
        self._hovered_key: str = ""
        self._ripples: List[ClickRipple] = []

        # Clipboard History (Tab 1: Буфер)
        self.clipboard_history: List[ClipboardEntry] = [
            ClipboardEntry("https://github.com/exuupery/island", "chrome", time.time() - 60, pinned=True),
            ClipboardEntry("#EC4880", "Figma", time.time() - 360),
            ClipboardEntry("Dynamic Island для Windows — клик вставляет запись прямо в окно", "Telegram", time.time() - 900),
        ]
        self._last_clip_text = ""
        try:
            cb = QApplication.clipboard()
            cb.dataChanged.connect(self._on_clipboard_changed)
        except Exception:
            pass

        # Shelf Files (Tab 2: Полка)
        self.shelf_files: List[Tuple[str, str]] = []

        # Text Tab working text (Tab 3: Текст)
        self.working_text: str = "ghbdtn! Этот текст набрали не в той раскладке,  с лишними   пробелами\nи рваными\nпереносами."

        # Note & Timer Tab (Tab 4: Заметка)
        self.note_category = 0
        self.note_items: List[List[Tuple[str, bool]]] = [
            [
                ("Мини-плеер и жидкий край в пилюле", True),
                ("Проверить буфер обмена и авто-вставку", False),
                ("Настроить быстрые ярлыки на Главной", False),
                ("Не забыть: протестировать раскладку в Тексте", False),
            ],
            [
                ("Добавить свои ярлыки через кнопку +", False),
                ("Попробовать персонажей: Глазки и Котик", True),
            ],
            [
                ("Подписка / игры в Steam", False),
                ("Новый SSD диск", False),
            ],
        ]
        self.timer_total_sec: float = 25 * 60.0
        self.timer_remaining_sec: float = 25 * 60.0
        self.timer_running: bool = False
        self.timer_label: str = "Отчёт"
        self._timer_last_tick: float = time.monotonic()

        # Controls Tab states (Tab 6: Управление)
        self.wifi_enabled = True
        self.bt_enabled = True
        self.keep_awake = False
        self._power_confirm_key = ""
        self._power_confirm_until = 0.0
        self.show_inline_mixer = False

        # App Launcher Dock items on Home tab (defaults + persisted custom user apps)
        default_launchers: List[Tuple[str, str, str]] = [
            ("Проводник", "explorer.exe", "folder"),
            ("Блокнот", "notepad.exe", "notepad"),
            ("PowerShell", "powershell.exe", "ps"),
            ("Диспетчер", "taskmgr.exe", "taskmgr"),
            ("Ножницы", "snippingtool.exe", "snip"),
            ("Калькулятор", "calc.exe", "calc"),
            ("Параметры", "ms-settings:", "settings"),
        ]
        if self.consent_mgr.custom_launchers:
            self.launcher_items: List[Tuple[str, str, str]] = [
                (it[0], it[1], it[2]) for it in self.consent_mgr.custom_launchers
            ]
        else:
            self.launcher_items = default_launchers

        # Interaction states
        self.is_hovered = False
        self.is_pinned_open = False
        self.is_drag_hover = False
        self.game_mode_locked = False
        self._hover_collapse_deadline = 0.0

        # Continuous phase clocks
        self._wave_phase = 0.0
        self._aurora_phase = 0.0
        self._marquee_phase = 0.0
        self._snow_particles: List[List[float]] = [
            [random.uniform(0, 1), random.uniform(0, 1), random.uniform(1.5, 3.4), random.uniform(0.14, 0.42)]
            for _ in range(22)
        ]

        self._last_cover_key = ""
        self._cover_hi_pix: Optional[QPixmap] = None

        self._last_kb_layout = get_active_keyboard_layout()
        self._last_master_vol, self._last_master_mute = self.audio.get_master_volume()
        self._osd_type = ""
        self._osd_text = ""
        self._osd_value = 0.0
        self._osd_until = 0.0
        self.fn_color_scanning_until = 0.0
        self.fn_picked_hex = "#EC4880"
        self.fn_picked_rgb = (236, 72, 128)

        self._app_sessions: List[AppAudioSession] = []

        self._hitboxes: Dict[str, QRectF] = {}
        self._dragging_vol_slider = False
        self._dragging_prog_slider = False
        self._scrub_preview_sec: Optional[float] = None
        self._dragging_bright_slider = False
        self._dragging_app_slider: Optional[str] = None

        # Auto-adaptive native monitor refresh rate loop via Rust Core (60..240 Hz!)
        self._display_hz: int = RUST_CORE.get_refresh_rate_hz()
        frame_ms = max(4, min(16, int(round(1000.0 / max(60, self._display_hz)))))
        self._last_frame_time = time.monotonic()
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(frame_ms)
        self._anim_timer.timeout.connect(self._on_anim_frame)

        # Fast 30ms watcher
        self._watch_timer = QTimer(self)
        self._watch_timer.setInterval(30)
        self._watch_timer.timeout.connect(self._on_fast_watch)
        self._watch_timer.start()

        self.sp_w.value = 130.0
        self.sp_h.value = 24.0
        self._update_target_dimensions()
        self._anim_timer.start()
        QTimer.singleShot(120, self._force_topmost)

    def _get_real_exe_pixmap(self, path: str) -> Optional[QPixmap]:
        """Extracts & caches real Windows .exe/.lnk icon using native Rust Win32 extractor."""
        if not path:
            return None
        if path in self._exe_icon_cache:
            return self._exe_icon_cache[path]
        pix = None
        try:
            rgba = RUST_CORE.extract_file_icon_rgba(path, 48)
            if rgba and len(rgba) == 48 * 48 * 4:
                img = QImage(rgba, 48, 48, 48 * 4, QImage.Format.Format_RGBA8888).copy()
                if not img.isNull():
                    pix = QPixmap.fromImage(img)
        except Exception:
            pix = None
        self._exe_icon_cache[path] = pix
        return pix

    def _open_spotlight(self):
        self.spotlight_open = True
        self.game_hide_state = "visible"
        self.is_hovered = False
        self.is_pinned_open = False
        self._osd_until = 0.0
        self._spotlight_sel = 0
        self._spot_input.setText("")
        self._spotlight_results = self.spotlight.search("")
        self._update_target_dimensions()
        self._ensure_anim_running()
        # Allow keyboard focus inside the Island while Spotlight is open
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
        self.activateWindow()
        self.raise_()
        spot_x = int((self.MAX_CANVAS_W - 540.0) * 0.5) + 46
        self._spot_input.setGeometry(spot_x, 30, 440, 32)
        self._spot_input.show()
        self._spot_input.setFocus()
        self.update()

    def _close_spotlight(self):
        if not self.spotlight_open:
            return
        self.spotlight_open = False
        self._spot_input.hide()
        self._spot_input.clearFocus()
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self._update_target_dimensions()
        self._ensure_anim_running()
        self.update()

    def _on_spotlight_text_changed(self, text: str):
        self._spotlight_results = self.spotlight.search(text)
        self._spotlight_sel = 0
        self.update()

    def _execute_spotlight_item(self, idx: int):
        if not (0 <= idx < len(self._spotlight_results)):
            self._close_spotlight()
            return
        item = self._spotlight_results[idx]
        act = item.get("action", "")
        self._close_spotlight()
        if act.startswith("copy:"):
            val = act[5:]
            QApplication.clipboard().setText(val)
            self._trigger_osd("drop", f"Скопировано: {val}", duration=1.5)
        elif act:
            launch_shell_target(act)

    def _execute_spotlight_selected(self):
        self._execute_spotlight_item(self._spotlight_sel)

    def eventFilter(self, obj, event):
        if obj is self._spot_input and event.type() == event.Type.KeyPress:
            k = event.key()
            if k == Qt.Key.Key_Escape:
                self._close_spotlight()
                return True
            elif k == Qt.Key.Key_Down and self._spotlight_results:
                self._spotlight_sel = (self._spotlight_sel + 1) % len(self._spotlight_results)
                self.update()
                return True
            elif k == Qt.Key.Key_Up and self._spotlight_results:
                self._spotlight_sel = (self._spotlight_sel - 1) % len(self._spotlight_results)
                self.update()
                return True
        return super().eventFilter(obj, event)

    def _reposition_canvas(self):
        screen = QGuiApplication.primaryScreen()
        if screen:
            geo = screen.geometry()
            self._screen_w = geo.width()
            self._screen_h = geo.height()
            x = geo.x() + (self._screen_w - self.MAX_CANVAS_W) // 2
            y = geo.y()
            self.setGeometry(x, y, self.MAX_CANVAS_W, self.MAX_CANVAS_H)

    def _force_topmost(self):
        try:
            hwnd_val = int(self.winId())
            self.telemetry.set_own_hwnd(hwnd_val)
            HWND_TOPMOST = -1
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOACTIVATE = 0x0010
            SWP_SHOWWINDOW = 0x0040
            ctypes.windll.user32.SetWindowPos(
                hwnd_val,
                HWND_TOPMOST,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
            )
        except Exception:
            pass

    def _on_clipboard_changed(self):
        try:
            txt = (QApplication.clipboard().text() or "").strip()
            if txt and txt != self._last_clip_text and len(txt) < 10000:
                self._last_clip_text = txt
                self.working_text = txt
                app_name = get_foreground_app_name()
                self.clipboard_history = [e for e in self.clipboard_history if e.text != txt]
                self.clipboard_history.insert(0, ClipboardEntry(txt, app_name, time.time()))
                self.clipboard_history = self.clipboard_history[:25]
        except Exception:
            pass

    def _on_region_captured(self, pix: QPixmap):
        try:
            QApplication.clipboard().setPixmap(pix)
            pics_dir = os.path.join(os.path.expanduser("~"), "Pictures", "Screenshots")
            os.makedirs(pics_dir, exist_ok=True)
            fname = os.path.join(pics_dir, f"Island_Area_{time.strftime('%Y%m%d_%H%M%S')}.png")
            pix.save(fname, "PNG")
            self.shelf_files.insert(0, (fname, time.strftime("%H:%M")))
            self._trigger_osd("drop", "Область скопирована в буфер и на полку", duration=1.6)
        except Exception:
            pass

    def _hide_to_game_arrow(self):
        self.game_hide_state = "hidden_arrow"
        self.is_hovered = False
        self.is_pinned_open = False
        self._game_summoned_override = False
        self._hovered_key = ""
        self._update_target_dimensions()
        self._ensure_anim_running()

    def _start_eyes_reveal(self):
        self.game_hide_state = "revealing"
        self._reveal_started_at = time.monotonic()
        self.is_hovered = False
        self.is_pinned_open = False
        self.sp_w.value = 22.0
        self.sp_h.value = 22.0
        self.sp_r.value = 11.0
        self.sp_top_y.value = -10.0
        self.sp_expand.value = 0.0
        self._update_target_dimensions()
        self._ensure_anim_running()

    def current_capsule_rect(self) -> QRectF:
        cw = max(18.0, self.sp_w.value)
        ch = max(14.0, self.sp_h.value)
        cx = (self.MAX_CANVAS_W - cw) / 2.0
        ty = self.sp_top_y.value
        return QRectF(cx, ty, cw, ch)

    def _trigger_osd(
        self,
        osd_type: str,
        text: str = "",
        value: float = 0.0,
        duration: float = 1.45,
        subtitle: str = "",
        app_name: str = "",
        target_kw: str = "",
    ):
        now = time.monotonic()
        self._osd_type = osd_type
        self._osd_text = text
        self._osd_value = value
        self._notif_subtitle = subtitle
        self._notif_app = app_name
        self._notif_target = target_kw
        self._osd_started_at = now
        self._osd_duration = max(0.3, duration)
        self._osd_until = now + duration
        if osd_type == "notif":
            self._eye_surprised_until = now + 1.8
        self._update_target_dimensions()
        self._ensure_anim_running()

    def _ensure_anim_running(self):
        if not self._anim_timer.isActive():
            self._last_frame_time = time.monotonic()
            self._anim_timer.start()

    def _switch_to_tab(self, new_tab: int):
        if new_tab != self.active_tab:
            self.prev_tab = self.active_tab
            self.active_tab = new_tab
            self.sp_tab_trans.value = 0.0
            self.sp_tab_trans.set_target(1.0)
            self.sp_cascade.value = 0.22

    def _update_target_dimensions(self):
        now = time.monotonic()
        track, _ = self.media.get_snapshot()
        is_music_playing = bool(track.is_playing)
        updating_now = self.is_updating_collapse or self.updater.status in ("downloading", "updated")
        if self.updater.status == "error":
            self.is_updating_collapse = False
            updating_now = False

        base_top = 9.0 if self.consent_mgr.island_float_mode == 1 else 0.0

        # Mode 0: Spotlight Search Bar (Alt + Space!)
        if self.spotlight_open and not updating_now:
            n_res = max(1, len(self._spotlight_results))
            spot_h = 66.0 + n_res * 48.0 + 12.0
            self.sp_osd.set_target(0.0)
            self.sp_cascade.set_target(0.0)
            self.sp_expand.set_target(0.0)
            self.sp_top_y.set_target(18.0, stiffness=420.0, damping=40.0)
            self.sp_w.set_target(540.0, stiffness=440.0, damping=41.0)
            self.sp_h.set_target(spot_h, stiffness=440.0, damping=41.0)
            self.sp_r.set_target(22.0)
            return

        # Mode A: Hidden in Fullscreen Game -> Tiny Top Arrow Pull-Tab (v)
        if self.game_hide_state == "hidden_arrow" and not updating_now:
            self.sp_osd.set_target(0.0)
            self.sp_cascade.set_target(0.0)
            self.sp_expand.set_target(0.0)
            self.sp_top_y.set_target(0.0, stiffness=380.0, damping=38.0)
            self.sp_w.set_target(52.0 if self._arrow_hovered else 44.0, stiffness=440.0, damping=40.0)
            self.sp_h.set_target(20.0 if self._arrow_hovered else 16.0, stiffness=440.0, damping=40.0)
            self.sp_r.set_target(10.0)
            return

        # Mode B: 3-Stage Signature Reveal Animation ("Точка -> 2 глазика -> Появление Острова")
        if self.game_hide_state == "revealing" and not updating_now:
            el = now - self._reveal_started_at
            self.sp_osd.set_target(0.0)
            self.sp_cascade.set_target(0.0)
            self.sp_expand.set_target(0.0)
            if el < 0.42:
                # Stage 1: Liquid Black Dot descends from top bezel
                self.sp_w.set_target(28.0, stiffness=360.0, damping=34.0)
                self.sp_h.set_target(28.0, stiffness=360.0, damping=34.0)
                self.sp_r.set_target(14.0)
                self.sp_top_y.set_target(max(8.0, base_top), stiffness=300.0, damping=30.0)
            elif el < 1.24:
                # Stage 2: Dot stretches into sleek pill and 2 big living eyes open, glance & blink!
                self.sp_w.set_target(116.0, stiffness=340.0, damping=32.0)
                self.sp_h.set_target(38.0, stiffness=340.0, damping=32.0)
                self.sp_r.set_target(19.0)
                self.sp_top_y.set_target(max(4.0, base_top), stiffness=340.0, damping=34.0)
            elif el < self._reveal_duration:
                # Stage 3: Morph into full live Dynamic Island capsule!
                tgt_w = 318.0 if is_music_playing else 218.0
                self.sp_w.set_target(tgt_w, stiffness=360.0, damping=38.0)
                self.sp_h.set_target(36.0, stiffness=360.0, damping=38.0)
                self.sp_r.set_target(18.0)
                self.sp_top_y.set_target(base_top, stiffness=360.0, damping=36.0)
            else:
                self.game_hide_state = "visible"
            return

        expanded = (
            (self.is_hovered or self.is_pinned_open or self.is_drag_hover)
            and not self.game_mode_locked
            and not updating_now
        )
        osd_active = (now < self._osd_until) and not expanded and not updating_now

        self.sp_osd.set_target(1.0 if osd_active else 0.0)
        self.sp_cascade.set_target(1.0 if expanded else 0.0)
        self.sp_top_y.set_target(base_top)

        for i in range(8):
            self.tab_open_sp[i].set_target(1.0 if i == self.active_tab else 0.0)

        if updating_now:
            self.sp_w.set_target(384.0, stiffness=400.0, damping=40.0)
            self.sp_h.set_target(58.0, stiffness=400.0, damping=40.0)
            self.sp_r.set_target(24.0)
            self.sp_expand.set_target(0.0)
        elif expanded:
            self.sp_w.set_target(self.EXPANDED_W, stiffness=420.0, damping=41.0)
            self.sp_h.set_target(self.EXPANDED_H, stiffness=420.0, damping=41.0)
            self.sp_r.set_target(28.0)
            self.sp_expand.set_target(1.0)
        elif osd_active:
            if self._osd_type in ("notif", "usb", "usb_out"):
                self.sp_w.set_target(392.0, stiffness=420.0, damping=41.0)
                self.sp_h.set_target(60.0, stiffness=420.0, damping=41.0)
                self.sp_r.set_target(24.0)
            else:
                self.sp_w.set_target(286.0, stiffness=420.0, damping=41.0)
                self.sp_h.set_target(40.0, stiffness=420.0, damping=41.0)
                self.sp_r.set_target(20.0)
            self.sp_expand.set_target(0.0)
        elif self._active_download is not None:
            # Feature #11 & #3: Live Steam / Browser Download Capsule
            self.sp_w.set_target(374.0, stiffness=400.0, damping=40.0)
            self.sp_h.set_target(54.0, stiffness=400.0, damping=40.0)
            self.sp_r.set_target(22.0)
            self.sp_expand.set_target(0.0)
        elif is_music_playing:
            pill_w = 388.0 if (self.show_lyrics_view and bool(self._cached_lyric_line)) else 318.0
            self.sp_w.set_target(pill_w, stiffness=420.0, damping=41.0)
            self.sp_h.set_target(36.0, stiffness=420.0, damping=41.0)
            self.sp_r.set_target(18.0)
            self.sp_expand.set_target(0.0)
        elif self.timer_running:
            self.sp_w.set_target(256.0, stiffness=420.0, damping=41.0)
            self.sp_h.set_target(36.0, stiffness=420.0, damping=41.0)
            self.sp_r.set_target(18.0)
            self.sp_expand.set_target(0.0)
        else:
            # Idle OR Paused Music -> Sleek Pill with Living Expressive 2 Eyes!
            self.sp_w.set_target(218.0, stiffness=420.0, damping=41.0)
            self.sp_h.set_target(36.0, stiffness=420.0, damping=41.0)
            self.sp_r.set_target(18.0)
            self.sp_expand.set_target(0.0)

    def _on_fast_watch(self):
        now = time.monotonic()

        # Native Rust Global Hotkeys (Ctrl+Shift+I: Eyes reveal | Alt+M: Mute | Alt+Space: Spotlight!)
        hk = RUST_CORE.poll_hotkeys()
        if hk & 1:
            if self.spotlight_open:
                self._close_spotlight()
            elif self.game_hide_state == "hidden_arrow":
                self._game_summoned_override = True
                self._start_eyes_reveal()
            else:
                self._hide_to_game_arrow()
        if hk & 2:
            self.audio.toggle_master_mute()
            vol, muted = self.audio.get_master_volume()
            self._last_master_vol, self._last_master_mute = vol, muted
            self._trigger_osd("vol", "Mute" if muted else f"{int(vol)}%", vol, duration=1.35)
        if hk & 4:
            if self.spotlight_open:
                self._close_spotlight()
            else:
                self._open_spotlight()

        # Live Activities (Steam/Browser Downloads, CapsLock, USB Drives via Rust!)
        self._active_download = self.live_watcher.get_download()
        live_ev = self.live_watcher.pop_event()
        if live_ev and not self.spotlight_open:
            ev_t = live_ev.get("type", "")
            if ev_t == "caps":
                self._trigger_osd("caps", live_ev.get("title", "CAPS LOCK"), duration=1.35)
            elif ev_t in ("usb", "usb_out"):
                self._trigger_osd(
                    ev_t,
                    live_ev.get("title", "USB"),
                    duration=3.6,
                    subtitle=live_ev.get("subtitle", ""),
                    app_name="USB",
                    target_kw=live_ev.get("target", ""),
                )

        # Zero-cost 1ns Rust Win32 clipboard sequence check
        c_seq = RUST_CORE.clipboard_seq()
        if c_seq != self._last_clip_seq:
            self._last_clip_seq = c_seq
            self._on_clipboard_changed()

        # Automatic Fullscreen Game Hide / Return (Rust Win32 Detector!)
        in_game = self.telemetry.is_game_fullscreen
        if in_game and not self._last_game_fs:
            self._game_summoned_override = False
            if self.game_hide_state == "visible" and not self.is_updating_collapse:
                self._hide_to_game_arrow()
        elif not in_game and self._last_game_fs:
            self._game_summoned_override = False
            if self.game_hide_state == "hidden_arrow":
                self._start_eyes_reveal()
        self._last_game_fs = in_game

        local_pos = QPointF(self.mapFromGlobal(QCursor.pos()))
        cap = self.current_capsule_rect()

        # Petting gesture detection over top capsule ('поглаживание' -> счастливые глазки ^ ^!)
        if cap.adjusted(-16, -6, 16, 22).contains(local_pos):
            dx_pet = local_pos.x() - self._pet_last_x
            if abs(dx_pet) > 9.0:
                new_dir = 1 if dx_pet > 0 else -1
                if self._pet_dir != 0 and new_dir != self._pet_dir and (now - self._pet_last_ts) < 0.75:
                    self._pet_flips += 1
                    if self._pet_flips >= 3:
                        self._eye_happy_until = now + 2.4
                        self._pet_flips = 0
                elif (now - self._pet_last_ts) > 0.9:
                    self._pet_flips = 0
                self._pet_dir = new_dir
                self._pet_last_x = local_pos.x()
                self._pet_last_ts = now
        else:
            if (now - self._pet_last_ts) > 1.0:
                self._pet_flips = 0

        if self.game_hide_state == "hidden_arrow":
            arrow_hit = cap.adjusted(-10.0, -4.0, 10.0, 8.0)
            self._arrow_hovered = arrow_hit.contains(local_pos)
            self.sp_ferro_pull.set_target(0.0)
            self._update_target_dimensions()
            self._ensure_anim_running()
            return

        # Prevent hover from hijacking active Notification/USB capsules, Spotlight, or Eyes Reveal!
        interactive_notif_active = (
            now < self._osd_until
            and self._osd_type in ("notif", "usb", "usb_out")
            and not self.is_hovered
            and not self.is_pinned_open
        )
        if interactive_notif_active:
            over_notif = cap.adjusted(-6.0, -4.0, 6.0, 8.0).contains(local_pos)
            self._notif_hovered = over_notif
            if over_notif:
                # Pause auto-dismiss while hovering so the user can easily click 'Открыть'!
                self._osd_until = max(self._osd_until, now + 0.65)
            self.sp_ferro_pull.set_target(0.0)
        else:
            self._notif_hovered = False

        if (
            self.game_hide_state == "visible"
            and not self.game_mode_locked
            and not self.is_updating_collapse
            and not self.spotlight_open
            and not interactive_notif_active
            and now >= self._suppress_hover_until
            and self.updater.status not in ("downloading", "updated")
        ):
            if not self.is_hovered and not self.is_pinned_open:
                dx = abs(local_pos.x() - cap.center().x())
                dy = local_pos.y() - cap.bottom()
                if dx < cap.width() * 0.65 and 0.0 <= dy < 50.0:
                    pull = (1.0 - dy / 50.0) * 8.5
                    self.sp_ferro_pull.set_target(pull)
                    self.sp_ferro_x.set_target(clamp(local_pos.x(), cap.left() + 28, cap.right() - 28))
                else:
                    self.sp_ferro_pull.set_target(0.0)
            else:
                self.sp_ferro_pull.set_target(0.0)

            if self.is_hovered:
                exp_x = (self.MAX_CANVAS_W - self.EXPANDED_W) * 0.5
                hit_zone = QRectF(exp_x - 14.0, -4.0, self.EXPANDED_W + 28.0, max(cap.height(), self.EXPANDED_H) + 22.0)
            else:
                hit_zone = cap.adjusted(-6.0, -2.0, 6.0, 7.0)

            inside = hit_zone.contains(local_pos)
            if inside and not self.is_hovered:
                self.is_hovered = True
                self._hover_collapse_deadline = 0.0
                self.sp_cascade.value = 0.0
                self._app_sessions = self.audio.get_app_sessions()
                self._update_target_dimensions()
            elif (
                not inside
                and self.is_hovered
                and not self._dragging_vol_slider
                and not self._dragging_bright_slider
                and not self._dragging_app_slider
            ):
                if self._hover_collapse_deadline == 0.0:
                    self._hover_collapse_deadline = now + 0.22
                elif now >= self._hover_collapse_deadline:
                    self.is_hovered = False
                    self._hover_collapse_deadline = 0.0
                    self._hovered_key = ""
                    self._update_target_dimensions()

        if now < self.fn_color_scanning_until:
            hex_c, rgb_c = get_cursor_pixel_hex()
            self.fn_picked_hex = hex_c
            self.fn_picked_rgb = rgb_c
            if now + 0.04 >= self.fn_color_scanning_until:
                QApplication.clipboard().setText(hex_c)
                self._trigger_osd("drop", f"Пипетка: {hex_c} в буфере", duration=1.6)

        if self.timer_running:
            dt_t = now - self._timer_last_tick
            self._timer_last_tick = now
            self.timer_remaining_sec = max(0.0, self.timer_remaining_sec - dt_t)
            if self.timer_remaining_sec <= 0.0:
                self.timer_running = False
                self._trigger_osd("drop", f"Таймер «{self.timer_label}» завершён", duration=2.5)
        else:
            self._timer_last_tick = now

        kb = get_active_keyboard_layout()
        if kb != self._last_kb_layout:
            self._last_kb_layout = kb
            label = "Русская" if kb == "RU" else "English" if kb == "EN" else kb
            self._trigger_osd("lang", label, duration=1.25)

        # Check for live incoming Telegram / Discord / Microphone / Headphone notifications (Items 1 & 2)
        new_notif = self.telemetry.pop_notification()
        if new_notif:
            self._trigger_osd(
                "notif",
                new_notif.get("title", "Уведомление"),
                duration=3.2,
                subtitle=new_notif.get("subtitle", ""),
                app_name=new_notif.get("app", ""),
                target_kw=new_notif.get("target", ""),
            )

        if not self._dragging_vol_slider:
            vol, muted = self.audio.get_master_volume()
            if abs(vol - self._last_master_vol) >= 1.0 or muted != self._last_master_mute:
                self._last_master_vol = vol
                self._last_master_mute = muted
                if not self.is_hovered and not self.is_pinned_open:
                    self._trigger_osd("vol", "Mute" if muted else f"{int(vol)}%", vol, duration=1.35)

        self._ensure_anim_running()

    def _get_btn_springs(self, key: str) -> Tuple[SpringValue, SpringValue]:
        if key not in self.btn_hover_sp:
            self.btn_hover_sp[key] = SpringValue(0.0, stiffness=500.0, damping=32.0)
            self.btn_press_sp[key] = SpringValue(0.0, stiffness=640.0, damping=28.0)
        return self.btn_hover_sp[key], self.btn_press_sp[key]

    def _on_anim_frame(self):
        now = time.monotonic()
        dt = now - self._last_frame_time
        self._last_frame_time = now

        track, pos_s = self.media.get_snapshot()

        # Update Live Synced Lyrics index (ONLY genuine synced LRC — never fake-scroll plain lyrics!)
        if track.title and track.is_playing and self.consent_mgr.lyrics_enabled:
            _, curr_l, _, l_st, l_idx = self.lyrics_engine.get_live_state(
                track.artist, track.title, pos_s, track.duration
            )
            self._cached_lyric_line = curr_l if (l_st == "synced" and curr_l and curr_l != "· · ·") else ""
            if l_idx != self._last_lyric_idx:
                self._last_lyric_idx = l_idx
                self.sp_lyric_slide.value = 1.0
                self.sp_lyric_slide.set_target(0.0)
        else:
            self._cached_lyric_line = ""

        self._update_target_dimensions()

        local_pos = QPointF(self.mapFromGlobal(QCursor.pos()))
        self.sp_mx.set_target(local_pos.x())
        self.sp_my.set_target(local_pos.y())

        cap = self.current_capsule_rect()
        eye_origin = QPointF(cap.left() + 26.0, cap.height() * 0.48)
        if self.is_drag_hover:
            # Look eagerly down-center toward the dragged file
            self.sp_eye_dx.set_target(1.8)
            self.sp_eye_dy.set_target(2.1)
        else:
            edx = local_pos.x() - eye_origin.x()
            edy = local_pos.y() - eye_origin.y()
            edist = max(1.0, math.hypot(edx, edy))
            self.sp_eye_dx.set_target((edx / edist) * min(2.6, edist * 0.015))
            self.sp_eye_dy.set_target((edy / edist) * min(1.9, edist * 0.015))

        # Ensure monochrome covers stay vibrant neon coral-rose
        tr, tg, tb = track.accent_color
        if max(tr, tg, tb) - min(tr, tg, tb) < 42:
            tr, tg, tb = 236, 72, 128
        self.sp_r1.set_target(float(tr))
        self.sp_g1.set_target(float(tg))
        self.sp_b1.set_target(float(tb))
        self.sp_update_prog.set_target(self.updater.progress)

        new_hover = ""
        if self.sp_expand.value > 0.35:
            for k, rect in self._hitboxes.items():
                if rect.contains(local_pos):
                    new_hover = k
                    break
        self._hovered_key = new_hover

        all_springs = [
            self.sp_mx, self.sp_my,
            self.sp_eye_dx, self.sp_eye_dy,
            self.sp_r1, self.sp_g1, self.sp_b1,
            self.sp_w, self.sp_h, self.sp_r,
            self.sp_expand, self.sp_osd, self.sp_cascade,
            self.sp_top_y, self.sp_ferro_pull, self.sp_ferro_x,
            self.sp_tab_trans, self.sp_update_prog, self.sp_lyric_slide,
        ]
        all_springs.extend(self.tab_open_sp)
        for k in list(self.btn_hover_sp.keys()):
            h_sp, p_sp = self.btn_hover_sp[k], self.btn_press_sp[k]
            h_sp.set_target(1.0 if k == self._hovered_key else 0.0)
            p_sp.set_target(0.0)
            all_springs.append(h_sp)
            all_springs.append(p_sp)

        # Single batched native Rust spring physics call (< 20 us for ALL springs at 60..240 Hz!)
        if not RUST_CORE.step_springs_batch(all_springs, dt):
            for sp in all_springs:
                sp.step(dt)

        # Compute Living Eye Emotion State Machine
        pk_now = self.audio._smooth_peak
        hour_now = time.localtime().tm_hour
        if now < self._eye_happy_until:
            self._eye_emotion = "happy"
        elif now < self._eye_surprised_until:
            self._eye_emotion = "surprised"
        elif self.is_drag_hover:
            self._eye_emotion = "curious"
        elif track.is_playing and pk_now > 0.58:
            self._eye_emotion = "vibe"
        elif hour_now >= 23 or hour_now < 6:
            self._eye_emotion = "sleepy"
        else:
            self._eye_emotion = "neutral"

        alive_ripples: List[ClickRipple] = []
        for rp in self._ripples:
            rp.progress += dt * 2.5
            if rp.progress < 1.0:
                alive_ripples.append(rp)
        self._ripples = alive_ripples

        blink_interval = 5.4 if self._eye_emotion == "sleepy" else 3.6
        self._blink_timer += dt
        if self._blink_timer > blink_interval:
            self._is_blinking = True
            if self._blink_timer > blink_interval + (0.24 if self._eye_emotion == "sleepy" else 0.14):
                self._is_blinking = False
                self._blink_timer = random.uniform(-0.8, 0.4)

        self._wave_phase = (self._wave_phase + dt * (1.2 + pk_now * 18.0 if track.is_playing else 0.0)) % (math.pi * 40.0)
        self._aurora_phase = (self._aurora_phase + dt * (0.8 + pk_now * 2.4 if track.is_playing else 0.45)) % (math.pi * 20.0)
        if track.is_playing:
            self._vinyl_angle = (self._vinyl_angle + 88.0 * dt) % 360.0
        if track.title:
            self._marquee_phase += dt * 28.0

        if self.active_tab == 5 and self.sp_expand.value > 0.4:
            for sp in self._snow_particles:
                sp[1] = (sp[1] + sp[3] * dt) % 1.0
                sp[0] = (sp[0] + math.sin(self._wave_phase * 0.5 + sp[1] * 6.0) * 0.04 * dt) % 1.0

        if self.sp_expand.value > 0.3:
            self._app_sessions = self.audio.get_app_sessions(trigger_poll=self.show_inline_mixer)

        self.update()

    # --- Drag & Drop (Tab 2: Полка) ---
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.is_drag_hover = True
            self._switch_to_tab(2)
            self._update_target_dimensions()
            self._ensure_anim_running()

    def dragLeaveEvent(self, event):
        self.is_drag_hover = False
        self._update_target_dimensions()
        self._ensure_anim_running()

    def dropEvent(self, event):
        self.is_drag_hover = False
        existing_paths = {p for p, _ in self.shelf_files}
        added = 0
        t_now = time.strftime("%H:%M")
        for url in event.mimeData().urls():
            local_f = url.toLocalFile()
            if local_f and os.path.exists(local_f) and local_f not in existing_paths:
                self.shelf_files.insert(0, (local_f, t_now))
                added += 1
        self.shelf_files = self.shelf_files[:8]
        if added:
            self._trigger_osd("drop", f"На полке: {len(self.shelf_files)} файл(ов)", duration=1.5)
        self._update_target_dimensions()
        self._ensure_anim_running()

    # --- Mouse & Wheel Interactions ---
    def wheelEvent(self, event):
        delta = 4.0 if event.angleDelta().y() > 0 else -4.0
        if self.vol_control_mode == 1:
            player_sess = self._find_player_audio_session()
            if player_sess:
                new_v = clamp(player_sess.volume + delta / 100.0)
                self.audio.set_app_volume(player_sess.name, new_v)
                self.update()
                return
        vol, _ = self.audio.get_master_volume()
        new_vol = clamp(vol + delta, 0.0, 100.0)
        self.audio.set_master_volume(new_vol)
        self._last_master_vol = new_vol
        if self.sp_expand.value < 0.4:
            self._trigger_osd("vol", f"{int(new_vol)}%", new_vol, duration=1.25)
        self.update()

    def _find_player_audio_session(self) -> Optional[AppAudioSession]:
        track, _ = self.media.get_snapshot()
        src = (track.source_app or "").lower()
        for s in self._app_sessions:
            sn = s.name.lower()
            if (src and sn in src) or any(k in sn for k in ("яндекс", "yandex", "spotify", "chrome", "msedge", "aimp", "vlc")):
                return s
        return self._app_sessions[0] if self._app_sessions else None

    def mousePressEvent(self, event):
        pos = event.position()
        if event.button() == Qt.MouseButton.RightButton:
            # Right-clicking an app icon in the Smart Dock removes it safely
            if self.sp_expand.value >= 0.45 and self.active_tab == 0:
                for k, rect in list(self._hitboxes.items()):
                    if k.startswith("launch_") and k != "launch_add" and rect.contains(pos):
                        try:
                            idx = int(k.split("_")[1])
                            if 0 <= idx < len(self.launcher_items) and len(self.launcher_items) > 1:
                                removed_name = self.launcher_items[idx][0]
                                self.launcher_items.pop(idx)
                                self.consent_mgr.custom_launchers = [list(it) for it in self.launcher_items]
                                self.consent_mgr.save_all()
                                self._trigger_osd("drop", f"Убрано: {removed_name}", duration=1.35)
                                self.update()
                                return
                        except Exception:
                            pass
            self.is_pinned_open = not self.is_pinned_open
            self._update_target_dimensions()
            self._ensure_anim_running()
            return

        if event.button() != Qt.MouseButton.LeftButton:
            return

        # Spotlight click handling (execute clicked row, or close when clicking outside)
        if self.spotlight_open:
            cap = self.current_capsule_rect()
            for key, rect in list(self._hitboxes.items()):
                if key.startswith("spot_row_") and rect.contains(pos):
                    self._execute_spotlight_item(int(key.split("_")[-1]))
                    return
            if not cap.contains(pos):
                self._close_spotlight()
            return

        if self.sp_expand.value < 0.45:
            if self.game_hide_state == "hidden_arrow":
                self._game_summoned_override = True
                self._start_eyes_reveal()
                return
            if self.game_hide_state == "revealing":
                return
            if time.monotonic() < self._osd_until and self._osd_type in ("notif", "usb", "usb_out"):
                target = self._notif_target
                osd_t = self._osd_type
                self._osd_until = 0.0
                self._notif_hovered = False
                if target:
                    if osd_t == "usb":
                        launch_shell_target(target)
                    else:
                        focus_media_player_window(target)
                self._update_target_dimensions()
                self._ensure_anim_running()
                return
            self.is_pinned_open = True
            self.is_hovered = True
            self.sp_cascade.value = 0.0
            self._update_target_dimensions()
            self._ensure_anim_running()
            return

        for key, rect in list(self._hitboxes.items()):
            if rect.contains(pos):
                _, p_sp = self._get_btn_springs(key)
                p_sp.value = 1.0
                r1 = int(clamp(self.sp_r1.value, 90, 255))
                g1 = int(clamp(self.sp_g1.value, 70, 255))
                b1 = int(clamp(self.sp_b1.value, 85, 255))
                self._ripples.append(
                    ClickRipple(
                        center=pos,
                        clip_rect=QRectF(rect),
                        clip_radius=min(16.0, min(rect.width(), rect.height()) * 0.45),
                        max_radius=max(rect.width(), rect.height()) * 1.15,
                        color=(r1, g1, b1),
                    )
                )
                self._handle_hitbox_click(key, pos, rect)
                self._ensure_anim_running()
                self.update()
                return

    def _handle_hitbox_click(self, key: str, pos: QPointF, rect: QRectF):
        if key.startswith("tab_"):
            self._switch_to_tab(int(key.split("_")[1]))
        elif key == "top_char":
            self.character_mode = (self.character_mode + 1) % 3
        elif key == "top_pin":
            self.is_pinned_open = not self.is_pinned_open
        elif key in ("top_mixer", "btn_inline_mixer", "btn_close_mixer"):
            self.show_inline_mixer = not self.show_inline_mixer
            self.audio.request_sessions_refresh()
            self._app_sessions = self.audio.get_app_sessions()
        elif key == "btn_vis_mode":
            self.visualizer_mode = (self.visualizer_mode + 1) % 3
        elif key == "btn_lyrics_toggle":
            self.show_lyrics_view = not self.show_lyrics_view
            self.consent_mgr.lyrics_enabled = True
            self.consent_mgr.save_all()
        elif key == "btn_lyr_minus":
            self.lyrics_engine.sync_offset_sec = round(max(-5.0, self.lyrics_engine.sync_offset_sec - 0.5), 1)
        elif key == "btn_lyr_plus":
            self.lyrics_engine.sync_offset_sec = round(min(5.0, self.lyrics_engine.sync_offset_sec + 0.5), 1)
        elif key == "btn_open_spotlight":
            QTimer.singleShot(20, self._open_spotlight)
        elif key == "btn_focus_player":
            track, _ = self.media.get_snapshot()
            focus_media_player_window(track.source_app)
        elif key == "btn_seek_back":
            _, pos_s = self.media.get_snapshot()
            self.media.seek_to(max(0.0, pos_s - 10.0))
        elif key == "btn_seek_fwd":
            track, pos_s = self.media.get_snapshot()
            self.media.seek_to(min(track.duration, pos_s + 10.0))
        elif key == "btn_vol_mode_sys":
            self.vol_control_mode = 0
        elif key == "btn_vol_mode_app":
            self.vol_control_mode = 1
            self.audio.request_sessions_refresh()
        elif key == "btn_audio_fx":
            self.audio_fx_mode = (self.audio_fx_mode + 1) % 3
            if self.audio_fx_mode == 2 and self._last_master_vol > 45:
                self.audio.set_master_volume(45.0)
                self._last_master_vol = 45.0
        elif key == "btn_prev":
            self.media.prev_track()
        elif key == "btn_play":
            self.media.play_pause()
        elif key == "btn_next":
            self.media.next_track()
        elif key == "btn_mute":
            if self.vol_control_mode == 1:
                ps = self._find_player_audio_session()
                if ps:
                    self.audio.toggle_app_mute(ps.name)
                    return
            self.audio.toggle_master_mute()
            self._last_master_vol, self._last_master_mute = self.audio.get_master_volume()
        elif key == "slider_vol":
            self._dragging_vol_slider = True
            self._set_vol_from_mouse(pos.x(), rect)
        elif key == "slider_prog":
            track, _ = self.media.get_snapshot()
            if track.duration > 0:
                self._dragging_prog_slider = True
                ratio = clamp((pos.x() - rect.left()) / max(1.0, rect.width()))
                self._scrub_preview_sec = ratio * track.duration
                self.media.seek_to(self._scrub_preview_sec)
        elif key == "btn_like":
            track, _ = self.media.get_snapshot()
            liked = self.likes_mgr.toggle_like(track.title, track.artist)
            self._trigger_osd("drop", "Добавлено в Любимое" if liked else "Убрано из Любимого", duration=1.35)
        elif key == "btn_dislike":
            track, _ = self.media.get_snapshot()
            self.likes_mgr.toggle_dislike(track.title, track.artist)
            self.media.next_track()
            self._trigger_osd("drop", "Не нравится · Пропуск трека", duration=1.35)
        elif key == "top_updates":
            self.legal_doc_mode = ""
            self._switch_to_tab(7)
        elif key == "btn_github_update":
            if self.updater.update_available:
                self.is_updating_collapse = True
                self.is_pinned_open = False
                self.is_hovered = False
                self.sp_update_prog.value = 0.02
                self.updater.install_update_async()
                self._update_target_dimensions()
                self._ensure_anim_running()
        elif key == "btn_github_check":
            self.updater.check_async()
        elif key == "btn_open_privacy":
            self.legal_doc_mode = "" if self.legal_doc_mode == "privacy" else "privacy"
        elif key == "btn_open_consent":
            self.legal_doc_mode = "" if self.legal_doc_mode == "consent" else "consent"
        elif key == "btn_close_legal":
            self.legal_doc_mode = ""
        elif key == "btn_toggle_consent":
            self.consent_mgr.set_accepted(not self.consent_mgr.accepted)
        elif key == "btn_style_lyrics":
            self.consent_mgr.lyrics_enabled = not self.consent_mgr.lyrics_enabled
            self.consent_mgr.save_all()
            if not self.consent_mgr.lyrics_enabled:
                self.show_lyrics_view = False
                self._cached_lyric_line = ""
            self._update_target_dimensions()
        elif key == "btn_style_float":
            self.consent_mgr.island_float_mode = (self.consent_mgr.island_float_mode + 1) % 2
            self.consent_mgr.save_all()
            self._update_target_dimensions()
        elif key == "btn_style_wave":
            self.consent_mgr.wave_style_mode = (self.consent_mgr.wave_style_mode + 1) % 3
            self.consent_mgr.save_all()
        elif key == "btn_style_bass":
            self.consent_mgr.bass_sens_mode = (self.consent_mgr.bass_sens_mode + 1) % 3
            self.consent_mgr.save_all()
        elif key == "top_hide_arrow":
            self._suppress_hover_until = time.monotonic() + 1.0
            self._hide_to_game_arrow()
        elif key == "btn_test_reveal":
            self._suppress_hover_until = time.monotonic() + 2.8
            self.is_pinned_open = False
            self.is_hovered = False
            self._hide_to_game_arrow()
            QTimer.singleShot(520, self._start_eyes_reveal)
        elif key == "btn_test_notif":
            self._suppress_hover_until = time.monotonic() + 4.8
            self.is_pinned_open = False
            self.is_hovered = False
            self._trigger_osd(
                "notif",
                "Новое сообщение · Егор",
                duration=4.5,
                subtitle="Наведите и нажмите «Открыть» — остров не развернётся!",
                app_name="Telegram",
                target_kw="telegram",
            )
        elif key == "qa_area":
            self.is_pinned_open = False
            self.is_hovered = False
            QTimer.singleShot(120, self.snip_overlay.start_capture)
        elif key == "qa_full":
            self._take_fullscreen_screenshot()
        elif key == "qa_ocr":
            self._switch_to_tab(3)
        elif key == "qa_qr":
            txt = (QApplication.clipboard().text() or "https://github.com/exuupery/island").strip()
            launch_shell_target(f"https://api.qrserver.com/v1/create-qr-code/?size=320x320&data={txt[:120]}")
        elif key == "qa_pipette":
            self.fn_color_scanning_until = time.monotonic() + 3.0
        elif key == "qa_clean":
            txt = transform_text_tool(QApplication.clipboard().text() or self.working_text, "trim_spaces")
            QApplication.clipboard().setText(txt)
            self._trigger_osd("drop", "Чистый текст в буфере", duration=1.4)
        elif key == "qa_record":
            freed = boost_free_ram()
            self._trigger_osd("drop", f"Буст ПК: +{freed} МБ ОЗУ свободно", duration=1.6)
        elif key == "qa_cmd":
            self._switch_to_tab(6)
        elif key == "launch_add":
            # Defer file dialog outside mousePressEvent & use parent=None so translucent Tool window NEVER crashes!
            QTimer.singleShot(30, self._open_add_launcher_dialog)
        elif key.startswith("launch_"):
            try:
                idx = int(key.split("_")[1])
                if 0 <= idx < len(self.launcher_items):
                    launch_shell_target(self.launcher_items[idx][1])
            except (ValueError, IndexError):
                pass
        elif key == "clip_clear":
            self.clipboard_history = [e for e in self.clipboard_history if e.pinned]
        elif key.startswith("clip_item_"):
            idx = int(key.split("_")[-1])
            if 0 <= idx < len(self.clipboard_history):
                entry = self.clipboard_history[idx]
                self._last_clip_text = entry.text
                QApplication.clipboard().setText(entry.text)
                QTimer.singleShot(60, simulate_ctrl_v)
                self._trigger_osd("drop", "Вставлено в активное окно", duration=1.2)
        elif key == "shelf_clear":
            self.shelf_files.clear()
        elif key == "shelf_drag_all" and self.shelf_files:
            self._start_file_drag([p for p, _ in self.shelf_files])
        elif key.startswith("shelf_card_"):
            idx = int(key.split("_")[-1])
            if 0 <= idx < len(self.shelf_files):
                self._start_file_drag([self.shelf_files[idx][0]])
        elif key == "txt_paste":
            self.working_text = QApplication.clipboard().text() or self.working_text
        elif key == "txt_copy":
            QApplication.clipboard().setText(self.working_text)
            self._trigger_osd("drop", "Текст скопирован", duration=1.3)
        elif key.startswith("txt_tool_"):
            action = key[len("txt_tool_"):]
            src = QApplication.clipboard().text() or self.working_text
            res = transform_text_tool(src, action)
            self.working_text = res
            self._last_clip_text = res
            QApplication.clipboard().setText(res)
        elif key.startswith("note_cat_"):
            self.note_category = int(key.split("_")[-1])
        elif key.startswith("note_chk_"):
            idx = int(key.split("_")[-1])
            items = self.note_items[self.note_category]
            if 0 <= idx < len(items):
                txt, done = items[idx]
                items[idx] = (txt, not done)
        elif key == "timer_toggle":
            self.timer_running = not self.timer_running
            self._timer_last_tick = time.monotonic()
        elif key == "timer_plus1":
            self.timer_total_sec += 60.0
            self.timer_remaining_sec += 60.0
        elif key == "timer_reset":
            self.timer_running = False
            self.timer_total_sec = 25 * 60.0
            self.timer_remaining_sec = 25 * 60.0
        elif key == "weather_refresh":
            self.weather_worker.refresh_async()
        elif key == "ctrl_wifi":
            self.wifi_enabled = not self.wifi_enabled
            launch_shell_target("ms-settings:network-wifi")
        elif key == "ctrl_bt":
            self.bt_enabled = not self.bt_enabled
            launch_shell_target("ms-settings:bluetooth")
        elif key == "ctrl_theme":
            self.telemetry.is_dark_theme = toggle_windows_dark_theme()
        elif key == "ctrl_awake":
            self.keep_awake = not self.keep_awake
            set_keep_awake(self.keep_awake)
        elif key == "slider_bright":
            self._dragging_bright_slider = True
            ratio = clamp((pos.x() - rect.left()) / max(1.0, rect.width()))
            self.dimmer_overlay.set_brightness(int(20 + ratio * 80))
        elif key == "pwr_lock":
            lock_workstation()
        elif key in ("pwr_sleep", "pwr_reboot", "pwr_shutdown"):
            now = time.monotonic()
            if self._power_confirm_key == key and now < self._power_confirm_until:
                self._power_confirm_key = ""
                if key == "pwr_sleep":
                    sleep_workstation()
                elif key == "pwr_reboot":
                    launch_shell_target("shutdown /r /t 0")
                elif key == "pwr_shutdown":
                    launch_shell_target("shutdown /s /t 0")
            else:
                self._power_confirm_key = key
                self._power_confirm_until = now + 3.0
        elif key.startswith("app_mute_"):
            app_name = key[len("app_mute_"):]
            self.audio.toggle_app_mute(app_name)
        elif key.startswith("app_slider_"):
            app_name = key[len("app_slider_"):]
            self._dragging_app_slider = app_name
            self._set_app_vol_from_mouse(app_name, pos.x(), rect)

    def _open_add_launcher_dialog(self):
        """Safely opens native Windows file picker outside mousePressEvent so translucent Frameless Tool window never crashes."""
        was_pinned = self.is_pinned_open
        self.is_pinned_open = True
        try:
            fpath, _ = QFileDialog.getOpenFileName(
                None,
                "Добавить программу или файл на Главную",
                "",
                "Программы и файлы (*.exe *.lnk *.bat *.cmd *.url *.*)",
            )
            if fpath and os.path.exists(fpath):
                name = os.path.splitext(os.path.basename(fpath))[0].strip()[:12] or "App"
                # Keep max 8 visible dock items (if already at 8, replace the oldest built-in or append cleanly)
                if len(self.launcher_items) >= 8:
                    self.launcher_items.insert(0, (name, fpath, "custom"))
                    self.launcher_items = self.launcher_items[:8]
                else:
                    self.launcher_items.append((name, fpath, "custom"))
                self.consent_mgr.custom_launchers = [list(it) for it in self.launcher_items]
                self.consent_mgr.save_all()
                self._trigger_osd("drop", f"Добавлено: {name}", duration=1.5)
        except Exception:
            self._trigger_osd("drop", "Не удалось добавить файл", duration=1.4)
        finally:
            self.is_pinned_open = was_pinned
            self._force_topmost()
            self._update_target_dimensions()
            self._ensure_anim_running()
            self.update()

    def _take_fullscreen_screenshot(self):
        try:
            screen = QGuiApplication.primaryScreen()
            if not screen:
                return
            pix = screen.grabWindow(0)
            QApplication.clipboard().setPixmap(pix)
            pics_dir = os.path.join(os.path.expanduser("~"), "Pictures", "Screenshots")
            os.makedirs(pics_dir, exist_ok=True)
            fname = os.path.join(pics_dir, f"Island_{time.strftime('%Y%m%d_%H%M%S')}.png")
            pix.save(fname, "PNG")
            self.shelf_files.insert(0, (fname, time.strftime("%H:%M")))
            self._trigger_osd("drop", "Скриншот экрана в буфере и на полку", duration=1.6)
        except Exception:
            pass

    def mouseMoveEvent(self, event):
        pos = event.position()
        if self._dragging_prog_slider and "slider_prog" in self._hitboxes:
            track, _ = self.media.get_snapshot()
            if track.duration > 0:
                r = self._hitboxes["slider_prog"]
                ratio = clamp((pos.x() - r.left()) / max(1.0, r.width()))
                self._scrub_preview_sec = ratio * track.duration
            self.update()
        elif self._dragging_vol_slider and "slider_vol" in self._hitboxes:
            self._set_vol_from_mouse(pos.x(), self._hitboxes["slider_vol"])
            self.update()
        elif self._dragging_bright_slider and "slider_bright" in self._hitboxes:
            r = self._hitboxes["slider_bright"]
            ratio = clamp((pos.x() - r.left()) / max(1.0, r.width()))
            self.dimmer_overlay.set_brightness(int(20 + ratio * 80))
            self.update()
        elif self._dragging_app_slider:
            key = f"app_slider_{self._dragging_app_slider}"
            if key in self._hitboxes:
                self._set_app_vol_from_mouse(self._dragging_app_slider, pos.x(), self._hitboxes[key])
                self.update()

    def mouseReleaseEvent(self, event):
        if self._dragging_prog_slider and self._scrub_preview_sec is not None:
            self.media.seek_to(self._scrub_preview_sec)
        self._dragging_prog_slider = False
        self._scrub_preview_sec = None
        self._dragging_vol_slider = False
        self._dragging_bright_slider = False
        self._dragging_app_slider = None

    def _set_vol_from_mouse(self, mx: float, rect: QRectF):
        ratio = clamp((mx - rect.left()) / max(1.0, rect.width()))
        if self.vol_control_mode == 1:
            ps = self._find_player_audio_session()
            if ps:
                self.audio.set_app_volume(ps.name, ratio)
                return
        vol = round(ratio * 100.0)
        self.audio.set_master_volume(vol)
        self._last_master_vol = vol

    def _set_app_vol_from_mouse(self, app_name: str, mx: float, rect: QRectF):
        ratio = clamp((mx - rect.left()) / max(1.0, rect.width()))
        self.audio.set_app_volume(app_name, ratio)
        for s in self._app_sessions:
            if s.name == app_name:
                s.volume = ratio

    def _start_file_drag(self, paths: List[str]):
        valid_urls = [QUrl.fromLocalFile(p) for p in paths if os.path.exists(p)]
        if not valid_urls:
            return
        mime = QMimeData()
        mime.setUrls(valid_urls)
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)

    def _draw_obsidian_card(
        self,
        p: QPainter,
        rect: QRectF,
        radius: float,
        cascade_order: int = 0,
        hovered_t: float = 0.0,
        active_border: Optional[QColor] = None,
        fill_color: Optional[QColor] = None,
    ) -> QRectF:
        casc = clamp((self.sp_cascade.value - cascade_order * 0.032) / 0.74)
        dy = (1.0 - casc) * 10.0
        r = rect.translated(0, dy)

        p.save()
        if fill_color:
            p.setBrush(fill_color)
        else:
            bg_v = int(lerp(15, 24, hovered_t))
            p.setBrush(QColor(bg_v, bg_v, bg_v + 4, 255))

        if active_border:
            p.setPen(QPen(active_border, 1.15))
        else:
            dist = math.hypot(self.sp_mx.value - r.center().x(), self.sp_my.value - r.center().y())
            prox = clamp(1.0 - dist / 240.0)
            alpha = int(lerp(16, 62, max(hovered_t, prox * 0.65)))
            p.setPen(QPen(QColor(255, 255, 255, alpha), 1.0))

        p.drawRoundedRect(r, radius, radius)
        p.restore()
        return r

    def _build_ferrofluid_island_path(
        self,
        cap: QRectF,
        br: float,
        exp: float,
        peak: float,
        bands: List[float],
    ) -> Tuple[QPainterPath, QPainterPath]:
        # Clean pill shape for Game Arrow & Stage 1/2 Reveal ("Точка -> 2 глазика")
        if self.game_hide_state == "hidden_arrow":
            path = QPainterPath()
            left, right, top, bottom = cap.left(), cap.right(), cap.top(), cap.bottom()
            ear = 6.0
            r_ar = min(9.0, cap.height() * 0.5)
            path.moveTo(left - ear, top)
            path.quadTo(left, top, left, top + ear)
            path.lineTo(left, bottom - r_ar)
            path.quadTo(left, bottom, left + r_ar, bottom)
            path.lineTo(right - r_ar, bottom)
            path.quadTo(right, bottom, right, bottom - r_ar)
            path.lineTo(right, top + ear)
            path.quadTo(right, top, right + ear, top)
            path.closeSubpath()
            return path, QPainterPath()

        if self.game_hide_state == "revealing" and (time.monotonic() - self._reveal_started_at) < 0.92:
            path = QPainterPath()
            rad = min(cap.width(), cap.height()) * 0.5
            path.addRoundedRect(cap, rad, rad)
            return path, QPainterPath()

        float_t = clamp(self.sp_top_y.value / 9.0, 0.0, 1.0)
        ear = lerp(9.0, 17.0, exp) * (1.0 - float_t)
        left = cap.left()
        right = cap.right()
        top = cap.top()
        bottom = cap.bottom()
        pull = self.sp_ferro_pull.value
        pull_x = self.sp_ferro_x.value

        x_start = left + br + 4.0
        x_end = right - br - 4.0
        span = max(10.0, x_end - x_start)
        num_pts = 36
        wave_pts: List[QPointF] = []

        # Audio-reactive liquid edge powered by compiled Native Rust solver (0.005 ms!)
        # Tight, controlled 2.8px max amplitude in folded pill so it NEVER droops over titlebars below!
        n_b = len(bands)
        wave_mode = self.consent_mgr.wave_style_mode
        wave_mult = 1.0 if wave_mode == 0 else (0.50 if wave_mode == 1 else 0.0)
        amp_max = lerp(2.8, 7.6, exp) * wave_mult
        active_gate = clamp((peak - 0.012) / 0.05) * (0.0 if wave_mode == 2 else 1.0)

        rust_dys = RUST_CORE.compute_wave_offsets(bands, peak, self._wave_phase, amp_max, active_gate, num_pts + 1)

        for i in range(num_pts + 1):
            t = i / num_pts
            px = x_start + t * span
            env = math.sin(t * math.pi) ** 0.8
            if rust_dys is not None:
                wave_dy = rust_dys[i]
            else:
                d = abs(t - 0.5) * 2.0
                fi = d * (n_b - 1) * 0.78
                i0 = int(fi)
                i1 = min(n_b - 1, i0 + 1)
                fr = fi - i0
                b_val = (bands[i0] * (1.0 - fr) + bands[i1] * fr) * active_gate
                shimmer = math.sin(t * math.pi * 7.0 + self._wave_phase) * (b_val * 1.4 + peak * 0.8) * active_gate
                wave_dy = (b_val * amp_max + shimmer) * env

            dist_cursor = abs(px - self.sp_mx.value)
            cursor_ripple = (
                math.cos(dist_cursor * 0.08 - self._wave_phase * 1.4)
                * 1.2
                * math.exp(-(dist_cursor * dist_cursor) / 3600.0)
                * env
                * exp
            )
            cursor_bulge = pull * math.exp(-(abs(px - pull_x) ** 2) / 1400.0) * env

            py = bottom + wave_dy + cursor_bulge + cursor_ripple
            wave_pts.append(QPointF(px, py))

        path = QPainterPath()
        if float_t > 0.25:
            # Floating Capsule Mode (Парящая пилюля с отступом сверху и скруглёнными верхними углами)
            top_r = lerp(4.0, br, float_t)
            path.moveTo(left + top_r, top)
            path.quadTo(left, top, left, top + top_r)
            path.lineTo(left, bottom - br)
            path.quadTo(left, bottom, x_start, wave_pts[0].y())
            for i in range(len(wave_pts) - 1):
                mid = QPointF(
                    (wave_pts[i].x() + wave_pts[i + 1].x()) * 0.5,
                    (wave_pts[i].y() + wave_pts[i + 1].y()) * 0.5,
                )
                path.quadTo(wave_pts[i], mid)
            path.lineTo(wave_pts[-1])
            path.quadTo(right, bottom, right, bottom - br)
            path.lineTo(right, top + top_r)
            path.quadTo(right, top, right - top_r, top)
            path.closeSubpath()
        else:
            # Classic Top-Bezel Notch Mode (Вырез у края экрана)
            path.moveTo(left - ear, top)
            path.quadTo(left, top, left, top + ear)
            path.lineTo(left, bottom - br)
            path.quadTo(left, bottom, x_start, wave_pts[0].y())
            for i in range(len(wave_pts) - 1):
                mid = QPointF(
                    (wave_pts[i].x() + wave_pts[i + 1].x()) * 0.5,
                    (wave_pts[i].y() + wave_pts[i + 1].y()) * 0.5,
                )
                path.quadTo(wave_pts[i], mid)
            path.lineTo(wave_pts[-1])
            path.quadTo(right, bottom, right, bottom - br)
            path.lineTo(right, top + ear)
            path.quadTo(right, top, right + ear, top)
            path.closeSubpath()

        edge_wave = QPainterPath()
        edge_wave.moveTo(wave_pts[0])
        for i in range(len(wave_pts) - 1):
            mid = QPointF(
                (wave_pts[i].x() + wave_pts[i + 1].x()) * 0.5,
                (wave_pts[i].y() + wave_pts[i + 1].y()) * 0.5,
            )
            edge_wave.quadTo(wave_pts[i], mid)
        edge_wave.lineTo(wave_pts[-1])

        return path, edge_wave

    # --- 120 FPS Master Paint Event ---
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        self._hitboxes.clear()
        cap = self.current_capsule_rect()
        br = clamp(self.sp_r.value, 12.0, min(cap.width(), cap.height()) * 0.48)
        exp = clamp(self.sp_expand.value, 0.0, 1.0)
        osd_t = clamp(self.sp_osd.value, 0.0, 1.0)

        track, pos_sec = self.media.get_snapshot()
        # TRUE MUSIC-ONLY AUDIO ISOLATION: ignores Discord, games, and Windows system sounds!
        raw_peak, raw_bands = self.audio.get_peak_and_bands(track.is_playing, track.source_app)
        bass_mult = 1.0 if self.consent_mgr.bass_sens_mode == 0 else (1.55 if self.consent_mgr.bass_sens_mode == 1 else 0.62)
        peak = clamp(raw_peak * bass_mult, 0.0, 1.0) if track.is_playing else 0.0
        bands = [clamp(b * bass_mult, 0.0, 1.0) for b in raw_bands] if track.is_playing else [0.0] * 32

        # Live Anti-Screamer / Loudness Limiter when audio_fx_mode == 1
        if self.audio_fx_mode == 1 and peak > 0.82 and self._last_master_vol > 32:
            nv = max(30.0, self._last_master_vol - 4.0)
            self.audio.set_master_volume(nv)
            self._last_master_vol = nv

        r1 = int(clamp(self.sp_r1.value, 90, 255))
        g1 = int(clamp(self.sp_g1.value, 70, 255))
        b1 = int(clamp(self.sp_b1.value, 85, 255))
        accent_col = QColor(r1, g1, b1)

        cover_key = f"{track.title}::{track.artist}::{len(track.cover_bytes) if track.cover_bytes else 0}"
        if cover_key != self._last_cover_key:
            self._last_cover_key = cover_key
            self._cover_hi_pix = make_rounded_pixmap(track.cover_bytes, 184, 36)
            self._marquee_phase = 0.0

        island_path, bottom_wave_path = self._build_ferrofluid_island_path(cap, br, exp, peak, bands)

        p.save()
        p.translate(0, 3.0)
        p.fillPath(island_path, QColor(0, 0, 0, 125))
        p.restore()

        p.save()
        p.setClipPath(island_path)
        p.fillPath(island_path, QColor(0, 0, 0, 255))

        if exp > 0.05:
            spot = QRadialGradient(QPointF(self.sp_mx.value, self.sp_my.value), 210.0)
            spot.setColorAt(0.0, QColor(r1, g1, b1, int(22 * exp)))
            spot.setColorAt(0.5, QColor(142, 162, 255, int(9 * exp)))
            spot.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.fillRect(cap, QBrush(spot))

        if self.spotlight_open:
            self._paint_spotlight_overlay(p, cap, accent_col)
        elif exp < 0.42 or self.game_hide_state in ("hidden_arrow", "revealing"):
            p.save()
            p.setOpacity(1.0 if self.game_hide_state in ("hidden_arrow", "revealing") else clamp(1.0 - exp * 2.5))
            if osd_t > 0.15 and self.game_hide_state == "visible":
                self._paint_osd_pill(p, cap, accent_col)
            else:
                self._paint_folded_live_pill(p, cap, track, bands, accent_col)
            p.restore()

        if exp > 0.06 and not self.spotlight_open and self.game_hide_state == "visible":
            p.save()
            p.setOpacity(clamp((exp - 0.06) / 0.58))
            # Scale the 780x420 dashboard locked to the capsule's live width from top-center!
            dash_x = (self.MAX_CANVAS_W - self.EXPANDED_W) * 0.5
            dash_cap = QRectF(dash_x, cap.top(), float(self.EXPANDED_W), float(self.EXPANDED_H))
            pivot = QPointF(self.MAX_CANVAS_W * 0.5, cap.top())
            sc_exp = clamp(cap.width() / float(self.EXPANDED_W), 0.38, 1.0)
            p.translate(pivot)
            p.scale(sc_exp, sc_exp)
            p.translate(-pivot)

            self._paint_top_7tab_navbar(p, dash_cap, accent_col)

            content_top = dash_cap.top() + 56.0
            tab_fns = {
                0: lambda painter: self._paint_tab_home(painter, dash_cap, content_top, track, pos_sec, peak, bands, accent_col),
                1: lambda painter: self._paint_tab_clipboard(painter, dash_cap, content_top, accent_col),
                2: lambda painter: self._paint_tab_shelf(painter, dash_cap, content_top, accent_col),
                3: lambda painter: self._paint_tab_text(painter, dash_cap, content_top, accent_col),
                4: lambda painter: self._paint_tab_note(painter, dash_cap, content_top, accent_col),
                5: lambda painter: self._paint_tab_weather(painter, dash_cap, content_top, accent_col),
                6: lambda painter: self._paint_tab_controls(painter, dash_cap, content_top, accent_col),
                7: lambda painter: self._paint_tab_updates(painter, dash_cap, content_top, accent_col),
            }

            # Direct A -> B transition (Never flies across intermediate tabs!)
            tr_t = clamp(self.sp_tab_trans.value, 0.0, 1.0)
            base_op = clamp((exp - 0.12) / 0.62)
            dir_sign = 1.0 if self.active_tab >= self.prev_tab else -1.0

            if tr_t < 0.98 and self.prev_tab != self.active_tab:
                p.save()
                p.setOpacity(base_op * (1.0 - tr_t))
                p.translate(-dir_sign * tr_t * 22.0, 0.0)
                tab_fns[self.prev_tab](p)
                p.restore()

            p.save()
            p.setOpacity(base_op * tr_t)
            p.translate(dir_sign * (1.0 - tr_t) * 22.0, 0.0)
            tab_fns[self.active_tab](p)
            p.restore()

            for rp in self._ripples:
                p.save()
                rclip = QPainterPath()
                rclip.addRoundedRect(rp.clip_rect, rp.clip_radius, rp.clip_radius)
                p.setClipPath(rclip, Qt.ClipOperation.IntersectClip)
                rad = rp.progress * rp.max_radius
                ralpha = int((1.0 - rp.progress) ** 1.6 * 95)
                rr, rg, rb = rp.color
                rgrad = QRadialGradient(rp.center, max(1.0, rad))
                rgrad.setColorAt(0.0, QColor(rr, rg, rb, ralpha // 2))
                rgrad.setColorAt(0.75, QColor(255, 255, 255, ralpha))
                rgrad.setColorAt(1.0, QColor(rr, rg, rb, 0))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(rgrad))
                p.drawEllipse(rp.center, rad, rad)
                p.restore()

            p.restore()

        p.restore()

        # Subtle outer rim for Floating mode, Hidden Arrow, or Reveal Droplet
        if self.game_hide_state == "hidden_arrow":
            p.setPen(QPen(QColor(255, 255, 255, 75 if self._arrow_hovered else 32), 1.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(island_path)
            return

        if self.sp_top_y.value > 1.5 or self.game_hide_state == "revealing":
            p.setPen(QPen(QColor(255, 255, 255, 38), 1.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(island_path)

        wave_mode = self.consent_mgr.wave_style_mode
        if wave_mode != 2 and not bottom_wave_path.isEmpty():
            wave_grad = QLinearGradient(QPointF(cap.left() + br, cap.bottom()), QPointF(cap.right() - br, cap.bottom()))
            wave_grad.setColorAt(0.0, QColor(r1, g1, b1, 0))
            wave_grad.setColorAt(0.14, QColor(r1, g1, b1, 225 if wave_mode == 0 else 145))
            wave_grad.setColorAt(0.50, QColor(min(255, r1 + 35), min(255, g1 + 25), min(255, b1 + 35), 250 if wave_mode == 0 else 175))
            wave_grad.setColorAt(0.86, QColor(142, 162, 255, 215 if wave_mode == 0 else 135))
            wave_grad.setColorAt(1.0, QColor(142, 162, 255, 0))
            glow_a = clamp(peak * 1.2)
            if wave_mode == 0 and glow_a > 0.04:
                p.save()
                p.setOpacity(glow_a * 0.42)
                p.setPen(QPen(QBrush(wave_grad), 4.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(bottom_wave_path)
                p.restore()
            p.setPen(QPen(QBrush(wave_grad), 1.75 if wave_mode == 0 else 1.25, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(bottom_wave_path)

    def _paint_privacy_dots(self, p: QPainter, right_x: float, cy: float) -> float:
        """Draws iOS-style Orange (Microphone) and Green (Camera) privacy indicator dots (Item 2)."""
        breathe = 0.5 + 0.5 * math.sin(self._aurora_phase * 3.0)
        if self.telemetry.cam_active:
            pt = QPointF(right_x - 4.0, cy)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(48, 209, 88, int(65 + 45 * breathe)))
            p.drawEllipse(pt, 5.5, 5.5)
            p.setBrush(QColor(48, 209, 88, 255))
            p.drawEllipse(pt, 3.1, 3.1)
            right_x -= 11.0
        if self.telemetry.mic_active:
            pt = QPointF(right_x - 4.0, cy)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 159, 10, int(65 + 45 * breathe)))
            p.drawEllipse(pt, 5.5, 5.5)
            p.setBrush(QColor(255, 159, 10, 255))
            p.drawEllipse(pt, 3.1, 3.1)
            right_x -= 11.0
        return right_x

    def _paint_spotlight_overlay(self, p: QPainter, cap: QRectF, accent_col: QColor):
        """Feature #2: OLED Spotlight Search (Alt+Space) — Rust Calc, Units, Real App Icons & Web."""
        # Keep QLineEdit geometry locked cleanly inside the top search bar
        in_x = int(cap.left() + 44)
        in_y = int(cap.top() + 14)
        in_w = max(120, int(cap.width() - 122))
        if self._spot_input.isVisible():
            self._spot_input.setGeometry(in_x, in_y, in_w, 30)

        # Magnifying glass icon on left + 'Alt+Space' subtle keycap badge on right
        VectorIcons.draw_search(p, QPointF(cap.left() + 24.0, cap.top() + 29.0), 15.5, QColor(235, 238, 248, 235))
        esc_r = QRectF(cap.right() - 66.0, cap.top() + 18.0, 50.0, 22.0)
        p.setPen(QPen(QColor(255, 255, 255, 28), 1.0))
        p.setBrush(QColor(22, 22, 28, 230))
        p.drawRoundedRect(esc_r, 7.0, 7.0)
        p.setPen(QColor(150, 155, 170, 225))
        p.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
        p.drawText(esc_r, Qt.AlignmentFlag.AlignCenter, "ESC")

        # Subtle divider below input bar
        p.setPen(QPen(QColor(255, 255, 255, 22), 1.0))
        p.drawLine(QPointF(cap.left() + 16.0, cap.top() + 56.0), QPointF(cap.right() - 16.0, cap.top() + 56.0))

        # Result rows
        ry = cap.top() + 64.0
        for idx, item in enumerate(self._spotlight_results[:5]):
            row_r = QRectF(cap.left() + 12.0, ry, cap.width() - 24.0, 44.0)
            self._hitboxes[f"spot_row_{idx}"] = row_r
            is_sel = idx == self._spotlight_sel
            if is_sel:
                p.setPen(QPen(QColor(255, 255, 255, 42), 1.0))
                p.setBrush(QColor(30, 30, 39, 245))
                p.drawRoundedRect(row_r, 12.0, 12.0)

            ic_r = QRectF(row_r.left() + 10.0, row_r.center().y() - 14.0, 28.0, 28.0)
            kind = item.get("kind", "app")
            ic_pix = self._get_real_exe_pixmap(item.get("icon_path", "")) if kind == "app" else None
            if ic_pix is not None:
                p.save()
                ic_clip = QPainterPath()
                ic_clip.addRoundedRect(ic_r, 7.0, 7.0)
                p.setClipPath(ic_clip, Qt.ClipOperation.IntersectClip)
                p.drawPixmap(ic_r.toRect(), ic_pix)
                p.restore()
            else:
                p.setPen(QPen(QColor(255, 255, 255, 26), 1.0))
                p.setBrush(QColor(24, 24, 32, 245))
                p.drawRoundedRect(ic_r, 8.0, 8.0)
                if kind == "calc":
                    VectorIcons.draw_calc(p, ic_r.center(), 14.0, QColor(255, 255, 255, 240))
                elif kind == "web":
                    VectorIcons.draw_search(p, ic_r.center(), 13.5, QColor(225, 228, 240, 235))
                else:
                    p.setPen(QColor(245, 245, 252))
                    p.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
                    p.drawText(ic_r, Qt.AlignmentFlag.AlignCenter, (item.get("title", "A")[:1] or "A").upper())

            t_f = QFont("Segoe UI", 9, QFont.Weight.Bold)
            s_f = QFont("Segoe UI", 7, QFont.Weight.Medium)
            avail_w = int(row_r.width() - 110.0)
            p.setPen(QColor(252, 252, 255, 252))
            p.setFont(t_f)
            p.drawText(
                QRectF(ic_r.right() + 11.0, row_r.top() + 5.0, float(avail_w), 18.0),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                QFontMetrics(t_f).elidedText(item.get("title", ""), Qt.TextElideMode.ElideRight, avail_w),
            )
            p.setPen(QColor(148, 152, 168, 225))
            p.setFont(s_f)
            p.drawText(
                QRectF(ic_r.right() + 11.0, row_r.top() + 23.0, float(avail_w), 15.0),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                QFontMetrics(s_f).elidedText(item.get("subtitle", ""), Qt.TextElideMode.ElideRight, avail_w),
            )
            if is_sel:
                p.setPen(QColor(175, 180, 198, 210))
                p.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
                p.drawText(QRectF(row_r.right() - 56.0, row_r.top(), 46.0, row_r.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, "Enter ↵")
            ry += 48.0

    # --- Folded Live Pill States ---
    def _paint_folded_live_pill(
        self,
        p: QPainter,
        cap: QRectF,
        track: TrackInfo,
        bands: List[float],
        accent_col: QColor,
    ):
        cy = cap.top() + cap.height() * 0.50
        w_data = self.weather_worker.data

        # 1. Hidden Game Arrow Pull-Tab ('v' стрелочка в играх)
        if self.game_hide_state == "hidden_arrow" and not self.is_updating_collapse:
            bob = math.sin(self._aurora_phase * 2.5) * 1.2 if self._arrow_hovered else 0.0
            col = QColor(255, 255, 255, 240 if self._arrow_hovered else 145)
            VectorIcons.draw_chevron_down(p, QPointF(cap.center().x(), cy + bob), 11.5 if self._arrow_hovered else 9.5, col)
            return

        # 2. 3-Stage Signature Reveal Animation ("Сначала точка -> 2 глазика -> Появление Острова")!
        if self.game_hide_state == "revealing" and not self.is_updating_collapse:
            el = time.monotonic() - self._reveal_started_at
            cx_r = cap.center().x()
            if el < 0.42:
                # Stage 1: Glowing Singularity Dot pulsing inside the descending black droplet
                st1 = clamp(el / 0.42)
                pulse_r = 4.0 + math.sin(st1 * math.pi) * 5.0
                p.setPen(Qt.PenStyle.NoPen)
                g_dot = QRadialGradient(QPointF(cx_r, cy), 13.0)
                g_dot.setColorAt(0.0, QColor(accent_col.red(), accent_col.green(), accent_col.blue(), 225))
                g_dot.setColorAt(1.0, QColor(accent_col.red(), accent_col.green(), accent_col.blue(), 0))
                p.setBrush(QBrush(g_dot))
                p.drawEllipse(QPointF(cx_r, cy), 12.0, 12.0)
                p.setBrush(QColor(255, 255, 255, 250))
                p.drawEllipse(QPointF(cx_r, cy), pulse_r * 0.54, pulse_r * 0.54)
                return
            elif el < 1.38:
                # Stage 2 -> 3: Two Big Living Eyes pop open, glance around, blink, and morph into the Island!
                eyes_in = clamp((el - 0.42) / 0.16)
                eyes_out = 1.0 - clamp((el - 1.18) / 0.20)
                op_eyes = eyes_in * eyes_out
                if op_eyes > 0.02:
                    p.save()
                    p.setOpacity(op_eyes)
                    blink_now = 0.84 <= el <= 0.96
                    glance_x = math.sin((el - 0.42) * 5.8) * 2.8
                    sc_y = 0.12 if blink_now else (0.25 + 0.75 * eyes_in)
                    for ox in (-13.0, 13.0):
                        ex = cx_r + ox
                        p.setPen(Qt.PenStyle.NoPen)
                        p.setBrush(QColor(255, 255, 255, 252))
                        p.drawEllipse(QPointF(ex, cy), 7.0, max(1.4, 8.6 * sc_y))
                        if not blink_now:
                            p.setBrush(QColor(10, 10, 16, 255))
                            p.drawEllipse(QPointF(ex + glance_x, cy), 3.2, 3.9 * sc_y)
                            p.setBrush(QColor(255, 255, 255, 248))
                            p.drawEllipse(QPointF(ex + glance_x - 1.1, cy - 1.4), 1.15, 1.15)
                    p.restore()
                if el < 1.22:
                    return
                # Fade-in of actual Island content during Stage 3 (1.22..1.72)
                p.setOpacity(clamp((el - 1.22) / 0.42))

        # 3. Living Plasma Update Download Capsule (when user clicks 'Обновить'!)
        if self.is_updating_collapse or self.updater.status in ("downloading", "updated"):
            prog = clamp(self.sp_update_prog.value, 0.03, 1.0)
            pct = int(round(prog * 100))
            done_flash = self.updater.status == "updated"
            bar_col = QColor(74, 222, 128) if done_flash else accent_col

            # Ambient pulse aura inside loading capsule
            aura = QRadialGradient(QPointF(cap.left() + 26.0 + prog * (cap.width() - 52.0), cap.bottom() - 14.0), 85.0)
            aura.setColorAt(0.0, QColor(bar_col.red(), bar_col.green(), bar_col.blue(), 65))
            aura.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.fillRect(cap, QBrush(aura))

            # Dual counter-rotating orbital rings on the left
            ic_pt = QPointF(cap.left() + 24.0, cap.top() + 20.0)
            p.setPen(QPen(QColor(bar_col.red(), bar_col.green(), bar_col.blue(), 90), 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.setBrush(Qt.BrushStyle.NoBrush)
            ang1 = int(((self._aurora_phase * 220.0) % 360.0) * 16)
            ang2 = int(((-self._aurora_phase * 160.0) % 360.0) * 16)
            p.drawArc(QRectF(ic_pt.x() - 10.0, ic_pt.y() - 10.0, 20.0, 20.0), ang1, 220 * 16)
            p.setPen(QPen(QColor(142, 162, 255, 195), 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawArc(QRectF(ic_pt.x() - 6.5, ic_pt.y() - 6.5, 13.0, 13.0), ang2, 190 * 16)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(bar_col)
            p.drawEllipse(ic_pt, 2.4, 2.4)

            p.setPen(QColor(255, 255, 255, 250))
            p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
            p.drawText(
                QRectF(cap.left() + 42.0, cap.top() + 9.0, cap.width() - 100.0, 20.0),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                self.updater.status_text,
            )

            p.setPen(bar_col)
            p.setFont(QFont("Segoe UI Variable Display", 10, QFont.Weight.Bold))
            p.drawText(
                QRectF(cap.right() - 58.0, cap.top() + 9.0, 40.0, 20.0),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                f"{pct}%",
            )

            # Plasma Liquid Progress Bar with glowing comet head & floating sparks
            bar_bg = QRectF(cap.left() + 18.0, cap.bottom() - 16.0, cap.width() - 36.0, 7.0)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(26, 26, 34, 255))
            p.drawRoundedRect(bar_bg, 3.5, 3.5)

            fill_w = max(10.0, bar_bg.width() * prog)
            fill_r = QRectF(bar_bg.left(), bar_bg.top(), fill_w, bar_bg.height())
            p_grad = QLinearGradient(fill_r.topLeft(), fill_r.topRight())
            if done_flash:
                p_grad.setColorAt(0.0, QColor(52, 211, 153))
                p_grad.setColorAt(1.0, QColor(167, 243, 208))
            else:
                p_grad.setColorAt(0.0, accent_col)
                p_grad.setColorAt(0.55, QColor(244, 114, 182, 255))
                p_grad.setColorAt(1.0, QColor(142, 162, 255, 255))
            p.setBrush(QBrush(p_grad))
            p.drawRoundedRect(fill_r, 3.5, 3.5)

            # Traveling specular sheen inside progress bar + bright comet tip
            head_x = fill_r.right()
            comet_g = QRadialGradient(QPointF(head_x, fill_r.center().y()), 14.0)
            comet_g.setColorAt(0.0, QColor(255, 255, 255, 245))
            comet_g.setColorAt(0.45, QColor(bar_col.red(), bar_col.green(), bar_col.blue(), 155))
            comet_g.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.setBrush(QBrush(comet_g))
            p.drawEllipse(QPointF(head_x, fill_r.center().y()), 14.0, 9.0)
            p.setBrush(QColor(255, 255, 255, 255))
            p.drawEllipse(QPointF(head_x - 2.0, fill_r.center().y()), 2.6, 2.6)
            return

        # 3.5 Live Steam / Browser Download Capsule (Features #11 & #3 — Rust multi-library scanner!)
        if self._active_download is not None:
            dl = self._active_download
            is_steam = dl.get("source") == "steam"
            dl_name = str(dl.get("name", "Загрузка"))
            dl_pct = float(dl.get("pct", -1.0))
            dl_mbps = float(dl.get("mbps", 0.0))
            dl_mb = float(dl.get("mb_done", 0.0))
            dl_col = QColor(56, 189, 248) if is_steam else QColor(52, 211, 153)

            aura_dl = QRadialGradient(QPointF(cap.left() + 34.0, cy), 95.0)
            aura_dl.setColorAt(0.0, QColor(dl_col.red(), dl_col.green(), dl_col.blue(), 58))
            aura_dl.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.fillRect(cap, QBrush(aura_dl))

            ic_pt = QPointF(cap.left() + 24.0, cap.top() + 20.0)
            p.setPen(QPen(QColor(dl_col.red(), dl_col.green(), dl_col.blue(), 110), 1.3))
            p.setBrush(QColor(18, 24, 34, 245))
            p.drawEllipse(ic_pt, 11.0, 11.0)
            VectorIcons.draw_download_arrow(p, ic_pt, 12.0, dl_col)

            tag_s = "STEAM" if is_steam else "ЗАГРУЗКА"
            t_f = QFont("Segoe UI", 8, QFont.Weight.Bold)
            p.setFont(t_f)
            p.setPen(QColor(250, 252, 255, 250))
            lbl_s = QFontMetrics(t_f).elidedText(f"{tag_s} · {dl_name}", Qt.TextElideMode.ElideRight, int(cap.width() - 145))
            p.drawText(QRectF(cap.left() + 42.0, cap.top() + 8.0, cap.width() - 140.0, 20.0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, lbl_s)

            spd_txt = f"{dl_mbps:.1f} МБ/с" if dl_mbps >= 0.1 else (f"{dl_mb:.0f} МБ" if dl_mb > 0.5 else "Загрузка")
            right_txt = f"{int(round(dl_pct))}% · {spd_txt}" if dl_pct >= 0.0 else spd_txt
            p.setPen(dl_col)
            p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
            p.drawText(QRectF(cap.right() - 118.0, cap.top() + 8.0, 102.0, 20.0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, right_txt)

            bar_bg = QRectF(cap.left() + 18.0, cap.bottom() - 14.0, cap.width() - 36.0, 5.5)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(28, 30, 38, 255))
            p.drawRoundedRect(bar_bg, 2.75, 2.75)
            if dl_pct >= 0.0:
                fw = max(8.0, bar_bg.width() * clamp(dl_pct / 100.0))
                p.setBrush(dl_col)
                p.drawRoundedRect(QRectF(bar_bg.left(), bar_bg.top(), fw, bar_bg.height()), 2.75, 2.75)
            else:
                # Smooth indeterminate plasma sweep for browser download
                sw_t = (self._aurora_phase * 0.45) % 1.0
                sw_w = bar_bg.width() * 0.34
                sw_x = bar_bg.left() + sw_t * (bar_bg.width() - sw_w)
                p.setBrush(dl_col)
                p.drawRoundedRect(QRectF(sw_x, bar_bg.top(), sw_w, bar_bg.height()), 2.75, 2.75)
            return

        # 4. Sleek Music Folded Capsule (ONLY when music is actively playing! When paused/idle -> Living 2 Eyes!)
        if track.is_playing:
            playing = True
            kick = clamp(peak_from_bands(bands))
            art_s = 22.0 + kick * 1.4
            art_c = QPointF(cap.left() + 21.0, cy)
            art_r = QRectF(art_c.x() - art_s * 0.5, art_c.y() - art_s * 0.5, art_s, art_s)
            if kick > 0.06:
                halo = QRadialGradient(art_c, art_s * 0.9)
                halo.setColorAt(0.0, QColor(accent_col.red(), accent_col.green(), accent_col.blue(), int(85 * kick)))
                halo.setColorAt(1.0, QColor(accent_col.red(), accent_col.green(), accent_col.blue(), 0))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(halo))
                p.drawEllipse(art_c, art_s * 0.9, art_s * 0.9)
            p.save()
            clip = QPainterPath()
            clip.addRoundedRect(art_r, 6.0, 6.0)
            p.setClipPath(clip, Qt.ClipOperation.IntersectClip)
            if self._cover_hi_pix:
                p.drawPixmap(art_r.toRect(), self._cover_hi_pix)
            else:
                p.fillPath(clip, accent_col)
            p.restore()
            p.setPen(QPen(QColor(255, 255, 255, 36), 0.85))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(art_r, 6.0, 6.0)

            # Live Synced Lyric Ticker in center (ONLY when user toggled 'Текст' ON & genuine synced LRC exists!)
            eq_right = self._paint_privacy_dots(p, cap.right() - 14.0, cy)
            groups = ((0, 3), (3, 8), (8, 14), (14, 21), (21, 30))
            bar_w = 2.4
            gap = 2.2
            n_bars = len(groups)
            eq_left = eq_right - n_bars * bar_w - (n_bars - 1) * gap

            live_lyr = self._cached_lyric_line if (self.show_lyrics_view and self.consent_mgr.lyrics_enabled) else ""
            if live_lyr:
                slide_y = self.sp_lyric_slide.value * 8.0
                lyr_rect = QRectF(art_r.right() + 10.0, cap.top() + slide_y, max(40.0, eq_left - art_r.right() - 20.0), cap.height())
                p.save()
                p.setClipRect(QRectF(art_r.right() + 8.0, cap.top() + 2.0, max(40.0, eq_left - art_r.right() - 16.0), cap.height() - 4.0), Qt.ClipOperation.IntersectClip)
                p.setOpacity(clamp(1.0 - self.sp_lyric_slide.value * 0.55))
                l_font = QFont("Segoe UI", 8, QFont.Weight.DemiBold)
                p.setFont(l_font)
                el_lyr = QFontMetrics(l_font).elidedText(live_lyr, Qt.TextElideMode.ElideRight, int(lyr_rect.width()))
                p.setPen(QColor(250, 250, 255, 248))
                p.drawText(lyr_rect, Qt.AlignmentFlag.AlignCenter, el_lyr)
                p.restore()
            else:
                p.setPen(QColor(255, 255, 255, 248))
                p.setFont(QFont("Segoe UI Variable Display", 9, QFont.Weight.Bold))
                p.drawText(
                    QRectF(cap.center().x() - 56.0, cap.top(), 46.0, cap.height()),
                    Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                    time.strftime("%H:%M"),
                )
                p.setPen(QPen(QColor(255, 255, 255, 42), 1.0))
                p.drawLine(QPointF(cap.center().x() - 3.0, cy - 5.5), QPointF(cap.center().x() - 3.0, cy + 5.5))
                VectorIcons.draw_weather_cloud(p, QPointF(cap.center().x() + 11.0, cy), 12.5, QColor(235, 242, 255, 235), filled=True)
                p.setPen(QColor(235, 240, 252, 240))
                p.drawText(
                    QRectF(cap.center().x() + 22.0, cap.top(), 38.0, cap.height()),
                    Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                    f"{w_data.temp}°",
                )

            # Sleek 5-bar micro-equalizer (refined 2.4px bars, capped at 13px max height)
            eq_grad = QLinearGradient(QPointF(eq_left, 0), QPointF(eq_right, 0))
            eq_grad.setColorAt(0.0, accent_col)
            eq_grad.setColorAt(1.0, QColor(142, 162, 255, 255))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(eq_grad))
            for gi, (a, b) in enumerate(groups):
                seg = bands[a:b] or [0.0]
                val = max(seg) * 0.65 + (sum(seg) / len(seg)) * 0.35 if playing else 0.0
                bh = 2.8 + clamp(val) * 11.5
                bx = eq_left + gi * (bar_w + gap)
                p.drawRoundedRect(QRectF(bx, cy - bh * 0.5, bar_w, bh), bar_w * 0.5, bar_w * 0.5)
            return

        if self.timer_running:
            VectorIcons.draw_weather_cloud(p, QPointF(cap.left() + 22.0, cy), 14.0, QColor(255, 255, 255, 230), filled=True)
            p.setPen(QColor(245, 158, 11, 245))
            p.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
            p.drawText(
                QRectF(cap.left() + 36.0, cap.top(), 48.0, cap.height() - 2),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                self.timer_label[:7],
            )
            ring_r = QRectF(cap.left() + 86.0, cy - 6.5, 13.0, 13.0)
            p.setPen(QPen(QColor(245, 158, 11, 70), 2.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(ring_r)
            p.setPen(QPen(QColor(245, 158, 11, 250), 2.2))
            ratio = clamp(self.timer_remaining_sec / max(1.0, self.timer_total_sec))
            p.drawArc(ring_r, 90 * 16, -int(ratio * 360 * 16))

            p.drawText(
                QRectF(cap.left() + 104.0, cap.top(), 46.0, cap.height() - 2),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                format_time(self.timer_remaining_sec),
            )
            p.setPen(QColor(255, 255, 255, 245))
            p.drawText(
                QRectF(cap.right() - 102.0, cap.top(), 50.0, cap.height() - 2),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                time.strftime("%H:%M"),
            )
            p.drawText(
                QRectF(cap.right() - 44.0, cap.top(), 30.0, cap.height() - 2),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                f"{w_data.temp}°",
            )
            return

        left_cx = cap.left() + 26.0
        bob_y = math.sin(self._aurora_phase * 1.4) * 1.1
        if self.character_mode == 1:
            self._draw_character_eyes(p, QPointF(left_cx, cy + bob_y))
        elif self.character_mode == 2:
            self._draw_character_cat(p, QPointF(left_cx, cy + bob_y))
        else:
            VectorIcons.draw_weather_cloud(p, QPointF(left_cx - 2.0, cy), 15.0, QColor(255, 255, 255, 235), filled=True)

        p.setPen(QColor(255, 255, 255, 248))
        p.setFont(QFont("Segoe UI Variable Display", 10, QFont.Weight.Bold))
        p.drawText(cap.adjusted(0, 0, 0, -2), Qt.AlignmentFlag.AlignCenter, time.strftime("%H:%M"))

        right_end = self._paint_privacy_dots(p, cap.right() - 14.0, cy)
        if self.shelf_files:
            badge = QRectF(right_end - 56.0, cy - 8.5, 26.0, 17.0)
            p.setPen(QPen(QColor(255, 255, 255, 35), 1.0))
            p.setBrush(QColor(26, 26, 32, 255))
            p.drawRoundedRect(badge, 8.5, 8.5)
            p.setPen(QColor(255, 255, 255, 235))
            p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
            p.drawText(badge, Qt.AlignmentFlag.AlignCenter, f"{len(self.shelf_files)}")

        p.setPen(QColor(255, 255, 255, 235))
        p.setFont(QFont("Segoe UI Variable Display", 10, QFont.Weight.Bold))
        p.drawText(
            QRectF(right_end - 28.0, cap.top(), 28.0, cap.height() - 2),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
            f"{w_data.temp}°",
        )

    def _draw_character_eyes(self, p: QPainter, center: QPointF):
        """Living Expressive Eyes Engine (reacts to petting, bass kicks, late night, notifications & drag-hover)."""
        p.save()
        look_x = self.sp_eye_dx.value
        look_y = self.sp_eye_dy.value
        emo = self._eye_emotion
        pk = self.audio._smooth_peak

        # Happy squinting '^ ^' when stroked/petted with cursor!
        if emo == "happy":
            p.setPen(QPen(QColor(255, 255, 255, 250), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.setBrush(Qt.BrushStyle.NoBrush)
            for ox in (-5.6, 5.6):
                ex = center.x() + ox
                ey = center.y() + 0.5
                arc = QPainterPath()
                arc.moveTo(ex - 3.6, ey + 1.4)
                arc.quadTo(ex, ey - 3.6, ex + 3.6, ey + 1.4)
                p.drawPath(arc)
            # Soft warm rosy blush under happy eyes
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(244, 114, 182, 130))
            p.drawEllipse(QPointF(center.x() - 9.2, center.y() + 3.2), 2.2, 1.3)
            p.drawEllipse(QPointF(center.x() + 9.2, center.y() + 3.2), 2.2, 1.3)
            p.restore()
            return

        vibe_bounce = -math.sin(self._wave_phase * 2.2) * pk * 1.6 if emo == "vibe" else 0.0
        for ox in (-5.5, 5.5):
            ex = center.x() + ox
            ey = center.y() + vibe_bounce
            if self._is_blinking:
                p.setPen(QPen(QColor(255, 255, 255, 235), 1.65, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawLine(QPointF(ex - 3.6, ey), QPointF(ex + 3.6, ey))
                continue

            rx = 4.6 if emo in ("surprised", "curious") else 4.3
            ry = 5.9 if emo == "surprised" else (5.5 if emo == "curious" else (4.2 if emo == "vibe" else 5.1))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 248))
            p.drawEllipse(QPointF(ex, ey), rx, ry)

            # Pupil + expressive catchlight sparkle
            pr_x = 1.8 if emo == "surprised" else 2.2
            pr_y = 2.0 if emo == "surprised" else (2.1 if emo == "vibe" else 2.5)
            p.setBrush(QColor(10, 10, 14, 255))
            p.drawEllipse(QPointF(ex + look_x, ey + look_y), pr_x, pr_y)
            p.setBrush(QColor(255, 255, 255, 245))
            p.drawEllipse(QPointF(ex + look_x - 0.8, ey + look_y - 0.9), 0.85 if emo == "surprised" else 0.72, 0.85 if emo == "surprised" else 0.72)
            if emo in ("surprised", "curious"):
                p.drawEllipse(QPointF(ex + look_x + 0.9, ey + look_y + 0.8), 0.45, 0.45)

            # Sleepy late-night half-closed upper eyelid (23:00..05:59)
            if emo == "sleepy":
                p.setBrush(QColor(0, 0, 0, 255))
                p.drawRect(QRectF(ex - rx - 1.0, ey - ry - 1.0, rx * 2.0 + 2.0, ry * 0.95))
                p.setPen(QPen(QColor(215, 220, 235, 195), 1.1, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawLine(QPointF(ex - rx + 0.3, ey - ry * 0.05), QPointF(ex + rx - 0.3, ey - ry * 0.05))
        p.restore()

    def _draw_character_cat(self, p: QPainter, center: QPointF):
        p.save()
        cx, cy = center.x(), center.y() + 1.5
        lx = self.sp_eye_dx.value * 0.65
        ly = self.sp_eye_dy.value * 0.65

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(242, 242, 247, 255))
        ears = QPainterPath()
        ears.moveTo(cx - 8.5, cy - 2.0)
        ears.lineTo(cx - 7.5, cy - 10.5)
        ears.lineTo(cx - 2.0, cy - 6.5)
        ears.moveTo(cx + 8.5, cy - 2.0)
        ears.lineTo(cx + 7.5, cy - 10.5)
        ears.lineTo(cx + 2.0, cy - 6.5)
        p.drawPath(ears)
        p.drawEllipse(QPointF(cx, cy), 8.8, 7.2)

        p.setBrush(QColor(235, 130, 150, 220))
        in_ears = QPainterPath()
        in_ears.moveTo(cx - 7.0, cy - 3.5)
        in_ears.lineTo(cx - 6.5, cy - 8.5)
        in_ears.lineTo(cx - 3.2, cy - 5.8)
        in_ears.moveTo(cx + 7.0, cy - 3.5)
        in_ears.lineTo(cx + 6.5, cy - 8.5)
        in_ears.lineTo(cx + 3.2, cy - 5.8)
        p.drawPath(in_ears)

        p.setBrush(QColor(18, 18, 22, 255))
        if self._is_blinking:
            p.setPen(QPen(QColor(18, 18, 22), 1.4))
            p.drawLine(QPointF(cx - 5.2, cy - 0.5), QPointF(cx - 2.0, cy - 0.5))
            p.drawLine(QPointF(cx + 2.0, cy - 0.5), QPointF(cx + 5.2, cy - 0.5))
        else:
            p.drawEllipse(QPointF(cx - 3.6 + lx, cy - 0.6 + ly), 1.45, 1.8)
            p.drawEllipse(QPointF(cx + 3.6 + lx, cy - 0.6 + ly), 1.45, 1.8)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(245, 158, 11, 255))
        p.drawEllipse(QPointF(cx, cy + 7.2), 1.8, 1.8)
        p.restore()

    def _paint_osd_pill(self, p: QPainter, cap: QRectF, accent_col: QColor):
        cy = cap.top() + cap.height() * 0.48
        if self._osd_type in ("notif", "usb", "usb_out"):
            # Upgraded Brand-Authentic Rich Notification & USB Live Activity Capsule!
            app_low = (self._notif_app or "").lower()
            if self._osd_type == "usb":
                b_col, b_col2, tag_txt = QColor(52, 211, 153), QColor(16, 185, 129), "USB НАКОПИТЕЛЬ"
                ic_fn = VectorIcons.draw_shelf_box
            elif self._osd_type == "usb_out":
                b_col, b_col2, tag_txt = QColor(148, 163, 184), QColor(100, 116, 139), "USB ОТКЛЮЧЁН"
                ic_fn = VectorIcons.draw_shelf_box
            elif "telegram" in app_low:
                b_col, b_col2, tag_txt = QColor(55, 174, 226), QColor(30, 150, 200), "TELEGRAM"
                ic_fn = VectorIcons.draw_telegram
            elif "discord" in app_low:
                b_col, b_col2, tag_txt = QColor(88, 101, 242), QColor(71, 82, 196), "DISCORD"
                ic_fn = VectorIcons.draw_discord
            elif "микрофон" in app_low:
                b_col, b_col2, tag_txt = QColor(255, 159, 10), QColor(234, 88, 12), "МИКРОФОН"
                ic_fn = VectorIcons.draw_microphone
            elif "аудио" in app_low:
                b_col, b_col2, tag_txt = QColor(48, 209, 88), QColor(16, 185, 129), "АУДИО"
                ic_fn = VectorIcons.draw_headphones
            else:
                b_col, b_col2, tag_txt = accent_col, QColor(142, 162, 255), (self._notif_app or "ISLAND").upper()[:10]
                ic_fn = VectorIcons.draw_sparkle

            # Soft radial brand aura radiating from the left icon
            aura_n = QRadialGradient(QPointF(cap.left() + 30.0, cy), 120.0)
            aura_n.setColorAt(0.0, QColor(b_col.red(), b_col.green(), b_col.blue(), 82 if self._notif_hovered else 64))
            aura_n.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.fillRect(cap, QBrush(aura_n))

            ic_c = QPointF(cap.left() + 28.0, cy - 1.0)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(b_col.red(), b_col.green(), b_col.blue(), 42))
            p.drawEllipse(ic_c, 18.0, 18.0)
            ic_grad = QLinearGradient(QPointF(ic_c.x() - 14, ic_c.y() - 14), QPointF(ic_c.x() + 14, ic_c.y() + 14))
            ic_grad.setColorAt(0.0, b_col)
            ic_grad.setColorAt(1.0, b_col2)
            p.setBrush(QBrush(ic_grad))
            p.drawEllipse(ic_c, 14.0, 14.0)
            ic_fn(p, ic_c, 14.5, QColor(255, 255, 255, 255))

            # Top meta row: brand badge + "сейчас"
            p.setPen(b_col)
            p.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
            p.drawText(
                QRectF(cap.left() + 52.0, cap.top() + 6.0, 165.0, 13.0),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                f"{tag_txt} · сейчас",
            )

            # Main title & subtitle (elided cleanly so text never overlaps 'Открыть' pill)
            avail_w = max(80, int(cap.width() - 138.0))
            t_font = QFont("Segoe UI", 9, QFont.Weight.Bold)
            s_font = QFont("Segoe UI", 8)
            title_elided = QFontMetrics(t_font).elidedText(self._osd_text, Qt.TextElideMode.ElideRight, avail_w)
            sub_elided = QFontMetrics(s_font).elidedText(
                self._notif_subtitle or "Нажмите, чтобы открыть", Qt.TextElideMode.ElideRight, avail_w
            )
            p.setPen(QColor(255, 255, 255, 252))
            p.setFont(t_font)
            p.drawText(
                QRectF(cap.left() + 52.0, cap.top() + 18.0, float(avail_w), 18.0),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                title_elided,
            )
            p.setPen(QColor(175, 180, 198, 235))
            p.setFont(s_font)
            p.drawText(
                QRectF(cap.left() + 52.0, cap.top() + 35.0, float(avail_w), 16.0),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                sub_elided,
            )

            # Interactive 'Открыть' pill button (glows brightly on hover!)
            if self._osd_type != "usb_out":
                open_pill = QRectF(cap.right() - 76.0, cy - 13.5, 62.0, 26.0)
                p.setPen(QPen(QColor(b_col.red(), b_col.green(), b_col.blue(), 240 if self._notif_hovered else 145), 1.2 if self._notif_hovered else 1.0))
                p.setBrush(QColor(b_col.red(), b_col.green(), b_col.blue(), 110 if self._notif_hovered else 48))
                p.drawRoundedRect(open_pill, 13.0, 13.0)
                p.setPen(QColor(255, 255, 255, 255))
                p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
                p.drawText(open_pill, Qt.AlignmentFlag.AlignCenter, "Открыть")

            # Smooth shrinking countdown hairline along the bottom edge
            rem_ratio = clamp((self._osd_until - time.monotonic()) / max(0.3, self._osd_duration), 0.0, 1.0)
            cd_w = (cap.width() - 48.0) * rem_ratio
            if cd_w > 2.0:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(b_col.red(), b_col.green(), b_col.blue(), 195))
                p.drawRoundedRect(QRectF(cap.left() + 24.0, cap.bottom() - 4.0, cd_w, 2.2), 1.1, 1.1)
        elif self._osd_type == "caps":
            is_on = "ВКЛ" in self._osd_text
            badge = QRectF(cap.left() + 14.0, cy - 10.5, 42.0, 21.0)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 245) if is_on else QColor(36, 36, 46, 255))
            p.drawRoundedRect(badge, 8.0, 8.0)
            p.setPen(QColor(0, 0, 0, 255) if is_on else QColor(175, 180, 195, 240))
            p.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
            p.drawText(badge, Qt.AlignmentFlag.AlignCenter, "CAPS")
            p.setPen(QColor(255, 255, 255, 245))
            p.setFont(QFont("Segoe UI", 9, QFont.Weight.DemiBold))
            p.drawText(
                QRectF(badge.right() + 12.0, cap.top(), cap.width() - 78.0, cap.height() - 2),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                self._osd_text,
            )
        elif self._osd_type == "lang":
            badge = QRectF(cap.left() + 14.0, cy - 10.5, 34.0, 21.0)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 245))
            p.drawRoundedRect(badge, 8.0, 8.0)
            p.setPen(QColor(0, 0, 0, 255))
            p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
            p.drawText(badge, Qt.AlignmentFlag.AlignCenter, self._last_kb_layout)

            p.setPen(QColor(255, 255, 255, 245))
            p.setFont(QFont("Segoe UI", 9, QFont.Weight.DemiBold))
            p.drawText(
                QRectF(badge.right() + 10.0, cap.top(), cap.width() - 68.0, cap.height() - 2),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                f"Раскладка · {self._osd_text}",
            )
        elif self._osd_type == "vol":
            VectorIcons.draw_speaker(
                p,
                QPointF(cap.left() + 24.0, cy),
                15.0,
                QColor(255, 255, 255, 245),
                self._osd_value / 100.0,
                self._last_master_mute or self._osd_value <= 0,
            )
            bar_bg = QRectF(cap.left() + 44.0, cy - 3.0, cap.width() - 98.0, 6.0)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(38, 38, 46, 255))
            p.drawRoundedRect(bar_bg, 3.0, 3.0)
            fw = bar_bg.width() * (0.0 if self._last_master_mute else clamp(self._osd_value / 100.0))
            if fw > 2.0:
                p.setBrush(QColor(255, 255, 255, 248))
                p.drawRoundedRect(QRectF(bar_bg.left(), bar_bg.top(), fw, 6.0), 3.0, 3.0)
            p.setPen(QColor(255, 255, 255, 235))
            p.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
            p.drawText(
                QRectF(bar_bg.right() + 8.0, cap.top(), 38.0, cap.height() - 2),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                self._osd_text,
            )
        else:
            p.setPen(QColor(255, 255, 255, 248))
            p.setFont(QFont("Segoe UI", 9, QFont.Weight.DemiBold))
            p.drawText(cap.adjusted(16, 0, -16, -2), Qt.AlignmentFlag.AlignCenter, self._osd_text)

    # --- Top 7-Tab Navigation Bar (With Pixel-Exact Font Widths so Text NEVER Clips!) ---
    def _paint_top_7tab_navbar(self, p: QPainter, cap: QRectF, accent_col: QColor):
        ty = cap.top() + 12.0
        tx = cap.left() + 22.0

        tabs = [
            ("Главная", 0, VectorIcons.draw_home),
            ("Буфер", 1, VectorIcons.draw_clipboard),
            ("Полка", 2, VectorIcons.draw_shelf_box),
            ("Текст", 3, VectorIcons.draw_text_t),
            ("Заметка", 4, VectorIcons.draw_note_edit),
            ("Погода", 5, VectorIcons.draw_weather_cloud),
            ("Управление", 6, VectorIcons.draw_sliders_h),
        ]

        tab_font = QFont("Segoe UI", 9, QFont.Weight.DemiBold)
        fm_tab = QFontMetrics(tab_font)

        curr_x = tx
        for title, idx, icon_fn in tabs:
            open_t = clamp(self.tab_open_sp[idx].value, 0.0, 1.0)
            h_sp, _ = self._get_btn_springs(f"tab_{idx}")
            hov = h_sp.value

            extra_w = 24.0 if (idx == 2 and self.shelf_files) else 0.0
            text_px = fm_tab.horizontalAdvance(title)
            full_w = 44.0 + text_px + extra_w
            tw = lerp(34.0, full_w, open_t)
            r = QRectF(curr_x, ty, tw, 32.0)
            self._hitboxes[f"tab_{idx}"] = r

            if open_t > 0.02 or hov > 0.02:
                p.setPen(QPen(QColor(255, 255, 255, int(lerp(0, 26, open_t))), 1.0))
                p.setBrush(QColor(34, 34, 41, int(max(open_t * 255, hov * 190))))
                p.drawRoundedRect(r, 16.0, 16.0)

            ic_x = lerp(r.center().x(), r.left() + 17.0, open_t)
            ic_col = QColor(255, 255, 255, int(lerp(125, 252, max(open_t, hov * 0.85))))
            icon_fn(p, QPointF(ic_x, r.center().y()), 14.5, ic_col)

            if open_t > 0.12:
                p.save()
                p.setClipRect(r.adjusted(26, 0, -4, 0), Qt.ClipOperation.IntersectClip)
                p.setOpacity(clamp((open_t - 0.12) / 0.72))
                p.setPen(QColor(255, 255, 255, 252))
                p.setFont(tab_font)
                p.drawText(r.adjusted(31, 0, -6, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, title)
                if idx == 2 and self.shelf_files:
                    cnt_r = QRectF(r.right() - 23.0, r.center().y() - 8.5, 17.0, 17.0)
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(QColor(54, 54, 64, 255))
                    p.drawRoundedRect(cnt_r, 8.5, 8.5)
                    p.setPen(QColor(255, 255, 255, 245))
                    p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
                    p.drawText(cnt_r, Qt.AlignmentFlag.AlignCenter, str(len(self.shelf_files)))
                p.restore()

            curr_x += tw + 5.0

        # Photo 2 Redesign: Unified Obsidian Glass Segmented Control Pill on Top-Right!
        has_upd = self.updater.update_available
        upd_open_t = clamp(self.tab_open_sp[7].value, 0.0, 1.0)
        seg_specs = [
            ("top_hide_arrow", VectorIcons.draw_chevron_up, False, 28.0),
            ("top_char", VectorIcons.draw_eyes_mini, False, 28.0),
            ("top_updates", VectorIcons.draw_download_arrow, self.active_tab == 7 or has_upd, lerp(28.0, 96.0, 1.0 if has_upd else upd_open_t)),
            ("top_pin", VectorIcons.draw_pushpin, self.is_pinned_open, 28.0),
            ("top_mixer", VectorIcons.draw_sliders_h, self.show_inline_mixer, 28.0),
        ]
        total_seg_w = sum(sw for _, _, _, sw in seg_specs) + (len(seg_specs) - 1) * 4.0 + 10.0
        seg_rect = QRectF(cap.right() - 22.0 - total_seg_w, ty, total_seg_w, 32.0)

        p.setPen(QPen(QColor(255, 255, 255, 22), 1.0))
        p.setBrush(QColor(18, 18, 24, 245))
        p.drawRoundedRect(seg_rect, 16.0, 16.0)

        sx = seg_rect.left() + 5.0
        for key, draw_fn, is_act, bw in seg_specs:
            brect = QRectF(sx, ty + 3.0, bw, 26.0)
            self._hitboxes[key] = brect
            h_sp, p_sp = self._get_btn_springs(key)
            hov, prs = h_sp.value, p_sp.value

            p.save()
            sc = 1.0 + hov * 0.05 - prs * 0.07
            p.translate(brect.center())
            p.scale(sc, sc)
            p.translate(-brect.center())

            if key == "top_updates" and has_upd:
                p.setPen(QPen(accent_col, 1.0))
                p.setBrush(QColor(accent_col.red(), accent_col.green(), accent_col.blue(), 46))
                p.drawRoundedRect(brect, 13.0, 13.0)
            elif is_act or hov > 0.04:
                p.setPen(Qt.PenStyle.NoPen)
                if key == "top_pin" and is_act:
                    p.setBrush(QColor(142, 162, 255, 58))
                else:
                    p.setBrush(QColor(42, 42, 52, int(max(225 if is_act else 0, hov * 205))))
                p.drawRoundedRect(brect, 13.0, 13.0)

            if key == "top_updates" and bw > 44.0:
                ic_pt = QPointF(brect.left() + 14.0, brect.center().y())
                ic_col = accent_col if has_upd else QColor(255, 255, 255, 250)
                draw_fn(p, ic_pt, 13.0, ic_col)
                p.save()
                p.setClipRect(brect.adjusted(22, 0, -4, 0), Qt.ClipOperation.IntersectClip)
                p.setPen(QColor(255, 255, 255, 250))
                p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
                p.drawText(brect.adjusted(24, 0, -10, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "Обновить")
                p.restore()
                if has_upd:
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(accent_col)
                    p.drawEllipse(QPointF(brect.right() - 8.0, brect.top() + 8.0), 2.8, 2.8)
            else:
                if key == "top_pin" and is_act:
                    ic_col = QColor(142, 162, 255, 255)
                elif key == "top_mixer" and is_act:
                    ic_col = accent_col
                else:
                    ic_col = QColor(255, 255, 255, 250 if is_act else int(lerp(155, 252, hov)))
                draw_fn(p, brect.center(), 13.5, ic_col)
            p.restore()
            sx += bw + 4.0

    # --- TAB 0: ГЛАВНАЯ (Upgraded Player Card, Functional Volume Bar & Full Mixer!) ---
    def _paint_tab_home(
        self,
        p: QPainter,
        cap: QRectF,
        top: float,
        track: TrackInfo,
        pos_sec: float,
        peak: float,
        bands: List[float],
        accent_col: QColor,
    ):
        left_x = cap.left() + 22.0
        r1, g1, b1 = accent_col.red(), accent_col.green(), accent_col.blue()

        # 1. Left Big Media Card (410 x 232 px)
        p_card = self._draw_obsidian_card(p, QRectF(left_x, top, 410.0, 232.0), 18.0, cascade_order=0)
        p_path = QPainterPath()
        p_path.addRoundedRect(p_card, 18.0, 18.0)

        p.save()
        p.setClipPath(p_path, Qt.ClipOperation.IntersectClip)

        # Orbiting 3-Blob Animated Aurora Mesh behind Album Cover (Always colorful!)
        r2, g2, b2 = track.secondary_color
        ox1 = math.cos(self._aurora_phase) * 32.0
        oy1 = math.sin(self._aurora_phase * 0.85) * 16.0
        aur1 = QRadialGradient(QPointF(p_card.center().x() - 72.0 + ox1, p_card.top() + 68.0 + oy1), 152.0 * (1.0 + peak * 0.16))
        aur1.setColorAt(0.0, QColor(r1, g1, b1, 118))
        aur1.setColorAt(0.6, QColor(r1, g1, b1, 28))
        aur1.setColorAt(1.0, QColor(15, 15, 18, 0))
        p.fillRect(p_card, QBrush(aur1))

        aur2 = QRadialGradient(QPointF(p_card.center().x() + 72.0 - ox1, p_card.top() + 68.0 - oy1), 152.0 * (1.0 + peak * 0.16))
        aur2.setColorAt(0.0, QColor(r2, g2, b2, 96))
        aur2.setColorAt(0.6, QColor(r1, g1, b1, 24))
        aur2.setColorAt(1.0, QColor(15, 15, 18, 0))
        p.fillRect(p_card, QBrush(aur2))

        if self.show_inline_mixer:
            self._paint_inline_mixer_inside_card(p, p_card, accent_col)
        else:
            # A) Top-Left Clickable Player Source Pill ('♪ Яндекс Музыка' / '♪ Плеер') -> Brings player window to front!
            player_sess = self._find_player_audio_session()
            raw_src = (track.source_app or (player_sess.name if player_sess else "") or "Плеер").lower()
            if "yandex" in raw_src or "яндекс" in raw_src:
                src_name = "Яндекс Музыка"
            elif "spotify" in raw_src:
                src_name = "Spotify"
            elif "chrome" in raw_src:
                src_name = "Chrome"
            elif "edge" in raw_src:
                src_name = "Edge"
            elif "telegram" in raw_src:
                src_name = "Telegram"
            else:
                src_name = (player_sess.name if player_sess else (track.source_app or "Плеер"))[:14]
            src_pill = QRectF(p_card.left() + 12.0, p_card.top() + 12.0, 112.0, 24.0)
            self._hitboxes["btn_focus_player"] = src_pill
            h_fp, _ = self._get_btn_springs("btn_focus_player")
            p.setPen(QPen(QColor(255, 255, 255, int(lerp(18, 55, h_fp.value))), 1.0))
            p.setBrush(QColor(22, 22, 28, 215))
            p.drawRoundedRect(src_pill, 12.0, 12.0)
            VectorIcons.draw_music_note_single(p, QPointF(src_pill.left() + 14.0, src_pill.center().y()), 11.5, accent_col)
            p.setPen(QColor(225, 225, 235, 240))
            p.setFont(QFont("Segoe UI", 7, QFont.Weight.DemiBold))
            p.drawText(src_pill.adjusted(24, 0, -6, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, src_name)

            # B) Top-Right Live Lyrics Pill ('Текст') + Compact Visualizer Pill ('Кольцо' / 'Волна' / 'Винил') — 27px clear of Album Art!
            lyr_pill = QRectF(p_card.right() - 136.0, p_card.top() + 12.0, 52.0, 24.0)
            self._hitboxes["btn_lyrics_toggle"] = lyr_pill
            h_lt, _ = self._get_btn_springs("btn_lyrics_toggle")
            if self.show_lyrics_view:
                p.setPen(QPen(accent_col, 1.0))
                p.setBrush(QColor(accent_col.red(), accent_col.green(), accent_col.blue(), 52))
            else:
                p.setPen(QPen(QColor(255, 255, 255, int(lerp(18, 55, h_lt.value))), 1.0))
                p.setBrush(QColor(22, 22, 28, 215))
            p.drawRoundedRect(lyr_pill, 12.0, 12.0)
            p.setPen(QColor(255, 255, 255, 250) if self.show_lyrics_view else QColor(215, 215, 225, 235))
            p.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold if self.show_lyrics_view else QFont.Weight.DemiBold))
            p.drawText(lyr_pill, Qt.AlignmentFlag.AlignCenter, "Текст")

            vis_names = {0: "Кольцо", 1: "Волна", 2: "Винил"}
            vis_pill = QRectF(p_card.right() - 78.0, p_card.top() + 12.0, 66.0, 24.0)
            self._hitboxes["btn_vis_mode"] = vis_pill
            h_vm, _ = self._get_btn_springs("btn_vis_mode")
            p.setPen(QPen(QColor(255, 255, 255, int(lerp(18, 55, h_vm.value))), 1.0))
            p.setBrush(QColor(22, 22, 28, 215))
            p.drawRoundedRect(vis_pill, 12.0, 12.0)
            p.setPen(QColor(215, 215, 225, 235))
            p.setFont(QFont("Segoe UI", 7, QFont.Weight.DemiBold))
            p.drawText(vis_pill, Qt.AlignmentFlag.AlignCenter, vis_names.get(self.visualizer_mode, "Кольцо"))

            if self.show_lyrics_view:
                # Apple-Music-style Real-Time Synced Lyrics Stage inside Player Card!
                prev_l, curr_l, next_l, l_status, _ = self.lyrics_engine.get_live_state(
                    track.artist, track.title, pos_sec, track.duration
                )
                # Compact track mini-header beneath top pills
                mini_r = QRectF(p_card.left() + 16.0, p_card.top() + 44.0, 26.0, 26.0)
                mclip = QPainterPath()
                mclip.addRoundedRect(mini_r, 6.0, 6.0)
                p.save()
                p.setClipPath(mclip, Qt.ClipOperation.IntersectClip)
                if self._cover_hi_pix:
                    p.drawPixmap(mini_r.toRect(), self._cover_hi_pix)
                else:
                    p.fillPath(mclip, accent_col)
                p.restore()
                t_sm = QFont("Segoe UI", 8, QFont.Weight.Bold)
                p.setFont(t_sm)
                p.setPen(QColor(245, 245, 252, 240))
                hdr_txt = f"{track.title or 'Трек'}  ·  {track.artist or ''}".strip(" ·")
                p.drawText(
                    QRectF(mini_r.right() + 9.0, mini_r.top() + 1.0, p_card.width() - 125.0, 24.0),
                    Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                    QFontMetrics(t_sm).elidedText(hdr_txt, Qt.TextElideMode.ElideRight, int(p_card.width() - 130)),
                )
                # Rhythm sync fine-tuning pills (-0.5с / +0.5с) right next to track header!
                off_s = self.lyrics_engine.sync_offset_sec
                btn_lm = QRectF(p_card.right() - 118.0, mini_r.top() + 3.0, 34.0, 20.0)
                btn_lp = QRectF(p_card.right() - 48.0, mini_r.top() + 3.0, 34.0, 20.0)
                self._hitboxes["btn_lyr_minus"] = btn_lm
                self._hitboxes["btn_lyr_plus"] = btn_lp
                for b_r, b_t in ((btn_lm, "-0.5с"), (btn_lp, "+0.5с")):
                    p.setPen(QPen(QColor(255, 255, 255, 28), 1.0))
                    p.setBrush(QColor(24, 24, 32, 225))
                    p.drawRoundedRect(b_r, 7.0, 7.0)
                    p.setPen(QColor(215, 220, 235, 235))
                    p.setFont(QFont("Segoe UI", 6, QFont.Weight.Bold))
                    p.drawText(b_r, Qt.AlignmentFlag.AlignCenter, b_t)
                p.setPen(QColor(175, 180, 198, 220))
                p.setFont(QFont("Segoe UI", 6, QFont.Weight.Bold))
                off_lbl = f"{off_s:+.1f}с" if abs(off_s) >= 0.05 else ("0.0с" if l_status == "synced" else "Текст")
                p.drawText(QRectF(btn_lm.right() + 2.0, mini_r.top() + 3.0, 32.0, 20.0), Qt.AlignmentFlag.AlignCenter, off_lbl)

                # 3-Line Sculpted Synced Lyrics Viewport with active obsidian glass pill & spring transition
                slide_dy = self.sp_lyric_slide.value * 12.0 if l_status == "synced" else 0.0
                stage_r = QRectF(p_card.left() + 14.0, p_card.top() + 73.0, p_card.width() - 28.0, 80.0)
                p.save()
                p.setClipRect(stage_r, Qt.ClipOperation.IntersectClip)
                if l_status in ("synced", "plain"):
                    f_dim = QFont("Segoe UI", 8, QFont.Weight.Medium)
                    f_act = QFont("Segoe UI Variable Display", 11, QFont.Weight.Bold)
                    fm_dim = QFontMetrics(f_dim)
                    fm_act = QFontMetrics(f_act)
                    max_lw = int(stage_r.width() - 24.0)

                    if prev_l:
                        p.setPen(QColor(175, 180, 198, 95))
                        p.setFont(f_dim)
                        p.drawText(
                            QRectF(stage_r.left() + 8.0, stage_r.top() + 1.0 + slide_dy, stage_r.width() - 16.0, 20.0),
                            Qt.AlignmentFlag.AlignCenter,
                            fm_dim.elidedText(prev_l, Qt.TextElideMode.ElideRight, max_lw),
                        )

                    # Sculpted frosted highlight pill behind active singing line
                    act_box = QRectF(stage_r.left() + 4.0, stage_r.top() + 24.0 + slide_dy, stage_r.width() - 8.0, 29.0)
                    p.setPen(QPen(QColor(255, 255, 255, 26), 1.0))
                    p.setBrush(QColor(255, 255, 255, 16))
                    p.drawRoundedRect(act_box, 10.0, 10.0)
                    p.setPen(QColor(255, 255, 255, 255))
                    p.setFont(f_act)
                    p.drawText(
                        act_box.adjusted(8, 0, -8, 0),
                        Qt.AlignmentFlag.AlignCenter,
                        fm_act.elidedText(curr_l or "· · ·", Qt.TextElideMode.ElideRight, max_lw),
                    )

                    if next_l:
                        p.setPen(QColor(180, 186, 205, 115))
                        p.setFont(f_dim)
                        p.drawText(
                            QRectF(stage_r.left() + 8.0, stage_r.top() + 56.0 + slide_dy, stage_r.width() - 16.0, 20.0),
                            Qt.AlignmentFlag.AlignCenter,
                            fm_dim.elidedText(next_l, Qt.TextElideMode.ElideRight, max_lw),
                        )
                elif l_status == "loading":
                    p.setPen(QColor(195, 200, 218, 210))
                    p.setFont(QFont("Segoe UI", 9, QFont.Weight.Medium))
                    p.drawText(stage_r, Qt.AlignmentFlag.AlignCenter, "Ищу точный текст трека...")
                else:
                    p.setPen(QColor(155, 160, 178, 195))
                    p.setFont(QFont("Segoe UI", 9, QFont.Weight.Medium))
                    p.drawText(stage_r, Qt.AlignmentFlag.AlignCenter, "Точный текст для этого трека не найден")
                p.restore()
            else:
                # C) Living Audio Visualizer around/behind the Album Cover!
                float_y = math.sin(self._aurora_phase * 1.3) * 2.0
                bass_sc = 1.0 + (peak * 0.05 if track.is_playing else 0.0)
                art_r = QRectF(p_card.center().x() - 44.0, p_card.top() + 14.0 + float_y, 88.0, 88.0)
                art_c = art_r.center()

                if self.visualizer_mode == 0:
                    # Mode 0: Real 24-Band FFT Spectrum Flanking Wings (Bass near cover -> Treble outward)
                    for i in range(12):
                        bv_l = bands[min(len(bands) - 1, i * 2)] if track.is_playing else 0.0
                        bv_r = bands[min(len(bands) - 1, i * 2 + 1)] if track.is_playing else 0.0
                        bh_l = 4.0 + bv_l * 38.0
                        bh_r = 4.0 + bv_r * 38.0
                        lx = art_r.left() - 14.0 - i * 8.0
                        rx = art_r.right() + 10.4 + i * 8.0
                        p.setPen(Qt.PenStyle.NoPen)
                        p.setBrush(QColor(r1, g1, b1, int(95 + bv_l * 160)))
                        p.drawRoundedRect(QRectF(lx, art_c.y() - bh_l * 0.5, 3.6, bh_l), 1.8, 1.8)
                        p.setBrush(QColor(r1, g1, b1, int(95 + bv_r * 160)))
                        p.drawRoundedRect(QRectF(rx, art_c.y() - bh_r * 0.5, 3.6, bh_r), 1.8, 1.8)
                elif self.visualizer_mode == 1:
                    # Mode 1: Real FFT Audio Wave behind the cover (flat when silent/paused)
                    for l_i, (ph_m, al) in enumerate(((1.0, 130), (-1.3, 85))):
                        wpath = QPainterPath()
                        wpath.moveTo(p_card.left(), art_c.y())
                        for step_i in range(17):
                            sx = p_card.left() + (step_i / 16.0) * p_card.width()
                            bv = bands[step_i % len(bands)] if track.is_playing else 0.0
                            sy = art_c.y() + math.sin(self._wave_phase * ph_m + step_i * 0.65) * (bv * 34.0)
                            wpath.lineTo(QPointF(sx, sy))
                        p.setPen(QPen(QColor(r1, g1, b1, al) if l_i == 0 else QColor(142, 162, 255, al), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                        p.setBrush(Qt.BrushStyle.NoBrush)
                        p.drawPath(wpath)
                else:
                    # Mode 2: Peeking Spinning Vinyl Disc behind the right side of the Album Cover
                    p.save()
                    p.translate(art_c.x() + 34.0, art_c.y())
                    p.rotate(self._vinyl_angle)
                    p.setPen(QPen(QColor(255, 255, 255, 45), 1.2))
                    p.setBrush(QColor(12, 12, 16, 255))
                    p.drawEllipse(QPointF(0, 0), 42.0, 42.0)
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    for gf in (0.82, 0.66, 0.50):
                        p.setPen(QPen(QColor(255, 255, 255, 22), 0.9))
                        p.drawEllipse(QPointF(0, 0), 42.0 * gf, 42.0 * gf)
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(accent_col)
                    p.drawEllipse(QPointF(0, 0), 13.0, 13.0)
                    p.setBrush(QColor(0, 0, 0))
                    p.drawEllipse(QPointF(0, 0), 4.0, 4.0)
                    p.restore()

                # Draw Rounded Album Cover
                p.save()
                p.translate(art_c)
                p.scale(bass_sc, bass_sc)
                p.translate(-art_c)

                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(0, 0, 0, 130))
                p.drawRoundedRect(art_r.translated(0, 6.0), 16.0, 16.0)

                aclip = QPainterPath()
                aclip.addRoundedRect(art_r, 16.0, 16.0)
                p.save()
                p.setClipPath(aclip, Qt.ClipOperation.IntersectClip)
                if self._cover_hi_pix:
                    p.drawPixmap(art_r.toRect(), self._cover_hi_pix)
                else:
                    grad_c = QLinearGradient(art_r.topLeft(), art_r.bottomRight())
                    grad_c.setColorAt(0.0, QColor(r1, g1, b1, 255))
                    grad_c.setColorAt(1.0, QColor(38, 30, 75, 255))
                    p.fillPath(aclip, QBrush(grad_c))
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(QColor(255, 225, 165, 235))
                    p.drawEllipse(QPointF(art_c.x(), art_c.y() + 2.0), 21.0, 21.0)
                p.restore()
                p.setPen(QPen(QColor(255, 255, 255, 42), 1.1))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(art_r, 16.0, 16.0)
                p.restore()

                # Centered Track Title & Artist
                title_str = track.title or "Midnight Drive"
                artist_str = track.artist or "Включи музыку в браузере или плеере"
                p.setPen(QColor(255, 255, 255, 252))
                t_font = QFont("Segoe UI Variable Display", 12, QFont.Weight.Bold)
                p.setFont(t_font)
                p.drawText(
                    QRectF(p_card.left() + 20.0, p_card.top() + 112.0, p_card.width() - 40.0, 22.0),
                    Qt.AlignmentFlag.AlignCenter,
                    QFontMetrics(t_font).elidedText(title_str, Qt.TextElideMode.ElideRight, int(p_card.width() - 40)),
                )

                p.setPen(QColor(165, 165, 178, 235))
                a_font = QFont("Segoe UI", 9, QFont.Weight.Medium)
                p.setFont(a_font)
                p.drawText(
                    QRectF(p_card.left() + 20.0, p_card.top() + 133.0, p_card.width() - 40.0, 18.0),
                    Qt.AlignmentFlag.AlignCenter,
                    QFontMetrics(a_font).elidedText(artist_str, Qt.TextElideMode.ElideRight, int(p_card.width() - 40)),
                )

            # Interactive Progress Timeline Bar (Uses instant scrub preview when dragging!)
            live_pos_sec = self._scrub_preview_sec if (self._dragging_prog_slider and self._scrub_preview_sec is not None) else pos_sec
            p_hov, _ = self._get_btn_springs("slider_prog")
            bh = lerp(5.0, 8.0, max(p_hov.value, 1.0 if self._dragging_prog_slider else 0.0))
            bar_r = QRectF(p_card.left() + 18.0, p_card.top() + 165.0 - bh * 0.5, p_card.width() - 36.0, bh)
            self._hitboxes["slider_prog"] = bar_r.adjusted(0, -10, 0, 10)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(44, 44, 54, 255))
            p.drawRoundedRect(bar_r, bh * 0.5, bh * 0.5)

            ratio = clamp(live_pos_sec / track.duration) if track.duration > 0 else 0.05
            fill_w = max(bh + 2.0, bar_r.width() * ratio)
            prog_grad = QLinearGradient(bar_r.topLeft(), QPointF(bar_r.left() + fill_w, bar_r.top()))
            prog_grad.setColorAt(0.0, accent_col)
            prog_grad.setColorAt(1.0, QColor(min(255, r1 + 30), min(255, g1 + 30), min(255, b1 + 30)))
            p.setBrush(QBrush(prog_grad))
            p.drawRoundedRect(QRectF(bar_r.left(), bar_r.top(), fill_w, bh), bh * 0.5, bh * 0.5)
            p.setBrush(QColor(255, 255, 255, 250))
            p.drawEllipse(QPointF(bar_r.left() + fill_w, bar_r.center().y()), 3.8 + 2.0 * p_hov.value, 3.8 + 2.0 * p_hov.value)

            # Timestamps & 7 Transport Controls (Dislike, -10с, Prev, Play/Pause, Next, +10с, Like!)
            ctrl_cy = p_card.top() + 198.0
            p.setPen(QColor(145, 145, 155, 230))
            p.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
            p.drawText(QRectF(bar_r.left(), ctrl_cy - 10.0, 36.0, 20.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, format_time(live_pos_sec))
            p.drawText(QRectF(bar_r.right() - 36.0, ctrl_cy - 10.0, 36.0, 20.0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, format_time(track.duration))

            cx = p_card.center().x()
            is_liked = self.likes_mgr.is_liked(track.title, track.artist)
            is_disliked = self.likes_mgr.is_disliked(track.title, track.artist)

            # Dislike (Не нравится) & Like (Нравится) Vector Heart Buttons
            btn_dislike_r = QRectF(cx - 146.0, ctrl_cy - 13.0, 26.0, 26.0)
            btn_like_r = QRectF(cx + 120.0, ctrl_cy - 13.0, 26.0, 26.0)
            self._hitboxes["btn_dislike"] = btn_dislike_r
            self._hitboxes["btn_like"] = btn_like_r

            hd_sp, pd_sp = self._get_btn_springs("btn_dislike")
            p.save()
            sc_d = 1.0 + hd_sp.value * 0.14 - pd_sp.value * 0.12
            p.translate(btn_dislike_r.center()); p.scale(sc_d, sc_d); p.translate(-btn_dislike_r.center())
            p.setPen(QPen(QColor(255, 255, 255, int(lerp(18, 52, hd_sp.value))), 1.0))
            p.setBrush(QColor(34, 20, 26, 235) if is_disliked else QColor(24, 24, 30, 220))
            p.drawRoundedRect(btn_dislike_r, 9.0, 9.0)
            VectorIcons.draw_dislike(p, btn_dislike_r.center(), 13.5, QColor(240, 95, 115) if is_disliked else QColor(195, 195, 208), active=is_disliked)
            p.restore()

            hl_sp, pl_sp = self._get_btn_springs("btn_like")
            p.save()
            sc_l = 1.0 + hl_sp.value * 0.16 - pl_sp.value * 0.12
            p.translate(btn_like_r.center()); p.scale(sc_l, sc_l); p.translate(-btn_like_r.center())
            p.setPen(QPen(QColor(255, 70, 115, 180) if is_liked else QColor(255, 255, 255, int(lerp(18, 52, hl_sp.value))), 1.0))
            p.setBrush(QColor(46, 18, 30, 245) if is_liked else QColor(24, 24, 30, 220))
            p.drawRoundedRect(btn_like_r, 9.0, 9.0)
            VectorIcons.draw_heart(
                p,
                btn_like_r.center(),
                13.5,
                QColor(255, 65, 115) if is_liked else QColor(215, 215, 225, int(lerp(195, 255, hl_sp.value))),
                filled=is_liked,
            )
            p.restore()

            # -10с and +10с seek pills
            for skey, srect, slbl in (
                ("btn_seek_back", QRectF(cx - 110.0, ctrl_cy - 12.0, 34.0, 24.0), "-10с"),
                ("btn_seek_fwd", QRectF(cx + 76.0, ctrl_cy - 12.0, 34.0, 24.0), "+10с"),
            ):
                self._hitboxes[skey] = srect
                hs, _ = self._get_btn_springs(skey)
                p.setPen(QPen(QColor(255, 255, 255, int(lerp(16, 48, hs.value))), 1.0))
                p.setBrush(QColor(24, 24, 30, 220))
                p.drawRoundedRect(srect, 8.0, 8.0)
                p.setPen(QColor(200, 200, 210, int(lerp(180, 255, hs.value))))
                p.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
                p.drawText(srect, Qt.AlignmentFlag.AlignCenter, slbl)

            for bkey, brect, is_play in (
                ("btn_prev", QRectF(cx - 64.0, ctrl_cy - 14.0, 28.0, 28.0), False),
                ("btn_play", QRectF(cx - 20.0, ctrl_cy - 20.0, 40.0, 40.0), True),
                ("btn_next", QRectF(cx + 36.0, ctrl_cy - 14.0, 28.0, 28.0), False),
            ):
                self._hitboxes[bkey] = brect
                h_sp, p_sp = self._get_btn_springs(bkey)
                sc = 1.0 + h_sp.value * 0.12 - p_sp.value * 0.12
                p.save()
                p.translate(brect.center())
                p.scale(sc, sc)
                p.translate(-brect.center())
                if is_play:
                    if track.is_playing:
                        p.setPen(Qt.PenStyle.NoPen)
                        p.setBrush(QColor(r1, g1, b1, int(45 + peak * 90)))
                        p.drawEllipse(brect.adjusted(-4, -4, 4, 4))
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(QColor(255, 255, 255, 252))
                    p.drawEllipse(brect)
                    if track.is_playing:
                        VectorIcons.draw_pause(p, brect.center(), 15.0, QColor(0, 0, 0, 255))
                    else:
                        VectorIcons.draw_play(p, brect.center(), 15.0, QColor(0, 0, 0, 255))
                elif bkey == "btn_prev":
                    VectorIcons.draw_prev(p, brect.center(), 15.0, QColor(215, 215, 225, int(lerp(205, 255, h_sp.value))))
                else:
                    VectorIcons.draw_next(p, brect.center(), 15.0, QColor(215, 215, 225, int(lerp(205, 255, h_sp.value))))
                p.restore()

        p.restore()

        # 2. Left Middle Volume & Output Bar (410 x 44 px — EVERY Button is now 100% Interactive & Clear!)
        v_card = self._draw_obsidian_card(p, QRectF(left_x, top + 240.0, 410.0, 44.0), 14.0, cascade_order=1)
        v_cy = v_card.center().y()

        # Interactive Mode Toggle Pill: [ 🔊 Общий | ♪ Плеер ]
        mode_pill = QRectF(v_card.left() + 8.0, v_cy - 14.0, 52.0, 28.0)
        self._hitboxes["btn_vol_mode_sys"] = QRectF(mode_pill.left(), mode_pill.top(), 26.0, 28.0)
        self._hitboxes["btn_vol_mode_app"] = QRectF(mode_pill.left() + 26.0, mode_pill.top(), 26.0, 28.0)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(26, 26, 32, 255))
        p.drawRoundedRect(mode_pill, 9.0, 9.0)

        active_pill_x = mode_pill.left() + (2.0 if self.vol_control_mode == 0 else 26.0)
        p.setBrush(accent_col if self.vol_control_mode == 1 else QColor(50, 50, 60, 255))
        p.drawRoundedRect(QRectF(active_pill_x, mode_pill.top() + 2.0, 24.0, 24.0), 7.0, 7.0)
        VectorIcons.draw_speaker_box(p, QPointF(mode_pill.left() + 14.0, v_cy), 13.0, QColor(255, 255, 255, 245 if self.vol_control_mode == 0 else 140))
        VectorIcons.draw_music_note_single(p, QPointF(mode_pill.left() + 38.0, v_cy), 13.0, QColor(255, 255, 255, 250 if self.vol_control_mode == 1 else 140))

        # Active volume value & mute status (Master vs Player App)
        disp_vol = self._last_master_vol
        disp_mute = self._last_master_mute
        if self.vol_control_mode == 1:
            ps = self._find_player_audio_session()
            if ps:
                disp_vol = round(ps.volume * 100.0)
                disp_mute = ps.muted

        mute_r = QRectF(v_card.left() + 66.0, v_cy - 14.0, 28.0, 28.0)
        self._hitboxes["btn_mute"] = mute_r
        VectorIcons.draw_speaker(p, mute_r.center(), 14.5, QColor(225, 225, 235, 245), disp_vol / 100.0, disp_mute)

        v_hov, _ = self._get_btn_springs("slider_vol")
        vh = lerp(5.0, 8.0, max(v_hov.value, 1.0 if self._dragging_vol_slider else 0.0))
        sl_r = QRectF(v_card.left() + 100.0, v_cy - vh * 0.5, 122.0, vh)
        self._hitboxes["slider_vol"] = sl_r.adjusted(0, -10, 0, 10)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(44, 44, 52, 255))
        p.drawRoundedRect(sl_r, vh * 0.5, vh * 0.5)
        v_ratio = 0.0 if disp_mute else clamp(disp_vol / 100.0)
        if v_ratio > 0.005:
            p.setBrush(accent_col if self.vol_control_mode == 1 else QColor(255, 255, 255, 248))
            p.drawRoundedRect(QRectF(sl_r.left(), sl_r.top(), sl_r.width() * v_ratio, vh), vh * 0.5, vh * 0.5)

        p.setPen(QColor(175, 175, 185, 240))
        p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        p.drawText(QRectF(sl_r.right() + 6.0, v_cy - 12.0, 26.0, 24.0), Qt.AlignmentFlag.AlignCenter, f"{int(disp_vol)}")

        # Per-App Mixer toggle button
        mix_btn = QRectF(v_card.left() + 260.0, v_cy - 14.0, 28.0, 28.0)
        self._hitboxes["btn_inline_mixer"] = mix_btn
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(accent_col if self.show_inline_mixer else QColor(30, 30, 38, 255))
        p.drawRoundedRect(mix_btn, 8.0, 8.0)
        VectorIcons.draw_sliders_h(p, mix_btn.center(), 13.0, QColor(255, 255, 255, 248))

        # Widened Audio Output / Limiter Mode Pill (108px so text NEVER clips!)
        fx_labels = {0: "Динамики", 1: "Лимитер", 2: "Ночной 45%"}
        dev_pill = QRectF(v_card.left() + 294.0, v_cy - 14.0, 108.0, 28.0)
        self._hitboxes["btn_audio_fx"] = dev_pill
        p.setPen(QPen(accent_col, 1.0) if self.audio_fx_mode > 0 else Qt.PenStyle.NoPen)
        p.setBrush(QColor(30, 24, 34, 255) if self.audio_fx_mode > 0 else QColor(26, 26, 32, 255))
        p.drawRoundedRect(dev_pill, 9.0, 9.0)
        VectorIcons.draw_speaker_box(p, QPointF(dev_pill.left() + 14.0, v_cy), 11.5, accent_col if self.audio_fx_mode > 0 else QColor(185, 185, 195))
        p.setPen(QColor(245, 245, 250, 245))
        p.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold if self.audio_fx_mode > 0 else QFont.Weight.Medium))
        p.drawText(dev_pill.adjusted(25, 0, -18, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, fx_labels[self.audio_fx_mode])
        VectorIcons.draw_chevron_down(p, QPointF(dev_pill.right() - 11.0, v_cy), 9.5, QColor(165, 165, 175))

        # 3. Sleek Monochrome Obsidian 2x4 Quick Actions Matrix (Zero rainbow clutter — pure OLED minimalism!)
        qa_x = left_x + 420.0
        qa_w = 111.0
        qa_h = 65.0
        qa_gap_x = 10.0
        qa_gap_y = 8.0

        now = time.monotonic()
        pipette_sub = self.fn_picked_hex if now < self.fn_color_scanning_until else "HEX цвет"
        qa_specs = [
            ("qa_area", "Область", "Скрин зоны", VectorIcons.draw_area_select),
            ("qa_full", "Экран", "Весь снимок", VectorIcons.draw_monitor),
            ("qa_ocr", "Текст", "С экрана", VectorIcons.draw_ocr_scan),
            ("qa_qr", "QR-код", "Из буфера", VectorIcons.draw_qr),
            ("qa_pipette", "Пипетка", pipette_sub, VectorIcons.draw_pipette),
            ("qa_clean", "Очистка", "Без пробелов", VectorIcons.draw_sparkle),
            ("qa_record", "Буст ОЗУ", f"ОЗУ {self.telemetry.ram_percent}%", VectorIcons.draw_lightning),
            ("btn_open_spotlight", "Поиск", "Alt + Space", VectorIcons.draw_search),
        ]

        for idx, (qkey, qlabel, qsub, qicon) in enumerate(qa_specs):
            col_i = idx % 2
            row_i = idx // 2
            qx = qa_x + col_i * (qa_w + qa_gap_x)
            qy = top + row_i * (qa_h + qa_gap_y)
            h_sp, p_sp = self._get_btn_springs(qkey)
            hov, prs = h_sp.value, p_sp.value

            q_border = QColor(255, 255, 255, int(lerp(18, 62, hov))) if hov > 0.04 else None
            qrect = self._draw_obsidian_card(
                p,
                QRectF(qx, qy, qa_w, qa_h),
                15.0,
                cascade_order=idx + 1,
                hovered_t=hov,
                active_border=q_border,
            )
            self._hitboxes[qkey] = qrect

            sc = 1.0 + hov * 0.035 - prs * 0.06
            p.save()
            p.translate(qrect.center())
            p.scale(sc, sc)
            p.translate(-qrect.center())

            # Pure neutral dark-glass icon container + crisp monochrome white/silver vector glyph
            badge_r = QRectF(qrect.left() + 9.0, qrect.center().y() - 14.0, 28.0, 28.0)
            p.setPen(QPen(QColor(255, 255, 255, int(lerp(20, 65, hov))), 1.0))
            p.setBrush(QColor(int(lerp(24, 38, hov)), int(lerp(24, 38, hov)), int(lerp(31, 47, hov)), 255))
            p.drawRoundedRect(badge_r, 9.0, 9.0)
            qicon(p, badge_r.center(), 14.0, QColor(255, 255, 255, int(lerp(212, 255, hov))))

            # Single-line bold title + clean muted subtitle
            p.setPen(QColor(246, 246, 252, int(lerp(236, 255, hov))))
            p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
            p.drawText(
                QRectF(badge_r.right() + 7.0, qrect.top() + 14.0, qrect.right() - badge_r.right() - 10.0, 17.0),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                qlabel,
            )
            p.setPen(QColor(142, 145, 158, int(lerp(205, 245, hov))))
            p.setFont(QFont("Segoe UI", 7, QFont.Weight.Medium))
            p.drawText(
                QRectF(badge_r.right() + 7.0, qrect.top() + 33.0, qrect.right() - badge_r.right() - 10.0, 15.0),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                qsub,
            )
            p.restore()

        # 4. Sculpted Full-Width OLED Glass Smart Dock with Clear Labels (Up to 8 apps + '+ Добавить'!)
        dock_r = self._draw_obsidian_card(p, QRectF(left_x, top + 292.0, 652.0, 64.0), 16.0, cascade_order=5)
        cursor_in_dock = dock_r.adjusted(-12, -16, 12, 16).contains(QPointF(self.sp_mx.value, self.sp_my.value))

        short_names = {
            "folder": "Файлы",
            "notepad": "Блокнот",
            "ps": "Терминал",
            "taskmgr": "Задачи",
            "snip": "Ножницы",
            "calc": "Калькул.",
            "settings": "Настройки",
        }
        items_vis = self.launcher_items[:8]
        total_slots = len(items_vis) + 1
        slot_w = (dock_r.width() - 24.0) / float(total_slots)

        for idx, (lname, lpath, lkind) in enumerate(items_vis):
            slot_cx = dock_r.left() + 12.0 + (idx + 0.5) * slot_w
            lrect = QRectF(slot_cx - 17.0, dock_r.top() + 7.0, 34.0, 34.0)
            hit_r = QRectF(slot_cx - slot_w * 0.45, dock_r.top() + 3.0, slot_w * 0.9, dock_r.height() - 6.0)
            lkey = f"launch_{idx}"
            self._hitboxes[lkey] = hit_r
            h_sp, p_sp = self._get_btn_springs(lkey)

            wave_mag = 0.0
            if cursor_in_dock:
                dx_dock = abs(self.sp_mx.value - slot_cx)
                wave_mag = math.exp(-(dx_dock * dx_dock) / 2400.0)

            mag = max(h_sp.value, wave_mag)
            sc = 1.0 + mag * 0.16 - p_sp.value * 0.10
            lift_y = -mag * 3.8

            p.save()
            p.translate(lrect.center() + QPointF(0, lift_y))
            p.scale(sc, sc)
            p.translate(-lrect.center())
            self._draw_launcher_app_icon(p, lrect, lkind, lname, lpath)
            p.restore()

            lbl_txt = short_names.get(lkind, lname[:9])
            p.setPen(QColor(245, 245, 255, int(lerp(165, 255, mag))))
            p.setFont(QFont("Segoe UI", 7, QFont.Weight.DemiBold if mag > 0.35 else QFont.Weight.Medium))
            p.drawText(
                QRectF(slot_cx - slot_w * 0.48, dock_r.bottom() - 19.0, slot_w * 0.96, 15.0),
                Qt.AlignmentFlag.AlignCenter,
                lbl_txt,
            )

        # Evenly aligned '+' Add button in the last slot
        add_cx = dock_r.left() + 12.0 + (len(items_vis) + 0.5) * slot_w
        add_r = QRectF(add_cx - 17.0, dock_r.top() + 7.0, 34.0, 34.0)
        self._hitboxes["launch_add"] = QRectF(add_cx - slot_w * 0.45, dock_r.top() + 3.0, slot_w * 0.9, dock_r.height() - 6.0)
        h_add, _ = self._get_btn_springs("launch_add")
        p.setPen(QPen(QColor(255, 255, 255, int(lerp(42, 125, h_add.value))), 1.2, Qt.PenStyle.DashLine))
        p.setBrush(QColor(24, 24, 32, int(lerp(140, 235, h_add.value))))
        p.drawRoundedRect(add_r, 10.0, 10.0)
        p.setPen(QColor(220, 224, 240, 240))
        p.setFont(QFont("Segoe UI", 12, QFont.Weight.Light))
        p.drawText(add_r, Qt.AlignmentFlag.AlignCenter, "+")
        p.setPen(QColor(165, 168, 182, int(lerp(175, 250, h_add.value))))
        p.setFont(QFont("Segoe UI", 7, QFont.Weight.Medium))
        p.drawText(QRectF(add_cx - slot_w * 0.48, dock_r.bottom() - 19.0, slot_w * 0.96, 15.0), Qt.AlignmentFlag.AlignCenter, "Добавить")

    def _draw_launcher_app_icon(self, p: QPainter, r: QRectF, kind: str, name: str = "", path: str = ""):
        """Sculpted OLED Glass Squircle App Icons — uses REAL Win32/Rust .exe icons for custom apps!"""
        p.save()
        if kind == "custom" and path:
            real_pix = self._get_real_exe_pixmap(path)
            if real_pix is not None:
                p.setPen(QPen(QColor(255, 255, 255, 34), 0.9))
                p.setBrush(QColor(22, 22, 30, 240))
                p.drawRoundedRect(r, 10.0, 10.0)
                ic_in = r.adjusted(4.0, 4.0, -4.0, -4.0)
                ic_clip = QPainterPath()
                ic_clip.addRoundedRect(ic_in, 6.0, 6.0)
                p.setClipPath(ic_clip, Qt.ClipOperation.IntersectClip)
                p.drawPixmap(ic_in.toRect(), real_pix)
                p.restore()
                return
        palettes = {
            "folder": (QColor(251, 191, 36), QColor(217, 119, 6)),
            "notepad": (QColor(56, 189, 248), QColor(2, 132, 199)),
            "ps": (QColor(59, 130, 246), QColor(29, 78, 216)),
            "taskmgr": (QColor(52, 211, 153), QColor(5, 150, 105)),
            "snip": (QColor(244, 114, 182), QColor(190, 24, 93)),
            "calc": (QColor(167, 139, 250), QColor(109, 40, 217)),
            "settings": (QColor(129, 140, 248), QColor(67, 56, 202)),
            "custom": (QColor(56, 58, 72), QColor(28, 30, 38)),
        }
        c1, c2 = palettes.get(kind, (QColor(56, 58, 72), QColor(28, 30, 38)))
        grad = QLinearGradient(r.topLeft(), r.bottomRight())
        grad.setColorAt(0.0, c1)
        grad.setColorAt(1.0, c2)

        p.setPen(QPen(QColor(255, 255, 255, 55), 0.9))
        p.setBrush(QBrush(grad))
        p.drawRoundedRect(r, 10.0, 10.0)

        # Subtle top glossy highlight inside squircle
        gloss = QLinearGradient(r.topLeft(), QPointF(r.left(), r.center().y()))
        gloss.setColorAt(0.0, QColor(255, 255, 255, 58))
        gloss.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(gloss))
        p.drawRoundedRect(r.adjusted(1, 1, -1, -r.height() * 0.45), 9.0, 9.0)

        white = QColor(255, 255, 255, 250)
        if kind == "folder":
            VectorIcons.draw_shelf_box(p, r.center(), 15.5, white)
        elif kind == "notepad":
            VectorIcons.draw_note_edit(p, r.center(), 15.0, white)
        elif kind == "ps":
            p.setPen(white)
            p.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, ">_")
        elif kind == "taskmgr":
            p.setPen(QPen(white, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            cx, cy = r.center().x(), r.center().y()
            path = QPainterPath()
            path.moveTo(cx - 9, cy + 2)
            path.lineTo(cx - 4, cy + 2)
            path.lineTo(cx - 1, cy - 6)
            path.lineTo(cx + 3, cy + 6)
            path.lineTo(cx + 6, cy - 1)
            path.lineTo(cx + 9, cy - 1)
            p.drawPath(path)
        elif kind == "snip":
            VectorIcons.draw_scissors(p, r.center(), 15.5, white)
        elif kind == "calc":
            VectorIcons.draw_calc(p, r.center(), 15.5, white)
        elif kind == "custom":
            letter = (name.strip()[:1] or "A").upper()
            p.setPen(white)
            p.setFont(QFont("Segoe UI Variable Display", 12, QFont.Weight.Bold))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, letter)
        else:
            VectorIcons.draw_gear(p, r.center(), 15.5, white)
        p.restore()

    def _paint_inline_mixer_inside_card(self, p: QPainter, card: QRectF, accent_col: QColor):
        p.setPen(QColor(255, 255, 255, 250))
        p.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        p.drawText(QRectF(card.left() + 18, card.top() + 14, 240, 22), Qt.AlignmentFlag.AlignLeft, "Громкость программ")

        back_btn = QRectF(card.right() - 88.0, card.top() + 12.0, 72.0, 26.0)
        self._hitboxes["btn_close_mixer"] = back_btn
        p.setPen(QPen(QColor(255, 255, 255, 28), 1.0))
        p.setBrush(QColor(28, 28, 36, 230))
        p.drawRoundedRect(back_btn, 13.0, 13.0)
        p.setPen(QColor(235, 235, 245, 245))
        p.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
        p.drawText(back_btn, Qt.AlignmentFlag.AlignCenter, "Плеер")

        sessions = self._app_sessions
        if not sessions:
            p.setPen(QColor(165, 165, 175, 220))
            p.setFont(QFont("Segoe UI", 9))
            p.drawText(card.adjusted(20, 40, -20, -20), Qt.AlignmentFlag.AlignCenter, "Загрузка активных аудио-сессий...")
            return

        y = card.top() + 48.0
        for s in sessions[:4]:
            row_r = QRectF(card.left() + 14.0, y - 3.0, card.width() - 28.0, 36.0)
            p.setPen(QPen(QColor(255, 255, 255, 16), 1.0))
            p.setBrush(QColor(20, 20, 26, 210))
            p.drawRoundedRect(row_r, 11.0, 11.0)

            m_rect = QRectF(row_r.left() + 6.0, y + 2.0, 26.0, 26.0)
            self._hitboxes[f"app_mute_{s.name}"] = m_rect
            VectorIcons.draw_speaker(p, m_rect.center(), 13.5, QColor(255, 255, 255, 240), s.volume, s.muted)

            p.setPen(QColor(255, 255, 255, 245))
            p.setFont(QFont("Segoe UI", 9, QFont.Weight.DemiBold))
            p.drawText(QRectF(row_r.left() + 38.0, y, 108.0, 30.0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, s.name[:15])

            sl_rect = QRectF(row_r.left() + 150.0, y + 12.0, row_r.width() - 204.0, 6.0)
            self._hitboxes[f"app_slider_{s.name}"] = sl_rect.adjusted(0, -8, 0, 8)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(44, 44, 54, 255))
            p.drawRoundedRect(sl_rect, 3.0, 3.0)
            fill = 0.0 if s.muted else clamp(s.volume)
            if fill > 0.01:
                fw = sl_rect.width() * fill
                p.setBrush(QColor(255, 255, 255, 245))
                p.drawRoundedRect(QRectF(sl_rect.left(), sl_rect.top(), fw, 6.0), 3.0, 3.0)
                if s.peak > 0.01 and not s.muted:
                    pw = min(fw, sl_rect.width() * clamp(s.peak * 1.25))
                    p.setBrush(accent_col)
                    p.drawRoundedRect(QRectF(sl_rect.left(), sl_rect.top(), pw, 6.0), 3.0, 3.0)

            p.setPen(QColor(175, 175, 185, 235))
            p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
            p.drawText(QRectF(sl_rect.right() + 8.0, y, 38.0, 30.0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, f"{int(s.volume * 100)}%")
            y += 42.0

    # --- TAB 1: БУФЕР ---
    def _paint_tab_clipboard(self, p: QPainter, cap: QRectF, top: float, accent_col: QColor):
        left_x = cap.left() + 22.0
        w = cap.width() - 44.0

        s_bar = self._draw_obsidian_card(p, QRectF(left_x, top, w - 48.0, 38.0), 12.0, cascade_order=0)
        VectorIcons.draw_search(p, QPointF(s_bar.left() + 20.0, s_bar.center().y()), 14.0, QColor(145, 145, 155))
        p.setPen(QColor(145, 145, 155, 220))
        p.setFont(QFont("Segoe UI", 9))
        p.drawText(s_bar.adjusted(38, 0, -12, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, f"Поиск по истории ({len(self.clipboard_history)})")

        h_tr, _ = self._get_btn_springs("clip_clear")
        trash_r = self._draw_obsidian_card(p, QRectF(cap.right() - 60.0, top, 38.0, 38.0), 12.0, cascade_order=0, hovered_t=h_tr.value)
        self._hitboxes["clip_clear"] = trash_r
        VectorIcons.draw_trash_bin(p, trash_r.center(), 15.0, QColor(190, 190, 200, 230))

        y = top + 48.0
        for idx, entry in enumerate(self.clipboard_history[:4]):
            h_sp, _ = self._get_btn_springs(f"clip_item_{idx}")
            hov = h_sp.value
            card_r = self._draw_obsidian_card(
                p,
                QRectF(left_x, y, w, 64.0),
                14.0,
                cascade_order=idx + 1,
                hovered_t=max(hov, 0.55 if idx == 0 else 0.0),
            )
            self._hitboxes[f"clip_item_{idx}"] = card_r

            ic_box = QRectF(card_r.left() + 12.0, card_r.center().y() - 17.0, 34.0, 34.0)
            p.setPen(Qt.PenStyle.NoPen)
            if entry.text.startswith("#") and len(entry.text) in (4, 7):
                p.setBrush(QColor(entry.text))
                p.drawRoundedRect(ic_box, 9.0, 9.0)
            else:
                p.setBrush(QColor(38, 38, 46, 255) if idx == 0 else QColor(24, 24, 30, 255))
                p.drawRoundedRect(ic_box, 9.0, 9.0)
                VectorIcons.draw_text_t(p, ic_box.center(), 14.0, QColor(175, 175, 185))

            p.setPen(QColor(255, 255, 255, 250))
            t_font = QFont("Segoe UI", 10, QFont.Weight.DemiBold)
            p.setFont(t_font)
            one_line = entry.text.replace("\n", " ")
            p.drawText(
                QRectF(ic_box.right() + 14.0, card_r.top() + 10.0, card_r.width() - 80.0, 22.0),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                QFontMetrics(t_font).elidedText(one_line, Qt.TextElideMode.ElideRight, int(card_r.width() - 80)),
            )

            meta_x = ic_box.right() + 14.0
            if entry.pinned:
                VectorIcons.draw_pushpin(p, QPointF(meta_x + 5.0, card_r.top() + 43.0), 10.5, QColor(142, 162, 255, 235))
                meta_x += 15.0

            p.setPen(QColor(135, 135, 145, 225))
            p.setFont(QFont("Segoe UI", 8, QFont.Weight.Medium))
            p.drawText(
                QRectF(meta_x, card_r.top() + 34.0, 260.0, 18.0),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                f"{entry.source_app} · {entry.relative_time()}",
            )
            y += 70.0

        p.setPen(QColor(130, 130, 140, 210))
        p.setFont(QFont("Segoe UI", 8))
        p.drawText(QRectF(left_x, cap.bottom() - 34.0, 380.0, 18.0), Qt.AlignmentFlag.AlignLeft, "Клик — вставить в активное окно · Shift+клик — копировать")

    # --- TAB 2: ПОЛКА ---
    def _paint_tab_shelf(self, p: QPainter, cap: QRectF, top: float, accent_col: QColor):
        left_x = cap.left() + 22.0

        ph_card = self._draw_obsidian_card(p, QRectF(left_x, top, 196.0, 340.0), 18.0, cascade_order=0)
        mini_p = QRectF(ph_card.left() + 14.0, ph_card.top() + 18.0, 50.0, 26.0)
        p.setPen(QPen(QColor(255, 255, 255, 24), 1.0))
        p.setBrush(QColor(0, 0, 0, 255))
        p.drawRoundedRect(mini_p, 13.0, 13.0)
        self._draw_character_eyes(p, mini_p.center())

        p.setPen(QColor(255, 255, 255, 248))
        p.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        p.drawText(QRectF(ph_card.left() + 74.0, ph_card.top() + 14.0, 110.0, 18.0), Qt.AlignmentFlag.AlignLeft, "Полка ПК")
        p.setPen(QColor(135, 135, 145, 220))
        p.setFont(QFont("Segoe UI", 8))
        p.drawText(QRectF(ph_card.left() + 74.0, ph_card.top() + 31.0, 110.0, 16.0), Qt.AlignmentFlag.AlignLeft, f"файлов: {len(self.shelf_files)}")

        dash_box = QRectF(ph_card.left() + 14.0, ph_card.top() + 58.0, ph_card.width() - 28.0, 212.0)
        p.setPen(QPen(QColor(255, 255, 255, 28), 1.1, Qt.PenStyle.DashLine))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(dash_box, 14.0, 14.0)
        VectorIcons.draw_shelf_box(p, QPointF(dash_box.center().x(), dash_box.top() + 62.0), 22.0, QColor(150, 150, 160))
        p.setPen(QColor(150, 150, 160, 220))
        p.setFont(QFont("Segoe UI", 8))
        p.drawText(
            dash_box.adjusted(10, 78, -10, -10),
            Qt.AlignmentFlag.AlignCenter,
            "Брось файлы на остров —\nони подождут здесь,\nпока не вытащишь их\nв нужное окно",
        )

        btn_add = QRectF(ph_card.left() + 14.0, ph_card.bottom() - 52.0, ph_card.width() - 28.0, 38.0)
        self._hitboxes["launch_add"] = btn_add
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(142, 162, 255, 255))
        p.drawRoundedRect(btn_add, 11.0, 11.0)
        p.setPen(QColor(10, 10, 16, 255))
        p.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        p.drawText(btn_add, Qt.AlignmentFlag.AlignCenter, "+ Выбрать файл")

        rx = ph_card.right() + 18.0
        h_dr, _ = self._get_btn_springs("shelf_drag_all")
        drag_pill = self._draw_obsidian_card(p, QRectF(rx, top + 2.0, 168.0, 38.0), 13.0, cascade_order=1, hovered_t=h_dr.value)
        self._hitboxes["shelf_drag_all"] = drag_pill
        VectorIcons.draw_drag_grip(p, QPointF(drag_pill.left() + 18.0, drag_pill.center().y()), 14.0, QColor(175, 175, 185))
        p.setPen(QColor(255, 255, 255, 248))
        p.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        p.drawText(drag_pill.adjusted(30, 0, -10, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, f"Перетащить все  {len(self.shelf_files)}")

        clr_r = QRectF(cap.right() - 115.0, top + 2.0, 92.0, 38.0)
        self._hitboxes["shelf_clear"] = clr_r
        VectorIcons.draw_trash_bin(p, QPointF(clr_r.left() + 14.0, clr_r.center().y()), 13.5, QColor(150, 150, 160))
        p.setPen(QColor(150, 150, 160, 225))
        p.setFont(QFont("Segoe UI", 9))
        p.drawText(clr_r.adjusted(26, 0, 0, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "Очистить")

        display_files = self.shelf_files if self.shelf_files else [
            ("Отчёт Q3.pdf", "15:37"),
            ("logo-final.png", "14:49"),
            ("заметки.txt", "вчера"),
        ]
        fx = rx + 12.0
        fy = top + 92.0
        for idx, (fpath, t_str) in enumerate(display_files[:3]):
            fname = os.path.basename(fpath)
            ext = os.path.splitext(fname)[1].upper().replace(".", "") or "FILE"
            h_fc, _ = self._get_btn_springs(f"shelf_card_{idx}")
            lift = -h_fc.value * 6.0
            card_r = QRectF(fx + idx * 136.0, fy + lift, 92.0, 108.0)
            self._hitboxes[f"shelf_card_{idx}"] = card_r

            p.setPen(Qt.PenStyle.NoPen)
            if ext in ("PDF", "DOC", "DOCX"):
                p.setBrush(QColor(200, 62, 62))
            elif ext in ("PNG", "JPG", "JPEG", "WEBP"):
                p.setBrush(QColor(68, 138, 245))
            else:
                p.setBrush(QColor(82, 92, 108))
            p.drawRoundedRect(card_r, 12.0, 12.0)

            p.setPen(QPen(QColor(255, 255, 255, 95), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(card_r.left() + 14, card_r.top() + 22), QPointF(card_r.right() - 22, card_r.top() + 22))
            p.drawLine(QPointF(card_r.left() + 14, card_r.top() + 32), QPointF(card_r.right() - 32, card_r.top() + 32))

            p.setPen(QColor(255, 255, 255, 250))
            p.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
            p.drawText(card_r.adjusted(12, 0, -8, -10), Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignLeft, ext[:4])

            p.setFont(QFont("Segoe UI", 9, QFont.Weight.DemiBold))
            p.drawText(
                QRectF(card_r.left() - 14, card_r.bottom() + 14, 120.0, 20.0),
                Qt.AlignmentFlag.AlignCenter,
                QFontMetrics(p.font()).elidedText(fname, Qt.TextElideMode.ElideMiddle, 118),
            )
            p.setPen(QColor(142, 162, 255 if idx == 0 else 145, 220))
            p.setFont(QFont("Segoe UI", 8))
            p.drawText(QRectF(card_r.left() - 14, card_r.bottom() + 34, 120.0, 18.0), Qt.AlignmentFlag.AlignCenter, t_str)

        p.setPen(QColor(125, 125, 135, 210))
        p.setFont(QFont("Segoe UI", 8))
        p.drawText(QRectF(rx, cap.bottom() - 34.0, 420.0, 18.0), Qt.AlignmentFlag.AlignCenter, "Тяни файл в любое окно · клик по карточке — перетащить")

    # --- TAB 3: ТЕКСТ ---
    def _paint_tab_text(self, p: QPainter, cap: QRectF, top: float, accent_col: QColor):
        left_x = cap.left() + 22.0

        t_box = self._draw_obsidian_card(p, QRectF(left_x, top, 368.0, 304.0), 16.0, cascade_order=0)
        p.setPen(QColor(245, 245, 250, 248))
        p.setFont(QFont("Segoe UI", 10))
        p.drawText(
            t_box.adjusted(18, 16, -18, -16),
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap,
            self.working_text[:550],
        )

        txt = self.working_text
        chars = len(txt)
        no_sp = len(re.sub(r"\s", "", txt))
        words = len(txt.split())
        lines = max(1, len(txt.splitlines()))
        p.setPen(QColor(135, 135, 145, 225))
        p.setFont(QFont("Segoe UI", 8))
        p.drawText(
            QRectF(left_x, t_box.bottom() + 12.0, 280.0, 20.0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            f"{chars} симв. · {no_sp} без пробелов · {words} слов · {lines} стр.",
        )

        btn_paste = QRectF(t_box.right() - 56.0, t_box.bottom() + 8.0, 24.0, 24.0)
        btn_copy = QRectF(t_box.right() - 26.0, t_box.bottom() + 8.0, 24.0, 24.0)
        self._hitboxes["txt_paste"] = btn_paste
        self._hitboxes["txt_copy"] = btn_copy
        VectorIcons.draw_clipboard(p, btn_paste.center(), 14.0, QColor(175, 175, 185))
        VectorIcons.draw_note_edit(p, btn_copy.center(), 14.0, QColor(175, 175, 185))

        rx = t_box.right() + 16.0
        rw = cap.right() - 22.0 - rx
        hdr = self._draw_obsidian_card(p, QRectF(rx, top, rw, 36.0), 11.0, cascade_order=1)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(46, 46, 54, 255))
        p.drawRoundedRect(QRectF(hdr.left() + 3, hdr.top() + 3, hdr.width() * 0.54, 30.0), 9.0, 9.0)
        p.setPen(QColor(255, 255, 255, 250))
        p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        p.drawText(QRectF(hdr.left() + 3, hdr.top(), hdr.width() * 0.54, 36.0), Qt.AlignmentFlag.AlignCenter, "Инструменты")

        VectorIcons.draw_sparkle(p, QPointF(hdr.left() + hdr.width() * 0.67, hdr.center().y()), 11.0, QColor(150, 150, 160))
        p.setPen(QColor(150, 150, 160, 215))
        p.drawText(QRectF(hdr.left() + hdr.width() * 0.72, hdr.top(), hdr.width() * 0.26, 36.0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "ИИ")

        tools = [
            ("layout", "Раскладка"),
            ("upper", "ВЕРХНИЙ"),
            ("lower", "нижний"),
            ("title", "Каждое Слово"),
            ("sentence", "Предложение"),
            ("translit", "Транслит"),
            ("no_breaks", "Убрать переносы"),
            ("trim_spaces", "Лишние пробелы"),
            ("json_pretty", "JSON красиво"),
            ("json_min", "JSON в строку"),
            ("to_b64", "В Base64"),
            ("from_b64", "Из Base64"),
        ]
        bw = (rw - 8.0) * 0.5
        bh = 40.0
        for idx, (act, label) in enumerate(tools):
            ci = idx % 2
            ri = idx // 2
            bx = rx + ci * (bw + 8.0)
            by = top + 46.0 + ri * (bh + 8.0)
            tkey = f"txt_tool_{act}"
            h_sp, _ = self._get_btn_springs(tkey)
            brect = self._draw_obsidian_card(
                p,
                QRectF(bx, by, bw, bh),
                11.0,
                cascade_order=ri + 1,
                hovered_t=h_sp.value,
            )
            self._hitboxes[tkey] = brect
            p.setPen(QColor(255, 255, 255, 248))
            p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
            p.drawText(brect, Qt.AlignmentFlag.AlignCenter, label)

    # --- TAB 4: ЗАМЕТКА & ТАЙМЕР ---
    def _paint_tab_note(self, p: QPainter, cap: QRectF, top: float, accent_col: QColor):
        left_x = cap.left() + 22.0

        cats = ["Заметка", "Идеи", "Покупки"]
        cx = left_x
        for idx, cname in enumerate(cats):
            crect = QRectF(cx, top, 74.0, 28.0)
            self._hitboxes[f"note_cat_{idx}"] = crect
            is_act = idx == self.note_category
            p.setPen(QPen(QColor(255, 255, 255, 22 if is_act else 12), 1.0))
            p.setBrush(QColor(34, 34, 42, 255) if is_act else QColor(16, 16, 20, 255))
            p.drawRoundedRect(crect, 14.0, 14.0)
            p.setPen(QColor(255, 255, 255, 250 if is_act else 155))
            p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold if is_act else QFont.Weight.Medium))
            p.drawText(crect, Qt.AlignmentFlag.AlignCenter, cname)
            cx += 80.0

        n_card = self._draw_obsidian_card(p, QRectF(left_x, top + 36.0, 396.0, 298.0), 16.0, cascade_order=1)
        p.setPen(QColor(255, 255, 255, 250))
        p.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        p.drawText(QRectF(n_card.left() + 18, n_card.top() + 14, 250, 22), Qt.AlignmentFlag.AlignLeft, "Список задач и заметки")

        iy = n_card.top() + 46.0
        for idx, (itxt, done) in enumerate(self.note_items[self.note_category]):
            row_r = QRectF(n_card.left() + 18.0, iy, n_card.width() - 36.0, 28.0)
            self._hitboxes[f"note_chk_{idx}"] = row_r

            chk_c = QPointF(row_r.left() + 8.0, row_r.center().y())
            if done:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(245, 158, 11, 255))
                p.drawEllipse(chk_c, 7.5, 7.5)
                p.setPen(QPen(QColor(0, 0, 0), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawLine(QPointF(chk_c.x() - 3, chk_c.y()), QPointF(chk_c.x() - 0.5, chk_c.y() + 2.5))
                p.drawLine(QPointF(chk_c.x() - 0.5, chk_c.y() + 2.5), QPointF(chk_c.x() + 3.5, chk_c.y() - 2.5))
            else:
                p.setPen(QPen(QColor(135, 135, 145, 200), 1.4))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(chk_c, 7.5, 7.5)

            f = QFont("Segoe UI", 9, QFont.Weight.Medium)
            f.setStrikeOut(done)
            p.setFont(f)
            p.setPen(QColor(135, 135, 145, 200) if done else QColor(245, 245, 250, 248))
            p.drawText(row_r.adjusted(24, 0, 0, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, itxt)
            iy += 34.0

        t_card = self._draw_obsidian_card(
            p,
            QRectF(n_card.right() + 14.0, top, cap.right() - 22.0 - (n_card.right() + 14.0), 334.0),
            18.0,
            cascade_order=2,
        )

        ring_rect = QRectF(t_card.left() + 18.0, t_card.top() + 20.0, 88.0, 88.0)
        p.setPen(QPen(QColor(245, 158, 11, 45), 6.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(ring_rect)
        ratio = clamp(self.timer_remaining_sec / max(1.0, self.timer_total_sec))
        p.setPen(QPen(QColor(245, 158, 11, 255), 6.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(ring_rect, 90 * 16, -int(ratio * 360 * 16))

        p.setPen(QColor(255, 255, 255, 252))
        p.setFont(QFont("Segoe UI Variable Display", 14, QFont.Weight.Bold))
        p.drawText(ring_rect, Qt.AlignmentFlag.AlignCenter, format_time(self.timer_remaining_sec))

        bx = ring_rect.right() + 14.0
        bw = t_card.right() - 14.0 - bx
        btn_toggle = QRectF(bx, t_card.top() + 22.0, bw, 40.0)
        btn_plus = QRectF(bx, t_card.top() + 70.0, (bw - 6.0) * 0.5, 32.0)
        btn_rst = QRectF(bx + (bw + 6.0) * 0.5, t_card.top() + 70.0, (bw - 6.0) * 0.5, 32.0)
        self._hitboxes["timer_toggle"] = btn_toggle
        self._hitboxes["timer_plus1"] = btn_plus
        self._hitboxes["timer_reset"] = btn_rst

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(34, 34, 42, 255))
        p.drawRoundedRect(btn_toggle, 14.0, 14.0)
        p.drawRoundedRect(btn_plus, 11.0, 11.0)
        p.drawRoundedRect(btn_rst, 11.0, 11.0)

        ic_pt = QPointF(btn_toggle.center().x() - 24.0, btn_toggle.center().y())
        if self.timer_running:
            VectorIcons.draw_pause(p, ic_pt, 12.0, QColor(255, 255, 255, 250))
        else:
            VectorIcons.draw_play(p, ic_pt, 12.0, QColor(255, 255, 255, 250))

        p.setPen(QColor(255, 255, 255, 250))
        p.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        p.drawText(btn_toggle.adjusted(18, 0, 0, 0), Qt.AlignmentFlag.AlignCenter, "Пауза" if self.timer_running else "Старт")
        p.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
        p.drawText(btn_plus, Qt.AlignmentFlag.AlignCenter, "+1 мин")
        p.drawText(btn_rst, Qt.AlignmentFlag.AlignCenter, "Сброс")

        p.setPen(QColor(150, 150, 160, 225))
        p.setFont(QFont("Segoe UI", 8))
        p.drawText(
            QRectF(t_card.left() + 18.0, ring_rect.bottom() + 16.0, t_card.width() - 36.0, 18.0),
            Qt.AlignmentFlag.AlignLeft,
            f"{self.timer_label} · виден в свёрнутой пилюле",
        )

        p.setPen(QColor(175, 175, 185, 235))
        p.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        p.drawText(QRectF(t_card.left() + 18.0, ring_rect.bottom() + 52.0, 160.0, 20.0), Qt.AlignmentFlag.AlignLeft, "Напоминания  2")

        rems = [("Перерыв / разминка", "через 25 мин"), ("Проверить загрузки", "сегодня 21:00")]
        ry = ring_rect.bottom() + 82.0
        for rtitle, rsub in rems:
            p.setPen(QColor(245, 245, 250, 245))
            p.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
            p.drawText(QRectF(t_card.left() + 18.0, ry, t_card.width() - 28.0, 16.0), Qt.AlignmentFlag.AlignLeft, rtitle)
            p.setPen(QColor(135, 135, 145, 220))
            p.setFont(QFont("Segoe UI", 8))
            p.drawText(QRectF(t_card.left() + 18.0, ry + 16.0, t_card.width() - 28.0, 16.0), Qt.AlignmentFlag.AlignLeft, rsub)
            ry += 42.0

    # --- TAB 5: ПОГОДА (Completely Redesigned Cinematic iOS-18 Weather Hub!) ---
    def _paint_tab_weather(self, p: QPainter, cap: QRectF, top: float, accent_col: QColor):
        left_x = cap.left() + 22.0
        w = self.weather_worker.data

        # 1. Left Cinematic Sky Scene Card (198 x 342 px)
        casc0 = clamp(self.sp_cascade.value / 0.72)
        sky_card = QRectF(left_x, top + (1.0 - casc0) * 10.0, 198.0, 342.0)
        sky_path = QPainterPath()
        sky_path.addRoundedRect(sky_card, 18.0, 18.0)

        p.save()
        p.setClipPath(sky_path, Qt.ClipOperation.IntersectClip)
        # Multi-stop deep twilight sky gradient
        sky_grad = QLinearGradient(sky_card.topLeft(), sky_card.bottomRight())
        sky_grad.setColorAt(0.0, QColor(24, 38, 72, 255))
        sky_grad.setColorAt(0.48, QColor(42, 66, 112, 255))
        sky_grad.setColorAt(1.0, QColor(68, 98, 148, 255))
        p.fillPath(sky_path, QBrush(sky_grad))

        # Glowing Celestial Sun/Moon Orb with breathing corona in upper-right of sky card
        orb_c = QPointF(sky_card.right() - 42.0, sky_card.top() + 72.0 + math.sin(self._aurora_phase) * 3.0)
        orb_glow = QRadialGradient(orb_c, 64.0)
        orb_glow.setColorAt(0.0, QColor(255, 215, 120, 155))
        orb_glow.setColorAt(0.45, QColor(255, 185, 90, 55))
        orb_glow.setColorAt(1.0, QColor(255, 185, 90, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(orb_glow))
        p.drawEllipse(orb_c, 64.0, 64.0)
        p.setBrush(QColor(255, 232, 165, 240))
        p.drawEllipse(orb_c, 16.0, 16.0)

        # Parallax drifting snowflakes / starlight particles
        for idx_s, (sx_n, sy_n, sr, _) in enumerate(self._snow_particles):
            p.setBrush(QColor(255, 255, 255, 185 if idx_s % 2 == 0 else 105))
            p.drawEllipse(
                QPointF(sky_card.left() + sx_n * sky_card.width(), sky_card.top() + sy_n * sky_card.height()),
                sr,
                sr,
            )

        # City Header + Refresh
        VectorIcons.draw_location_pin(p, QPointF(sky_card.left() + 22, sky_card.top() + 26), 14.0, QColor(255, 255, 255, 250))
        p.setPen(QColor(255, 255, 255, 252))
        p.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        p.drawText(QRectF(sky_card.left() + 34, sky_card.top() + 16, 125, 22), Qt.AlignmentFlag.AlignLeft, w.city)

        ref_r = QRectF(sky_card.right() - 36, sky_card.top() + 15, 24, 24)
        self._hitboxes["weather_refresh"] = ref_r
        VectorIcons.draw_refresh(p, ref_r.center(), 13.5, QColor(255, 255, 255, 235))

        # Layered Hero Cloud with golden sun peeking behind
        VectorIcons.draw_weather_cloud(p, QPointF(sky_card.left() + 46, sky_card.top() + 74), 44.0, QColor(255, 255, 255, 248), filled=True, sun_behind=True)

        # Giant Thin Temperature + Condition Badge
        p.setPen(QColor(255, 255, 255, 255))
        p.setFont(QFont("Segoe UI Variable Display", 36, QFont.Weight.Light))
        p.drawText(QRectF(sky_card.left() + 16, sky_card.top() + 102, 150, 58), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, f"{w.temp}°")

        p.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        p.drawText(QRectF(sky_card.left() + 18, sky_card.top() + 164, 170, 20), Qt.AlignmentFlag.AlignLeft, w.condition)

        p.setPen(QColor(220, 232, 250, 225))
        p.setFont(QFont("Segoe UI", 8, QFont.Weight.Medium))
        p.drawText(
            QRectF(sky_card.left() + 18, sky_card.top() + 184, 172, 20),
            Qt.AlignmentFlag.AlignLeft,
            f"Ощущается {w.feels_like}° · {w.temp_min}°..{w.temp_max}°",
        )

        # Automatic Location Badge inside Sky Card (Manual cities removed as requested!)
        auto_pill = QRectF(sky_card.left() + 12.0, sky_card.top() + 214.0, sky_card.width() - 24.0, 28.0)
        self._hitboxes["weather_refresh"] = auto_pill
        p.setPen(QPen(QColor(255, 255, 255, 55), 1.0))
        p.setBrush(QColor(255, 255, 255, 28))
        p.drawRoundedRect(auto_pill, 11.0, 11.0)
        VectorIcons.draw_location_pin(p, QPointF(auto_pill.left() + 15.0, auto_pill.center().y()), 12.5, QColor(125, 211, 252, 245))
        p.setPen(QColor(255, 255, 255, 248))
        p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        p.drawText(auto_pill.adjusted(27, 0, -6, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "Точная геолокация")

        # Frosted Bottom Summary Pill
        sum_pill = QRectF(sky_card.left() + 12, sky_card.bottom() - 74, sky_card.width() - 24, 44.0)
        p.setPen(QPen(QColor(255, 255, 255, 32), 1.0))
        p.setBrush(QColor(18, 28, 52, 165))
        p.drawRoundedRect(sum_pill, 12.0, 12.0)
        p.setPen(QColor(255, 255, 255, 245))
        p.setFont(QFont("Segoe UI", 8, QFont.Weight.Medium))
        p.drawText(
            sum_pill.adjusted(10, 4, -10, -4),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            f"Ветер {w.wind_speed} м/с ({w.wind_dir_label})\nВлажность {w.humidity}% · {w.pressure_mm} мм",
        )
        p.setPen(QColor(210, 225, 245, 165))
        p.setFont(QFont("Segoe UI", 7))
        p.drawText(QRectF(sky_card.left() + 14, sky_card.bottom() - 23, 170, 15), Qt.AlignmentFlag.AlignLeft, "Живой метеорадар Open-Meteo")
        p.restore()
        p.setPen(QPen(QColor(255, 255, 255, 35), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(sky_card, 18.0, 18.0)

        # 2. Right Top Hourly Forecast Row with Glowing Bezier Temperature Spline!
        rx = sky_card.right() + 12.0
        rw = cap.right() - 22.0 - rx
        h_card = self._draw_obsidian_card(p, QRectF(rx, top, rw, 92.0), 16.0, cascade_order=1)

        col_w = (rw - 20.0) / 8.0
        hourly_slice = w.hourly[:8]
        temps = [ht for _, ht, _ in hourly_slice] or [0]
        min_t, max_t = min(temps), max(temps)
        span_t = max(2.0, float(max_t - min_t))

        curve_pts: List[QPointF] = []
        for idx, (hlbl, htemp, hprec) in enumerate(hourly_slice):
            cx = rx + 10.0 + idx * col_w
            norm_t = (htemp - min_t) / span_t
            pt_y = h_card.top() + 65.0 - norm_t * 10.0
            curve_pts.append(QPointF(cx + col_w * 0.5, pt_y))

            p.setPen(QColor(155, 155, 168, 230))
            p.setFont(QFont("Segoe UI", 7, QFont.Weight.DemiBold))
            p.drawText(QRectF(cx, h_card.top() + 6, col_w, 13), Qt.AlignmentFlag.AlignCenter, hlbl)
            VectorIcons.draw_weather_cloud(p, QPointF(cx + col_w * 0.5, h_card.top() + 25), 12.5, QColor(255, 255, 255, 240), filled=True, sun_behind=(idx % 2 == 0))
            p.setPen(QColor(125, 211, 252, 235))
            p.setFont(QFont("Segoe UI", 7, QFont.Weight.Medium))
            p.drawText(QRectF(cx, h_card.top() + 74, col_w, 13), Qt.AlignmentFlag.AlignCenter, f"{hprec} мм")

        # Draw Smooth Bezier Temperature Curve across the 8 hours
        if len(curve_pts) >= 2:
            cpath = QPainterPath()
            cpath.moveTo(curve_pts[0])
            for i in range(len(curve_pts) - 1):
                mid = QPointF((curve_pts[i].x() + curve_pts[i + 1].x()) * 0.5, (curve_pts[i].y() + curve_pts[i + 1].y()) * 0.5)
                cpath.quadTo(curve_pts[i], mid)
            cpath.lineTo(curve_pts[-1])
            p.setPen(QPen(QColor(125, 211, 252, 185), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(cpath)

            for idx, pt in enumerate(curve_pts):
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(125, 211, 252, 255))
                p.drawEllipse(pt, 2.4, 2.4)
                p.setPen(QColor(255, 255, 255, 252))
                p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
                p.drawText(QRectF(pt.x() - col_w * 0.5, pt.y() - 15.0, col_w, 14), Qt.AlignmentFlag.AlignCenter, f"{hourly_slice[idx][1]}°")

        # 3. Right Bottom 3x2 Grid of 6 High-Detail iOS-18 Weather Metric Tiles
        tw = (rw - 20.0) / 3.0
        th = 117.0
        uv_lbl = "низкий" if w.uv_index <= 2 else ("средний" if w.uv_index <= 5 else "высокий")
        tiles_meta = [
            ("ВЕТЕР", f"{w.wind_speed} м/с", w.wind_dir_label, "wind"),
            ("ВЛАЖНОСТЬ", f"{w.humidity}%", f"Точка росы {w.dew_point}°", "hum"),
            ("ДАВЛЕНИЕ", f"{w.pressure_mm}", "мм рт. ст.", "press"),
            ("СОЛНЦЕ", f"{w.sunrise}", f"{w.sunset}", "sun"),
            ("ОСАДКИ", f"{w.precipitation_mm}", "мм / сут", "prec"),
            ("УФ-ИНДЕКС", f"{w.uv_index}", uv_lbl, "uv"),
        ]
        for idx, (mtitle, mval, msub, mkind) in enumerate(tiles_meta):
            ci = idx % 3
            ri = idx // 3
            tx = rx + ci * (tw + 10.0)
            ty = top + 100.0 + ri * (th + 8.0)
            trect = self._draw_obsidian_card(p, QRectF(tx, ty, tw, th), 15.0, cascade_order=idx + 2)
            tx, ty = trect.left(), trect.top()

            p.setPen(QColor(142, 162, 255, 220))
            p.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
            p.drawText(QRectF(tx + 12, ty + 9, tw - 24, 14), Qt.AlignmentFlag.AlignLeft, mtitle)

            if mkind == "wind":
                comp_c = QPointF(tx + 34, ty + 64)
                p.setPen(QPen(QColor(255, 255, 255, 38), 1.2))
                p.setBrush(QColor(22, 24, 32, 255))
                p.drawEllipse(comp_c, 22.0, 22.0)
                # 12 compass tick marks
                for ti in range(12):
                    tang = ti * (math.pi / 6.0)
                    p.setPen(QPen(QColor(255, 255, 255, 110 if ti % 3 == 0 else 45), 1.1))
                    p.drawLine(
                        QPointF(comp_c.x() + math.cos(tang) * 18.0, comp_c.y() + math.sin(tang) * 18.0),
                        QPointF(comp_c.x() + math.cos(tang) * 21.5, comp_c.y() + math.sin(tang) * 21.5),
                    )
                # Oscillating live wind compass needle
                gust = math.sin(self._aurora_phase * 2.2) * 0.12
                ang = math.radians(w.wind_dir_deg - 90) + gust
                p.setPen(QPen(accent_col, 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawLine(comp_c, QPointF(comp_c.x() + math.cos(ang) * 15.0, comp_c.y() + math.sin(ang) * 15.0))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(255, 255, 255))
                p.drawEllipse(comp_c, 2.5, 2.5)

                p.setPen(QColor(255, 255, 255, 252))
                p.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
                p.drawText(QRectF(tx + 64, ty + 42, tw - 68, 22), Qt.AlignmentFlag.AlignLeft, mval)
                p.setPen(QColor(155, 155, 168))
                p.setFont(QFont("Segoe UI", 8))
                p.drawText(QRectF(tx + 64, ty + 64, tw - 68, 18), Qt.AlignmentFlag.AlignLeft, msub)

            elif mkind == "hum":
                p.setPen(QColor(255, 255, 255, 252))
                p.setFont(QFont("Segoe UI Variable Display", 16, QFont.Weight.Bold))
                p.drawText(QRectF(tx + 12, ty + 28, tw - 24, 28), Qt.AlignmentFlag.AlignLeft, mval)
                bar = QRectF(tx + 12, ty + 64, tw - 24, 6.0)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(36, 38, 48))
                p.drawRoundedRect(bar, 3.0, 3.0)
                h_grad = QLinearGradient(bar.topLeft(), bar.topRight())
                h_grad.setColorAt(0.0, QColor(56, 189, 248))
                h_grad.setColorAt(1.0, QColor(129, 140, 248))
                p.setBrush(QBrush(h_grad))
                p.drawRoundedRect(QRectF(bar.left(), bar.top(), bar.width() * clamp(w.humidity / 100.0), 6.0), 3.0, 3.0)
                p.setPen(QColor(155, 155, 168))
                p.setFont(QFont("Segoe UI", 8))
                p.drawText(QRectF(tx + 12, ty + 90, tw - 24, 18), Qt.AlignmentFlag.AlignLeft, msub)

            elif mkind == "press":
                arc_r = QRectF(tx + 12, ty + 40, 50, 50)
                p.setPen(QPen(QColor(45, 48, 60), 4.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawArc(arc_r, -30 * 16, 240 * 16)
                p.setPen(QPen(QColor(142, 162, 255, 245), 4.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawArc(arc_r, 30 * 16, 135 * 16)
                p.setPen(QColor(255, 255, 255, 252))
                p.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
                p.drawText(QRectF(tx + 70, ty + 40, tw - 76, 22), Qt.AlignmentFlag.AlignLeft, mval)
                p.setPen(QColor(155, 155, 168))
                p.setFont(QFont("Segoe UI", 8))
                p.drawText(QRectF(tx + 70, ty + 62, tw - 76, 18), Qt.AlignmentFlag.AlignLeft, msub)

            elif mkind == "sun":
                # Horizon line + golden trajectory + glowing sun orb
                p.setPen(QPen(QColor(255, 255, 255, 30), 1.0))
                p.drawLine(QPointF(tx + 12, ty + 76), QPointF(tx + tw - 12, ty + 76))
                sun_path = QPainterPath()
                sun_path.moveTo(tx + 14, ty + 76)
                sun_path.quadTo(tx + tw * 0.5, ty + 22, tx + tw - 14, ty + 76)
                p.setPen(QPen(QColor(250, 195, 65, 210), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(sun_path)
                sun_pt = QPointF(tx + tw * 0.64, ty + 55)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(250, 195, 65, 85))
                p.drawEllipse(sun_pt, 9.5, 9.5)
                p.setBrush(QColor(255, 225, 115, 255))
                p.drawEllipse(sun_pt, 4.8, 4.8)
                p.setPen(QColor(165, 165, 178))
                p.setFont(QFont("Segoe UI", 7, QFont.Weight.DemiBold))
                p.drawText(QRectF(tx + 12, ty + 90, 55, 16), Qt.AlignmentFlag.AlignLeft, mval)
                p.drawText(QRectF(tx + tw - 67, ty + 90, 55, 16), Qt.AlignmentFlag.AlignRight, msub)

            elif mkind == "prec":
                p.setPen(QColor(255, 255, 255, 252))
                p.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
                p.drawText(QRectF(tx + 12, ty + 28, tw - 24, 22), Qt.AlignmentFlag.AlignLeft, f"{mval} {msub}")
                p.setPen(Qt.PenStyle.NoPen)
                for bi in range(14):
                    bh = 6.0 + (math.sin(bi * 0.6 + self._aurora_phase * 1.4) * 0.5 + 0.5) * 20.0
                    p.setBrush(QColor(56, 189, 248, int(145 + bh * 4)))
                    p.drawRoundedRect(QRectF(tx + 14 + bi * 8.2, ty + 100 - bh, 3.6, bh), 1.8, 1.8)

            else:
                p.setPen(QColor(255, 255, 255, 252))
                p.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
                p.drawText(QRectF(tx + 12, ty + 28, tw - 24, 24), Qt.AlignmentFlag.AlignLeft, f"{mval} · {msub}")
                bar = QRectF(tx + 12, ty + 66, tw - 24, 6.0)
                uv_g = QLinearGradient(bar.topLeft(), bar.topRight())
                uv_g.setColorAt(0.0, QColor(74, 222, 128))
                uv_g.setColorAt(0.45, QColor(250, 204, 21))
                uv_g.setColorAt(0.75, QColor(249, 115, 22))
                uv_g.setColorAt(1.0, QColor(168, 85, 247))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(uv_g))
                p.drawRoundedRect(bar, 3.0, 3.0)
                p.setBrush(QColor(255, 255, 255))
                p.drawEllipse(QPointF(bar.left() + 12, bar.center().y()), 4.8, 4.8)
                p.setPen(QColor(155, 155, 168))
                p.setFont(QFont("Segoe UI", 7))
                p.drawText(QRectF(tx + 12, ty + 90, tw - 24, 16), Qt.AlignmentFlag.AlignLeft, "Защита не требуется")

    # --- TAB 6: УПРАВЛЕНИЕ (Чистая панель быстрых переключателей, телеметрии и питания) ---
    def _paint_tab_controls(self, p: QPainter, cap: QRectF, top: float, accent_col: QColor):
        left_x = cap.left() + 22.0
        w = cap.width() - 44.0
        periwinkle = QColor(142, 162, 255, 255)

        cw = (w - 30.0) / 4.0
        ch = 104.0
        bat_pct = self.telemetry.battery_percent if self.telemetry.battery_percent is not None else 100
        top_cards = [
            ("ctrl_wifi", "Wi-Fi", "Включён" if self.wifi_enabled else "Выключен", self.wifi_enabled, VectorIcons.draw_wifi),
            ("ctrl_bt", "Bluetooth", "Включён" if self.bt_enabled else "Выключен", self.bt_enabled, VectorIcons.draw_bluetooth),
            ("ctrl_theme", "Тёмная тема", "Включена" if self.telemetry.is_dark_theme else "Светлая", self.telemetry.is_dark_theme, VectorIcons.draw_theme_contrast),
            (
                "ctrl_awake",
                "Не гасить ПК" if self.telemetry.battery_percent is None else "Батарея",
                "Активен" if self.keep_awake else f"Питание {bat_pct}%",
                self.keep_awake,
                lambda painter, c, s, col: VectorIcons.draw_battery_icon(painter, c, s, col, bat_pct / 100.0),
            ),
        ]

        for idx, (ckey, ctitle, csub, is_on, ic_fn) in enumerate(top_cards):
            cx = left_x + idx * (cw + 10.0)
            h_sp, _ = self._get_btn_springs(ckey)
            crect = self._draw_obsidian_card(
                p,
                QRectF(cx, top, cw, ch),
                16.0,
                cascade_order=idx,
                hovered_t=max(h_sp.value, 0.35 if is_on else 0.0),
            )
            self._hitboxes[ckey] = crect

            ic_c = QPointF(crect.left() + 28.0, crect.top() + 30.0)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(periwinkle if is_on else QColor(38, 38, 46, 255))
            p.drawEllipse(ic_c, 17.5, 17.5)

            ic_col = QColor(10, 10, 16, 255) if is_on else QColor(225, 225, 235, 240)
            ic_fn(p, ic_c, 15.0, ic_col)

            p.setPen(QColor(255, 255, 255, 252))
            p.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
            p.drawText(QRectF(crect.left() + 14, crect.top() + 56, cw - 20, 20), Qt.AlignmentFlag.AlignLeft, ctitle)
            p.setPen(QColor(145, 145, 155, 230))
            p.setFont(QFont("Segoe UI", 8, QFont.Weight.Medium))
            p.drawText(QRectF(crect.left() + 14, crect.top() + 76, cw - 20, 18), Qt.AlignmentFlag.AlignLeft, csub)

        mid_y = top + ch + 10.0
        tel_w = w * 0.56
        tel_r = self._draw_obsidian_card(p, QRectF(left_x, mid_y, tel_w, 76.0), 16.0, cascade_order=3)

        gauges = [
            (f"{self.telemetry.cpu_percent} %", "Процессор", self.telemetry.cpu_percent / 100.0),
            (f"{self.telemetry.ram_percent} %", "Память", self.telemetry.ram_percent / 100.0),
            (f"{self.telemetry.gpu_percent} %", "Видеокарта", self.telemetry.gpu_percent / 100.0),
        ]
        gx = tel_r.left() + 16.0
        ty_m = tel_r.top() + 12.0
        for gval, glbl, grat in gauges:
            p.setPen(QColor(255, 255, 255, 250))
            p.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
            p.drawText(QRectF(gx, ty_m + 2, 72, 18), Qt.AlignmentFlag.AlignLeft, gval)

            gbar = QRectF(gx, ty_m + 26, 74.0, 5.5)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(42, 42, 50, 255))
            p.drawRoundedRect(gbar, 2.7, 2.7)
            p.setBrush(periwinkle)
            p.drawRoundedRect(QRectF(gbar.left(), gbar.top(), gbar.width() * clamp(grat), 5.5), 2.7, 2.7)

            p.setPen(QColor(135, 135, 145, 225))
            p.setFont(QFont("Segoe UI", 7))
            p.drawText(QRectF(gx, ty_m + 37, 76, 16), Qt.AlignmentFlag.AlignLeft, glbl)
            gx += 88.0

        p.setPen(QColor(255, 255, 255, 248))
        p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        p.drawText(QRectF(gx, ty_m + 4, 102, 16), Qt.AlignmentFlag.AlignLeft, f"{self.telemetry.net_down_str}")
        p.setPen(QColor(145, 145, 155, 225))
        p.setFont(QFont("Segoe UI", 7))
        p.drawText(QRectF(gx, ty_m + 21, 102, 15), Qt.AlignmentFlag.AlignLeft, f"{self.telemetry.net_up_str}")
        p.drawText(QRectF(gx, ty_m + 37, 102, 15), Qt.AlignmentFlag.AlignLeft, "Сеть")

        br_r = self._draw_obsidian_card(p, QRectF(tel_r.right() + 10.0, mid_y, w - tel_w - 10.0, 76.0), 16.0, cascade_order=4)
        VectorIcons.draw_sun(p, QPointF(br_r.left() + 24.0, br_r.center().y()), 16.0, QColor(215, 215, 225, 235))

        p.setPen(QColor(155, 155, 165, 230))
        p.setFont(QFont("Segoe UI", 8, QFont.Weight.Medium))
        p.drawText(QRectF(br_r.left() + 46, br_r.top() + 15, 145, 18), Qt.AlignmentFlag.AlignLeft, "Яркость монитора")
        p.drawText(QRectF(br_r.right() - 58, br_r.top() + 15, 42, 18), Qt.AlignmentFlag.AlignRight, f"{self.dimmer_overlay.brightness_pct} %")

        b_bar = QRectF(br_r.left() + 46.0, br_r.top() + 43.0, br_r.width() - 62.0, 6.0)
        self._hitboxes["slider_bright"] = b_bar.adjusted(0, -10, 0, 10)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(42, 42, 50, 255))
        p.drawRoundedRect(b_bar, 3.0, 3.0)
        b_rat = clamp((self.dimmer_overlay.brightness_pct - 20) / 80.0)
        p.setBrush(QColor(235, 195, 38, 255))
        p.drawRoundedRect(QRectF(b_bar.left(), b_bar.top(), b_bar.width() * b_rat, 6.0), 3.0, 3.0)

        # Row 3: Power Action Cards (Блокировка, Сон, Перезагрузка, Выключение)
        py = mid_y + 86.0
        ph = 94.0
        now = time.monotonic()
        pwr_specs = [
            ("pwr_lock", "Блокировка", VectorIcons.draw_padlock),
            ("pwr_sleep", "Сон", VectorIcons.draw_moon_crescent),
            ("pwr_reboot", "Перезагрузка", VectorIcons.draw_refresh),
            ("pwr_shutdown", "Выключение", VectorIcons.draw_power_off),
        ]
        for idx, (pkey, plbl, p_icon) in enumerate(pwr_specs):
            px = left_x + idx * (cw + 10.0)
            h_sp, _ = self._get_btn_springs(pkey)
            confirming = self._power_confirm_key == pkey and now < self._power_confirm_until
            prect = self._draw_obsidian_card(
                p,
                QRectF(px, py, cw, ph),
                16.0,
                cascade_order=idx + 4,
                hovered_t=h_sp.value,
                active_border=QColor(235, 85, 100, 195) if confirming else None,
                fill_color=QColor(34, 18, 22, 255) if confirming else None,
            )
            self._hitboxes[pkey] = prect

            p_icon(p, QPointF(prect.center().x(), prect.top() + 31.0), 16.5, QColor(205, 205, 215, 240))
            p.setPen(QColor(255, 255, 255, 248))
            p.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
            txt_lbl = "Нажми ещё раз" if confirming else plbl
            p.drawText(QRectF(prect.left(), prect.top() + 56, cw, 22), Qt.AlignmentFlag.AlignCenter, txt_lbl)

        # Row 4: Monochrome Island Controls & Live Tests (Moved out of Updates tab cleanly into Управление!)
        ctrl_row_y = py + ph + 10.0
        float_lbl = "Форма: Парящ." if self.consent_mgr.island_float_mode == 1 else "Форма: Вырез"
        wave_names = {0: "Волна: Неон", 1: "Волна: Мягк.", 2: "Волна: Выкл"}
        bass_names = {0: "Бас: 100%", 1: "Бас: 155%+", 2: "Бас: 65%"}
        ctrl_pills = [
            ("btn_style_float", float_lbl),
            ("btn_style_wave", wave_names.get(self.consent_mgr.wave_style_mode, "Волна: Неон")),
            ("btn_style_bass", bass_names.get(self.consent_mgr.bass_sens_mode, "Бас: 100%")),
            ("btn_test_notif", "Тест увед."),
            ("btn_test_reveal", "Тест глазок"),
        ]
        cp_gap = 8.0
        cp_w = (w - 4 * cp_gap) / 5.0
        for s_idx, (s_key, s_lbl) in enumerate(ctrl_pills):
            s_rect = QRectF(left_x + s_idx * (cp_w + cp_gap), ctrl_row_y, cp_w, 38.0)
            self._hitboxes[s_key] = s_rect
            hs, _ = self._get_btn_springs(s_key)
            p.setPen(QPen(QColor(255, 255, 255, int(lerp(20, 68, hs.value))), 1.0))
            p.setBrush(QColor(int(lerp(18, 30, hs.value)), int(lerp(18, 30, hs.value)), int(lerp(24, 38, hs.value)), 248))
            p.drawRoundedRect(s_rect, 12.0, 12.0)
            p.setPen(QColor(240, 242, 252, int(lerp(225, 255, hs.value))))
            p.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
            p.drawText(s_rect, Qt.AlignmentFlag.AlignCenter, s_lbl)

    # --- TAB 7: ОТДЕЛЬНАЯ ВКЛАДКА ОБНОВЛЕНИЙ (Рядом с Закрепить) ---
    def _paint_tab_updates(self, p: QPainter, cap: QRectF, top: float, accent_col: QColor):
        left_x = cap.left() + 22.0
        w = cap.width() - 44.0
        periwinkle = QColor(142, 162, 255, 255)

        # Full Legal Document Viewer Overlay inside Updates tab when user clicks 'Политика' or 'Согласие'
        if self.legal_doc_mode in ("privacy", "consent"):
            doc_card = self._draw_obsidian_card(p, QRectF(left_x, top, w, 342.0), 18.0, cascade_order=0)
            is_priv = self.legal_doc_mode == "privacy"
            title_doc = "Политика конфиденциальности «Island»" if is_priv else "Согласие пользователя и условия «Island»"
            raw_doc = self.consent_mgr.read_document("PRIVACY_POLICY.md" if is_priv else "USER_CONSENT.md")
            clean_doc = re.sub(r"[#*>]", "", raw_doc).strip()

            p.setPen(QColor(255, 255, 255, 252))
            p.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
            p.drawText(
                QRectF(doc_card.left() + 18, doc_card.top() + 12, w - 120, 24),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                title_doc,
            )

            back_r = QRectF(doc_card.right() - 88, doc_card.top() + 11, 72, 26)
            self._hitboxes["btn_close_legal"] = back_r
            p.setPen(QPen(QColor(255, 255, 255, 35), 1.0))
            p.setBrush(QColor(34, 34, 44, 255))
            p.drawRoundedRect(back_r, 13.0, 13.0)
            p.setPen(QColor(255, 255, 255, 245))
            p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
            p.drawText(back_r, Qt.AlignmentFlag.AlignCenter, "Назад")

            p.setPen(QColor(195, 195, 208, 240))
            p.setFont(QFont("Segoe UI", 8))
            p.drawText(
                doc_card.adjusted(18, 42, -18, -54),
                Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap,
                clean_doc[:1180] + ("..." if len(clean_doc) > 1180 else ""),
            )

            chk_bar = QRectF(doc_card.left() + 14, doc_card.bottom() - 46, doc_card.width() - 28, 36)
            self._hitboxes["btn_toggle_consent"] = chk_bar
            p.setPen(QPen(QColor(142, 162, 255, 90 if self.consent_mgr.accepted else 30), 1.0))
            p.setBrush(QColor(22, 24, 32, 255))
            p.drawRoundedRect(chk_bar, 10.0, 10.0)

            box_r = QRectF(chk_bar.left() + 10, chk_bar.center().y() - 7.5, 15.0, 15.0)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(periwinkle if self.consent_mgr.accepted else QColor(45, 45, 55))
            p.drawRoundedRect(box_r, 4.0, 4.0)
            if self.consent_mgr.accepted:
                p.setPen(QPen(QColor(10, 10, 16), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawLine(QPointF(box_r.left() + 3.5, box_r.center().y()), QPointF(box_r.center().x() - 0.5, box_r.bottom() - 3.5))
                p.drawLine(QPointF(box_r.center().x() - 0.5, box_r.bottom() - 3.5), QPointF(box_r.right() - 3.0, box_r.top() + 3.5))

            p.setPen(QColor(235, 235, 245, 245))
            p.setFont(QFont("Segoe UI", 7, QFont.Weight.DemiBold))
            p.drawText(
                chk_bar.adjusted(32, 0, -8, 0),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                "Я ознакомился и принимаю Политику конфиденциальности и Согласие на использование «Island» (Local-First)",
            )
            return

        # Centered Luxury Update Studio Card (No text wall of release notes, pure sculpted UI & hero Update button!)
        has_upd = self.updater.update_available
        u_card = self._draw_obsidian_card(
            p,
            QRectF(left_x, top, w, 342.0),
            20.0,
            cascade_order=0,
            active_border=QColor(accent_col.red(), accent_col.green(), accent_col.blue(), 115) if has_upd else None,
        )

        u_clip = QPainterPath()
        u_clip.addRoundedRect(u_card, 20.0, 20.0)
        p.save()
        p.setClipPath(u_clip, Qt.ClipOperation.IntersectClip)

        # Ambient dual-aurora backdrop glow + drifting starlight dust inside Update Studio
        breathe = 0.5 + 0.5 * math.sin(self._aurora_phase * 2.1)
        cx_u = u_card.center().x()
        orb_pt = QPointF(cx_u, u_card.top() + 76.0)

        bg_glow = QRadialGradient(orb_pt, 225.0)
        if has_upd:
            bg_glow.setColorAt(0.0, QColor(accent_col.red(), accent_col.green(), accent_col.blue(), int(86 + 26 * breathe)))
            bg_glow.setColorAt(0.48, QColor(142, 162, 255, int(34 + 14 * breathe)))
            bg_glow.setColorAt(1.0, QColor(12, 12, 18, 0))
        else:
            bg_glow.setColorAt(0.0, QColor(74, 222, 128, int(62 + 22 * breathe)))
            bg_glow.setColorAt(0.50, QColor(56, 189, 248, int(24 + 10 * breathe)))
            bg_glow.setColorAt(1.0, QColor(12, 12, 18, 0))
        p.fillRect(u_card, QBrush(bg_glow))

        # Living drifting starlight particles (zero-alloc deterministic trigonometry)
        p.setPen(Qt.PenStyle.NoPen)
        for pi in range(14):
            seed = pi * 1.6180339
            px_d = u_card.left() + 26.0 + ((math.sin(seed * 7.3 + self._aurora_phase * (0.28 + (pi % 3) * 0.12)) * 0.5 + 0.5) * (w - 52.0))
            py_d = u_card.top() + 18.0 + ((math.cos(seed * 5.1 - self._aurora_phase * (0.22 + (pi % 4) * 0.08)) * 0.5 + 0.5) * 185.0)
            p_alpha = int(32 + 52 * (0.5 + 0.5 * math.sin(self._aurora_phase * 2.6 + seed * 3.0)))
            p_col = accent_col if (has_upd and pi % 2 == 0) else (QColor(142, 162, 255) if pi % 3 == 0 else QColor(255, 255, 255))
            p.setBrush(QColor(p_col.red(), p_col.green(), p_col.blue(), p_alpha))
            pr = 1.2 + (pi % 3) * 0.55
            p.drawEllipse(QPointF(px_d, py_d), pr, pr)

        # Top-Left Hardware Engine Pill (Rust Core v2.1 Native DLL status + Display Hz) & Top-Right Channel Pill
        rust_active = RUST_CORE.is_loaded
        rc_pill = QRectF(u_card.left() + 16.0, u_card.top() + 14.0, 152.0, 22.0)
        p.setPen(QPen(QColor(251, 146, 60, 120) if rust_active else QColor(148, 163, 184, 70), 1.0))
        p.setBrush(QColor(42, 24, 14, 195) if rust_active else QColor(24, 25, 34, 190))
        p.drawRoundedRect(rc_pill, 11.0, 11.0)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(251, 146, 60, 245) if rust_active else QColor(148, 163, 184, 210))
        p.drawEllipse(QPointF(rc_pill.left() + 11.0, rc_pill.center().y()), 3.0, 3.0)
        p.setPen(QColor(255, 218, 185, 248) if rust_active else QColor(200, 206, 220, 220))
        p.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
        p.drawText(
            rc_pill.adjusted(18, 0, -4, 0),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            f"Rust Core v2.2 · {self._display_hz}Hz" if rust_active else f"Python Engine · {self._display_hz}Hz",
        )

        gh_pill = QRectF(u_card.right() - 122.0, u_card.top() + 14.0, 106.0, 22.0)
        p.setPen(QPen(QColor(142, 162, 255, 75), 1.0))
        p.setBrush(QColor(22, 24, 36, 190))
        p.drawRoundedRect(gh_pill, 11.0, 11.0)
        p.setPen(QColor(186, 198, 255, 235))
        p.setFont(QFont("Segoe UI", 7, QFont.Weight.DemiBold))
        p.drawText(gh_pill, Qt.AlignmentFlag.AlignCenter, "GitHub OTA")

        # 3-Ring Counter-Rotating Orbital Gyroscope around central emblem + satellite nodes
        ring_specs = [
            (49.0, 48.0, 200, 1.15, 72),
            (39.0, -64.0, 235, 1.35, 115),
            (31.0, 92.0, 150, 1.05, 85),
        ]
        orb_c_base = accent_col if has_upd else QColor(74, 222, 128)
        for ri, (r_rad, spd, span_deg, pen_w, alpha_v) in enumerate(ring_specs):
            ang_deg = (self._aurora_phase * spd + ri * 75.0) % 360.0
            r_col = QColor(142, 162, 255, alpha_v) if (ri == 2 and has_upd) else QColor(orb_c_base.red(), orb_c_base.green(), orb_c_base.blue(), alpha_v)
            p.setPen(QPen(r_col, pen_w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawArc(QRectF(orb_pt.x() - r_rad, orb_pt.y() - r_rad, r_rad * 2, r_rad * 2), int(ang_deg * 16), span_deg * 16)
            # Orbiting luminous satellite dot at arc tip
            rad_tip = math.radians(-ang_deg)
            sx_dot = orb_pt.x() + math.cos(rad_tip) * r_rad
            sy_dot = orb_pt.y() + math.sin(rad_tip) * r_rad
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 210 if ri == 1 else 160))
            p.drawEllipse(QPointF(sx_dot, sy_dot), 2.1 if ri == 1 else 1.5, 2.1 if ri == 1 else 1.5)

        core_grad = QRadialGradient(orb_pt.x(), orb_pt.y() - 6.0, 28.0)
        core_grad.setColorAt(0.0, QColor(36, 36, 48, 255))
        core_grad.setColorAt(1.0, QColor(14, 14, 20, 255))
        p.setPen(QPen(orb_c_base if has_upd else QColor(74, 222, 128, 205), 1.65))
        p.setBrush(QBrush(core_grad))
        p.drawEllipse(orb_pt, 25.0, 25.0)
        if has_upd:
            VectorIcons.draw_download_arrow(p, orb_pt, 20.0, accent_col)
        else:
            VectorIcons.draw_refresh(p, orb_pt, 20.0, QColor(74, 222, 128, 250))

        # App Title & Version Transition Pill
        p.setPen(QColor(255, 255, 255, 252))
        p.setFont(QFont("Segoe UI Variable Display", 14, QFont.Weight.Bold))
        p.drawText(
            QRectF(u_card.left(), u_card.top() + 114.0, w, 24.0),
            Qt.AlignmentFlag.AlignCenter,
            f"Island  v{self.updater.local_version}",
        )

        status_pill = QRectF(cx_u - 122.0, u_card.top() + 141.0, 244.0, 25.0)
        p.setPen(QPen(accent_col if has_upd else QColor(74, 222, 128, 125), 1.1))
        p.setBrush(QColor(44, 18, 34, 218) if has_upd else QColor(16, 40, 28, 200))
        p.drawRoundedRect(status_pill, 12.5, 12.5)
        p.setPen(QColor(255, 228, 240) if has_upd else QColor(167, 243, 208))
        p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        p.drawText(
            status_pill,
            Qt.AlignmentFlag.AlignCenter,
            f"Доступна сборка  v{self.updater.remote_version}" if has_upd else "Установлена актуальная версия",
        )

        # Centerpiece Hero 'Обновить' Button (276 x 52 px)
        act_btn = QRectF(cx_u - 138.0, u_card.top() + 178.0, 276.0, 52.0)
        if has_upd:
            self._hitboxes["btn_github_update"] = act_btn
            hu, pu = self._get_btn_springs("btn_github_update")
            sc_b = 1.0 + hu.value * 0.04 - pu.value * 0.06
            p.save()
            p.translate(act_btn.center())
            p.scale(sc_b, sc_b)
            p.translate(-act_btn.center())

            # Multi-layer breathing plasma halo
            glow_rect = act_btn.adjusted(-11, -11, 11, 11)
            for gi in range(4):
                gr = glow_rect.adjusted(gi * 2.4, gi * 2.4, -gi * 2.4, -gi * 2.4)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(
                    QColor(
                        accent_col.red(),
                        accent_col.green(),
                        accent_col.blue(),
                        int((24 + 24 * breathe + 40 * hu.value) / (gi + 1)),
                    )
                )
                p.drawRoundedRect(gr, 25.0 - gi * 2.0, 25.0 - gi * 2.0)

            btn_path = QPainterPath()
            btn_path.addRoundedRect(act_btn, 18.0, 18.0)
            btn_grad = QLinearGradient(act_btn.topLeft(), act_btn.bottomRight())
            btn_grad.setColorAt(0.0, accent_col)
            btn_grad.setColorAt(0.48, QColor(244, 114, 182, 255))
            btn_grad.setColorAt(1.0, QColor(129, 140, 248, 255))
            p.fillPath(btn_path, QBrush(btn_grad))

            # Top glass reflection + animated diagonal light sheen
            p.save()
            p.setClipPath(btn_path, Qt.ClipOperation.IntersectClip)
            gloss = QLinearGradient(act_btn.topLeft(), act_btn.bottomLeft())
            gloss.setColorAt(0.0, QColor(255, 255, 255, 92))
            gloss.setColorAt(0.46, QColor(255, 255, 255, 0))
            p.fillRect(act_btn, QBrush(gloss))

            sweep_t = (self._aurora_phase * 0.42) % 1.8 - 0.4
            sx = act_btn.left() + sweep_t * act_btn.width()
            sweep = QLinearGradient(QPointF(sx - 56, 0), QPointF(sx + 56, 0))
            sweep.setColorAt(0.0, QColor(255, 255, 255, 0))
            sweep.setColorAt(0.5, QColor(255, 255, 255, 98))
            sweep.setColorAt(1.0, QColor(255, 255, 255, 0))
            p.fillRect(act_btn, QBrush(sweep))
            p.restore()

            p.setPen(QPen(QColor(255, 255, 255, 105), 1.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(act_btn.adjusted(0.5, 0.5, -0.5, -0.5), 18.0, 18.0)

            # Animated download icon + 'Обновить' + version badge chip
            ic_c = QPointF(act_btn.left() + 34.0, act_btn.center().y() + math.sin(self._aurora_phase * 4.5) * 1.4)
            VectorIcons.draw_download_arrow(p, ic_c, 18.0, QColor(255, 255, 255, 255))

            p.setPen(QColor(255, 255, 255, 255))
            p.setFont(QFont("Segoe UI Variable Display", 11, QFont.Weight.Bold))
            p.drawText(
                QRectF(act_btn.left() + 54.0, act_btn.top(), 132.0, act_btn.height()),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                "Обновить",
            )

            v_chip = QRectF(act_btn.right() - 78.0, act_btn.center().y() - 12.0, 62.0, 24.0)
            p.setPen(QPen(QColor(255, 255, 255, 82), 1.0))
            p.setBrush(QColor(0, 0, 0, 88))
            p.drawRoundedRect(v_chip, 12.0, 12.0)
            p.setPen(QColor(255, 255, 255, 252))
            p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
            p.drawText(v_chip, Qt.AlignmentFlag.AlignCenter, f"v{self.updater.remote_version}")
            p.restore()
        else:
            self._hitboxes["btn_github_check"] = act_btn
            hc, _ = self._get_btn_springs("btn_github_check")
            checking = self.updater.status == "checking"
            p.setPen(QPen(QColor(255, 255, 255, int(lerp(28, 76, hc.value))), 1.1))
            p.setBrush(QColor(int(lerp(22, 34, hc.value)), int(lerp(23, 35, hc.value)), int(lerp(32, 46, hc.value)), 255))
            p.drawRoundedRect(act_btn, 18.0, 18.0)
            chk_font = QFont("Segoe UI", 10, QFont.Weight.Bold)
            label = "Проверка обновлений..." if checking else "Проверить обновления"
            tw_lbl = QFontMetrics(chk_font).horizontalAdvance(label)
            total_w = 18.0 + 10.0 + tw_lbl
            ic_c = QPointF(act_btn.center().x() - total_w * 0.5 + 9.0, act_btn.center().y())
            p.save()
            p.translate(ic_c)
            p.rotate((self._wave_phase * 60.0) % 360.0 if checking else hc.value * 180.0)
            p.translate(-ic_c)
            VectorIcons.draw_refresh(p, ic_c, 15.0, QColor(232, 234, 246, 245))
            p.restore()
            p.setPen(QColor(238, 240, 250, 248))
            p.setFont(chk_font)
            p.drawText(
                QRectF(ic_c.x() + 18.0, act_btn.top(), tw_lbl + 6.0, act_btn.height()),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                label,
            )

        # Pure Sacred Update Studio — NO extra feature buttons here (all moved to Tab 6 Управление!)
        # Symmetrical Bottom Legal Pills ('Политика конфиденциальности' & 'Пользовательское соглашение')
        btn_priv = QRectF(cx_u - 216.0, u_card.bottom() - 50.0, 208.0, 32.0)
        btn_cons = QRectF(cx_u + 8.0, u_card.bottom() - 50.0, 208.0, 32.0)
        self._hitboxes["btn_open_privacy"] = btn_priv
        self._hitboxes["btn_open_consent"] = btn_cons

        for b_key, b_rect, b_lbl in (
            ("btn_open_privacy", btn_priv, "Политика конфиденциальности"),
            ("btn_open_consent", btn_cons, "Пользовательское соглашение"),
        ):
            hb, _ = self._get_btn_springs(b_key)
            p.setPen(QPen(QColor(255, 255, 255, int(lerp(20, 58, hb.value))), 1.0))
            p.setBrush(QColor(22, 22, 28, 235))
            p.drawRoundedRect(b_rect, 11.0, 11.0)
            if b_key == "btn_open_consent":
                dot_c = QPointF(b_rect.left() + 14.0, b_rect.center().y())
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(74, 222, 128) if self.consent_mgr.accepted else QColor(148, 163, 184))
                p.drawEllipse(dot_c, 3.0, 3.0)
                p.setPen(QColor(225, 225, 235, 240))
                p.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
                p.drawText(b_rect.adjusted(12, 0, 0, 0), Qt.AlignmentFlag.AlignCenter, b_lbl)
            else:
                p.setPen(QColor(225, 225, 235, 240))
                p.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
                p.drawText(b_rect, Qt.AlignmentFlag.AlignCenter, b_lbl)

        p.restore()
