"""Behaviour regressions for dispatcher source-run mutation fencing."""
from __future__ import annotations

import base64
import json
import socket
from contextlib import contextmanager

import pytest


@contextmanager
def operator_env(monkeypatch):
    """Temporarily remove dispatcher identity for trusted setup/operator calls."""
    with monkeypatch.context() as scoped:
        scoped.delenv("HERMES_KANBAN_TASK", raising=False)
        scoped.delenv("HERMES_KANBAN_RUN_ID", raising=False)
        yield


@pytest.fixture
def live_worker(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "source-run-worker")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    from pathlib import Path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        source = kb.create_task(conn, title="source", assignee="source-run-worker")
        foreign = kb.create_task(conn, title="foreign", assignee="peer")
        dependent = kb.create_task(
            conn, title="dependent", assignee="peer", parents=[source],
        )
        claimed = kb.claim_task(conn, source, claimer="stable-lock")
        run_id = claimed.current_run_id
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", source)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return {
        "source": source,
        "foreign": foreign,
        "dependent": dependent,
        "run_id": int(run_id),
        "home": home,
    }


def _counts(conn):
    return {
        table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        for table in (
            "tasks", "task_runs", "task_comments", "task_events", "task_links",
            "task_attachments",
        )
    }


def _replace_run(monkeypatch, worker):
    from hermes_cli import kanban_db as kb
    with operator_env(monkeypatch):
        conn = kb.connect()
        try:
            assert kb.reclaim_task(conn, worker["source"], reason="test replacement")
            successor = kb.claim_task(conn, worker["source"], claimer="stable-lock")
            assert successor is not None
            return int(successor.current_run_id)
        finally:
            conn.close()


def test_live_source_run_can_comment_self_and_cross_task(live_worker):
    from tools import kanban_tools as kt
    assert json.loads(kt._handle_comment({
        "task_id": live_worker["source"], "body": "self",
    }))["ok"]
    assert json.loads(kt._handle_comment({
        "task_id": live_worker["foreign"], "body": "cross-task handoff",
    }))["ok"]


def test_terminal_source_run_cannot_comment_or_create(monkeypatch, live_worker):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt
    with operator_env(monkeypatch):
        conn = kb.connect()
        try:
            assert kb.complete_task(conn, live_worker["source"], summary="terminal")
            before = _counts(conn)
        finally:
            conn.close()

    comment = json.loads(kt._handle_comment({
        "task_id": live_worker["foreign"], "body": "zombie write",
    }))
    create = json.loads(kt._handle_create({
        "title": "zombie child", "assignee": "peer", "parents": [live_worker["source"]],
    }))
    assert comment.get("ok") is not True
    assert create.get("ok") is not True
    assert "source-run fence rejected" in comment.get("error", "")
    assert "do not retry" in create.get("error", "")

    conn = kb.connect()
    try:
        assert _counts(conn) == before
    finally:
        conn.close()


def test_replaced_run_cannot_mutate_or_extend_successor_claim(monkeypatch, live_worker):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt
    successor_run = _replace_run(monkeypatch, live_worker)
    assert successor_run != live_worker["run_id"]

    conn = kb.connect()
    try:
        before = _counts(conn)
        source_before = kb.get_task(conn, live_worker["source"])
        claim_before = source_before.claim_expires
    finally:
        conn.close()

    calls = [
        kt._handle_comment({"task_id": live_worker["foreign"], "body": "stale"}),
        kt._handle_create({"title": "stale child", "assignee": "peer"}),
        kt._handle_link({
            "parent_id": live_worker["foreign"],
            "child_id": live_worker["dependent"],
        }),
        kt._handle_complete({"summary": "stale completion"}),
        kt._handle_block({"reason": "stale block"}),
        kt._handle_request_review({"summary": "stale review"}),
        kt._handle_request_changes({"reason": "stale change request"}),
        kt._handle_heartbeat({"note": "stale heartbeat"}),
    ]
    for output in calls:
        payload = json.loads(output)
        assert payload.get("ok") is not True
        assert str(live_worker["run_id"]) in payload.get("error", "")
        assert str(successor_run) in payload.get("error", "")

    conn = kb.connect()
    try:
        assert _counts(conn) == before
        source_after = kb.get_task(conn, live_worker["source"])
        assert source_after.current_run_id == successor_run
        assert source_after.claim_expires == claim_before
        assert source_after.claim_lock == "stable-lock"
        assert kb.get_task(conn, live_worker["dependent"]).status == "todo"
    finally:
        conn.close()


