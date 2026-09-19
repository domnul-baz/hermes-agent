"""Behavioral coverage for Kanban's pytest live-DB guard (#295).

Every path called "production" here is a fresh fake root under ``tmp_path``;
the tests never resolve or open an operator's Kanban database.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

import hermes_state
from hermes_cli import kanban_db as kb


@pytest.fixture
def fake_production_root(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "simulated-live-kanban"
    monkeypatch.setattr(kb, "_KANBAN_DB_GUARD_EXTRA_DENY_ROOTS", (root,))
    return root


@pytest.mark.parametrize(
    "relative",
    [
        Path("kanban.db"),
        Path("kanban") / "boards" / "project-a" / "kanban.db",
    ],
)
def test_canonical_production_kanban_shapes_are_refused(
    fake_production_root, relative
):
    with pytest.raises(RuntimeError, match="live-system guard"):
        kb._ensure_test_kanban_isolation(fake_production_root / relative)


def test_guard_error_names_real_bypass_without_pytest_marker(fake_production_root):
    with pytest.raises(RuntimeError, match="live-system guard") as exc_info:
        kb._ensure_test_kanban_isolation(fake_production_root / "kanban.db")

    message = str(exc_info.value)
    assert f"{kb._KANBAN_DB_GUARD_BYPASS_ENV}=1" in message
    assert "@pytest.mark.live_system_guard_bypass" not in message


@pytest.mark.parametrize(
    "relative",
    [
        Path("scratch") / "kanban.db",
        Path("kanban") / "boards" / "project-a" / "scratch" / "kanban.db",
        Path("kanban") / "boards" / "project-a" / "other.db",
    ],
)
def test_unrelated_kanban_scratch_shapes_remain_writable(
    fake_production_root, relative
):
    path = fake_production_root / relative
    with kb.connect_closing(path) as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    assert path.exists()


def test_inherited_live_db_pin_is_refused_before_any_creation(
    fake_production_root, monkeypatch, tmp_path
):
    """Replay #295: temporary home plus inherited dispatcher pins."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermetic-home"))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(fake_production_root / "kanban.db"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "project-a")

    with pytest.raises(RuntimeError, match="live-system guard"):
        kb.connect()

    assert not fake_production_root.exists()


def test_explicit_db_path_is_refused_before_parent_creation(fake_production_root):
    path = fake_production_root / "kanban" / "boards" / "project-a" / "kanban.db"
    with pytest.raises(RuntimeError, match="live-system guard"):
        kb.connect(path)
    assert not fake_production_root.exists()


def test_sqlite_connect_backstop_refuses_bypassed_writable_open(fake_production_root):
    path = fake_production_root / "kanban.db"
    with pytest.raises(RuntimeError, match="live-system guard"):
        kb._sqlite_connect(path)
    assert not fake_production_root.exists()


def test_read_only_notify_count_intentionally_bypasses_writable_guard(
    fake_production_root,
):
    path = fake_production_root / "kanban.db"
    path.parent.mkdir()
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE kanban_notify_subs (task_id TEXT)")
    assert kb.count_notify_subs(path) == 0


def test_global_bypass_allows_a_canonical_fake_root(fake_production_root, monkeypatch):
    monkeypatch.setattr(kb, "_KANBAN_DB_GUARD_BYPASS", True)
    with kb.connect_closing(fake_production_root / "kanban.db") as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1


def test_env_bypass_allows_a_canonical_fake_root(fake_production_root, monkeypatch):
    monkeypatch.setenv(kb._KANBAN_DB_GUARD_BYPASS_ENV, "1")
    with kb.connect_closing(fake_production_root / "kanban.db") as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1


def test_non_test_context_keeps_normal_production_open_semantics(
    fake_production_root, monkeypatch
):
    monkeypatch.setattr(hermes_state, "_in_test_context", lambda: False)
    with kb.connect_closing(fake_production_root / "kanban.db") as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1


def test_init_db_refuses_before_creating_root_db_or_lock(fake_production_root):
    path = fake_production_root / "kanban.db"

    with pytest.raises(RuntimeError, match="live-system guard"):
        kb.init_db(path)

    assert not fake_production_root.exists()
    assert not path.exists()
    assert not path.with_name(path.name + ".init.lock").exists()


def test_repair_db_refuses_before_creating_root_db_or_lock(fake_production_root):
    path = fake_production_root / "kanban.db"

    with pytest.raises(RuntimeError, match="live-system guard"):
        kb.repair_db(path)

    assert not fake_production_root.exists()
    assert not path.exists()
    assert not path.with_name(path.name + ".init.lock").exists()


def _sanitized_child_env() -> dict[str, str]:
    """Keep platform runtime variables while dropping test/agent inheritance."""
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("HERMES_", "PYTEST_"))
    }


def test_out_of_tree_pytest_child_refuses_worker_pin_without_creating_fake_root(
    fake_production_root, tmp_path
):
    """Pytest's own current-test context activates the out-of-tree guard."""
    repo = Path(__file__).resolve().parents[2]
    child_test = tmp_path / "test_live_kanban_pin.py"
    child_test.write_text(
        "from hermes_cli.kanban_db import connect\n\n"
        "def test_inherited_live_pin_is_refused():\n"
        "    connect()\n",
        encoding="utf-8",
    )
    env = _sanitized_child_env()
    env.update({
        "PYTHONPATH": str(repo),
        "HERMES_KANBAN_DB": str(fake_production_root / "kanban.db"),
        "HERMES_KANBAN_BOARD": "project-a",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        kb._KANBAN_DB_GUARD_DENY_ROOTS_ENV: os.pathsep.join(
            (str(tmp_path / "unrelated-root"), str(fake_production_root))
        ),
    })
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--confcutdir",
            str(tmp_path),
            str(child_test),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0
    assert "live-system guard" in (result.stdout + result.stderr)
    assert not fake_production_root.exists()


def test_subprocess_descendant_marker_refuses_worker_pin(
    fake_production_root, tmp_path
):
    """The inherited isolation marker also covers non-pytest descendants."""
    repo = Path(__file__).resolve().parents[2]
    env = _sanitized_child_env()
    env.update({
        "PYTHONPATH": str(repo),
        "HERMES_TEST_ISOLATION": str(tmp_path / "child-isolation"),
        "HERMES_KANBAN_DB": str(fake_production_root / "kanban.db"),
        "HERMES_KANBAN_BOARD": "project-a",
        kb._KANBAN_DB_GUARD_DENY_ROOTS_ENV: str(fake_production_root),
    })
    result = subprocess.run(
        [sys.executable, "-c", "from hermes_cli.kanban_db import connect; connect()"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0
    assert "live-system guard" in result.stderr
    assert not fake_production_root.exists()
