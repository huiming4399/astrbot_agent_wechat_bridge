"""通过容器内 X11 自动化发送微信消息。

agent-wechat 的动作规划层在部分微信版本下会持续返回 ``No action selected``
（上游 issue #169 / #170 / #171 / #173），导致 ``/api/messages/send``
完全不可用。本模块绕过规划层，直接使用容器内的 ``xdotool`` / ``xclip``
操作微信窗口，作为文本消息发送链路的兜底实现。
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import time
import threading
from functools import wraps
from typing import Any

from astrbot.api import logger

X11_LOG_PREFIX = "[agent_wechat][x11]"

DEFAULT_CONTAINER = "agent-wechat"
DEFAULT_DISPLAY = ":99"
CONTAINER_TEXT_PATH = "/tmp/astrbot_outbound.txt"
CONTAINER_IMAGE_PATH = "/tmp/astrbot_outbound_image"

STEP_TIMEOUT_SECONDS = 30.0
A11Y_RETRY_ATTEMPTS = 12
A11Y_RETRY_INTERVAL_SECONDS = 0.12
STATE_WAIT_ATTEMPTS = 25
IMAGE_DECODE_WAIT_SECONDS = 1.3
_SEND_LOCK = threading.RLock()


def serialized_send(method):
    @wraps(method)
    def wrapped(*args, **kwargs):
        with _SEND_LOCK:
            return method(*args, **kwargs)
    return wrapped


class X11SendError(RuntimeError):
    """X11 兜底发送失败。"""


# 图片气泡里真正图片的位置不固定（左侧带头像、右侧自己发送、不同宽高），
# 因此在气泡内取多个候选点逐个尝试，点空位置不会产生副作用。
_IMAGE_CLICK_X_FRACTIONS = (0.22, 0.34, 0.46, 0.62, 0.78)
_IMAGE_CLICK_Y_FRACTION = 0.55
_IMAGE_ITEM_NAME_PREFIXES = ("image", "图片", "[photo]")


def _image_bubble_points(tree: Any) -> list[tuple[int, int]]:
    """返回消息列表里图片气泡的候选点击坐标。

    WeChat 只把收到的图片存成加密的 ``.dat``，只有真正渲染过（例如打开图片
    查看器）才会写出未加密的临时副本。这里定位可见的图片气泡，供调用方逐个
    点开以触发解码。坐标按“越新的消息越靠前”排序，同一气泡内给出多个候选点。
    """
    messages = None
    for node in _iter_nodes(tree):
        if node.get("role") == "list" and str(node.get("name") or "").strip() == "Messages":
            messages = node
            break
    if messages is None:
        return []

    items: list[tuple[float, float, float, float]] = []
    for node in _iter_nodes(messages):
        if node.get("role") != "list-item":
            continue
        name = str(node.get("name") or "").strip().lower()
        if not name or not name.startswith(_IMAGE_ITEM_NAME_PREFIXES):
            continue
        bounds = node.get("bounds")
        if not isinstance(bounds, dict):
            continue
        try:
            x = float(bounds.get("x", 0))
            y = float(bounds.get("y", 0))
            width = float(bounds.get("width", 0))
            height = float(bounds.get("height", 0))
        except (TypeError, ValueError):
            continue
        if width <= 0 or height <= 0:
            continue
        items.append((x, y, width, height))

    # 最新的消息在列表最下方，优先尝试它。
    items.sort(key=lambda item: item[1], reverse=True)

    points: list[tuple[int, int]] = []
    for x, y, width, height in items:
        click_y = y + height * _IMAGE_CLICK_Y_FRACTION
        for fraction in _IMAGE_CLICK_X_FRACTIONS:
            points.append((int(x + width * fraction), int(click_y)))
    return points


def _iter_nodes(node: Any):
    """深度优先遍历无障碍树。"""
    stack = [node]
    while stack:
        current = stack.pop()
        if not isinstance(current, dict):
            continue
        yield current
        children = current.get("children")
        if isinstance(children, list):
            stack.extend(children)


def _states(node: dict[str, Any]) -> list[str]:
    value = node.get("states")
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _has_state(node: dict[str, Any], name: str) -> bool:
    return name in _states(node)


def _center(bounds: Any) -> tuple[int, int] | None:
    """把无障碍节点的 bounds 换算成中心点坐标。"""
    if not isinstance(bounds, dict):
        return None
    try:
        x = float(bounds.get("x", 0))
        y = float(bounds.get("y", 0))
        width = float(bounds.get("width", 0))
        height = float(bounds.get("height", 0))
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return int(round(x + width / 2)), int(round(y + height / 2))


def find_chat_list_items(tree: dict[str, Any]) -> list[dict[str, Any]]:
    """取出左侧「Chats」列表里的会话行。"""
    items: list[dict[str, Any]] = []
    for node in _iter_nodes(tree):
        if str(node.get("role") or "") != "list":
            continue
        if str(node.get("name") or "") != "Chats":
            continue
        for child in node.get("children") or []:
            if isinstance(child, dict) and str(child.get("role") or "") == "list-item":
                items.append(child)
    return items


def match_chat_item(
    items: list[dict[str, Any]], display_name: str
) -> dict[str, Any] | None:
    """按显示名称匹配会话行。

    会话行的无障碍名称形如 ``"晦鸣 你好喵 18:11"``，即「名称 + 最近消息 + 时间」，
    因此优先做整名匹配，再退化为前缀匹配。
    """
    target = display_name.strip()
    if not target:
        return None
    for item in items:
        if str(item.get("name") or "").strip() == target:
            return item
    for item in items:
        name = str(item.get("name") or "").strip()
        if name.startswith(f"{target} "):
            return item
    return None


def find_composer(tree: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """定位当前会话的输入框与发送按钮。

    微信的无障碍树里，输入框（``text`` + ``EDITABLE``）与发送按钮
    （``push-button`` 名称为 ``Send(S)``）是同一父节点下的兄弟节点，
    这里按同样的约束查找，避免匹配到搜索框。
    """
    for node in _iter_nodes(tree):
        children = node.get("children")
        if not isinstance(children, list):
            continue
        send_btn: dict[str, Any] | None = None
        edit_candidates: list[dict[str, Any]] = []
        for child in children:
            if not isinstance(child, dict):
                continue
            role = str(child.get("role") or "")
            if role == "push-button" and str(child.get("name") or "") == "Send(S)":
                send_btn = child
            elif role == "text" and _has_state(child, "EDITABLE"):
                edit_candidates.append(child)
        if send_btn is None or not edit_candidates:
            continue
        focused = [c for c in edit_candidates if _has_state(c, "FOCUSED")]
        return (focused[0] if focused else edit_candidates[0]), send_btn
    return None


class X11Sender:
    """通过 ``docker exec`` 在容器内驱动 xdotool / xclip 发送文本消息。"""

    def __init__(
        self,
        container: str = DEFAULT_CONTAINER,
        display: str = DEFAULT_DISPLAY,
    ) -> None:
        self.container = (container or DEFAULT_CONTAINER).strip()
        self.display = (display or DEFAULT_DISPLAY).strip()

    # ------------------------------------------------------------------ 底层

    def _exec_script(self, script: str, *, timeout: float = STEP_TIMEOUT_SECONDS) -> str:
        """把脚本通过 stdin 交给容器内的 ``sh`` 执行，避免任何转义问题。"""
        command = [
            "sg",
            "docker",
            "-c",
            f"docker exec -i {shlex.quote(self.container)} sh -s",
        ]
        try:
            result = subprocess.run(
                command,
                input=script,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise X11SendError("未找到 sg/docker 命令，无法执行容器内操作") from exc
        except subprocess.TimeoutExpired as exc:
            raise X11SendError(f"容器内操作超时（{timeout:.0f}s）") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise X11SendError(
                f"容器内操作失败（exit={result.returncode}）: {detail[:300]}"
            )
        return result.stdout

    def _click(self, x: int, y: int) -> None:
        self._exec_script(
            f"export DISPLAY={shlex.quote(self.display)}\n"
            f"xdotool mousemove {int(x)} {int(y)} click 1\n"
        )

    def _copy_text_into_container(self, host_path: str) -> None:
        command = [
            "sg",
            "docker",
            "-c",
            f"docker cp {shlex.quote(host_path)} "
            f"{shlex.quote(self.container)}:{CONTAINER_TEXT_PATH}",
        ]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=STEP_TIMEOUT_SECONDS
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise X11SendError(f"拷贝待发送文本失败: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise X11SendError(f"拷贝待发送文本失败: {detail[:200]}")

    def _copy_binary_into_container(self, host_path: str, container_path: str) -> None:
        command = ["sg", "docker", "-c", f"docker cp {shlex.quote(host_path)} "
                   f"{shlex.quote(self.container)}:{shlex.quote(container_path)}"]
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=STEP_TIMEOUT_SECONDS)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise X11SendError(f"拷贝待发送图片失败: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise X11SendError(f"拷贝待发送图片失败: {detail[:200]}")

    def _a11y(self, client: Any) -> dict[str, Any]:
        try:
            payload = client.debug_a11y()
        except Exception as exc:  # noqa: BLE001 - 需要把底层异常统一成发送错误
            raise X11SendError(f"读取无障碍树失败: {exc}") from exc
        tree = payload.get("tree") if isinstance(payload, dict) else None
        if not isinstance(tree, dict):
            raise X11SendError("无障碍树为空，微信窗口可能尚未就绪")
        return tree

    def _composer_state(
        self, client: Any
    ) -> tuple[tuple[dict[str, Any], dict[str, Any]] | None, bool | None]:
        """返回 (输入框/发送按钮节点对, 发送按钮是否禁用)。"""
        pair = find_composer(self._a11y(client))
        if pair is None:
            return None, None
        return pair, _has_state(pair[1], "DISABLED")

    def _wait_send_button(
        self, client: Any, *, disabled: bool
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """轮询发送按钮状态，命中时连同节点坐标一起返回，省掉一次无障碍树请求。"""
        for _ in range(STATE_WAIT_ATTEMPTS):
            pair, state = self._composer_state(client)
            if state is disabled and pair is not None:
                return pair
            time.sleep(A11Y_RETRY_INTERVAL_SECONDS)
        return None

    # ------------------------------------------------------------------ 流程

    def _resolve_display_name(self, client: Any, chat_id: str) -> str | None:
        try:
            chat = client.get_chat(chat_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"{X11_LOG_PREFIX} get_chat 失败 chat={chat_id}: {exc}")
            chat = None
        if isinstance(chat, dict):
            name = str(chat.get("name") or "").strip()
            if name:
                return name
        try:
            for item in client.list_chats(limit=200):
                if str(item.get("id") or "") == chat_id or str(
                    item.get("username") or ""
                ) == chat_id:
                    name = str(item.get("name") or "").strip()
                    if name:
                        return name
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"{X11_LOG_PREFIX} list_chats 失败: {exc}")
        return None

    def _ensure_chat_open(self, client: Any, chat_id: str, *, allow_switch: bool = True) -> None:
        display_name = self._resolve_display_name(client, chat_id)
        if not display_name or display_name == chat_id:
            raise X11SendError(f"无法确认会话 {chat_id} 的真实名称，已停止发送")
        chats = client.list_chats(limit=-1)
        owners = [c for c in chats if c.get("name") == display_name]
        if len(owners) != 1 or str(owners[0].get("id")) != chat_id:
            raise X11SendError("会话名称不唯一，无法确认发送对象")

        for _ in range(A11Y_RETRY_ATTEMPTS):
            tree = self._a11y(client)
            items = find_chat_list_items(tree)
            if not items:
                raise X11SendError("无障碍树里没有会话列表，微信窗口状态异常")
            matches = []
            for item in items:
                label = str(item.get("name") or "").strip()
                candidates = [c for c in chats if c.get("name") and
                              (label == c["name"] or label.startswith(c["name"] + " "))]
                if candidates:
                    longest = max(len(c["name"]) for c in candidates)
                    candidates = [c for c in candidates if len(c["name"]) == longest]
                    if len(candidates) == 1 and candidates[0].get("id") == chat_id:
                        matches.append(item)
            target = matches[0] if len(matches) == 1 else None
            if target is None:
                raise X11SendError(
                    f"会话列表里找不到「{display_name}」，它可能不在可视区域内"
                )
            if _has_state(target, "SELECTED"):
                return
            if not allow_switch:
                raise X11SendError("发送前会话已切换，已停止发送")
            point = _center(target.get("bounds"))
            if point is None:
                raise X11SendError(f"会话「{display_name}」坐标不可用")
            self._click(*point)
            time.sleep(A11Y_RETRY_INTERVAL_SECONDS)

        raise X11SendError(f"点击会话「{display_name}」后仍未进入已选中状态")

    def _focus_composer(
        self, client: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        for _ in range(A11Y_RETRY_ATTEMPTS):
            tree = self._a11y(client)
            pair = find_composer(tree)
            if pair is None:
                time.sleep(A11Y_RETRY_INTERVAL_SECONDS)
                continue
            edit_node = pair[0]
            if _has_state(edit_node, "FOCUSED"):
                return pair
            point = _center(edit_node.get("bounds"))
            if point is None:
                time.sleep(A11Y_RETRY_INTERVAL_SECONDS)
                continue
            self._click(*point)
            time.sleep(A11Y_RETRY_INTERVAL_SECONDS)
        raise X11SendError("无法把焦点切到微信输入框")

    @serialized_send
    def force_decode_image(
        self, client: Any, chat_id: str, index: int = 0
    ) -> bool:
        """点开倒数第 ``index`` 张图片气泡，促使微信写出未加密副本。

        返回是否真的点开了一张图片。点开后立即用 Esc 关闭查看器，避免影响
        后续的发送流程。
        """
        self._ensure_chat_open(client, chat_id)
        time.sleep(0.2)
        tree = self._a11y(client)
        points = _image_bubble_points(tree)
        if index >= len(points):
            return False

        x, y = points[index]
        self._click(x, y)
        time.sleep(IMAGE_DECODE_WAIT_SECONDS)
        try:
            self._exec_script(
                f"export DISPLAY={shlex.quote(self.display)}\n"
                "xdotool key --clearmodifiers Escape\n"
            )
        finally:
            time.sleep(0.15)
        return True

    @serialized_send
    def send_text(self, client: Any, chat_id: str, text: str) -> None:
        """在容器内模拟人工操作，把 ``text`` 发送到 ``chat_id``。"""
        if not text:
            return

        self._ensure_chat_open(client, chat_id)
        pair = self._focus_composer(client)

        if _has_state(pair[1], "DISABLED") is False:
            raise X11SendError("微信输入框里仍有未发送的内容，为避免串消息已中止")

        host_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", suffix=".txt", delete=False
            ) as handle:
                handle.write(text.rstrip("\n"))
                host_path = handle.name
            self._copy_text_into_container(host_path)

            self._exec_script(
                f"export DISPLAY={shlex.quote(self.display)}\n"
                f"setsid xclip -selection clipboard -i {CONTAINER_TEXT_PATH} "
                f"</dev/null >/dev/null 2>&1 &\n"
                "sleep 0.25\n"
                "xdotool key --clearmodifiers ctrl+v\n"
            )

            pair = self._wait_send_button(client, disabled=False)
            if pair is None:
                raise X11SendError("粘贴后发送按钮仍未激活，文本可能没有进入输入框")

            point = _center(pair[1].get("bounds"))
            if point is None:
                raise X11SendError("发送按钮坐标不可用")

            self._ensure_chat_open(client, chat_id, allow_switch=False)
            self._click(*point)

            if not self._wait_send_button(client, disabled=True):
                raise X11SendError("点击发送后输入框未清空，消息可能没有发出去")
        finally:
            if host_path:
                try:
                    os.unlink(host_path)
                except OSError:
                    pass

    @serialized_send
    def send_image(self, client: Any, chat_id: str, image: dict[str, Any]) -> None:
        """在容器内把图片粘贴到当前微信会话并发送。"""
        encoded = image.get("data")
        mime = str(image.get("mimeType") or "image/png")
        if not isinstance(encoded, str) or not encoded:
            raise X11SendError("图片数据为空")
        import base64
        host_path = None
        try:
            with tempfile.NamedTemporaryFile("wb", suffix=".img", delete=False) as handle:
                handle.write(base64.b64decode(encoded, validate=True))
                host_path = handle.name
            self._ensure_chat_open(client, chat_id)
            pair = self._focus_composer(client)
            if _has_state(pair[1], "DISABLED") is False:
                raise X11SendError("微信输入框里仍有未发送的内容")
            self._copy_binary_into_container(host_path, CONTAINER_IMAGE_PATH)
            self._exec_script(
                f"export DISPLAY={shlex.quote(self.display)}\n"
                f"/opt/tools/paste-image {shlex.quote(CONTAINER_IMAGE_PATH)} {shlex.quote(mime)}\n"
            )
            pair = self._wait_send_button(client, disabled=False)
            if pair is None:
                raise X11SendError("粘贴图片后发送按钮未激活")
            point = _center(pair[1].get("bounds"))
            if point is None:
                raise X11SendError("发送按钮坐标不可用")
            self._ensure_chat_open(client, chat_id, allow_switch=False)
            self._click(*point)
            if not self._wait_send_button(client, disabled=True):
                raise X11SendError("发送图片后输入框未清空")
        finally:
            if host_path:
                try:
                    os.unlink(host_path)
                except OSError:
                    pass
