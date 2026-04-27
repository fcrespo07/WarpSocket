from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw

from warpsocket.tunnel import TunnelManager, TunnelState

log = logging.getLogger(__name__)

_RESOURCES = Path(__file__).parent / "resources"
_BASE_ICON = _RESOURCES / "app_icon.png"

_STATE_COLORS: dict[TunnelState, tuple[int, int, int]] = {
    TunnelState.DISCONNECTED: (128, 128, 128),
    TunnelState.CONNECTING: (255, 200, 0),
    TunnelState.CONNECTED: (0, 200, 0),
    TunnelState.RECONNECTING: (255, 150, 0),
    TunnelState.FAILED: (220, 0, 0),
}

_STATE_TOOLTIPS: dict[TunnelState, str] = {
    TunnelState.DISCONNECTED: "WarpSocket — desconectado",
    TunnelState.CONNECTING: "WarpSocket — conectando...",
    TunnelState.CONNECTED: "WarpSocket — conectado",
    TunnelState.RECONNECTING: "WarpSocket — reconectando...",
    TunnelState.FAILED: "WarpSocket — fallo de conexión",
}


def load_base_icon() -> Image.Image:
    return Image.open(_BASE_ICON).convert("RGBA")


def icon_for_state(state: TunnelState, base: Image.Image) -> Image.Image:
    color = _STATE_COLORS[state]
    img = base.copy()
    draw = ImageDraw.Draw(img)
    size = img.width
    dot = size // 3
    margin = size // 16
    box = [size - dot - margin, size - dot - margin, size - margin, size - margin]
    draw.ellipse(box, fill=color, outline=(255, 255, 255), width=max(1, size // 64))
    return img


def tooltip_for_state(state: TunnelState) -> str:
    return _STATE_TOOLTIPS[state]


class TrayApp:
    def __init__(
        self,
        manager: TunnelManager,
        on_import_warpcfg: Callable[[], None],
        on_view_logs: Callable[[], None],
        on_quit: Callable[[], None],
    ) -> None:
        self._manager = manager
        self._on_import = on_import_warpcfg
        self._on_view_logs = on_view_logs
        self._on_quit = on_quit
        self._icon = None
        self._base = load_base_icon()
        self._manager.add_listener(self._on_state_change)

    def _on_state_change(self, state: TunnelState) -> None:
        if self._icon is None:
            return
        try:
            self._icon.icon = icon_for_state(state, self._base)
            self._icon.title = tooltip_for_state(state)
        except Exception:
            log.exception("Failed to update tray icon for state %s", state)

    def _reconnect_async(self) -> None:
        threading.Thread(
            target=lambda: (self._manager.stop(), self._manager.start()),
            daemon=True,
            name="warpsocket-tray-reconnect",
        ).start()

    def _quit(self) -> None:
        if self._icon is not None:
            self._icon.stop()
        try:
            self._on_quit()
        except Exception:
            log.exception("Error in on_quit callback")

    def run(self) -> None:
        """Start the tray icon in a background thread (non-blocking)."""
        import pystray

        menu = pystray.Menu(
            pystray.MenuItem("Ver logs", lambda _icon, _item: self._on_view_logs()),
            pystray.MenuItem("Reconectar", lambda _icon, _item: self._reconnect_async()),
            pystray.MenuItem(
                "Importar .warpcfg...", lambda _icon, _item: self._on_import()
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Salir", lambda _icon, _item: self._quit()),
        )

        initial_state = self._manager.state
        self._icon = pystray.Icon(
            name="warpsocket",
            icon=icon_for_state(initial_state, self._base),
            title=tooltip_for_state(initial_state),
            menu=menu,
        )
        self._icon.run_detached()
