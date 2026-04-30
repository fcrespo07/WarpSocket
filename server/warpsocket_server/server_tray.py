from __future__ import annotations

import logging
import queue
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw

from warpsocket_server.server_manager import ServerManager, ServerState

log = logging.getLogger(__name__)

_RESOURCES = Path(__file__).parent / "resources"

_STATE_COLORS: dict[ServerState, tuple[int, int, int]] = {
    ServerState.STOPPED:  (128, 128, 128),
    ServerState.STARTING: (255, 200,   0),
    ServerState.RUNNING:  (  0, 200,   0),
    ServerState.ERROR:    (220,   0,   0),
}

_STATE_TOOLTIPS: dict[ServerState, str] = {
    ServerState.STOPPED:  "WarpSocket Server — detenido",
    ServerState.STARTING: "WarpSocket Server — iniciando…",
    ServerState.RUNNING:  "WarpSocket Server — activo",
    ServerState.ERROR:    "WarpSocket Server — error",
}

_NO_CONFIG_TOOLTIP = "WarpSocket Server — sin configurar"
_NO_CONFIG_DOT = (100, 100, 100)


def _load_base_icon() -> Image.Image:
    ico = _RESOURCES / "app_icon.png"
    if ico.exists():
        return Image.open(ico).convert("RGBA")
    # Fallback: generate a simple icon.
    img = Image.new("RGBA", (64, 64), (30, 30, 30, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle([8, 8, 56, 56], outline=(100, 180, 255), width=4)
    draw.text((18, 20), "WS", fill=(100, 180, 255))
    return img


def _icon_for_state(state: ServerState | None, base: Image.Image) -> Image.Image:
    color = _STATE_COLORS.get(state, _NO_CONFIG_DOT) if state is not None else _NO_CONFIG_DOT
    img = base.copy()
    draw = ImageDraw.Draw(img)
    size = img.width
    dot = size // 3
    margin = size // 16
    box = [size - dot - margin, size - dot - margin, size - margin, size - margin]
    draw.ellipse(box, fill=color, outline=(255, 255, 255), width=max(1, size // 64))
    return img


class ServerTrayApp:
    """System tray icon for the server.

    Left-click (or 'Abrir WarpSocket Server') → shows main window.
    'Parar y salir' → full shutdown.
    """

    def __init__(
        self,
        manager: ServerManager | None,
        ui_queue: queue.Queue[Callable[[], None]],
        on_show: Callable[[], None],
        on_quit: Callable[[], None],
    ) -> None:
        self._manager = manager
        self._ui_queue = ui_queue
        self._on_show = on_show
        self._on_quit = on_quit
        self._icon = None
        self._base = _load_base_icon()

        if manager:
            manager.add_listener(self._on_state_change)

    def update_manager(self, manager: ServerManager) -> None:
        self._manager = manager
        manager.add_listener(self._on_state_change)
        self._refresh_icon()

    def _on_state_change(self, state: ServerState) -> None:
        if self._icon is None:
            return
        try:
            self._icon.icon = _icon_for_state(state, self._base)
            self._icon.title = _STATE_TOOLTIPS.get(state, "WarpSocket Server")
        except Exception:
            log.exception("Failed to update tray icon for state %s", state)

    def _refresh_icon(self) -> None:
        if self._icon is None:
            return
        state = self._manager.state if self._manager else None
        self._on_state_change(state)

    def _open_window(self, icon, item) -> None:
        self._ui_queue.put(self._on_show)

    def _quit(self, icon, item) -> None:
        if self._icon is not None:
            self._icon.stop()
        self._ui_queue.put(self._on_quit)

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass

    def run(self) -> None:
        import pystray

        state = self._manager.state if self._manager else None
        tooltip = _STATE_TOOLTIPS.get(state, _NO_CONFIG_TOOLTIP) if state else _NO_CONFIG_TOOLTIP

        menu = pystray.Menu(
            pystray.MenuItem(
                "Abrir WarpSocket Server",
                self._open_window,
                default=True,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Parar y salir", self._quit),
        )

        self._icon = pystray.Icon(
            name="warpsocket-server",
            icon=_icon_for_state(state, self._base),
            title=tooltip,
            menu=menu,
        )
        self._icon.run_detached()
