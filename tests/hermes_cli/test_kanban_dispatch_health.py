"""Result-derived dispatcher health regression coverage."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kb, "_memory_pressure_level", lambda: "ok")
    kb.init_db()
    return home


def test_legitimate_tick_results_are_not_unhealthy():
    cases = [
        kb.DispatchResult(capacity_capped="max_spawn"),
        kb.DispatchResult(memory_pressure="critical"),
        kb.DispatchResult(skipped_locked=True),
        kb.DispatchResult(candidates_examined=1, skipped_per_profile_capped=[("t", "alice", 1)]),
        kb.DispatchResult(candidates_examined=1, skipped_nonspawnable=["t"]),
        kb.DispatchResult(candidates_examined=1, respawn_guarded=[("t", "recent_success")]),
        kb.DispatchResult(candidates_examined=1, claim_contended=1),
        kb.DispatchResult(),
    ]
    assert all(kb.dispatch_health_reason(result) is None for result in cases)


def test_board_cap_is_typed_and_quiet(kanban_home, all_assignees_spawnable):
    with kb.connect() as conn:
        running = kb.create_task(conn, title="running", assignee="alice")
        assert kb.claim_task(conn, running) is not None
        kb.create_task(conn, title="queued", assignee="alice")
        result = kb.dispatch_once(
            conn, max_spawn=1, spawn_fn=lambda *args, **kwargs: 42,
            reconcile_orphans=False,
        )
    assert result.capacity_capped == "max_spawn"
    assert result.candidates_examined == 0
    assert kb.dispatch_health_reason(result) is None


def test_first_spawn_failure_is_visible_before_breaker(
    kanban_home, all_assignees_spawnable,
):
    def broken_spawn(*args, **kwargs):
        raise RuntimeError("profile executable missing")

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="broken", assignee="alice")
        result = kb.dispatch_once(conn, spawn_fn=broken_spawn, failure_limit=3)
    assert result.spawn_failure_count == 1
    assert result.spawn_failed == [(task_id, "profile executable missing")]
    assert result.auto_blocked == []
    assert kb.dispatch_health_reason(result) == "launch_failed"


def test_partial_success_does_not_mask_launch_failure(
    kanban_home, all_assignees_spawnable,
):
    calls = 0

    def mixed_spawn(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("first launch failed")
        return 42

    with kb.connect() as conn:
        kb.create_task(conn, title="first", assignee="alice")
        kb.create_task(conn, title="second", assignee="alice")
        result = kb.dispatch_once(conn, spawn_fn=mixed_spawn, failure_limit=3)
    assert result.spawn_failure_count == 1
    assert len(result.spawned) == 1
    assert kb.dispatch_health_reason(result) == "launch_failed"


def test_failure_samples_are_bounded_and_json_serializable(
    kanban_home, all_assignees_spawnable,
):
    def broken_spawn(*args, **kwargs):
        raise RuntimeError("boom")

    with kb.connect() as conn:
        for index in range(5):
            kb.create_task(conn, title=f"broken-{index}", assignee="alice")
        result = kb.dispatch_once(conn, spawn_fn=broken_spawn, failure_limit=9)
    assert result.spawn_failure_count == 5
    assert len(result.spawn_failed) == 3
    json.dumps(dataclasses.asdict(result))


def test_unassigned_unknown_and_unavailable_are_distinct():
    assert kb.dispatch_health_reason(None) == "tick_unavailable"
    assert kb.dispatch_health_reason(
        kb.DispatchResult(candidates_examined=1, skipped_unassigned=["t"])
    ) == "unassigned"
    assert kb.dispatch_health_reason(
        kb.DispatchResult(candidates_examined=1)
    ) == "unknown"
