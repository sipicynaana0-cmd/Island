import ctypes
import os
import sys

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from backend import AudioEngine, MediaSessionWorker, SystemTelemetry
from island_ui import DynamicIslandWidget


def create_tray_icon() -> QIcon:
    """Loads the custom high-resolution island.ico logo for the Windows System Tray & App."""
    ico_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "island.ico")
    if os.path.exists(ico_path):
        return QIcon(ico_path)
    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(12, 12, 18, 255))
    p.drawRoundedRect(QRectF(8, 20, 48, 24), 12, 12)
    p.end()
    return QIcon(pix)


def set_ClickThrough(hwnd: int, enable: bool):
    """Toggles Win32 WS_EX_TRANSPARENT so clicks pass straight through into fullscreen games."""
    GWL_EXSTYLE = -20
    WS_EX_LAYERED = 0x00080000
    WS_EX_TRANSPARENT = 0x00000020
    user32 = ctypes.windll.user32
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    if enable:
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT)
    else:
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style & ~WS_EX_TRANSPARENT)


def toggle_windows_autostart(enable: bool) -> bool:
    """Adds or removes silent VBS launcher from Windows CurrentVersion\\Run registry key."""
    try:
        import winreg

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        app_name = "DynamicIslandWindows"
        vbs_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "Start_Dynamic_Island.vbs"))
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS) as key:
            if enable:
                winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, f'wscript.exe "{vbs_path}"')
            else:
                try:
                    winreg.DeleteValue(key, app_name)
                except FileNotFoundError:
                    pass
        return True
    except Exception:
        return False


def is_in_windows_autostart() -> bool:
    try:
        import winreg

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, "DynamicIslandWindows")
            return True
    except Exception:
        return False


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Island")
    app_icon = create_tray_icon()
    app.setWindowIcon(app_icon)

    media = MediaSessionWorker()
    audio = AudioEngine()
    telemetry = SystemTelemetry()

    island = DynamicIslandWidget(media, audio, telemetry)
    island.show()

    # System Tray Setup
    tray = QSystemTrayIcon(app_icon, app)
    tray.setToolTip("Dynamic Island для Windows")

    menu = QMenu()
    menu.setStyleSheet(
        """
        QMenu {
            background-color: #050507;
            color: #F2F2F7;
            border: 1px solid #24242C;
            border-radius: 12px;
            padding: 6px;
            font-family: 'Segoe UI';
            font-size: 12px;
        }
        QMenu::item {
            padding: 7px 22px;
            border-radius: 7px;
        }
        QMenu::item:selected {
            background-color: #24242E;
            color: #FFFFFF;
        }
        QMenu::separator {
            height: 1px;
            background: #1C1C24;
            margin: 5px 10px;
        }
        """
    )

    act_expand = QAction("Раскрыть / Закрепить остров", menu)
    def _on_toggle_expand():
        island.is_pinned_open = not island.is_pinned_open
        island.is_hovered = island.is_pinned_open
        island._update_target_dimensions()
        island._ensure_anim_running()
    act_expand.triggered.connect(_on_toggle_expand)
    menu.addAction(act_expand)

    act_standby = QAction("Скриншот области экрана", menu)
    act_standby.triggered.connect(island.snip_overlay.start_capture)
    menu.addAction(act_standby)

    act_gamemode = QAction("Игровой режим (пропускать клики)", menu)
    act_gamemode.setCheckable(True)
    def _on_toggle_gamemode(checked: bool):
        island.game_mode_locked = checked
        island.is_hovered = False
        island.is_pinned_open = False
        set_ClickThrough(int(island.winId()), checked)
        island._update_target_dimensions()
        island._ensure_anim_running()
    act_gamemode.triggered.connect(_on_toggle_gamemode)
    menu.addAction(act_gamemode)

    menu.addSeparator()

    act_autostart = QAction("Запускать вместе с Windows", menu)
    act_autostart.setCheckable(True)
    act_autostart.setChecked(is_in_windows_autostart())
    act_autostart.triggered.connect(toggle_windows_autostart)
    menu.addAction(act_autostart)

    menu.addSeparator()

    act_quit = QAction("Выход", menu)
    def _on_quit():
        media.stop()
        tray.hide()
        app.quit()
    act_quit.triggered.connect(_on_quit)
    menu.addAction(act_quit)

    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: _on_toggle_expand()
        if reason == QSystemTrayIcon.ActivationReason.Trigger
        else None
    )
    tray.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
