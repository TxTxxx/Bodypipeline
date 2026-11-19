"""Structured language-instruction templates shared by Server and Viewer."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

KEY_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
SOURCE_VARIABLE = re.compile(r"\{\{([A-Za-z][A-Za-z0-9_]{0,63})\}\}")
MAX_RENDERED_LENGTH = 2000


class TextPart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["text"]
    text: str = Field(min_length=1, max_length=500)


class VariablePart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["variable"]
    key: str = Field(pattern=KEY_PATTERN.pattern)


class VariableDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=80)
    options: list[str] = Field(min_length=1, max_length=5000)
    default: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_options(self):
        self.options = [option.strip() for option in self.options]
        if any(not option or len(option) > 200 for option in self.options):
            raise ValueError("候选值必须为 1–200 个字符")
        if len(set(self.options)) != len(self.options):
            raise ValueError("同一变量的候选值不能重复")
        if self.default is not None:
            self.default = self.default.strip()
            if self.default not in self.options:
                raise ValueError("默认值必须是该变量的候选值")
        return self


class InstructionTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parts: list[TextPart | VariablePart] = Field(min_length=1, max_length=200)
    variables: dict[str, VariableDefinition] = Field(
        min_length=1, max_length=100
    )

    @model_validator(mode="after")
    def validate_references(self):
        for key in self.variables:
            if not KEY_PATTERN.fullmatch(key):
                raise ValueError(f"变量名“{key}”无效")
        used = {
            part.key for part in self.parts if isinstance(part, VariablePart)
        }
        missing = sorted(used - set(self.variables))
        if missing:
            raise ValueError("模板引用了未定义变量：" + "、".join(missing))
        unused = sorted(set(self.variables) - used)
        if unused:
            raise ValueError("变量必须在模板中使用：" + "、".join(unused))
        if not any(
            isinstance(part, VariablePart) or part.text.strip() for part in self.parts
        ):
            raise ValueError("模板不能为空")
        return self


def _value_error(exc: ValidationError) -> ValueError:
    messages = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error["loc"])
        messages.append(f"{location}: {error['msg']}" if location else error["msg"])
    return ValueError("；".join(messages))


def normalize_template(payload: dict[str, Any] | InstructionTemplate) -> dict[str, Any]:
    """Validate and return the canonical JSON representation."""

    try:
        model = (
            payload
            if isinstance(payload, InstructionTemplate)
            else InstructionTemplate.model_validate(payload)
        )
    except ValidationError as exc:
        raise _value_error(exc) from exc
    return model.model_dump(mode="json")


def default_selections(template: dict[str, Any]) -> dict[str, str]:
    normalized = normalize_template(template)
    selections = {}
    for key, definition in normalized["variables"].items():
        value = definition.get("default")
        if value is None:
            value = definition["options"][0]
        selections[key] = value
    return selections


def render_instruction(
    template: dict[str, Any], selections: dict[str, str] | None
) -> str:
    normalized = normalize_template(template)
    supplied = selections or {}
    variables = normalized["variables"]
    unknown = sorted(set(supplied) - set(variables))
    if unknown:
        raise ValueError("不支持这些模板变量：" + "、".join(unknown))
    chosen = default_selections(normalized)
    for key, value in supplied.items():
        if not isinstance(value, str):
            raise ValueError(f"变量 {key} 的选项必须是字符串")
        value = value.strip()
        if value not in variables[key]["options"]:
            raise ValueError(f"变量 {key} 使用了已移除选项：{value}")
        chosen[key] = value
    rendered = "".join(
        part.get("text", "")
        if part["type"] == "text"
        else chosen[part["key"]]
        for part in normalized["parts"]
    ).strip()
    if not rendered or len(rendered) > MAX_RENDERED_LENGTH:
        raise ValueError("渲染后的语言指令必须为 1–2000 个字符")
    return rendered


def parse_template_source(
    source: str, variables: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Convert the advanced ``{{variable}}`` syntax into visual parts.

    The candidate definitions are intentionally supplied by the visual
    inspector. Source editing only changes text and variable placement.
    """

    if not isinstance(source, str) or not source.strip():
        raise ValueError("模板源文本不能为空")
    if len(source) > 4000:
        raise ValueError("模板源文本不能超过 4000 个字符")
    definitions = variables or {}
    parts: list[dict[str, str]] = []
    cursor = 0
    found = []
    for match in SOURCE_VARIABLE.finditer(source):
        if match.start() > cursor:
            parts.append({"type": "text", "text": source[cursor : match.start()]})
        key = match.group(1)
        found.append(key)
        parts.append({"type": "variable", "key": key})
        cursor = match.end()
    if cursor < len(source):
        parts.append({"type": "text", "text": source[cursor:]})
    if not found:
        raise ValueError("源文本至少需要一个 {{variable}} 变量")
    missing = sorted(set(found) - set(definitions))
    if missing:
        raise ValueError("请先为这些变量配置候选值：" + "、".join(missing))
    unused = sorted(set(definitions) - set(found))
    if unused:
        raise ValueError("模板未使用这些变量：" + "、".join(unused))
    return {"parts": parts, "variables": definitions}


def migration_warnings(
    template: dict[str, Any], assignments: dict[int | str, dict[str, str]]
) -> list[dict[str, Any]]:
    normalized = normalize_template(template)
    warnings = []
    for raw_episode, selections in sorted(
        assignments.items(), key=lambda item: int(item[0])
    ):
        episode = int(raw_episode)
        for key, value in sorted((selections or {}).items()):
            definition = normalized["variables"].get(key)
            if definition is None:
                warnings.append(
                    {
                        "kind": "variable_missing",
                        "episode_index": episode,
                        "variable": key,
                        "value": value,
                        "message": f"Episode {episode} 仍使用已移除变量 {key}",
                    }
                )
            elif value not in definition["options"]:
                warnings.append(
                    {
                        "kind": "option_missing",
                        "episode_index": episode,
                        "variable": key,
                        "value": value,
                        "message": (
                            f"Episode {episode} 的变量 {key} 仍使用已移除选项“{value}”"
                        ),
                    }
                )
    return warnings
