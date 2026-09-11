"""图片气泡定位与促发解码的单元测试。"""

from __future__ import annotations

import importlib

from test_agent_wechat_event import _install_astrbot_stubs

_install_astrbot_stubs()
x11 = importlib.import_module('src.agent_wechat_x11')


def _tree(*items):
    return {
        'role': 'frame',
        'name': 'Weixin',
        'children': [
            {
                'role': 'list',
                'name': 'Messages',
                'bounds': {'x': 423, 'y': 115, 'width': 704, 'height': 490},
                'children': list(items),
            }
        ],
    }


def _item(name, y, height=288):
    return {
        'role': 'list-item',
        'name': name,
        'bounds': {'x': 423, 'y': y, 'width': 704, 'height': height},
    }


def test_image_bubble_points_prefers_newest_item():
    tree = _tree(
        _item('Image\n', 116),
        _item('这是谁\n ', 404, 75),
        _item('Image\n', 500, 200),
    )

    points = x11._image_bubble_points(tree)

    # 最新的图片（y=500）排在前面，图片气泡内给出多个候选点。
    assert len(points) == 10
    first_item_points = points[:5]
    assert all(y == int(500 + 200 * x11._IMAGE_CLICK_Y_FRACTION) for _, y in first_item_points)
    assert len({x for x, _ in first_item_points}) == 5
    assert all(423 <= x <= 423 + 704 for x, _ in first_item_points)
    # 第二个图片气泡（y=116）排在后面。
    assert all(y == int(116 + 288 * x11._IMAGE_CLICK_Y_FRACTION) for _, y in points[5:])


def test_image_bubble_points_ignores_non_image_items():
    tree = _tree(_item('你好啊\n ', 116, 60))
    assert x11._image_bubble_points(tree) == []


def test_image_bubble_points_without_message_list():
    assert x11._image_bubble_points({'role': 'frame', 'children': []}) == []
