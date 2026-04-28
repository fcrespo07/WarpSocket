from __future__ import annotations

import logging
import queue
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw

from warpsocket.tunnel import TunnelManager, TunnelState

log = logging.getLogger(__name__)

_RESOURCES = Path(__file__).parent / "resources"
_BASE_ICON = _RESOURCES / "app_icon.png"

_STATE_COLORS: dict[TunnelState, tuple[int, int, int]] = {
    TunnelState.DISCONNECTED: (128, 128, 128),
    TunnelState.CONNECTING:   (255, 200,   0),
    TunnelState.CONNECTED:    (  0, 200,   0),
    TunnelState.RECONNECTING: (255, 150,   0),
    TunnelState.FAILED:       (220,   0,   0),
}

_STATE_TOOLTIPS: dict[TunnelState, str] = {
    TunnelState.DISCONNECTED: "WarpSocket — desconectado",
    TunnelState.CONNECTING:   "WarpSocket — conectando...",
    TunnelState.CONNECTED:    "WarpSocket — conectado",
    TunnelState.RECONNECTING: "WarpSocket — reconectando...",
    TunnelState.FAILED:       "WarpSocket — fallo de conexión",
}

_NO_CONFIG_TOOLTIP = "WarpSocket — sin configuración"
_NO_CONFIG_DOT = (100, 100, 100)


def load_base_icon() -> Image.Image:
    return Image.open(_BASE_ICON).convert("RGBA")


def icon_for_state(state: TunnelState | None, base: Image.Image) -> Image.Image:
    color = _STATE_COLORS.get(state, _NO_CONFIG_DOT) if state is not None else _NO_CONFIG_DOT
    img = base.copy()
    draw = ImageDraw.Draw(img)
    size = img.width
    dot = size // 3
    margin = size // 16
    box = [size - dot - margin, size - dot - margin, size - margin, size - margin]
    draw.ellipse(box, fill=color, outline=(255, 255, 255), width=max(1, size // 64))
    return img


class TrayApp:
    """System tray icon.

    Left-click (or 'Abrir WarpSocket'): shows the main window via ui_queue.
    'Parar y salir': full quit — stops tunnel and exits the process.
    """

    def __init__(
        self,
        manager: TunnelManager | None,
        ui_queue: queue.Queue[Callable[[], None]],
        on_show: Callable[[], None],
        on_quit: Callable[[], None],
    ) -> None:
        self._manager = manager
        self._ui_queue = ui_queue
        self._on_show = on_show
        self._on_quit = on_quit
        self._icon = None
        self._base = load_base_icon()

        if manager:
            manager.add_listener(self._on_state_change)

    def update_manager(self, manager: TunnelManager) -> None:
        """Wire a new TunnelManager (called after config import)."""
        self._manager = manager
        manager.add_listener(self._on_state_change)

    def _on_state_change(self, state: TunnelState) -> None:
        if self._icon is None:
            return
        try:
            self._icon.icon = icon_for_state(state, self._base)
            self._icon.title = _STATE_TOOLTIPS.get(state, "WarpSocket")
        except Exception:
            log.exception("Failed to update tray icon for state %s", state)

    def _open_window(self, icon, item) -> None:
        self._ui_queue.put(self._on_show)

    def _quit(self, icon, item) -> None:
        # Stop pystray first so the tray thread can clean up.
        if self._icon is not None:
            self._icon.stop()
        # Push the actual quit work to the tkinter main thread.
        self._ui_queue.put(self._on_quit)

    def stop(self) -> None:
        """Stop the tray icon (called during shutdown)."""
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass

    def run(self) -> None:
        """Start pystray in a background thread (non-blocking)."""
        import pystray

        initial_state = self._manager.state if self._manager else None

        menu = pystray.Menu(
            pystray.MenuItem(
                "Abrir WarpSocket",
                self._open_window,
                default=True,  # triggered by left-click on Windows
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Parar y salir", self._quit),
        )

        self._icon = pystray.Icon(
            name="warpsocket",
            icon=icon_for_state(initial_state, self._base),
            title=_STATE_TOOLTIPS.get(initial_state, _NO_CONFIG_TOOLTIP)
            if initial_state is not None
            else _NO_CONFIG_TOOLTIP,
            menu=menu,
        )
        self._icon.run_detached()
