from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest
from test_wechat_conversation import KEY, TwoChats

from lemonbot.connectors.wechat_agent import AgentWechatHTTP, LabPaused
from lemonbot.connectors.wechat_agent_conversation import load_targets
from lemonbot.connectors.wechat_agent_state import private_write
from lemonbot.connectors.wechat_agent_whitelist import (
    WechatWhitelist,
    read_whitelist,
    sync_whitelist,
)


def seed(mock: TwoChats, state: Path) -> None:
    private_write(
        state / "conversation-binding.json",
        json.dumps(
            {
                "account_ref": mock.targets()[0].binding.account_ref,
                "operator_confirmed": True,
                "targets": [
                    {"binding": asdict(t.binding), "name": t.name, "is_group": t.is_group}
                    for t in mock.targets()
                ],
            }
        ).encode(),
    )


async def sync(mock: TwoChats, state: Path, config: WechatWhitelist, *, reject=False):
    calls = []

    def metadata(request):
        assert request.url.path == "/bindings"
        calls.append(json.loads(request.content))
        return httpx.Response(
            503 if reject else 200,
            json={"accountVerified": True, "self_identity": mock.account, **calls[-1]},
        )

    anchor = mock.targets()[0].binding
    http = AgentWechatHTTP("synthetic", transport=httpx.MockTransport(mock.handle))
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:6175", transport=httpx.MockTransport(metadata)
    ) as bridge:
        try:
            result = await sync_whitelist(http, anchor, KEY, state, config, bridge)
        finally:
            await http.close()
    return result, calls


@pytest.mark.parametrize(
    "text",
    [
        "groups=[]\ncontacts=[]",
        'groups=["testing","testing"]',
        'groups=[" testing"]',
        'groups=["testing"]\ncontacts=["testing"]',
        'groups=["testing"]\nunknown=true',
        'version=2\ngroups=["testing"]',
        'groups="testing"',
    ],
)
def test_invalid_config_fails_with_redacted_code(tmp_path, text):
    path = tmp_path / "whitelist.toml"
    path.write_text(text)
    path.chmod(0o600)
    with pytest.raises(LabPaused, match=r"^InvalidWhitelistConfig$"):
        read_whitelist(path)


def test_unicode_names_and_private_config_permissions(tmp_path):
    path = tmp_path / "whitelist.toml"
    path.write_text('groups=["中文群"]\ncontacts=["张三"]\n')
    path.chmod(0o600)
    parsed = read_whitelist(path)
    assert parsed.groups == ("中文群",) and parsed.contacts == ("张三",)
    path.chmod(0o666)
    with pytest.raises(LabPaused, match="InvalidWhitelistConfig"):
        read_whitelist(path)
    path.chmod(0o600)
    link = tmp_path / "link.toml"
    link.symlink_to(path)
    with pytest.raises(LabPaused, match="InvalidWhitelistConfig"):
        read_whitelist(link)


@pytest.mark.asyncio
async def test_existing_bindings_are_reused_and_removed_targets_are_pruned(tmp_path):
    mock = TwoChats()
    seed(mock, tmp_path)
    private_write(
        tmp_path / "targets-paused.json",
        json.dumps({mock.targets()[1].binding.chat_ref: "AnchorLost"}).encode(),
    )
    targets, calls = await sync(mock, tmp_path, WechatWhitelist(groups=("testing",)))
    assert len(targets) == 1 and targets[0].binding.chat_id == mock.group
    assert mock.opens == 0
    assert calls == [{"account": mock.account, "chat_ids": [mock.group]}]
    assert json.loads((tmp_path / "targets-paused.json").read_text()) == {}
    assert load_targets(tmp_path / "conversation-binding.json", targets[0].binding.account_ref, KEY)
    assert (tmp_path / "metadata-binding.json").stat().st_mode & 0o077 == 0


