"""Regression tests for operator-facing cron failure classification."""

from cron.scheduler import _summarize_cron_failure_for_delivery


def test_no_agent_subprocess_timeout_is_not_labeled_provider_failure():
    message = _summarize_cron_failure_for_delivery(
        {"name": "repo-watchdog", "no_agent": True},
        "subprocess.TimeoutExpired: git fetch timed out after 45 seconds",
    )

    assert "script timeout" in message
    assert "No model provider or fallback chain was involved" in message
    assert "provider timeout" not in message
    assert "Fallback chain was exhausted" not in message


def test_agent_timeout_keeps_provider_failure_classification():
    message = _summarize_cron_failure_for_delivery(
        {"name": "research", "no_agent": False},
        "httpx.ReadTimeout: provider timed out",
    )

    assert "provider timeout" in message
    assert "Fallback chain was exhausted or unavailable" in message
