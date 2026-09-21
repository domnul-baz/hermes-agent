"""Per-board gateway dispatcher health accounting."""

from __future__ import annotations

import inspect

from gateway.kanban_watchers import GatewayKanbanWatchersMixin, _update_dispatch_health


def test_legitimate_capacity_never_warns():
    state = {}
    for tick in range(50):
        assert _update_dispatch_health(state, "capped", None, now=float(tick)) is None
    assert state == {}


def test_launch_failure_warns_on_first_tick():
    state = {}
    message = _update_dispatch_health(state, "broken", "launch_failed", now=0.0)
    assert message is not None
    assert "[broken]" in message
    assert "reason=launch_failed" in message
    assert "consecutive_ticks=1" in message


def test_sustained_nonlaunch_reason_is_windowed_and_rate_limited():
    state = {}
    for tick in range(5):
        assert _update_dispatch_health(state, "board", "unknown", now=float(tick)) is None
    assert _update_dispatch_health(state, "board", "unknown", now=5.0) is not None
    assert _update_dispatch_health(state, "board", "unknown", now=6.0) is None
    assert _update_dispatch_health(state, "board", "unknown", now=305.0) is not None


def test_board_states_are_independent_and_recover_separately():
    state = {}
    assert _update_dispatch_health(state, "a", None, now=0.0) is None
    assert _update_dispatch_health(state, "b", "launch_failed", now=0.0) is not None
    assert "a" not in state
    assert state["b"]["bad_ticks"] == 1
    assert _update_dispatch_health(state, "b", None, now=1.0) is None
    assert state == {}


def test_reason_change_starts_a_new_episode():
    state = {}
    for tick in range(5):
        _update_dispatch_health(state, "board", "unknown", now=float(tick))
    assert _update_dispatch_health(state, "board", "unassigned", now=5.0) is None
    assert state["board"]["bad_ticks"] == 1


def test_watcher_has_no_second_queue_oracle():
    source = inspect.getsource(GatewayKanbanWatchersMixin._kanban_dispatcher_watcher)
    assert "_ready_nonempty" not in source
    assert "has_spawnable_ready" not in source
    assert "has_spawnable_review" not in source
