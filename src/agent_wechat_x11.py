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


class X11SendError(RuntimeError):
    """X11 兜底发送失败。"""


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
        self._display_name_cache: dict[str, str] = {}
        self._active_chat_id: str | None = None

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
        cached = self._display_name_cache.get(chat_id)
        if cached:
            return cached
        try:
            chat = client.get_chat(chat_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"{X11_LOG_PREFIX} get_chat 失败 chat={chat_id}: {exc}")
            chat = None
        if isinstance(chat, dict):
            name = str(chat.get("name") or "").strip()
            if name:
                self._display_name_cache[chat_id] = name
                return name
        try:
            for item in client.list_chats(limit=200):
                if str(item.get("id") or "") == chat_id or str(
                    item.get("username") or ""
                ) == chat_id:
                    name = str(item.get("name") or "").strip()
                    if name:
                        self._display_name_cache[chat_id] = name
                        return name
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"{X11_LOG_PREFIX} list_chats 失败: {exc}")
        return None

    def _ensure_chat_open(self, client: Any, chat_id: str) -> None:
        # A decorated reply can send text and an image back-to-back.  WeChat
        # may refresh the chat list between those sends and temporarily omit
        # the just-selected group; retain the confirmed selection locally.
        if self._active_chat_id == chat_id:
            return
        display_name = self._resolve_display_name(client, chat_id)
        if not display_name:
            raise X11SendError(f"无法解析会话 {chat_id} 的显示名称")

        for _ in range(A11Y_RETRY_ATTEMPTS):
            tree = self._a11y(client)
            items = find_chat_list_items(tree)
            if not items:
                raise X11SendError("无障碍树里没有会话列表，微信窗口状态异常")
            target = match_chat_item(items, display_name)
            # Newly joined/group chats may be returned by agent-wechat with the
            # raw ``@chatroom`` id instead of their display name.  WeChat's
            # visible row still contains the latest message preview, so use it
            # as a stable secondary locator.
            if target is None and chat_id.endswith("@chatroom"):
                try:
                    messages = client.list_messages(chat_id, limit=8)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        f"{X11_LOG_PREFIX} list group messages failed chat={chat_id}: {exc}"
                    )
                    messages = []
                previews = [
                    str(message.get("content") or "").strip()
                    for message in reversed(messages)
                    if isinstance(message, dict)
                ]
                previews = [preview for preview in previews if preview]
                matches = [
                    item
                    for item in items
                    if any(preview in str(item.get("name") or "") for preview in previews)
                ]
                if len(matches) == 1:
                    target = matches[0]
            if target is None:
                raise X11SendError(
                    f"会话列表里找不到「{display_name}」，它可能不在可视区域内"
                )
            if _has_state(target, "SELECTED"):
                self._active_chat_id = chat_id
                return
            point = _center(target.get("bounds"))
            if point is None:
                raise X11SendError(f"会话「{display_name}」坐标不可用")
            self._click(*point)
            time.sleep(A11Y_RETRY_INTERVAL_SECONDS)

        raise X11SendError(f"点击会话「{display_name}」后仍未进入已选中状态")

    def _focus_composer(
        self, client: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        # Search suggestions can remain open after a manual lookup and obscure
        # the chat composer.  Escape is harmless in the normal chat view and
        # restores the composer before locating it.
        self._exec_script(
            f"export DISPLAY={shlex.quote(self.display)}\n"
            "xdotool key --clearmodifiers Escape\n"
        )
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

            self._click(*point)

            if not self._wait_send_button(client, disabled=True):
                raise X11SendError("点击发送后输入框未清空，消息可能没有发出去")
        finally:
            if host_path:
                try:
                    os.unlink(host_path)
                except OSError:
                    pass

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
                handle.write(base64.b64decode(encoded))
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
            self._click(*point)
            if not self._wait_send_button(client, disabled=True):
                raise X11SendError("发送图片后输入框未清空")
        finally:
            if host_path:
                try:
                    os.unlink(host_path)
                except OSError:
                    pass
