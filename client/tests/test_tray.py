from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from PIL import Image

from warpsocket.tray import (
    TrayApp,
    icon_for_state,
    load_base_icon,
    tooltip_for_state,
)
from warpsocket.tunnel import TunnelState


def test_load_base_icon_returns_rgba_image():
    img = load_base_icon()
    assert isinstance(img, Image.Image)
    assert img.mode == "RGBA"


def test_icon_for_state_handles_all_states():
    base = load_base_icon()
    for state in TunnelState:
        img = icon_for_state(state, base)
        assert isinstance(img, Image.Image)
        assert img.size == base.size


def test_icon_for_state_differs_per_state():
    base = load_base_icon()
    images = {state: icon_for_state(state, base) for state in TunnelState}
    seen: set[bytes] = set()
    for img in images.values():
        seen.add(img.tobytes())
    assert len(seen) == len(TunnelState)  # every state distinct


def test_tooltip_for_state_returns_string_for_all_states():
    for state in TunnelState:
        t = tooltip_for_state(state)
        assert isinstance(t, str)
        assert "WarpSocket" in t


def test_tooltip_for_unknown_state_raises():
    with pytest.raises(KeyError):
        tooltip_for_state("not-a-state")  # type: ignore[arg-type]


def test_tray_subscribes_to_manager_on_construction():
    manager = MagicMock()
    manager.state = TunnelState.DISCONNECTED
    TrayApp(
        manager=manager,
        on_import_warpcfg=lambda: None,
        on_view_logs=lambda: None,
        on_quit=lambda: None,
    )
    manager.add_listener.assert_called_once()


def test_tray_state_change_updates_icon():
    manager = MagicMock()
    manager.state = TunnelState.DISCONNECTED
    app = TrayApp(
        manager=manager,
        on_import_warpcfg=lambda: None,
        on_view_logs=lambda: None,
        on_quit=lambda: None,
    )
    fake_icon = MagicMock()
    app._icon = fake_icon

    app._on_state_change(TunnelState.CONNECTED)

    assert fake_icon.icon is not None
    assert fake_icon.title == tooltip_for_state(TunnelState.CONNECTED)


def test_tray_state_change_noop_before_run():
    manager = MagicMock()
    manager.state = TunnelState.DISCONNECTED
    app = TrayApp(
        manager=manager,
        on_import_warpcfg=lambda: None,
        on_view_logs=lambda: None,
        on_quit=lambda: None,
    )
    app._on_state_change(TunnelState.CONNECTED)  # icon is None; must not raise


def test_tray_quit_calls_on_quit_and_stops_icon():
    manager = MagicMock()
    manager.state = TunnelState.DISCONNECTED
    on_quit = MagicMock()
    app = TrayApp(
        manager=manager,
        on_import_warpcfg=lambda: None,
        on_view_logs=lambda: None,
        on_quit=on_quit,
    )
    fake_icon = MagicMock()
    app._icon = fake_icon

    app._quit()

    fake_icon.stop.assert_called_once()
    on_quit.assert_called_once()
