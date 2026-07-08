"""Discord approval/confirmation prompts use compact Markdown headings."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import plugins.platforms.discord.adapter as discord_adapter
from plugins.platforms.discord.adapter import DiscordAdapter


def _adapter_with_channel():
    channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=12345)))
    client = SimpleNamespace(get_channel=lambda _cid: channel)

    adapter = object.__new__(DiscordAdapter)
    adapter._client = client
    adapter._allowed_user_ids = set()
    adapter._allowed_role_ids = set()
    adapter._nonconversational_messages = SimpleNamespace(mark_many=lambda _ids: None)
    return adapter, channel


@pytest.mark.asyncio
async def test_slash_confirm_title_is_h3_and_body_is_unchanged():
    adapter, channel = _adapter_with_channel()
    body = (
        "Detalii: https://example.test/review/4\n\n"
        "Choose:\n"
        "• **Approve Once** — proceed this time only\n"
        "• **Cancel** — keep current conversation"
    )

    result = await adapter.send_slash_confirm(
        chat_id="42",
        title="Am invatat 4 tipare noi - le aplic?",
        message=body,
        session_key="sess-1",
        confirm_id="confirm-1",
    )

    assert result.success is True
    embed = channel.send.await_args.kwargs["embed"]
    assert embed.title is None
    assert embed.description.startswith("### Am invatat 4 tipare noi - le aplic?\n\n")
    assert body in embed.description
    assert not embed.description.startswith("# ")


@pytest.mark.asyncio
async def test_exec_approval_title_is_h3_without_changing_command_or_reason():
    adapter, channel = _adapter_with_channel()

    result = await adapter.send_exec_approval(
        chat_id="42",
        command="rm -rf /tmp/demo",
        session_key="sess-1",
        description="dangerous command",
    )

    assert result.success is True
    embed = channel.send.await_args.kwargs["embed"]
    assert embed.title is None
    assert embed.description == "### ⚠️ Command Approval Required\n\n```\nrm -rf /tmp/demo\n```"
    assert embed.fields == [{"name": "Reason", "value": "dangerous command", "inline": False}]


@pytest.mark.asyncio
async def test_update_prompt_title_is_h3_and_prompt_is_unchanged():
    adapter, channel = _adapter_with_channel()
    discord_adapter.discord.Color.gold = lambda: 7

    result = await adapter.send_update_prompt(
        chat_id="42",
        prompt="Continue update?",
        default="n",
        session_key="sess-1",
    )

    assert result.success is True
    embed = channel.send.await_args.kwargs["embed"]
    assert embed.title is None
    assert embed.description == "### ⚕ Update Needs Your Input\n\nContinue update? (default: n)"