@pytest.mark.asyncio
async def test_new_contact_is_navigated_and_bound_with_durable_identity(tmp_path):
    mock = TwoChats()
    seed(mock, tmp_path)
    mock.names["second-peer"] = "Second contact"
    mock.messages["second-peer"] = []
    targets, calls = await sync(
        mock, tmp_path, WechatWhitelist(groups=("testing",), contacts=("Second contact",))
    )
    assert len(targets) == 2 and targets[1].binding.chat_id == "second-peer"
    assert mock.opens == 1
    assert calls[0]["chat_ids"] == [mock.group, "second-peer"]
    assert all(t.binding.chat_id != mock.peer for t in targets)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing", "duplicate", "wrong_kind", "navigation", "bridge"])
async def test_failure_preserves_previous_binding(tmp_path, case):
    mock = TwoChats()
    seed(mock, tmp_path)
    previous = (tmp_path / "conversation-binding.json").read_bytes()
    config = WechatWhitelist(groups=("testing",), contacts=("contact",))
    if case == "missing":
        config = WechatWhitelist(contacts=("missing",))
    elif case == "duplicate":
        mock.names["other-peer"] = "contact"
    elif case == "wrong_kind":
        config = WechatWhitelist(groups=("contact",))
    elif case == "navigation":
        mock.names["new-peer"] = "new contact"
        mock.messages["new-peer"] = []
        mock.navigation_failure = True
        config = WechatWhitelist(contacts=("new contact",))
    with pytest.raises(LabPaused):
        await sync(mock, tmp_path, config, reject=case == "bridge")
    assert (tmp_path / "conversation-binding.json").read_bytes() == previous
    assert not (tmp_path / "metadata-binding.json").exists()


@pytest.mark.asyncio
async def test_reused_name_cannot_silently_switch_identity(tmp_path):
    mock = TwoChats()
    seed(mock, tmp_path)
    del mock.names[mock.peer]
    mock.names["imposter"] = "contact"
    with pytest.raises(LabPaused, match="WhitelistTargetIdentityChanged"):
        await sync(mock, tmp_path, WechatWhitelist(contacts=("contact",)))
    assert mock.opens == 0


@pytest.mark.asyncio
async def test_account_change_does_not_update_bindings(tmp_path):
    mock = TwoChats()
    seed(mock, tmp_path)
    previous = (tmp_path / "conversation-binding.json").read_bytes()
    original = mock.handle

    def handle(request):
        if request.url.path == "/api/status/auth":
            return httpx.Response(200, json={"status": "logged_in", "loggedInUser": "other"})
        return original(request)

    mock.handle = handle
    with pytest.raises(LabPaused, match="AccountChanged"):
        await sync(mock, tmp_path, WechatWhitelist(groups=("testing",)))
    assert (tmp_path / "conversation-binding.json").read_bytes() == previous
    assert mock.opens == 0


@pytest.mark.asyncio
async def test_multiple_contacts_and_private_only_are_supported(tmp_path):
    mock = TwoChats()
    mock.names["second-peer"] = "Second contact"
    mock.messages["second-peer"] = []
    targets, _calls = await sync(
        mock, tmp_path, WechatWhitelist(contacts=("contact", "Second contact"))
    )
    assert len(targets) == 2 and all(not t.is_group for t in targets)


@pytest.mark.asyncio
async def test_multiple_named_groups_keep_distinct_identity_bindings(tmp_path):
    mock = TwoChats()
    seed(mock, tmp_path)
    mock.names["second@chatroom"] = "Second group"
    mock.messages["second@chatroom"] = []
    original = mock.handle

    def handle(request):
        response = original(request)
        if request.url.path == "/api/chats":
            rows = response.json()
            for row in rows:
                row["isGroup"] = row["id"].endswith("@chatroom")
            return httpx.Response(200, json=rows)
        if request.url.path.startswith("/api/chats/") and request.method == "GET":
            row = response.json()
            row["isGroup"] = row["id"].endswith("@chatroom")
            return httpx.Response(200, json=row)
        return response

    mock.handle = handle
    targets, _calls = await sync(
        mock, tmp_path, WechatWhitelist(groups=("testing", "Second group"), contacts=("contact",))
    )
    assert len(targets) == 3 and sum(t.is_group for t in targets) == 2
    assert len({t.binding.chat_ref for t in targets}) == 3
    assert mock.opens == 1


