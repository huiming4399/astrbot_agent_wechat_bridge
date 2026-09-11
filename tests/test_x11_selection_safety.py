import importlib
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_agent_wechat_event import _install_astrbot_stubs

_install_astrbot_stubs()
x11 = importlib.import_module('src.agent_wechat_x11')


def setup_sender(chats, rows):
    client = SimpleNamespace(
        get_chat=lambda chat_id: next(c for c in chats if c['id'] == chat_id),
        list_chats=lambda **kwargs: chats,
    )
    sender = x11.X11Sender()
    sender._a11y = Mock(return_value={'role': 'list', 'name': 'Chats', 'children': rows})
    sender._click = Mock()
    return sender, client


def row(name, selected=False):
    return {'role': 'list-item', 'name': name, 'states': ['SELECTED'] if selected else [],
            'bounds': {'x': 0, 'y': 0, 'width': 100, 'height': 60}}


def test_raw_group_id_never_matches_message_content():
    sender, client = setup_sender([{'id': '123@chatroom', 'name': '123@chatroom'}], [row('Other 好的', True)])
    with pytest.raises(x11.X11SendError):
        sender._ensure_chat_open(client, '123@chatroom')
    sender._click.assert_not_called()


def test_same_display_name_is_rejected():
    sender, client = setup_sender([{'id': 'a', 'name': '同名'}, {'id': 'b', 'name': '同名'}], [row('同名 hi', True)])
    with pytest.raises(x11.X11SendError):
        sender._ensure_chat_open(client, 'a')


def test_selection_is_checked_again_after_external_switch():
    sender, client = setup_sender([{'id': 'a', 'name': 'Alice'}, {'id': 'b', 'name': 'Bob'}], [row('Alice hi', True), row('Bob hello')])
    sender._ensure_chat_open(client, 'a')
    sender._a11y.return_value['children'] = [row('Alice hi'), row('Bob hello', True)]
    with pytest.raises(x11.X11SendError):
        sender._ensure_chat_open(client, 'a', allow_switch=False)
    sender._click.assert_not_called()


def test_duplicate_visible_rows_are_rejected():
    sender, client = setup_sender([{'id': 'a', 'name': 'Alice'}], [row('Alice hi', True), row('Alice other')])
    with pytest.raises(x11.X11SendError):
        sender._ensure_chat_open(client, 'a')


def test_prefix_collision_does_not_select_longer_name():
    sender, client = setup_sender([{'id': 'a', 'name': 'Alice'}, {'id': 'b', 'name': 'Alice Work'}], [row('Alice Work hi', True)])
    with pytest.raises(x11.X11SendError):
        sender._ensure_chat_open(client, 'a')


def test_shared_window_serializes_different_senders():
    active = 0
    peak = 0
    start = threading.Barrier(2)

    @x11.serialized_send
    def operation(sender):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        time.sleep(0.03)
        active -= 1

    def run():
        start.wait()
        operation(x11.X11Sender())

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert peak == 1
