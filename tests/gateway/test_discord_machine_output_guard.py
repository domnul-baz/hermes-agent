"""Behavior contract for the final Discord machine-output safety net."""

from plugins.platforms.discord.adapter import _humanize_raw_discord_output


def test_docs_sync_success_is_humanized():
    raw = '{"mode":"sync","ok":true,"builders":[{"name":"wiki-index","ok":true}]}'

    delivered = _humanize_raw_discord_output(raw)

    assert delivered.startswith("✅ Operațiunea s-a încheiat corect.")
    assert '"builders"' not in delivered
    assert '"mode"' not in delivered


def test_machine_error_remains_humanly_signaled():
    raw = '{"mode":"sync","ok":false,"error":"builder failed","builders":[]}'

    delivered = _humanize_raw_discord_output(raw)

    assert delivered.startswith("⚠️ Operațiunea nu s-a încheiat corect")
    assert "necesită verificare" in delivered
    assert "builder failed" not in delivered


def test_normal_human_conclusion_is_unchanged():
    human = "Am sincronizat documentația. Echipa vede acum informațiile actualizate."

    assert _humanize_raw_discord_output(human) == human


def test_raw_error_log_is_humanized():
    raw = "stderr: Traceback (most recent call last):\n  File '/tmp/job.py', line 1"

    delivered = _humanize_raw_discord_output(raw)

    assert delivered.startswith("⚠️ Operațiunea nu s-a încheiat corect")
    assert "Traceback" not in delivered