@pytest.mark.asyncio
async def test_kept_target_pause_survives_configuration_sync(tmp_path):
    mock = TwoChats()
    seed(mock, tmp_path)
    reason = {mock.targets()[0].binding.chat_ref: "AnchorLost"}
    private_write(tmp_path / "targets-paused.json", json.dumps(reason).encode())
    await sync(mock, tmp_path, WechatWhitelist(groups=("testing",)))
    assert json.loads((tmp_path / "targets-paused.json").read_text()) == reason


@pytest.mark.asyncio
@pytest.mark.parametrize("archive_enabled", [False, True])
async def test_runtime_startup_synchronizes_allowlist_and_keeps_persistent_pause(
    tmp_path, monkeypatch, archive_enabled
):
    from lemonbot.config.settings import AppSettings
    from lemonbot.connectors.wechat_agent import CHANNEL
    from lemonbot.connectors.wechat_agent_state import prepare_state, save_binding
    from lemonbot.orchestration import FakeModelBackend
    from lemonbot.runtime import LemonbotRuntime

    mock = TwoChats()
    state = tmp_path / "state"
    key = prepare_state(state)
    assert key != KEY
    # The fixture's identity references must use the actual durable comparison key.
    monkeypatch.setattr("test_wechat_conversation.KEY", key)
    save_binding(state / "binding.json", mock.targets()[0].binding)
    private_write(state / "token", b"synthetic")
    mock.names["second-peer"] = "Second contact"
    mock.messages["second-peer"] = []
    config = tmp_path / "whitelist.toml"
    config.write_text('groups=["testing"]\ncontacts=["contact","Second contact"]\n')
    config.chmod(0o600)

    original_client = httpx.AsyncClient

    def metadata(request):
        if request.url.path == "/bindings":
            return httpx.Response(
                200,
                json={
                    "accountVerified": True,
                    "self_identity": mock.account,
                    **json.loads(request.content),
                },
            )
        return mock.metadata(request)

    def client(*args, **kwargs):
        if kwargs.get("base_url") == "http://127.0.0.1:6175":
            kwargs["transport"] = httpx.MockTransport(metadata)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr("lemonbot.runtime.AgentWechatHTTP", lambda _token: mock.http())

    async def model(_runtime):
        return FakeModelBackend()

    monkeypatch.setattr(LemonbotRuntime, "_build_model", model)
    raw = AppSettings().model_dump(mode="python")
    raw["profile"] = "lab"
    raw["context_memory"]["archive_enabled"] = archive_enabled
    raw["runtime"] = {"connector": "wechat_agent", "data_root": str(tmp_path / "runtime")}
    raw["wechat_agent"] = {
        "enabled": True,
        "mode": "conversation",
        "state_dir": str(state),
        "token_file": str(state / "token"),
        "whitelist_file": str(config),
    }
    raw["models"].update(
        provider="deepseek",
        flash_model="deepseek-flash",
        pro_model="deepseek-flash",
        max_input_tokens=8192,
        max_output_tokens=512,
    )
    raw["models"]["budget"].update(
        enabled=True,
        daily_limit_cny="5",
        monthly_limit_cny="100",
        flash_input_cny_per_million="2",
        flash_output_cny_per_million="8",
        pro_input_cny_per_million="2",
        pro_output_cny_per_million="8",
    )
    runtime = LemonbotRuntime(AppSettings.model_validate(raw))
    try:
        await runtime.initialize()
        assert all(
            (child.archive is not None) == archive_enabled
            for child in runtime.connector.transport.children.values()
        )
        assert runtime.pipeline.memory_context is None
        old_refs = runtime.connector.chat_refs
        assert len(old_refs) == 3
        assert all([await runtime.repository.is_allowlisted(CHANNEL, ref) for ref in old_refs])
        await runtime.connector.pause("ManualPause")
    finally:
        await runtime.close()
    config.write_text('contacts=["Second contact"]\n')
    runtime = LemonbotRuntime(AppSettings.model_validate(raw))
    try:
        await runtime.initialize()
        assert runtime.connector.pause_reason == "ManualPause"
        assert len(runtime.connector.chat_refs) == 1
        for ref in old_refs:
            assert await runtime.repository.is_allowlisted(CHANNEL, ref) == (
                ref in runtime.connector.chat_refs
            )
    finally:
        await runtime.close()
