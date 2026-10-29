"""Immutable, validated manual-annotation scheme definitions."""

from __future__ import annotations

import copy
import math
import re
import uuid

from pipeline.shared.core import DomainError, digest, now

WIDGETS = {
    "text": {"text", "textarea"},
    "int": {"number", "slider"},
    "float": {"number", "slider"},
    "bool": {"toggle"},
    "single": {"dropdown", "buttons"},
    "multi": {"checkboxes", "dropdown"},
}
DEFAULT_WIDGET = {
    "text": "text",
    "int": "number",
    "float": "number",
    "bool": "toggle",
    "single": "dropdown",
    "multi": "checkboxes",
}


def fail(message):
    raise DomainError(message, 422)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_-]{0,63}", value
    ):
        fail("字段和类型标识必须为 1–64 位字母、数字、下划线或连字符")
    return value


def validate_value(field, value):
    if value is None:
        return
    kind = field["dtype"]
    if kind == "text":
        if not isinstance(value, str) or len(value) > field.get("max_length", 20000):
            fail(f"{field['label']} 必须为长度有效的文本")
    elif kind == "bool":
        if type(value) is not bool:
            fail(f"{field['label']} 必须为布尔值")
    elif kind in {"int", "float"}:
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or (kind == "int" and type(value) is not int)
        ):
            fail(f"{field['label']} 的数值类型无效")
        if value < field.get("min", -math.inf) or value > field.get("max", math.inf):
            fail(f"{field['label']} 超出允许范围")
        if field.get("step") is not None:
            n = (value - field.get("min", 0)) / field["step"]
            if not math.isclose(n, round(n), abs_tol=1e-7):
                fail(f"{field['label']} 不符合步长")
    else:
        allowed = {o["value"] for o in field["options"]}
        if kind == "single":
            if not isinstance(value, str) or value not in allowed:
                fail(f"{field['label']} 含非法选项")
        elif (
            not isinstance(value, list)
            or any(not isinstance(v, str) or v not in allowed for v in value)
            or len(set(value)) != len(value)
        ):
            fail(f"{field['label']} 必须为不重复的有效选项列表")


def validate_values(fields, values, complete=False):
    if not isinstance(values, dict):
        fail("字段值必须为对象")
    definitions = {f["id"]: f for f in fields}
    if set(values) - definitions.keys():
        fail("含方案未定义的字段")
    for key, value in values.items():
        validate_value(definitions[key], value)
    if complete:
        for field in fields:
            value = values.get(field["id"])
            if field.get("required") and (value is None or value == "" or value == []):
                fail(f"请填写必填字段：{field['label']}")
    return copy.deepcopy(values)


def validate_scheme(body):
    if not isinstance(body, dict):
        fail("方案必须为对象")
    if set(body) - {"name", "types", "revision", "version"}:
        fail("方案含未知字段")
    name = body.get("name")
    types = body.get("types")
    if (
        not isinstance(name, str)
        or not 1 <= len(name.strip()) <= 200
        or not isinstance(types, list)
        or not 1 <= len(types) <= 100
    ):
        fail("方案名称或类型列表无效")
    ids, keys, result = set(), set(), []
    for entry in types:
        if not isinstance(entry, dict) or set(entry) - {
            "id",
            "label",
            "scope",
            "allow_overlap",
            "fields",
            "camera_bound",
        }:
            fail("标注类型含未知字段")
        tid = identifier(entry.get("id"))
        if (
            tid in ids
            or not isinstance(entry.get("scope"), str)
            or entry.get("scope") not in {"episode", "interval", "frame"}
        ):
            fail("类型标识重复或范围无效")
        ids.add(tid)
        label = entry.get("label", tid)
        if not isinstance(label, str) or not 1 <= len(label) <= 200:
            fail("类型显示名称无效")
        if (
            type(entry.get("allow_overlap", True)) is not bool
            or type(entry.get("camera_bound", False)) is not bool
        ):
            fail("类型选项必须为布尔值")
        fields = entry.get("fields")
        if not isinstance(fields, list) or not 1 <= len(fields) <= 100:
            fail("每种类型须有 1–100 个字段")
        seen, validated = set(), []
        for item in fields:
            if not isinstance(item, dict) or set(item) - {
                "id",
                "label",
                "dtype",
                "widget",
                "options",
                "required",
                "default",
                "min",
                "max",
                "step",
                "max_length",
                "unit",
                "key",
                "description",
            }:
                fail("字段定义含未知字段")
            field = copy.deepcopy(item)
            fid = identifier(field.get("id"))
            kind = field.get("dtype")
            if fid in seen or not isinstance(kind, str) or kind not in WIDGETS:
                fail("字段标识重复或数据类型无效")
            seen.add(fid)
            field.setdefault("label", fid)
            if (
                not isinstance(field["label"], str)
                or not 1 <= len(field["label"]) <= 200
            ):
                fail("字段名称无效")
            field.setdefault("widget", DEFAULT_WIDGET[kind])
            field.setdefault("required", False)
            if (
                not isinstance(field["widget"], str)
                or field["widget"] not in WIDGETS[kind]
                or type(field["required"]) is not bool
            ):
                fail("字段控件与类型不匹配")
            for numeric in ("min", "max", "step"):
                if numeric in field and (
                    kind not in {"int", "float"}
                    or type(field[numeric]) not in (float, int)
                    or not math.isfinite(field[numeric])
                ):
                    fail("数值约束无效")
            if (
                field.get("min", -math.inf) > field.get("max", math.inf)
                or field.get("step", 1) <= 0
            ):
                fail("数值范围或步长无效")
            if "max_length" in field and (
                kind != "text"
                or type(field["max_length"]) is not int
                or not 1 <= field["max_length"] <= 20000
            ):
                fail("文本长度约束无效")
            for text in ("unit", "description"):
                if text in field and (
                    not isinstance(field[text], str) or len(field[text]) > 2000
                ):
                    fail("字段说明无效")
            if field.get("key"):
                shortcut = field["key"]
                if (
                    not isinstance(shortcut, str)
                    or len(shortcut) != 1
                    or (entry["scope"], shortcut.lower()) in keys
                ):
                    fail("快捷键重复或无效")
                keys.add((entry["scope"], shortcut.lower()))
            if kind in {"single", "multi"}:
                options = field.get("options")
                if not isinstance(options, list) or not 1 <= len(options) <= 200:
                    fail("选项字段须配置选项")
                values = set()
                for option in options:
                    if (
                        not isinstance(option, dict)
                        or set(option) != {"value", "label"}
                        or any(
                            not isinstance(option[v], str)
                            or not 1 <= len(option[v]) <= 200
                            for v in ("value", "label")
                        )
                        or option["value"] in values
                    ):
                        fail("选项格式错误或存储值重复")
                    values.add(option["value"])
            elif "options" in field:
                fail("非选项类型不能配置选项")
            if "default" in field:
                validate_value(field, field["default"])
            validated.append(field)
        result.append(
            {
                "id": tid,
                "label": label,
                "scope": entry["scope"],
                "allow_overlap": entry.get("allow_overlap", True),
                "camera_bound": entry.get("camera_bound", False),
                "fields": validated,
            }
        )
    return {"name": name.strip(), "types": result}


