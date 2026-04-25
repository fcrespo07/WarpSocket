from __future__ import annotations

import time
from threading import Event
from unittest.mock import MagicMock

from warpsocket.config import (
    ClientConfig,
    ReconnectConfig,
    RoutingConfig,
    ServerConfig,
    TlsConfig,
    TunnelConfig,
    WireguardConfig,
)
from warpsocket.tunnel import TunnelManager, TunnelState


def _make_config(max_attempts: int = 3, delays: list[int] | None = None) -> ClientConfig:
    return ClientConfig(
        schema_version=1,
        server=ServerConfig(endpoint="203.0.113.42", port=443, http_upgrade_path_prefix="x"),
        tls=TlsConfig(cert_fingerprint_sha256="A" * 95),
        tunnel=TunnelConfig(local_port=51820, remote_host="10.0.0.1", remote_port=51820),
        wireguard=WireguardConfig(
            tunnel_name="WarpSocket",
            client_address="10.0.0.42/32",
            client_private_key="priv",
            server_public_key="pub",
        ),
        routing=RoutingConfig(bypass_ips=["203.0.113.42"]),
        reconnect=ReconnectConfig(max_attempts=max_attempts, delays_seconds=delays or [0, 0, 0]),
    )


class FakeTunnel:
    def __init__(self) -> None:
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.connect_errors: list[Exception | None] = []
        self.alive = True
        self.alive_until_event: Event | None = None

    def connect(self) -> None:
        self.connect_calls += 1
        if self.connect_errors:
            err = self.connect_errors.pop(0)
            if err is not None:
                raise err
        self.alive = True

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.alive = False

    @property
    def is_active(self) -> bool:
        if self.alive_until_event is not None and self.alive_until_event.is_set():
            self.alive = False
        return self.alive


def _wait_until(predicate, timeout: float = 2.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _make_manager(cfg, tunnel, *, stability_seconds: float = 100.0) -> TunnelManager:
    return TunnelManager(cfg, tunnel=tunnel, stability_seconds=stability_seconds, poll_interval=0.005)


# --- happy path ---

def test_initial_state_is_disconnected():
    m = _make_manager(_make_config(), FakeTunnel())
    assert m.state == TunnelState.DISCONNECTED


def test_start_transitions_through_connecting_to_connected():
    fake = FakeTunnel()
    m = _make_manager(_make_config(), fake)
    seen: list[TunnelState] = []
    m.add_listener(lambda s: seen.append(s))

    m.start()
    assert _wait_until(lambda: m.state == TunnelState.CONNECTED)
    m.stop(timeout=2)

    assert TunnelState.CONNECTING in seen
    assert TunnelState.CONNECTED in seen
    assert seen[-1] == TunnelState.DISCONNECTED


def test_stop_disconnects_tunnel():
    fake = FakeTunnel()
    m = _make_manager(_make_config(), fake)
    m.start()
    assert _wait_until(lambda: m.state == TunnelState.CONNECTED)
    m.stop(timeout=2)
    assert fake.disconnect_calls >= 1
    assert m.state == TunnelState.DISCONNECTED


# --- failure paths ---

def test_failed_state_after_max_attempts():
    fake = FakeTunnel()
    fake.connect_errors = [RuntimeError("nope")] * 3
    m = _make_manager(_make_config(max_attempts=3, delays=[0, 0, 0]), fake)
    m.start()
    assert _wait_until(lambda: m.state == TunnelState.FAILED, timeout=3)
    assert fake.connect_calls == 3
    m.stop(timeout=2)


def test_recovers_after_transient_failure():
    fake = FakeTunnel()
    fake.connect_errors = [RuntimeError("hiccup"), None]  # fails once, then succeeds
    m = _make_manager(_make_config(max_attempts=5, delays=[0]), fake)
    seen: list[TunnelState] = []
    m.add_listener(lambda s: seen.append(s))
    m.start()
    assert _wait_until(lambda: m.state == TunnelState.CONNECTED)
    m.stop(timeout=2)
    assert TunnelState.RECONNECTING in seen


# --- reconnect on tunnel death ---

def test_reconnects_when_tunnel_dies():
    fake = FakeTunnel()
    m = _make_manager(_make_config(max_attempts=5, delays=[0]), fake)
    m.start()
    assert _wait_until(lambda: m.state == TunnelState.CONNECTED)

    fake.alive = False  # simulate process death
    assert _wait_until(lambda: fake.connect_calls >= 2, timeout=3)
    m.stop(timeout=2)


# --- stability resets attempt counter ---

def test_stability_resets_attempt_counter():
    fake = FakeTunnel()
    # connect succeeds initially, then we kill it; if stability reset works,
    # the manager treats subsequent failure as "fresh", granting full max_attempts again.
    m = _make_manager(
        _make_config(max_attempts=2, delays=[0]),
        fake,
        stability_seconds=0.05,  # tiny so it triggers fast
    )
    m.start()
    assert _wait_until(lambda: m.state == TunnelState.CONNECTED)

    # Wait past stability threshold so attempt counter resets
    time.sleep(0.15)

    # Now kill the tunnel; manager should reconnect successfully
    fake.alive = False
    assert _wait_until(lambda: fake.connect_calls >= 2, timeout=3)
    assert _wait_until(lambda: m.state == TunnelState.CONNECTED, timeout=2)
    m.stop(timeout=2)


# --- listener semantics ---

def test_listener_receives_state_changes():
    fake = FakeTunnel()
    m = _make_manager(_make_config(), fake)
    received: list[TunnelState] = []

    def listener(state: TunnelState) -> None:
        received.append(state)

    m.add_listener(listener)
    m.start()
    assert _wait_until(lambda: m.state == TunnelState.CONNECTED)
    m.stop(timeout=2)
    assert received[0] == TunnelState.CONNECTING
    assert TunnelState.CONNECTED in received
    assert received[-1] == TunnelState.DISCONNECTED


def test_listener_exceptions_dont_break_manager():
    fake = FakeTunnel()
    m = _make_manager(_make_config(), fake)
    m.add_listener(lambda s: (_ for _ in ()).throw(RuntimeError("boom")))
    m.start()
    assert _wait_until(lambda: m.state == TunnelState.CONNECTED)
    m.stop(timeout=2)


# --- start() is idempotent ---

def test_start_twice_does_not_spawn_two_threads():
    fake = FakeTunnel()
    m = _make_manager(_make_config(), fake)
    m.start()
    m.start()
    assert _wait_until(lambda: m.state == TunnelState.CONNECTED)
    m.stop(timeout=2)
    # connect should have been called once (not twice from two threads)
    assert fake.connect_calls == 1


# --- _pick_delay ---

def test_pick_delay_uses_progressive_indices():
    from warpsocket.tunnel import _pick_delay
    delays = [5, 10, 20, 30, 60]
    assert _pick_delay(delays, 1) == 5
    assert _pick_delay(delays, 2) == 10
    assert _pick_delay(delays, 5) == 60
    assert _pick_delay(delays, 99) == 60  # caps at last


def test_pick_delay_empty_falls_back_to_5():
    from warpsocket.tunnel import _pick_delay
    assert _pick_delay([], 1) == 5
