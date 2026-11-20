"""Instruction templates are validated before they can reach a LeRobot dataset."""

import pytest

from pipeline.server.instruction_templates import (
    migration_warnings,
    normalize_template,
    parse_template_source,
    render_instruction,
)


def template():
    return {
        "parts": [
            {"type": "text", "text": "请将"},
            {"type": "variable", "key": "object"},
            {"type": "text", "text": "放入"},
            {"type": "variable", "key": "target"},
            {"type": "text", "text": "中"},
        ],
        "variables": {
            "object": {
                "label": "物体",
                "options": ["红色方块", "蓝色圆柱"],
                "default": "红色方块",
            },
            "target": {
                "label": "目标位置",
                "options": ["左侧盒子", "右侧盒子"],
                "default": "左侧盒子",
            },
        },
    }


def test_template_is_normalized_and_rendered_from_selected_options():
    normalized = normalize_template(template())
    assert normalized["parts"][1] == {"type": "variable", "key": "object"}
    assert render_instruction(
        normalized, {"object": "蓝色圆柱", "target": "右侧盒子"}
    ) == "请将蓝色圆柱放入右侧盒子中"
    assert render_instruction(normalized, {}) == "请将红色方块放入左侧盒子中"


def test_source_syntax_round_trips_into_structured_parts():
    parsed = parse_template_source(
        "请将{{object}}放入{{target}}中",
        variables=template()["variables"],
    )
    assert normalize_template(parsed) == normalize_template(template())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["parts"].append({"type": "variable", "key": "missing"}),
        lambda value: value["variables"].update(
            object={"label": "物体", "options": ["x", "x"], "default": "x"}
        ),
        lambda value: value["variables"]["object"].update(default="not-an-option"),
        lambda value: value["variables"].update(
            unused={"label": "未使用", "options": ["x"], "default": "x"}
        ),
        lambda value: value["parts"].append({"type": "bogus", "text": "x"}),
        lambda value: value["variables"].update(**{"bad key": {"label": "x", "options": ["x"]}}),
    ],
)
def test_invalid_templates_are_rejected(mutate):
    value = template()
    mutate(value)
    with pytest.raises(ValueError):
        normalize_template(value)


def test_render_rejects_unknown_required_or_removed_options():
    normalized = normalize_template(template())
    with pytest.raises(ValueError, match="target"):
        render_instruction(normalized, {"target": "不存在"})
    with pytest.raises(ValueError, match="不支持"):
        render_instruction(normalized, {"extra": "x"})


def test_option_migration_warnings_name_episode_and_choice():
    changed = template()
    changed["variables"]["object"]["options"] = ["红色方块"]
    changed["variables"]["object"]["default"] = "红色方块"
    assignments = {
        3: {"object": "蓝色圆柱", "target": "右侧盒子"},
        8: {"object": "红色方块", "target": "右侧盒子"},
    }
    warnings = migration_warnings(normalize_template(changed), assignments)
    assert warnings == [
        {
            "kind": "option_missing",
            "episode_index": 3,
            "variable": "object",
            "value": "蓝色圆柱",
            "message": "Episode 3 的变量 object 仍使用已移除选项“蓝色圆柱”",
        }
    ]
