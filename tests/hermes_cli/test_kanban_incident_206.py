"""Regression coverage for incident #206 worker-exit attribution."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    kb.init_db()
    with kb._worker_exit_registry_lock:
        kb._managed_worker_processes.clear()
        kb._recent_worker_exits.clear()
    try:
        yield home
    finally:
        with kb._worker_exit_registry_lock:
            kb._managed_worker_processes.clear()
            kb._recent_worker_exits.clear()


def _claimed(conn, title="incident"):
    task_id = kb.create_task(conn, title=title, assignee="worker")
    task = kb.claim_task(conn, task_id)
    assert task is not None
    return task_id, task


def _run_row(conn, task_id):
    return conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()


def _wait_for_observer(pid):
    with kb._worker_exit_registry_lock:
        observation = kb._managed_worker_processes[pid]
    assert observation.exited.wait(3), f"observer did not publish pid {pid}"
    return observation


def _remember(conn, proc, task_id, run_id):
    kb._remember_managed_worker_process(
        proc,
        task_id=task_id,
        run_id=run_id,
        board_db_path=kb._connection_main_db_path(conn),
    )


def _managed_child(code):
    return subprocess.Popen([sys.executable, "-c", code])


def test_managed_rc1_survives_intervening_popen_and_cleans_after_terminal_row(board, monkeypatch):
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    with kb.connect() as conn:
        task_id, task = _claimed(conn)
        # The detector clock seam is intentionally a small synthetic epoch.
        # Keep this attempt outside grace without changing process-global time.
        conn.execute("UPDATE task_runs SET started_at = 0 WHERE task_id = ?", (task_id,))
        proc = _managed_child("import sys; sys.exit(1)")
        _remember(conn, proc, task_id, task.current_run_id)
        kb._set_worker_pid(conn, task_id, proc.pid)
        # This unrelated Popen must retain its own wait status: there is no
        # process-wide waitpid(-1) sweep to steal it.
        intervening = _managed_child("import sys; sys.exit(7)")
        assert intervening.wait(timeout=3) == 7
        observed = _wait_for_observer(proc.pid)
        assert observed.returncode == 1

        assert kb.detect_crashed_workers(conn) == [task_id]
        assert "code 1" in _run_row(conn, task_id)["error"]
        with kb._worker_exit_registry_lock:
            assert proc.pid not in kb._managed_worker_processes


def test_observer_records_real_between_tick_latency(board, monkeypatch):
    monkeypatch.setattr(kb, "_worker_exit_observation_clock", lambda: 1002.0)
    monkeypatch.setattr(kb, "_worker_exit_detection_clock", lambda: 1060.0)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    with kb.connect() as conn:
        task_id, task = _claimed(conn)
        # The detector clock seam is intentionally a small synthetic epoch.
        # Keep this attempt outside grace without changing process-global time.
        conn.execute("UPDATE task_runs SET started_at = 0 WHERE task_id = ?", (task_id,))
        proc = _managed_child("import sys; sys.exit(1)")
        _remember(conn, proc, task_id, task.current_run_id)
        kb._set_worker_pid(conn, task_id, proc.pid)
        _wait_for_observer(proc.pid)

        assert kb.detect_crashed_workers(conn) == [task_id]
        run = _run_row(conn, task_id)
        metadata = json.loads(run["metadata"])
        assert run["ended_at"] == 1060
        assert metadata["worker_exit_observed_at"] == 1002.0
        assert metadata["detection_latency_seconds"] == pytest.approx(58.0)
        event = next(e for e in kb.list_events(conn, task_id) if e.kind == "crashed")
        assert event.payload["detection_latency_seconds"] == pytest.approx(58.0)


@pytest.mark.parametrize(
    ("script", "expected_kind", "expected_outcome"),
    [
        ("import sys; sys.exit(0)", "clean_exit", "crashed"),
        ("import sys; sys.exit(75)", "rate_limited", "rate_limited"),
        ("import os, signal; os.kill(os.getpid(), signal.SIGTERM)", "signaled", "crashed"),
    ],
)
def test_real_observer_preserves_exit_semantics(board, monkeypatch, script, expected_kind, expected_outcome):
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    with kb.connect() as conn:
        task_id, task = _claimed(conn, expected_kind)
        proc = _managed_child(script)
        _remember(conn, proc, task_id, task.current_run_id)
        kb._set_worker_pid(conn, task_id, proc.pid)
        _wait_for_observer(proc.pid)

        crashed = kb.detect_crashed_workers(conn)
        assert (crashed == []) is (expected_kind == "rate_limited")
        assert _run_row(conn, task_id)["outcome"] == expected_outcome
        event = next(e for e in kb.list_events(conn, task_id)
                     if e.kind in {"protocol_violation", "rate_limited", "crashed"})
        assert event.payload["exit_kind"] == expected_kind


@pytest.mark.parametrize("terminal", ["complete", "block"])
def test_dispatcher_cleans_normal_terminal_worker_after_observation(board, terminal):
    with kb.connect() as conn:
        task_id, task = _claimed(conn, terminal)
        proc = _managed_child("pass")
        _remember(conn, proc, task_id, task.current_run_id)
        kb._set_worker_pid(conn, task_id, proc.pid)
        _wait_for_observer(proc.pid)
        if terminal == "complete":
            assert kb.complete_task(conn, task_id, result="done")
        else:
            assert kb.block_task(conn, task_id, reason="needs input")
        assert kb._cleanup_managed_worker_processes(conn) == [proc.pid]
        with kb._worker_exit_registry_lock:
            assert proc.pid not in kb._managed_worker_processes


def test_cleanup_uses_exact_board_run_and_task_identity(board, monkeypatch, tmp_path):
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    first_db = tmp_path / "first.db"
    second_db = tmp_path / "second.db"
    with kb.connect(db_path=first_db) as first, kb.connect(db_path=second_db) as second:
        task_id, task = _claimed(first, "first board")
        other_id, other_task = _claimed(second, "second board")
        assert task.current_run_id == other_task.current_run_id == 1
        proc = _managed_child("import sys; sys.exit(75)")
        _remember(first, proc, task_id, task.current_run_id)
        kb._set_worker_pid(first, task_id, proc.pid)
        _wait_for_observer(proc.pid)

        # A colliding run id on another board must not release our observer.
        assert kb._cleanup_managed_worker_processes(second) == []
        with kb._worker_exit_registry_lock:
            assert proc.pid in kb._managed_worker_processes

        assert kb.detect_crashed_workers(first) == []
        assert _run_row(first, task_id)["outcome"] == "rate_limited"
        with kb._worker_exit_registry_lock:
            assert proc.pid not in kb._managed_worker_processes


def test_reclaim_closure_cleans_observed_worker(board, monkeypatch):
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    with kb.connect() as conn:
        task_id, task = _claimed(conn, "reclaim")
        proc = _managed_child("pass")
        _remember(conn, proc, task_id, task.current_run_id)
        kb._set_worker_pid(conn, task_id, proc.pid)
        _wait_for_observer(proc.pid)
        conn.execute("UPDATE tasks SET claim_expires = 0 WHERE id = ?", (task_id,))

        assert kb.release_stale_claims(conn) == 1
        assert _run_row(conn, task_id)["outcome"] == "reclaimed"
        assert kb._cleanup_managed_worker_processes(conn) == [proc.pid]


def test_observer_wait_failure_is_expirable_and_non_waiters_are_not_retained(board, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(kb, "_worker_exit_observation_clock", lambda: clock[0])

    class RaisingWait:
        pid = 48101
        returncode = None

        def wait(self):
            raise RuntimeError("observer failure")

    class NoWait:
        pid = 48102

    with kb.connect() as conn:
        task_id, task = _claimed(conn, "observer failure")
        proc = RaisingWait()
        _remember(conn, proc, task_id, task.current_run_id)
        _wait_for_observer(proc.pid)
        kb._remember_managed_worker_process(NoWait())
        with kb._worker_exit_registry_lock:
            observed = kb._managed_worker_processes[proc.pid]
            assert observed.returncode is None
            assert observed.observed_at == 1000.0
            assert NoWait.pid not in kb._managed_worker_processes
        assert kb.reap_worker_zombies() == []

        conn.execute("DELETE FROM task_runs WHERE id = ?", (task.current_run_id,))
        assert kb._cleanup_managed_worker_processes(conn) == []
        clock[0] += kb._RECENT_WORKER_EXIT_TTL_SECONDS
        assert kb._cleanup_managed_worker_processes(conn) == [proc.pid]
        with kb._worker_exit_registry_lock:
            assert proc.pid not in kb._managed_worker_processes


def test_reap_reports_a_real_observer_exit_once(board):
    with kb.connect() as conn:
        task_id, task = _claimed(conn, "reap once")
        proc = _managed_child("import sys; sys.exit(1)")
        _remember(conn, proc, task_id, task.current_run_id)
        _wait_for_observer(proc.pid)
        assert kb.reap_worker_zombies() == [proc.pid]
        assert kb.reap_worker_zombies() == []


def test_non_goal_spawn_uses_quiet_path_and_rc_policies_remain_distinct(board, monkeypatch, tmp_path):
    captured = {}

    class FakePopen:
        pid = 43001

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        return FakePopen()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with kb.connect() as conn:
        task_id, task = _claimed(conn)
        assert task.goal_mode is False
        assert kb._default_spawn(task, str(workspace)) == 43001
        assert "-Q" in captured["cmd"]
        assert "HERMES_KANBAN_GOAL_MODE" not in captured["env"]
        # Backward-compatible raw-status fallback still distinguishes generic
        # rc=1 (a crash) from rc=75 (neutral quota requeue).
        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
        kb._set_worker_pid(conn, task_id, 43001)
        kb._record_worker_exit(43001, 1 << 8)
        assert kb.detect_crashed_workers(conn) == [task_id]
        assert _run_row(conn, task_id)["outcome"] == "crashed"
        assert kb.get_task(conn, task_id).consecutive_failures == 1
        next_id, _ = _claimed(conn, "quota")
        kb._set_worker_pid(conn, next_id, 43002)
        kb._record_worker_exit(43002, kb.KANBAN_RATE_LIMIT_EXIT_CODE << 8)
        assert kb.detect_crashed_workers(conn) == []
        assert kb.get_task(conn, next_id).status == "ready"
        assert kb.get_task(conn, next_id).consecutive_failures == 0


def test_retry_grace_and_malformed_missing_current_run_remain_detectable(board, monkeypatch):
    clock = [1010.0]
    monkeypatch.setattr(kb, "_worker_exit_detection_clock", lambda: clock[0])
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "30")
    with kb.connect() as conn:
        task_id, _ = _claimed(conn, "retry grace")
        conn.execute("UPDATE task_runs SET started_at = 1000 WHERE task_id = ?", (task_id,))
        kb._set_worker_pid(conn, task_id, 45001)
        assert kb.detect_crashed_workers(conn) == []
        clock[0] = 1031.0
        assert kb.detect_crashed_workers(conn) == [task_id]

        legacy_id, _ = _claimed(conn, "legacy malformed")
        conn.execute(
            "UPDATE tasks SET current_run_id = 999999, started_at = 0, worker_pid = 45002 WHERE id = ?",
            (legacy_id,),
        )
        assert kb.detect_crashed_workers(conn) == [legacy_id]
