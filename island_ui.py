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
from PyQt6.QtWidgets import QApplication, QFileDialog, QWidget

from backend import (
    AppAudioSession,
    AudioEngine,
    ClipboardEntry,
    ConsentManager,
    GitHubUpdater,
    MediaSessionWorker,
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
        self.legal_doc_mode: str = ""  # "", "privacy", "consent"
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

        self.character_mode = 2
        self._blink_timer = 0.0
        self._is_blinking = False

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

        # App Launcher Dock items on Home tab
        self.launcher_items: List[Tuple[str, str, str]] = [
            ("Проводник", "explorer.exe", "folder"),
            ("Блокнот", "notepad.exe", "notepad"),
            ("PowerShell", "powershell.exe", "ps"),
            ("Диспетчер", "taskmgr.exe", "taskmgr"),
            ("Ножницы", "snippingtool.exe", "snip"),
            ("Калькулятор", "calc.exe", "calc"),
            ("Параметры", "ms-settings:", "settings"),
        ]

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

        # 120 FPS animation loop
        self._last_frame_time = time.monotonic()
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(8)
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
            HWND_TOPMOST = -1
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOACTIVATE = 0x0010
            SWP_SHOWWINDOW = 0x0040
            ctypes.windll.user32.SetWindowPos(
                int(self.winId()),
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

    def current_capsule_rect(self) -> QRectF:
        cw = max(120.0, self.sp_w.value)
        ch = max(26.0, self.sp_h.value)
        cx = (self.MAX_CANVAS_W - cw) / 2.0
        ty = max(0.0, self.sp_top_y.value)
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
        self._osd_type = osd_type
        self._osd_text = text
        self._osd_value = value
        self._notif_subtitle = subtitle
        self._notif_app = app_name
        self._notif_target = target_kw
        self._osd_until = time.monotonic() + duration
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
        has_media = bool(track.is_playing or track.title)
        updating_now = self.is_updating_collapse or self.updater.status in ("downloading", "updated")
        if self.updater.status == "error":
            self.is_updating_collapse = False
            updating_now = False

        expanded = (
            (self.is_hovered or self.is_pinned_open or self.is_drag_hover)
            and not self.game_mode_locked
            and not updating_now
        )
        osd_active = (now < self._osd_until) and not expanded and not updating_now

        self.sp_osd.set_target(1.0 if osd_active else 0.0)
        self.sp_cascade.set_target(1.0 if expanded else 0.0)
        self.sp_top_y.set_target(9.0 if self.consent_mgr.island_float_mode == 1 else 0.0)

        for i in range(8):
            self.tab_open_sp[i].set_target(1.0 if i == self.active_tab else 0.0)

        if updating_now:
            self.sp_w.set_target(368.0, stiffness=400.0, damping=40.0)
            self.sp_h.set_target(52.0, stiffness=400.0, damping=40.0)
            self.sp_r.set_target(24.0)
            self.sp_expand.set_target(0.0)
        elif expanded:
            self.sp_w.set_target(self.EXPANDED_W, stiffness=420.0, damping=41.0)
            self.sp_h.set_target(self.EXPANDED_H, stiffness=420.0, damping=41.0)
            self.sp_r.set_target(28.0)
            self.sp_expand.set_target(1.0)
        elif osd_active:
            if self._osd_type == "notif":
                self.sp_w.set_target(372.0, stiffness=420.0, damping=41.0)
                self.sp_h.set_target(54.0, stiffness=420.0, damping=41.0)
                self.sp_r.set_target(24.0)
            else:
                self.sp_w.set_target(286.0, stiffness=420.0, damping=41.0)
                self.sp_h.set_target(42.0, stiffness=420.0, damping=41.0)
                self.sp_r.set_target(20.0)
            self.sp_expand.set_target(0.0)
        elif has_media:
            self.sp_w.set_target(332.0, stiffness=420.0, damping=41.0)
            self.sp_h.set_target(40.0, stiffness=420.0, damping=41.0)
            self.sp_r.set_target(19.0)
            self.sp_expand.set_target(0.0)
        elif self.timer_running:
            self.sp_w.set_target(258.0, stiffness=420.0, damping=41.0)
            self.sp_h.set_target(36.0, stiffness=420.0, damping=41.0)
            self.sp_r.set_target(18.0)
            self.sp_expand.set_target(0.0)
        else:
            self.sp_w.set_target(214.0, stiffness=420.0, damping=41.0)
            self.sp_h.set_target(36.0, stiffness=420.0, damping=41.0)
            self.sp_r.set_target(18.0)
            self.sp_expand.set_target(0.0)

    def _on_fast_watch(self):
        now = time.monotonic()

        if not self.game_mode_locked and not self.is_updating_collapse and self.updater.status not in ("downloading", "updated"):
            local_pos = QPointF(self.mapFromGlobal(QCursor.pos()))
            cap = self.current_capsule_rect()

            if not self.is_hovered and not self.is_pinned_open:
                dx = abs(local_pos.x() - cap.center().x())
                dy = local_pos.y() - cap.bottom()
                if dx < cap.width() * 0.65 and 0.0 <= dy < 55.0:
                    pull = (1.0 - dy / 55.0) * 11.5
                    self.sp_ferro_pull.set_target(pull)
                    self.sp_ferro_x.set_target(clamp(local_pos.x(), cap.left() + 28, cap.right() - 28))
                else:
                    self.sp_ferro_pull.set_target(0.0)
            else:
                self.sp_ferro_pull.set_target(0.0)

            if self.is_hovered:
                exp_x = (self.MAX_CANVAS_W - self.EXPANDED_W) * 0.5
                hit_zone = QRectF(exp_x - 14.0, -4.0, self.EXPANDED_W + 28.0, max(cap.height(), self.EXPANDED_H) + 18.0)
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
                    self._hover_collapse_deadline = now + 0.18
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

        track, _ = self.media.get_snapshot()
        self._update_target_dimensions()

        local_pos = QPointF(self.mapFromGlobal(QCursor.pos()))
        self.sp_mx.set_target(local_pos.x())
        self.sp_my.set_target(local_pos.y())
        self.sp_mx.step(dt)
        self.sp_my.step(dt)

        cap = self.current_capsule_rect()
        eye_origin = QPointF(cap.left() + 26.0, cap.height() * 0.48)
        edx = local_pos.x() - eye_origin.x()
        edy = local_pos.y() - eye_origin.y()
        edist = max(1.0, math.hypot(edx, edy))
        self.sp_eye_dx.set_target((edx / edist) * min(2.6, edist * 0.015))
        self.sp_eye_dy.set_target((edy / edist) * min(1.9, edist * 0.015))
        self.sp_eye_dx.step(dt)
        self.sp_eye_dy.step(dt)

        # Ensure monochrome covers stay vibrant neon coral-rose
        tr, tg, tb = track.accent_color
        if max(tr, tg, tb) - min(tr, tg, tb) < 42:
            tr, tg, tb = 236, 72, 128
        self.sp_r1.set_target(float(tr))
        self.sp_g1.set_target(float(tg))
        self.sp_b1.set_target(float(tb))
        self.sp_r1.step(dt)
        self.sp_g1.step(dt)
        self.sp_b1.step(dt)

        self.sp_w.step(dt)
        self.sp_h.step(dt)
        self.sp_r.step(dt)
        self.sp_expand.step(dt)
        self.sp_osd.step(dt)
        self.sp_cascade.step(dt)
        self.sp_top_y.step(dt)
        self.sp_ferro_pull.step(dt)
        self.sp_ferro_x.step(dt)
        self.sp_tab_trans.step(dt)
        self.sp_update_prog.set_target(self.updater.progress)
        self.sp_update_prog.step(dt)
        for sp_t in self.tab_open_sp:
            sp_t.step(dt)

        new_hover = ""
        if self.sp_expand.value > 0.35:
            for k, rect in self._hitboxes.items():
                if rect.contains(local_pos):
                    new_hover = k
                    break
        self._hovered_key = new_hover

        for k in list(self.btn_hover_sp.keys()):
            h_sp, p_sp = self.btn_hover_sp[k], self.btn_press_sp[k]
            h_sp.set_target(1.0 if k == self._hovered_key else 0.0)
            p_sp.set_target(0.0)
            h_sp.step(dt)
            p_sp.step(dt)

        alive_ripples: List[ClickRipple] = []
        for rp in self._ripples:
            rp.progress += dt * 2.5
            if rp.progress < 1.0:
                alive_ripples.append(rp)
        self._ripples = alive_ripples

        self._blink_timer += dt
        if self._blink_timer > 3.6:
            self._is_blinking = True
            if self._blink_timer > 3.74:
                self._is_blinking = False
                self._blink_timer = random.uniform(-0.8, 0.4)

        pk_now = self.audio._smooth_peak
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
            self.is_pinned_open = not self.is_pinned_open
            self._update_target_dimensions()
            self._ensure_anim_running()
            return

        if event.button() != Qt.MouseButton.LeftButton:
            return

        if self.sp_expand.value < 0.45:
            if time.monotonic() < self._osd_until and self._osd_type == "notif":
                self._osd_until = 0.0
                if self._notif_target:
                    focus_media_player_window(self._notif_target)
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
        elif key == "btn_test_notif":
            self.is_pinned_open = False
            self.is_hovered = False
            self._trigger_osd(
                "notif",
                "Telegram · Новое сообщение",
                duration=3.5,
                subtitle="Кликни по капсуле, чтобы открыть окно",
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
        elif key.startswith("launch_"):
            idx = int(key.split("_")[1])
            if 0 <= idx < len(self.launcher_items):
                launch_shell_target(self.launcher_items[idx][1])
        elif key == "launch_add":
            fpath, _ = QFileDialog.getOpenFileName(self, "Добавить программу или файл на Главную")
            if fpath:
                name = os.path.splitext(os.path.basename(fpath))[0][:12]
                self.launcher_items.append((name, fpath, "custom"))
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
        float_t = clamp(self.sp_top_y.value / 9.0, 0.0, 1.0)
        ear = lerp(11.0, 18.0, exp) * (1.0 - float_t)
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

        # Audio-reactive liquid edge: strictly driven by real FFT spectrum bands & bass peak.
        # Respects user Wave Style (Item 6: 0=Неон, 1=Мягкая, 2=Выкл) and Bass Sensitivity!
        n_b = len(bands)
        wave_mode = self.consent_mgr.wave_style_mode
        wave_mult = 1.0 if wave_mode == 0 else (0.52 if wave_mode == 1 else 0.0)
        amp_max = lerp(7.0, 10.5, exp) * wave_mult
        active_gate = clamp((peak - 0.012) / 0.05) * (0.0 if wave_mode == 2 else 1.0)

        for i in range(num_pts + 1):
            t = i / num_pts
            px = x_start + t * span
            env = math.sin(t * math.pi) ** 0.75
            d = abs(t - 0.5) * 2.0
            fi = d * (n_b - 1) * 0.78
            i0 = int(fi)
            i1 = min(n_b - 1, i0 + 1)
            fr = fi - i0
            b_val = (bands[i0] * (1.0 - fr) + bands[i1] * fr) * active_gate
            shimmer = math.sin(t * math.pi * 8.0 + self._wave_phase) * (b_val * 2.4 + peak * 1.3) * active_gate
            wave_dy = (b_val * amp_max + shimmer) * env

            dist_cursor = abs(px - self.sp_mx.value)
            cursor_ripple = (
                math.cos(dist_cursor * 0.08 - self._wave_phase * 1.4)
                * 1.4
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
        raw_peak, raw_bands = self.audio.get_peak_and_bands(track.is_playing)
        bass_mult = 1.0 if self.consent_mgr.bass_sens_mode == 0 else (1.55 if self.consent_mgr.bass_sens_mode == 1 else 0.62)
        peak = clamp(raw_peak * bass_mult, 0.0, 1.0)
        bands = [clamp(b * bass_mult, 0.0, 1.0) for b in raw_bands]

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

        if exp < 0.42:
            p.save()
            p.setOpacity(clamp(1.0 - exp * 2.5))
            if osd_t > 0.15:
                self._paint_osd_pill(p, cap, accent_col)
            else:
                self._paint_folded_live_pill(p, cap, track, bands, accent_col)
            p.restore()

        if exp > 0.06:
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

        # Subtle outer rim when in Floating Capsule mode (Парящая пилюля)
        if self.sp_top_y.value > 1.5:
            p.setPen(QPen(QColor(255, 255, 255, int(clamp(self.sp_top_y.value / 9.0) * 36)), 1.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(island_path)

        wave_mode = self.consent_mgr.wave_style_mode
        if wave_mode != 2:
            wave_grad = QLinearGradient(QPointF(cap.left() + br, cap.bottom()), QPointF(cap.right() - br, cap.bottom()))
            wave_grad.setColorAt(0.0, QColor(r1, g1, b1, 0))
            wave_grad.setColorAt(0.14, QColor(r1, g1, b1, 230 if wave_mode == 0 else 155))
            wave_grad.setColorAt(0.50, QColor(min(255, r1 + 35), min(255, g1 + 25), min(255, b1 + 35), 255 if wave_mode == 0 else 185))
            wave_grad.setColorAt(0.86, QColor(142, 162, 255, 220 if wave_mode == 0 else 145))
            wave_grad.setColorAt(1.0, QColor(142, 162, 255, 0))
            glow_a = clamp(peak * 1.3)
            if wave_mode == 0 and glow_a > 0.04:
                p.save()
                p.setOpacity(glow_a * 0.45)
                p.setPen(QPen(QBrush(wave_grad), 5.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(bottom_wave_path)
                p.restore()
            p.setPen(QPen(QBrush(wave_grad), 1.95 if wave_mode == 0 else 1.35, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
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

    # --- Folded Live Pill States ---
    def _paint_folded_live_pill(
        self,
        p: QPainter,
        cap: QRectF,
        track: TrackInfo,
        bands: List[float],
        accent_col: QColor,
    ):
        cy = cap.top() + cap.height() * 0.48
        w_data = self.weather_worker.data

        # Dedicated Collapsed Update Loading Bar State (when user clicks 'Обновить')!
        if self.is_updating_collapse or self.updater.status in ("downloading", "updated"):
            prog = clamp(self.sp_update_prog.value, 0.02, 1.0)
            pct = int(round(prog * 100))

            ic_pt = QPointF(cap.left() + 22.0, cap.top() + 17.5)
            p.save()
            p.translate(ic_pt)
            p.rotate((self._wave_phase * 42.0) % 360.0)
            p.translate(-ic_pt)
            VectorIcons.draw_refresh(p, ic_pt, 13.5, accent_col)
            p.restore()

            p.setPen(QColor(255, 255, 255, 248))
            p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
            p.drawText(
                QRectF(cap.left() + 36.0, cap.top() + 7.0, cap.width() - 92.0, 20.0),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                self.updater.status_text,
            )

            p.setPen(accent_col)
            p.setFont(QFont("Segoe UI Variable Display", 9, QFont.Weight.Bold))
            p.drawText(
                QRectF(cap.right() - 56.0, cap.top() + 7.0, 38.0, 20.0),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                f"{pct}%",
            )

            bar_bg = QRectF(cap.left() + 18.0, cap.bottom() - 15.5, cap.width() - 36.0, 6.5)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(30, 30, 38, 255))
            p.drawRoundedRect(bar_bg, 3.25, 3.25)

            fill_w = max(8.0, bar_bg.width() * prog)
            fill_r = QRectF(bar_bg.left(), bar_bg.top(), fill_w, bar_bg.height())
            p_grad = QLinearGradient(fill_r.topLeft(), fill_r.topRight())
            p_grad.setColorAt(0.0, accent_col)
            p_grad.setColorAt(0.65, QColor(244, 114, 182, 255))
            p_grad.setColorAt(1.0, QColor(142, 162, 255, 255))
            p.setBrush(QBrush(p_grad))
            p.drawRoundedRect(fill_r, 3.25, 3.25)
            return

        if track.is_playing or track.title:
            playing = track.is_playing
            kick = clamp(peak_from_bands(bands)) if playing else 0.0
            art_s = 24.0 + kick * 2.2
            art_c = QPointF(cap.left() + 24.0, cy)
            art_r = QRectF(art_c.x() - art_s * 0.5, art_c.y() - art_s * 0.5, art_s, art_s)
            if playing and kick > 0.05:
                halo = QRadialGradient(art_c, art_s * 0.95)
                halo.setColorAt(0.0, QColor(accent_col.red(), accent_col.green(), accent_col.blue(), int(110 * kick)))
                halo.setColorAt(1.0, QColor(accent_col.red(), accent_col.green(), accent_col.blue(), 0))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(halo))
                p.drawEllipse(art_c, art_s * 0.95, art_s * 0.95)
            p.save()
            clip = QPainterPath()
            clip.addRoundedRect(art_r, 7.0, 7.0)
            p.setClipPath(clip, Qt.ClipOperation.IntersectClip)
            if self._cover_hi_pix:
                p.drawPixmap(art_r.toRect(), self._cover_hi_pix)
            else:
                p.fillPath(clip, accent_col)
            if not playing:
                p.fillPath(clip, QColor(0, 0, 0, 120))
            p.restore()
            if not playing:
                VectorIcons.draw_pause(p, art_c, 9.0, QColor(255, 255, 255, 235))

            p.setPen(QColor(255, 255, 255, 248))
            p.setFont(QFont("Segoe UI Variable Display", 10, QFont.Weight.Bold))
            p.drawText(
                QRectF(cap.center().x() - 62.0, cap.top(), 52.0, cap.height() - 2),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                time.strftime("%H:%M"),
            )
            VectorIcons.draw_weather_cloud(p, QPointF(cap.center().x() + 6.0, cy), 14.0, QColor(255, 255, 255, 235), filled=True)
            p.drawText(
                QRectF(cap.center().x() + 18.0, cap.top(), 44.0, cap.height() - 2),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                f"{w_data.temp}°",
            )

            eq_right = self._paint_privacy_dots(p, cap.right() - 14.0, cy)
            groups = ((0, 3), (3, 8), (8, 14), (14, 21), (21, 30))
            bar_w = 3.0
            gap = 2.6
            n_bars = len(groups)
            eq_left = eq_right - n_bars * bar_w - (n_bars - 1) * gap
            eq_grad = QLinearGradient(QPointF(eq_left, 0), QPointF(eq_right, 0))
            eq_grad.setColorAt(0.0, accent_col)
            eq_grad.setColorAt(1.0, QColor(142, 162, 255, 255))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(eq_grad))
            for gi, (a, b) in enumerate(groups):
                seg = bands[a:b] or [0.0]
                val = max(seg) * 0.65 + (sum(seg) / len(seg)) * 0.35 if playing else 0.0
                bh = 3.0 + clamp(val) * 15.0
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
        p.save()
        look_x = self.sp_eye_dx.value
        look_y = self.sp_eye_dy.value

        for ox in (-5.2, 5.2):
            ex = center.x() + ox
            ey = center.y()
            if self._is_blinking:
                p.setPen(QPen(QColor(255, 255, 255, 235), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawLine(QPointF(ex - 3.5, ey), QPointF(ex + 3.5, ey))
            else:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(255, 255, 255, 245))
                p.drawEllipse(QPointF(ex, ey), 4.2, 5.0)
                p.setBrush(QColor(10, 10, 14, 255))
                p.drawEllipse(QPointF(ex + look_x, ey + look_y), 2.1, 2.4)
                p.setBrush(QColor(255, 255, 255, 240))
                p.drawEllipse(QPointF(ex + look_x - 0.7, ey + look_y - 0.8), 0.7, 0.7)
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
        if self._osd_type == "notif":
            # Rich Telegram / Discord / Microphone / Headphone Notification Capsule (Items 1 & 2)
            app_low = (self._notif_app or "").lower()
            if "telegram" in app_low:
                b_col = QColor(42, 171, 238, 255)
            elif "discord" in app_low:
                b_col = QColor(88, 101, 242, 255)
            elif "микрофон" in app_low:
                b_col = QColor(255, 159, 10, 255)
            elif "камера" in app_low or "аудио" in app_low:
                b_col = QColor(48, 209, 88, 255)
            else:
                b_col = accent_col

            ic_c = QPointF(cap.left() + 26.0, cy)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(b_col.red(), b_col.green(), b_col.blue(), 45))
            p.drawEllipse(ic_c, 16.5, 16.5)
            p.setBrush(b_col)
            p.drawEllipse(ic_c, 12.5, 12.5)
            if "микрофон" in app_low:
                VectorIcons.draw_voice_bars(p, ic_c, 12.5, QColor(255, 255, 255, 252))
            elif "аудио" in app_low:
                VectorIcons.draw_speaker(p, ic_c, 12.5, QColor(255, 255, 255, 252), 0.8, False)
            else:
                VectorIcons.draw_location_pin(p, ic_c, 12.0, QColor(255, 255, 255, 252))

            p.setPen(QColor(255, 255, 255, 252))
            p.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
            p.drawText(
                QRectF(cap.left() + 48.0, cap.top() + 7.0, cap.width() - 126.0, 19.0),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                self._osd_text,
            )
            p.setPen(QColor(175, 180, 195, 235))
            p.setFont(QFont("Segoe UI", 8))
            p.drawText(
                QRectF(cap.left() + 48.0, cap.top() + 25.0, cap.width() - 126.0, 18.0),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                self._notif_subtitle or "Нажмите, чтобы открыть окно",
            )

            open_pill = QRectF(cap.right() - 72.0, cy - 12.0, 58.0, 24.0)
            p.setPen(QPen(QColor(b_col.red(), b_col.green(), b_col.blue(), 125), 1.0))
            p.setBrush(QColor(b_col.red(), b_col.green(), b_col.blue(), 38))
            p.drawRoundedRect(open_pill, 12.0, 12.0)
            p.setPen(QColor(255, 255, 255, 248))
            p.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
            p.drawText(open_pill, Qt.AlignmentFlag.AlignCenter, "Открыть")
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

        right_x = cap.right() - 22.0
        for key, draw_fn, is_circle_active in (
            ("top_mixer", VectorIcons.draw_sliders_h, self.show_inline_mixer),
            ("top_pin", VectorIcons.draw_pushpin, self.is_pinned_open),
        ):
            right_x -= 32.0
            brect = QRectF(right_x, ty + 1.0, 30.0, 30.0)
            self._hitboxes[key] = brect
            h_sp, p_sp = self._get_btn_springs(key)
            hov, prs = h_sp.value, p_sp.value
            sc = 1.0 + hov * 0.08 - prs * 0.10

            p.save()
            p.translate(brect.center())
            p.scale(sc, sc)
            p.translate(-brect.center())
            if is_circle_active or key == "top_pin" or hov > 0.05:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(
                    QColor(46, 46, 56, 255)
                    if is_circle_active
                    else QColor(28, 28, 34, int(max(185 if key == "top_pin" else 0, hov * 225)))
                )
                p.drawEllipse(brect)
            ic_col = QColor(255, 255, 255, 248 if is_circle_active else int(lerp(135, 248, hov)))
            draw_fn(p, brect.center(), 14.0, ic_col)
            p.restore()
            right_x -= 6.0

        # Dedicated Updates Tab right next to Pin ('Закрепить')!
        upd_open_t = clamp(self.tab_open_sp[7].value, 0.0, 1.0)
        hu_top, pu_top = self._get_btn_springs("top_updates")
        has_upd = self.updater.update_available
        pill_open = max(upd_open_t, 1.0 if has_upd else 0.0)
        upd_w = lerp(30.0, 108.0, pill_open)
        right_x -= upd_w
        urect = QRectF(right_x, ty + 1.0, upd_w, 30.0)
        self._hitboxes["top_updates"] = urect

        p.save()
        sc_u = 1.0 + hu_top.value * 0.06 - pu_top.value * 0.08
        p.translate(urect.center())
        p.scale(sc_u, sc_u)
        p.translate(-urect.center())
        if self.active_tab == 7 or has_upd or hu_top.value > 0.04:
            p.setPen(
                QPen(
                    accent_col if has_upd else QColor(255, 255, 255, int(lerp(18, 42, max(upd_open_t, hu_top.value)))),
                    1.1,
                )
            )
            p.setBrush(
                QColor(44, 24, 34, 245)
                if has_upd
                else QColor(36, 36, 44, int(max(upd_open_t * 255, hu_top.value * 215, 175)))
            )
        else:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(26, 26, 32, 185))
        p.drawRoundedRect(urect, 15.0, 15.0)

        ic_ux = lerp(urect.center().x(), urect.left() + 15.5, pill_open)
        VectorIcons.draw_refresh(
            p,
            QPointF(ic_ux, urect.center().y()),
            13.5,
            accent_col if has_upd else QColor(255, 255, 255, 248 if self.active_tab == 7 else 210),
        )
        if pill_open > 0.15:
            p.save()
            p.setClipRect(urect.adjusted(24, 0, -4, 0), Qt.ClipOperation.IntersectClip)
            p.setOpacity(clamp((pill_open - 0.15) / 0.7))
            p.setPen(QColor(255, 255, 255, 252))
            p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
            p.drawText(
                urect.adjusted(27, 0, -6, 0),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                "Обновления",
            )
            p.restore()
        if has_upd:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(accent_col)
            p.drawEllipse(QPointF(urect.right() - 8.5, urect.top() + 8.0), 3.2, 3.2)
        p.restore()
        right_x -= 6.0

        # Character switcher button to the left of Updates tab
        right_x -= 32.0
        crect_top = QRectF(right_x, ty + 1.0, 30.0, 30.0)
        self._hitboxes["top_char"] = crect_top
        hc_sp, _ = self._get_btn_springs("top_char")
        if hc_sp.value > 0.05:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(28, 28, 34, int(hc_sp.value * 225)))
            p.drawEllipse(crect_top)
        VectorIcons.draw_voice_bars(p, crect_top.center(), 14.0, QColor(255, 255, 255, int(lerp(135, 248, hc_sp.value))))

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

            # B) Top-Right Visualizer Style Switcher Pill ('Кольцо' / 'Волна' / 'Винил')
            vis_names = {0: "Спектр: Кольцо", 1: "Спектр: Волна", 2: "Спектр: Винил"}
            vis_pill = QRectF(p_card.right() - 108.0, p_card.top() + 12.0, 96.0, 24.0)
            self._hitboxes["btn_vis_mode"] = vis_pill
            h_vm, _ = self._get_btn_springs("btn_vis_mode")
            p.setPen(QPen(QColor(255, 255, 255, int(lerp(18, 55, h_vm.value))), 1.0))
            p.setBrush(QColor(22, 22, 28, 215))
            p.drawRoundedRect(vis_pill, 12.0, 12.0)
            p.setPen(QColor(215, 215, 225, 235))
            p.setFont(QFont("Segoe UI", 7, QFont.Weight.DemiBold))
            p.drawText(vis_pill, Qt.AlignmentFlag.AlignCenter, vis_names.get(self.visualizer_mode, "Кольцо"))

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

        # 3. Right 2x4 Grid of 8 Quick Action Cards
        qa_x = left_x + 420.0
        qa_w = 111.0
        qa_h = 65.0
        qa_gap_x = 10.0
        qa_gap_y = 8.0

        now = time.monotonic()
        pipette_lbl = self.fn_picked_hex if now < self.fn_color_scanning_until else "Пипетка"
        qa_specs = [
            ("qa_area", "Область", VectorIcons.draw_area_select),
            ("qa_full", "Весь экран", VectorIcons.draw_monitor),
            ("qa_ocr", "Текст с\nэкрана", VectorIcons.draw_ocr_scan),
            ("qa_qr", "QR-код", VectorIcons.draw_qr),
            ("qa_pipette", pipette_lbl, VectorIcons.draw_pipette),
            ("qa_clean", "Чистый\nтекст", VectorIcons.draw_eraser),
            ("qa_record", "Буст ОЗУ", VectorIcons.draw_video_cam),
            ("qa_cmd", "Команда", VectorIcons.draw_search),
        ]

        for idx, (qkey, qlabel, qicon) in enumerate(qa_specs):
            col_i = idx % 2
            row_i = idx // 2
            qx = qa_x + col_i * (qa_w + qa_gap_x)
            qy = top + row_i * (qa_h + qa_gap_y)
            h_sp, p_sp = self._get_btn_springs(qkey)
            hov, prs = h_sp.value, p_sp.value

            qrect = self._draw_obsidian_card(
                p,
                QRectF(qx, qy, qa_w, qa_h),
                15.0,
                cascade_order=idx + 1,
                hovered_t=hov,
            )
            self._hitboxes[qkey] = qrect

            sc = 1.0 + hov * 0.045 - prs * 0.07
            p.save()
            p.translate(qrect.center())
            p.scale(sc, sc)
            p.translate(-qrect.center())

            ic_col = accent_col if (qkey == "qa_pipette" and now < self.fn_color_scanning_until) else QColor(225, 225, 235, int(lerp(215, 255, hov)))
            qicon(p, QPointF(qrect.left() + 22.0, qrect.center().y()), 16.0, ic_col)

            p.setPen(QColor(255, 255, 255, 250))
            p.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
            p.drawText(
                qrect.adjusted(38, 4, -6, -4),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                qlabel,
            )
            p.restore()

        # 4. Bottom Full-Width App Launcher Bar with macOS Dock Wave Magnification
        dock_r = self._draw_obsidian_card(p, QRectF(left_x, top + 294.0, 652.0, 58.0), 16.0, cascade_order=5)
        cursor_in_dock = dock_r.adjusted(-15, -18, 15, 18).contains(QPointF(self.sp_mx.value, self.sp_my.value))

        ix = dock_r.left() + 18.0
        iy = dock_r.center().y() - 19.0
        for idx, (lname, _, lkind) in enumerate(self.launcher_items[:8]):
            lrect = QRectF(ix, iy, 38.0, 38.0)
            lkey = f"launch_{idx}"
            self._hitboxes[lkey] = lrect
            h_sp, p_sp = self._get_btn_springs(lkey)

            wave_mag = 0.0
            if cursor_in_dock:
                dx_dock = abs(self.sp_mx.value - lrect.center().x())
                wave_mag = math.exp(-(dx_dock * dx_dock) / 2800.0)

            mag = max(h_sp.value, wave_mag)
            sc = 1.0 + mag * 0.22 - p_sp.value * 0.12
            lift_y = -mag * 6.5

            p.save()
            p.translate(lrect.center() + QPointF(0, lift_y))
            p.scale(sc, sc)
            p.translate(-lrect.center())
            self._draw_launcher_app_icon(p, lrect, lkind)
            p.restore()
            ix += 54.0

        add_r = QRectF(ix, iy, 38.0, 38.0)
        self._hitboxes["launch_add"] = add_r
        h_add, _ = self._get_btn_springs("launch_add")
        p.setPen(QPen(QColor(255, 255, 255, int(lerp(45, 110, h_add.value))), 1.2, Qt.PenStyle.DashLine))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(add_r, 10.0, 10.0)
        p.setPen(QColor(200, 200, 210, 235))
        p.setFont(QFont("Segoe UI", 13, QFont.Weight.Light))
        p.drawText(add_r, Qt.AlignmentFlag.AlignCenter, "+")

    def _draw_launcher_app_icon(self, p: QPainter, r: QRectF, kind: str):
        p.save()
        p.setPen(Qt.PenStyle.NoPen)
        if kind == "folder":
            p.setBrush(QColor(245, 192, 66))
            p.drawRoundedRect(r.adjusted(2, 6, -2, -3), 5.5, 5.5)
            p.setBrush(QColor(56, 138, 240))
            p.drawRoundedRect(QRectF(r.left() + 8, r.bottom() - 12, r.width() - 16, 8), 3.0, 3.0)
        elif kind == "notepad":
            p.setBrush(QColor(72, 178, 215))
            p.drawRoundedRect(r.adjusted(4, 3, -4, -3), 5.5, 5.5)
            p.setPen(QPen(QColor(255, 255, 255, 225), 1.4))
            for ly in (12, 18, 24):
                p.drawLine(QPointF(r.left() + 9, r.top() + ly), QPointF(r.right() - 9, r.top() + ly))
        elif kind == "ps":
            p.setBrush(QColor(46, 114, 210))
            p.drawRoundedRect(r.adjusted(3, 5, -3, -5), 6.0, 6.0)
            p.setPen(QColor(255, 255, 255, 248))
            p.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, ">_")
        elif kind == "taskmgr":
            p.setBrush(QColor(68, 178, 235))
            p.drawRoundedRect(r.adjusted(3, 5, -3, -5), 6.0, 6.0)
            p.setBrush(QColor(15, 35, 60))
            p.drawRoundedRect(r.adjusted(7, 9, -7, -9), 3.0, 3.0)
            p.setPen(QPen(QColor(115, 245, 180), 1.5))
            p.drawLine(QPointF(r.left() + 9, r.center().y() + 3), QPointF(r.center().x(), r.top() + 12))
            p.drawLine(QPointF(r.center().x(), r.top() + 12), QPointF(r.right() - 9, r.center().y() + 2))
        elif kind == "snip":
            p.setBrush(QColor(115, 120, 130))
            p.drawRoundedRect(r.adjusted(4, 6, -4, -6), 6.0, 6.0)
            p.setBrush(QColor(35, 205, 235))
            p.drawEllipse(QPointF(r.left() + 11, r.bottom() - 10), 7.5, 7.5)
            p.setPen(QColor(255, 255, 255))
            p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
            p.drawText(QRectF(r.left() + 4, r.bottom() - 17, 14, 14), Qt.AlignmentFlag.AlignCenter, "+")
        elif kind == "calc":
            p.setBrush(QColor(58, 62, 72))
            p.drawRoundedRect(r.adjusted(5, 3, -5, -3), 6.0, 6.0)
            p.setBrush(QColor(236, 72, 128))
            p.drawRoundedRect(QRectF(r.center().x() + 1, r.center().y() + 1, 9, 11), 2.5, 2.5)
        else:
            p.setBrush(QColor(75, 125, 230))
            p.drawRoundedRect(r.adjusted(4, 5, -4, -5), 6.0, 6.0)
            VectorIcons.draw_monitor(p, r.center(), 18.0, QColor(255, 255, 255, 245))
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
        ch = 122.0
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

            ic_c = QPointF(crect.left() + 30.0, crect.top() + 34.0)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(periwinkle if is_on else QColor(38, 38, 46, 255))
            p.drawEllipse(ic_c, 19.5, 19.5)

            ic_col = QColor(10, 10, 16, 255) if is_on else QColor(225, 225, 235, 240)
            ic_fn(p, ic_c, 16.5, ic_col)

            p.setPen(QColor(255, 255, 255, 252))
            p.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
            p.drawText(QRectF(crect.left() + 16, crect.top() + 68, cw - 24, 20), Qt.AlignmentFlag.AlignLeft, ctitle)
            p.setPen(QColor(145, 145, 155, 230))
            p.setFont(QFont("Segoe UI", 8, QFont.Weight.Medium))
            p.drawText(QRectF(crect.left() + 16, crect.top() + 90, cw - 24, 18), Qt.AlignmentFlag.AlignLeft, csub)

        mid_y = top + ch + 12.0
        tel_w = w * 0.56
        tel_r = self._draw_obsidian_card(p, QRectF(left_x, mid_y, tel_w, 82.0), 16.0, cascade_order=3)

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

        br_r = self._draw_obsidian_card(p, QRectF(tel_r.right() + 10.0, mid_y, w - tel_w - 10.0, 82.0), 16.0, cascade_order=4)
        VectorIcons.draw_sun(p, QPointF(br_r.left() + 24.0, br_r.center().y()), 16.0, QColor(215, 215, 225, 235))

        p.setPen(QColor(155, 155, 165, 230))
        p.setFont(QFont("Segoe UI", 8, QFont.Weight.Medium))
        p.drawText(QRectF(br_r.left() + 46, br_r.top() + 18, 145, 18), Qt.AlignmentFlag.AlignLeft, "Яркость монитора")
        p.drawText(QRectF(br_r.right() - 58, br_r.top() + 18, 42, 18), Qt.AlignmentFlag.AlignRight, f"{self.dimmer_overlay.brightness_pct} %")

        b_bar = QRectF(br_r.left() + 46.0, br_r.top() + 46.0, br_r.width() - 62.0, 6.0)
        self._hitboxes["slider_bright"] = b_bar.adjusted(0, -10, 0, 10)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(42, 42, 50, 255))
        p.drawRoundedRect(b_bar, 3.0, 3.0)
        b_rat = clamp((self.dimmer_overlay.brightness_pct - 20) / 80.0)
        p.setBrush(QColor(235, 195, 38, 255))
        p.drawRoundedRect(QRectF(b_bar.left(), b_bar.top(), b_bar.width() * b_rat, 6.0), 3.0, 3.0)

        # Row 3: Power Action Cards (Блокировка, Сон, Перезагрузка, Выключение)
        py = mid_y + 94.0
        ph = 112.0
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

            p_icon(p, QPointF(prect.center().x(), prect.top() + 38.0), 17.0, QColor(205, 205, 215, 240))
            p.setPen(QColor(255, 255, 255, 248))
            p.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
            txt_lbl = "Нажми ещё раз" if confirming else plbl
            p.drawText(QRectF(prect.left(), prect.top() + 66, cw, 22), Qt.AlignmentFlag.AlignCenter, txt_lbl)

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

        # Ambient dual-aurora backdrop glow inside Update Studio
        breathe = 0.5 + 0.5 * math.sin(self._aurora_phase * 2.0)
        cx_u = u_card.center().x()
        orb_pt = QPointF(cx_u, u_card.top() + 74.0)

        bg_glow = QRadialGradient(orb_pt, 210.0)
        if has_upd:
            bg_glow.setColorAt(0.0, QColor(accent_col.red(), accent_col.green(), accent_col.blue(), int(75 + 25 * breathe)))
            bg_glow.setColorAt(0.55, QColor(142, 162, 255, 24))
            bg_glow.setColorAt(1.0, QColor(15, 15, 20, 0))
        else:
            bg_glow.setColorAt(0.0, QColor(74, 222, 128, int(55 + 20 * breathe)))
            bg_glow.setColorAt(0.55, QColor(142, 162, 255, 18))
            bg_glow.setColorAt(1.0, QColor(15, 15, 20, 0))
        p.fillRect(u_card, QBrush(bg_glow))

        # Orbital Concentric Vector Rings around central emblem
        for ri, r_rad in enumerate((46.0, 36.0)):
            p.setPen(
                QPen(
                    QColor(accent_col.red(), accent_col.green(), accent_col.blue(), 55 - ri * 18)
                    if has_upd
                    else QColor(74, 222, 128, 48 - ri * 16),
                    1.2,
                )
            )
            p.setBrush(Qt.BrushStyle.NoBrush)
            ang_start = int(((self._aurora_phase * (55 if ri == 0 else -40)) % 360) * 16)
            p.drawArc(QRectF(orb_pt.x() - r_rad, orb_pt.y() - r_rad, r_rad * 2, r_rad * 2), ang_start, 210 * 16)

        p.setPen(QPen(accent_col if has_upd else QColor(74, 222, 128, 195), 1.6))
        p.setBrush(QColor(20, 20, 26, 255))
        p.drawEllipse(orb_pt, 27.0, 27.0)
        VectorIcons.draw_refresh(
            p,
            orb_pt,
            22.0,
            accent_col if has_upd else QColor(74, 222, 128, 250),
        )

        # App Title & Version Transition Pill
        p.setPen(QColor(255, 255, 255, 252))
        p.setFont(QFont("Segoe UI Variable Display", 15, QFont.Weight.Bold))
        p.drawText(
            QRectF(u_card.left(), u_card.top() + 108.0, w, 26.0),
            Qt.AlignmentFlag.AlignCenter,
            f"Island  v{self.updater.local_version}",
        )

        status_pill = QRectF(cx_u - 118.0, u_card.top() + 138.0, 236.0, 26.0)
        p.setPen(QPen(accent_col if has_upd else QColor(74, 222, 128, 120), 1.1))
        p.setBrush(QColor(42, 20, 32, 210) if has_upd else QColor(18, 38, 28, 195))
        p.drawRoundedRect(status_pill, 13.0, 13.0)
        p.setPen(QColor(255, 228, 238) if has_upd else QColor(167, 243, 208))
        p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        p.drawText(
            status_pill,
            Qt.AlignmentFlag.AlignCenter,
            f"Доступна новая сборка  v{self.updater.remote_version}" if has_upd else "Установлена актуальная версия",
        )

        # Centerpiece Hero 'Обновить' Button (264 x 52 px)
        act_btn = QRectF(cx_u - 132.0, u_card.top() + 176.0, 264.0, 52.0)
        if has_upd:
            self._hitboxes["btn_github_update"] = act_btn
            hu, pu = self._get_btn_springs("btn_github_update")
            sc_b = 1.0 + hu.value * 0.04 - pu.value * 0.06
            p.save()
            p.translate(act_btn.center())
            p.scale(sc_b, sc_b)
            p.translate(-act_btn.center())

            # Multi-layer breathing neon aura halo
            glow_rect = act_btn.adjusted(-10, -10, 10, 10)
            for gi in range(4):
                gr = glow_rect.adjusted(gi * 2.2, gi * 2.2, -gi * 2.2, -gi * 2.2)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(
                    QColor(
                        accent_col.red(),
                        accent_col.green(),
                        accent_col.blue(),
                        int((20 + 22 * breathe + 38 * hu.value) / (gi + 1)),
                    )
                )
                p.drawRoundedRect(gr, 24.0 - gi * 2.0, 24.0 - gi * 2.0)

            btn_path = QPainterPath()
            btn_path.addRoundedRect(act_btn, 17.0, 17.0)
            btn_grad = QLinearGradient(act_btn.topLeft(), act_btn.bottomRight())
            btn_grad.setColorAt(0.0, accent_col)
            btn_grad.setColorAt(0.52, QColor(244, 114, 182, 255))
            btn_grad.setColorAt(1.0, QColor(142, 162, 255, 255))
            p.fillPath(btn_path, QBrush(btn_grad))

            # Top glass reflection + animated diagonal light sheen
            p.save()
            p.setClipPath(btn_path, Qt.ClipOperation.IntersectClip)
            gloss = QLinearGradient(act_btn.topLeft(), act_btn.bottomLeft())
            gloss.setColorAt(0.0, QColor(255, 255, 255, 82))
            gloss.setColorAt(0.48, QColor(255, 255, 255, 0))
            p.fillRect(act_btn, QBrush(gloss))

            sweep_t = (self._aurora_phase * 0.38) % 1.8 - 0.4
            sx = act_btn.left() + sweep_t * act_btn.width()
            sweep = QLinearGradient(QPointF(sx - 52, 0), QPointF(sx + 52, 0))
            sweep.setColorAt(0.0, QColor(255, 255, 255, 0))
            sweep.setColorAt(0.5, QColor(255, 255, 255, 88))
            sweep.setColorAt(1.0, QColor(255, 255, 255, 0))
            p.fillRect(act_btn, QBrush(sweep))
            p.restore()

            p.setPen(QPen(QColor(255, 255, 255, 85), 1.15))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(act_btn.adjusted(0.5, 0.5, -0.5, -0.5), 17.0, 17.0)

            # Icon + 'Обновить' + version badge pill inside button
            ic_c = QPointF(act_btn.left() + 34.0, act_btn.center().y())
            p.save()
            p.translate(ic_c)
            p.rotate(hu.value * 210.0)
            p.translate(-ic_c)
            VectorIcons.draw_refresh(p, ic_c, 17.0, QColor(255, 255, 255, 255))
            p.restore()

            p.setPen(QColor(255, 255, 255, 255))
            p.setFont(QFont("Segoe UI Variable Display", 11, QFont.Weight.Bold))
            p.drawText(
                QRectF(act_btn.left() + 52.0, act_btn.top(), 125.0, act_btn.height()),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                "Обновить",
            )

            v_chip = QRectF(act_btn.right() - 74.0, act_btn.center().y() - 12.0, 58.0, 24.0)
            p.setPen(QPen(QColor(255, 255, 255, 65), 1.0))
            p.setBrush(QColor(0, 0, 0, 75))
            p.drawRoundedRect(v_chip, 12.0, 12.0)
            p.setPen(QColor(255, 255, 255, 250))
            p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
            p.drawText(v_chip, Qt.AlignmentFlag.AlignCenter, f"v{self.updater.remote_version}")
            p.restore()
        else:
            self._hitboxes["btn_github_check"] = act_btn
            hc, _ = self._get_btn_springs("btn_github_check")
            checking = self.updater.status == "checking"
            p.setPen(QPen(QColor(255, 255, 255, int(lerp(24, 68, hc.value))), 1.1))
            p.setBrush(QColor(int(lerp(24, 34, hc.value)), int(lerp(24, 34, hc.value)), int(lerp(32, 44, hc.value)), 255))
            p.drawRoundedRect(act_btn, 17.0, 17.0)
            chk_font = QFont("Segoe UI", 10, QFont.Weight.Bold)
            label = "Проверка обновлений..." if checking else "Проверить обновления"
            tw_lbl = QFontMetrics(chk_font).horizontalAdvance(label)
            total_w = 18.0 + 10.0 + tw_lbl
            ic_c = QPointF(act_btn.center().x() - total_w * 0.5 + 9.0, act_btn.center().y())
            p.save()
            p.translate(ic_c)
            p.rotate((self._wave_phase * 60.0) % 360.0 if checking else hc.value * 180.0)
            p.translate(-ic_c)
            VectorIcons.draw_refresh(p, ic_c, 15.0, QColor(230, 230, 240, 245))
            p.restore()
            p.setPen(QColor(235, 235, 245, 245))
            p.setFont(chk_font)
            p.drawText(
                QRectF(ic_c.x() + 18.0, act_btn.top(), tw_lbl + 6.0, act_btn.height()),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                label,
            )

        # Item 6 & Item 1/2: 4-Pill Island Appearance & Live Notification Bar
        float_lbl = "Форма: Парящая" if self.consent_mgr.island_float_mode == 1 else "Форма: Вырез"
        wave_names = {0: "Волна: Неон", 1: "Волна: Мягкая", 2: "Волна: Выкл"}
        bass_names = {0: "Бас: 100%", 1: "Бас: 155% +", 2: "Бас: 65%"}
        style_pills = [
            ("btn_style_float", float_lbl),
            ("btn_style_wave", wave_names.get(self.consent_mgr.wave_style_mode, "Волна: Неон")),
            ("btn_style_bass", bass_names.get(self.consent_mgr.bass_sens_mode, "Бас: 100%")),
            ("btn_test_notif", "Тест уведомления"),
        ]
        sp_w_item = 152.0
        sp_gap = 10.0
        sp_start_x = cx_u - (4 * sp_w_item + 3 * sp_gap) * 0.5
        sp_y = u_card.bottom() - 88.0
        for s_idx, (s_key, s_lbl) in enumerate(style_pills):
            s_rect = QRectF(sp_start_x + s_idx * (sp_w_item + sp_gap), sp_y, sp_w_item, 30.0)
            self._hitboxes[s_key] = s_rect
            hs, _ = self._get_btn_springs(s_key)
            p.setPen(QPen(QColor(142, 162, 255, int(lerp(35, 110, hs.value))), 1.0))
            p.setBrush(QColor(int(lerp(24, 34, hs.value)), int(lerp(25, 35, hs.value)), int(lerp(34, 46, hs.value)), 240))
            p.drawRoundedRect(s_rect, 10.5, 10.5)
            p.setPen(QColor(238, 240, 250, 248))
            p.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
            p.drawText(s_rect, Qt.AlignmentFlag.AlignCenter, s_lbl)

        # Symmetrical Bottom Legal Pills ('Политика конфиденциальности' & 'Пользовательское соглашение')
        btn_priv = QRectF(cx_u - 216.0, u_card.bottom() - 46.0, 208.0, 30.0)
        btn_cons = QRectF(cx_u + 8.0, u_card.bottom() - 46.0, 208.0, 30.0)
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
