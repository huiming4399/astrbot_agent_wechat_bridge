"""发送链路的路由测试：默认直接走 X11，兜底模式先试接口。"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_agent_wechat_event import _load_event_module  # noqa: E402

event_module = _load_event_module()
AgentWeChatMessageEvent = event_module.AgentWeChatMessageEvent


class _FakeClient:
    def __init__(self, *, x11_sender=None, mode="always", api_results=None):
        self.x11_sender = x11_sender
        self.x11_send_mode = mode
        self.api_results = list(api_results or [{"success": True}])
        self.api_calls = 0
        self.open_chat_calls = 0

    def send_message(self, payload, timeout=None):
        self.api_calls += 1
        return self.api_results[min(self.api_calls - 1, len(self.api_results) - 1)]

    def open_chat(self, chat_id, clear_unreads=True):
        self.open_chat_calls += 1
        return {"ok": True}


class _FakeX11Sender:
    def __init__(self, fail_times=0):
        self.sent = []
        self.fail_times = fail_times

    def send_text(self, client, chat_id, text):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("xdotool boom")
        self.sent.append((chat_id, text))


def _run_send(client, chain):
    return asyncio.run(
        AgentWeChatMessageEvent.send_message_chain(client, "wxid_x", chain)
    )


def _plain_chain(text="hello"):
    return event_module.MessageChain([event_module.Plain(text=text)])


@pytest.fixture(autouse=True)
def _reset_api_breaker():
    AgentWeChatMessageEvent._api_failure_streak = 0
    AgentWeChatMessageEvent._api_failure_at = 0.0
    yield
    AgentWeChatMessageEvent._api_failure_streak = 0
    AgentWeChatMessageEvent._api_failure_at = 0.0


def test_always_mode_skips_api_entirely() -> None:
    sender = _FakeX11Sender()
    client = _FakeClient(x11_sender=sender, mode="always")

    _run_send(client, _plain_chain("走 X11"))

    assert sender.sent == [("wxid_x", "走 X11")]
    assert client.api_calls == 0


def test_fallback_mode_uses_api_when_it_succeeds() -> None:
    sender = _FakeX11Sender()
    client = _FakeClient(x11_sender=sender, mode="fallback")

    _run_send(client, _plain_chain("走接口"))

    assert client.api_calls == 1
    assert sender.sent == []


def test_fallback_mode_switches_to_x11_after_recoverable_error() -> None:
    sender = _FakeX11Sender()
    client = _FakeClient(
        x11_sender=sender,
        mode="fallback",
        api_results=[{"success": False, "error": "No action selected"}],
    )

    _run_send(client, _plain_chain("兜底"))

    assert client.api_calls == event_module.SEND_RECOVERY_RETRY_ATTEMPTS
    assert sender.sent == [("wxid_x", "兜底")]


def test_circuit_breaker_skips_api_after_failure() -> None:
    sender = _FakeX11Sender()
    client = _FakeClient(
        x11_sender=sender,
        mode="fallback",
        api_results=[{"success": False, "error": "No action selected"}],
    )

    _run_send(client, _plain_chain("第一条"))
    calls_after_first = client.api_calls

    _run_send(client, _plain_chain("第二条"))

    assert calls_after_first == event_module.SEND_RECOVERY_RETRY_ATTEMPTS
    assert client.api_calls == calls_after_first
    assert sender.sent == [("wxid_x", "第一条"), ("wxid_x", "第二条")]


def test_fallback_mode_without_x11_sender_raises() -> None:
    client = _FakeClient(
        x11_sender=None,
        mode="fallback",
        api_results=[{"success": False, "error": "No action selected"}],
    )

    with pytest.raises(RuntimeError):
        _run_send(client, _plain_chain("无兜底"))


def test_x11_failure_raises_runtime_error() -> None:
    sender = _FakeX11Sender(fail_times=1)
    client = _FakeClient(x11_sender=sender, mode="always")

    with pytest.raises(RuntimeError):
        _run_send(client, _plain_chain("失败"))
