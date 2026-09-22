"""Regression coverage for incident #309 cross-board completion references."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def cross_board_home(tmp_path, monkeypatch):
    """Give every audit test an isolated Hermes and Kanban root."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _prose_evidence(conn, text):
    return kb._scan_prose_for_phantom_references(conn, text)


def _reference_event(conn, task_id):
    return [
        event for event in kb.list_events(conn, task_id)
        if event.kind == "suspected_hallucinated_references"
    ]


def test_bare_ids_are_current_board_only(cross_board_home):
    conn = kb.connect()
    try:
        current = kb.create_task(conn, title="current")
        kb.create_board("other")
        other = kb.connect(board="other")
        try:
            foreign = kb.create_task(other, title="foreign")
        finally:
            other.close()

        assert _prose_evidence(conn, current) == []
        assert _prose_evidence(conn, foreign) == [{
            "reference": foreign, "task_id": foreign, "board": None,
            "reference_type": "bare", "status": "missing_task",
        }]
    finally:
        conn.close()


def test_qualified_references_are_strict_read_only_and_complete_cleanly(cross_board_home, monkeypatch):
    conn = kb.connect()
    try:
        kb.create_board("apr")
        apr = kb.connect(board="apr")
        try:
            existing = kb.create_task(apr, title="apr task")
        finally:
            apr.close()
        missing = "t_deadbeef"
        missing_board_path = kb.kanban_db_path(board="missing")

        assert _prose_evidence(conn, f"apr:{existing}") == []
        assert _prose_evidence(conn, f"(`apr:{existing}`)") == []
        assert not missing_board_path.exists()
        assert _prose_evidence(conn, f"missing:{missing}") == [{
            "reference": f"missing:{missing}", "task_id": missing,
            "board": "missing", "reference_type": "qualified",
            "status": "missing_board",
        }]
        assert not missing_board_path.exists()
        assert not missing_board_path.parent.exists()

        assert _prose_evidence(conn, f"apr:{missing}") == [{
            "reference": f"apr:{missing}", "task_id": missing, "board": "apr",
            "reference_type": "qualified", "status": "missing_task",
        }]
        kb.write_board_metadata("broken")
        assert _prose_evidence(conn, f"broken:{missing}") == [{
            "reference": f"broken:{missing}", "task_id": missing,
            "board": "broken", "reference_type": "qualified",
            "status": "lookup_error",
        }]

        monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path()))
        assert _prose_evidence(conn, f"apr:{existing}") == []

        task_id = kb.create_task(conn, title="completion target")
        assert kb.complete_task(conn, task_id, summary=f"apr:{existing}")
        assert _reference_event(conn, task_id) == []
    finally:
        conn.close()


def test_qualified_references_share_one_read_only_connection_per_board(cross_board_home, monkeypatch):
    conn = kb.connect()
    try:
        kb.create_board("apr")
        apr = kb.connect(board="apr")
        try:
            existing = kb.create_task(apr, title="apr task")
        finally:
            apr.close()

        apr_path = kb.kanban_db_path(board="apr").resolve()
        original_connect = kb.sqlite3.connect
        connections = []

        def spy_connect(database, *args, **kwargs):
            if database == apr_path.as_uri() + "?mode=ro" and kwargs.get("uri"):
                connections.append(database)
            return original_connect(database, *args, **kwargs)

        monkeypatch.setattr(kb.sqlite3, "connect", spy_connect)
        evidence = _prose_evidence(
            conn, f"apr:{existing} apr:t_deadbeef apr:t_cafebabe"
        )

        assert connections == [apr_path.as_uri() + "?mode=ro"]
        assert [item["reference"] for item in evidence] == [
            "apr:t_deadbeef", "apr:t_cafebabe",
        ]
    finally:
        conn.close()


@pytest.mark.parametrize("reference", [
    "APR:t_deadbeef", ":t_deadbeef", "foo::t_deadbeef", "../apr:t_deadbeef",
])
def test_malformed_qualifiers_emit_exactly_one_record_without_bare_fallback(cross_board_home, reference):
    conn = kb.connect()
    try:
        evidence = _prose_evidence(conn, reference)
        assert evidence == [{
            "reference": reference, "task_id": "t_deadbeef", "board": None,
            "reference_type": "malformed", "status": "malformed_reference",
        }]
    finally:
        conn.close()


def test_evidence_order_dedup_and_caps(cross_board_home, monkeypatch):
    conn = kb.connect()
    try:
        first = "t_deadbeef"
        second = "t_cafebabe"
        text = f"{first}, apr:{second}, {first}, missing:{second}"
        evidence = _prose_evidence(conn, text)
        assert [item["reference"] for item in evidence] == [
            first, f"apr:{second}", f"missing:{second}",
        ]

        monkeypatch.setattr(kb, "_MAX_PROSE_TASK_REFERENCES", 2)
        capped = _prose_evidence(conn, f"{first} {second} t_01234567")
        assert [item["status"] for item in capped] == [
            "missing_task", "missing_task", "limit_exceeded",
        ]

        monkeypatch.setattr(kb, "_MAX_PROSE_QUALIFIED_BOARDS", 1)
        boards_capped = _prose_evidence(conn, f"one:{first} two:{second}")
        assert [item["status"] for item in boards_capped] == [
            "missing_board", "limit_exceeded",
        ]
    finally:
        conn.close()


def test_completion_event_filters_only_verified_bare_reference(cross_board_home):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="parent", assignee="worker")
        verified = kb.create_task(conn, title="verified", created_by=task_id)
        kb.create_board("apr")
        assert kb.complete_task(
            conn, task_id,
            summary=f"{verified} apr:{verified}",
            created_cards=[verified],
        )
        events = _reference_event(conn, task_id)
        assert len(events) == 1
        event = events[0]
        assert event.payload["phantom_refs"] == [f"apr:{verified}"]
        assert event.payload["reference_evidence"] == [{
            "reference": f"apr:{verified}", "task_id": verified, "board": "apr",
            "reference_type": "qualified", "status": "missing_task",
        }]
        assert event.payload["source"] == "completion_summary"
    finally:
        conn.close()