@pytest.mark.parametrize("bad_run_id", [None, "not-an-int", "0", "01"])
def test_missing_or_malformed_run_id_fails_closed(monkeypatch, live_worker, bad_run_id):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt
    if bad_run_id is None:
        monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    else:
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", bad_run_id)
    conn = kb.connect()
    try:
        before = _counts(conn)
    finally:
        conn.close()

    for output in (
        kt._handle_comment({"task_id": live_worker["foreign"], "body": "bad identity"}),
        kt._handle_create({"title": "bad identity child", "assignee": "peer"}),
    ):
        payload = json.loads(output)
        assert payload.get("ok") is not True
        assert "source-run fence rejected" in payload.get("error", "")
        expected_reason = "missing-run-id" if bad_run_id is None else "malformed-run-id"
        assert expected_reason in payload.get("error", "")

    conn = kb.connect()
    try:
        assert _counts(conn) == before
    finally:
        conn.close()


def test_operator_without_task_env_can_comment(monkeypatch, live_worker):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt
    with operator_env(monkeypatch):
        payload = json.loads(kt._handle_comment({
            "task_id": live_worker["foreign"], "body": "operator note",
        }))
    assert payload.get("ok") is True
    conn = kb.connect()
    try:
        assert [c.body for c in kb.list_comments(conn, live_worker["foreign"])] == ["operator note"]
    finally:
        conn.close()


def test_stale_run_cannot_attach_and_leaves_no_blob(monkeypatch, live_worker):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt
    _replace_run(monkeypatch, live_worker)
    payload = base64.b64encode(b"should-not-land").decode()
    result = json.loads(kt._handle_attach({
        "filename": "proof.txt", "content_base64": payload,
    }))
    assert result.get("ok") is not True
    conn = kb.connect()
    try:
        assert kb.list_attachments(conn, live_worker["source"]) == []
        assert not kb.task_attachments_dir(live_worker["source"]).exists()
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("handler_name", "db_name", "args"),
    [
        ("_handle_complete", "complete_task", {"summary": "done"}),
        ("_handle_block", "block_task", {"reason": "blocked"}),
        ("_handle_heartbeat", "heartbeat_worker", {"note": "alive"}),
    ],
)
def test_existing_target_run_cas_is_still_passed(
    monkeypatch, live_worker, handler_name, db_name, args,
):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt
    seen = []

    def fake(*call_args, **call_kwargs):
        seen.append(call_kwargs.get("expected_run_id"))
        return True

    monkeypatch.setattr(kb, db_name, fake)
    result = json.loads(getattr(kt, handler_name)(dict(args)))
    assert result.get("ok") is True
    assert seen == [live_worker["run_id"]]



def test_replaced_run_cannot_use_direct_cli_mutators(monkeypatch, live_worker):
    """The DB layer fences terminal-accessible CLI/direct-import mutators too."""
    from hermes_cli import kanban_db as kb
    successor_run = _replace_run(monkeypatch, live_worker)
    signals = []
    with operator_env(monkeypatch):
        conn = kb.connect()
        try:
            archived = kb.create_task(conn, title="archived evidence", assignee="peer")
            assert kb.archive_task(conn, archived)
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET claim_lock = ?, worker_pid = ? WHERE id = ?",
                    (f"{socket.gethostname()}:999999", 999999, live_worker["source"]),
                )
        finally:
            conn.close()

    conn = kb.connect()
    try:
        before = _counts(conn)
        with pytest.raises(kb.SourceRunFenceError):
            kb.reclaim_task(
                conn,
                live_worker["source"],
                reason="stale reclaim",
                signal_fn=lambda pid, sig: signals.append((pid, sig)),
            )
        with pytest.raises(kb.SourceRunFenceError):
            kb.claim_task(conn, live_worker["dependent"])
        with pytest.raises(kb.SourceRunFenceError):
            kb.archive_task(conn, live_worker["foreign"])
        with pytest.raises(kb.SourceRunFenceError):
            kb.delete_archived_task(conn, archived)
        assert signals == []
        assert _counts(conn) == before
        assert kb.get_task(conn, archived).status == "archived"
        source = kb.get_task(conn, live_worker["source"])
        assert source.current_run_id == successor_run
        assert source.status == "running"
    finally:
        conn.close()


