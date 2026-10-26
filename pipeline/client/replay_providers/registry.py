"""Explicit replay-provider routing from the Client's local robot Profile."""

from __future__ import annotations

import math
import re

from .agilex import AgileXReplayProvider
from .arx import ArxReplayProvider
from .base import ReplayProvider
from .simulated import SimulatedReplayProvider

_PROVIDERS: dict[str, type[ReplayProvider]] = {}


def register_provider(provider: type[ReplayProvider], *, replace=False) -> None:
    """Called by deployment code/tests; no dynamic import from config or files."""
    caps = provider.capabilities
    for value in (caps.id, caps.version):
        if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", value):
            raise ValueError(
                "回放 Provider id/version 只能包含字母、数字、下划线和连字符"
            )
    if not caps.robot_types or not caps.file_extensions:
        raise ValueError("回放 Provider 必须声明机器人类型和输入扩展名")
    if any(
        not ext.startswith(".") or ext != ext.lower() for ext in caps.file_extensions
    ):
        raise ValueError("回放文件扩展名必须以点开头并使用小写")
    if not math.isfinite(caps.progress_timeout) or caps.progress_timeout <= 0:
        raise ValueError("回放进度超时必须是正数")
    if caps.id in _PROVIDERS and not replace:
        raise ValueError(f"回放 Provider 已注册：{caps.id}")
    _PROVIDERS[caps.id] = provider


def create_provider(config: dict, profile: dict, mode: str) -> ReplayProvider | None:
    provider_id = config.get("provider")
    if not provider_id:
        if config.get("enabled", False):
            raise ValueError("启用回放时必须显式配置 replay.provider")
        return None
    if not isinstance(provider_id, str) or provider_id not in _PROVIDERS:
        raise ValueError(f"未知回放 Provider：{provider_id!r}")
    factory = _PROVIDERS[provider_id]
    caps = factory.capabilities
    robot_type = profile.get("robot_type")
    if robot_type not in caps.robot_types:
        raise ValueError(
            f"回放 Provider {provider_id} 不支持 robot_type={robot_type!r}"
        )
    if caps.simulated and mode != "mock":
        raise ValueError("模拟回放 Provider 只能用于 mock Client")
    return factory(config, profile)


register_provider(AgileXReplayProvider)
register_provider(SimulatedReplayProvider)

register_provider(ArxReplayProvider)