def compatible(old, new):
    types = {t["id"]: t for t in new["types"]}
    for original in old["types"]:
        target = types.get(original["id"])
        if (
            not target
            or any(target[k] != original[k] for k in ("scope", "camera_bound"))
            or (original["allow_overlap"] and not target["allow_overlap"])
        ):
            return False
        fields = {f["id"]: f for f in target["fields"]}
        for field in original["fields"]:
            updated = fields.get(field["id"])
            if (
                not updated
                or updated["dtype"] != field["dtype"]
                or (not field["required"] and updated["required"])
            ):
                return False
            if any(
                updated.get(k, default) != field.get(k, default)
                for k, default in (("step", None),)
            ):
                return False
            if (
                updated.get("min", -math.inf) > field.get("min", -math.inf)
                or updated.get("max", math.inf) < field.get("max", math.inf)
                or updated.get("max_length", 20000) < field.get("max_length", 20000)
            ):
                return False
            if {o["value"] for o in field.get("options", [])} - {
                o["value"] for o in updated.get("options", [])
            }:
                return False
        if any(
            f["required"]
            for f in target["fields"]
            if f["id"] not in {x["id"] for x in original["fields"]}
        ):
            return False
    return not any(
        f["required"]
        for t in new["types"]
        if t["id"] not in {x["id"] for x in old["types"]}
        for f in t["fields"]
    )


class SchemeService:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store

    def list(self):
        return [self.get(row["id"]) for row in self.store.all("annotation_scheme")]

    def get(self, identifier, version=None):
        head = self.store.get("annotation_scheme", identifier)
        selected = head["version"] if version is None else version
        if type(selected) is not int or selected < 1:
            fail("方案版本无效")
        result = self.store.get("annotation_scheme_version", f"{identifier}-{selected}")
        return {**result, "id": identifier, "archived": head.get("archived", False)}

    def _publish(self, identifier, version, definition, archived=False):
        snapshot = {
            **definition,
            "id": f"{identifier}-{version}",
            "version": version,
            "scheme_id": identifier,
            "digest": digest(definition),
            "created_at": now(),
        }
        self.store.put("annotation_scheme_version", snapshot["id"], snapshot)
        self.store.put(
            "annotation_scheme",
            identifier,
            {
                "id": identifier,
                "version": version,
                "archived": archived,
                "created_at": now(),
            },
        )
        return self.get(identifier)

    def create(self, body):
        definition = validate_scheme(body)
        with self.store.lock:
            return self._publish(uuid.uuid4().hex, 1, definition)

    def version(self, identifier, body):
        definition = validate_scheme(body)
        with self.store.lock:
            head = self.get(identifier)
            if head["archived"]:
                raise DomainError("已停用方案不能发布新版本", 409)
            expected = body.get("revision", body.get("version"))
            if type(expected) is not int or expected != head["version"]:
                raise DomainError("方案版本已变化，请重新加载", 409)
            return self._publish(identifier, head["version"] + 1, definition)

    def copy(self, identifier, body=None):
        head = self.get(identifier)
        return self.create(
            {
                "name": (body or {}).get("name", head["name"] + " 副本"),
                "types": head["types"],
            }
        )

    def archive(self, identifier):
        with self.store.lock:
            head = self.store.get("annotation_scheme", identifier)
            self.store.put("annotation_scheme", identifier, {**head, "archived": True})
            return self.get(identifier)