def test_live_run_can_attach_and_complete_end_to_end(live_worker):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt
    encoded = base64.b64encode(b"proof").decode()
    attached = json.loads(kt._handle_attach({
        "filename": "proof.txt", "content_base64": encoded,
    }))
    assert attached.get("ok") is True
    completed = json.loads(kt._handle_complete({"summary": "verified"}))
    assert completed.get("ok") is True
    conn = kb.connect()
    try:
        assert kb.get_task(conn, live_worker["source"]).status == "done"
        assert kb.get_task(conn, live_worker["dependent"]).status == "ready"
        assert len(kb.list_attachments(conn, live_worker["source"])) == 1
    finally:
        conn.close()



def test_cross_board_refusal_is_not_reported_as_terminal(monkeypatch, live_worker):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt
    with operator_env(monkeypatch):
        kb.create_board("other")
    payload = json.loads(kt._handle_comment({
        "board": "other",
        "task_id": live_worker["foreign"],
        "body": "cross-board write",
    }))
    assert payload.get("ok") is not True
    assert "absent from the selected board" in payload.get("error", "")
    assert "do not retry from this worker run" in payload.get("error", "")
    assert "worker run is over" not in payload.get("error", "")


def test_replaced_run_cannot_start_dispatch_tick(monkeypatch, live_worker):
    from hermes_cli import kanban_db as kb

    _replace_run(monkeypatch, live_worker)
    calls = []
    monkeypatch.setattr(kb, "reap_worker_zombies", lambda: calls.append("reap"))
    conn = kb.connect()
    try:
        before = _counts(conn)
        with pytest.raises(kb.SourceRunFenceError, match="dispatch_once"):
            kb.dispatch_once(conn, dry_run=True, spawn_fn=lambda *args: None)
        assert calls == []
        assert _counts(conn) == before
    finally:
        conn.close()


def test_replaced_run_cannot_recompute_dependency_release(monkeypatch, live_worker):
    from hermes_cli import kanban_db as kb

    _replace_run(monkeypatch, live_worker)
    conn = kb.connect()
    try:
        before = _counts(conn)
        with pytest.raises(kb.SourceRunFenceError, match="recompute_ready"):
            kb.recompute_ready(conn)
        assert _counts(conn) == before
        assert kb.get_task(conn, live_worker["dependent"]).status == "todo"
    finally:
        conn.close()


def test_replaced_run_cannot_attach_url_or_fetch(monkeypatch, live_worker):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    _replace_run(monkeypatch, live_worker)
    fetched = []
    monkeypatch.setattr(
        kt,
        "_download_url_with_cap",
        lambda *args: fetched.append(args) or (b"unexpected", "text/plain"),
    )
    result = json.loads(kt._handle_attach_url({
        "url": "https://example.com/proof.txt",
        "filename": "proof.txt",
    }))
    assert result.get("ok") is not True
    assert "source-run fence rejected" in result.get("error", "")
    assert fetched == []
    conn = kb.connect()
    try:
        assert kb.list_attachments(conn, live_worker["source"]) == []
        assert not kb.task_attachments_dir(live_worker["source"]).exists()
    finally:
        conn.close()


def test_deleted_source_task_refusal_says_do_not_retry(monkeypatch, live_worker):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    with operator_env(monkeypatch):
        conn = kb.connect()
        try:
            assert kb.delete_task(conn, live_worker["source"])
        finally:
            conn.close()
    result = json.loads(kt._handle_comment({
        "task_id": live_worker["foreign"],
        "body": "zombie write after source deletion",
    }))
    error = result.get("error", "")
    assert result.get("ok") is not True
    assert "do not retry from this worker run" in error
    assert "retry on the source task's board" not in error
