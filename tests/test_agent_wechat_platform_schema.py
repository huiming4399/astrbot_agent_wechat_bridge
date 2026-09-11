"""平台适配器 WebUI 配置元数据的格式回归测试。

AstrBot 4.x 的 ``register_platform_adapter(config_metadata=...)`` 要求扁平结构
（字段名 -> {description, type, hint, ...}）。历史版本曾误用
``{"en-US": {...}}`` + ``label``/``field_type`` 的写法，导致 WebUI 表单里一个
字段都渲染不出来，这里做回归保护。
"""

from __future__ import annotations

import ast
import pathlib

MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "src"
    / "agent_wechat_platform_adapter.py"
)
ALLOWED_TYPES = {"string", "bool", "int", "float", "list"}
LANGUAGE_KEYS = {"en-US", "zh-CN", "ja-JP"}


def _literal(name: str):
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == name
                for target in node.targets
            )
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {MODULE_PATH.name}")


def test_config_metadata_is_flat_field_map():
    metadata = _literal("CONFIG_METADATA")

    assert not LANGUAGE_KEYS & set(metadata), "config_metadata 不能按语言嵌套"
    assert "group_mention_free_senders" in metadata

    for field, spec in metadata.items():
        assert isinstance(spec, dict), field
        assert spec.get("type") in ALLOWED_TYPES, f"{field} 缺少合法的 type"
        assert spec.get("description"), f"{field} 缺少 description"
        assert "field_type" not in spec, f"{field} 使用了 3.x 的 field_type"
        assert "label" not in spec, f"{field} 使用了 3.x 的 label"
        if "labels" in spec:
            assert len(spec["labels"]) == len(spec.get("options", [])), field


def test_default_config_covers_metadata_fields():
    metadata = _literal("CONFIG_METADATA")
    default_config = _literal("DEFAULT_CONFIG")

    assert set(metadata) <= set(default_config)
    assert isinstance(default_config["group_mention_free_senders"], str)
