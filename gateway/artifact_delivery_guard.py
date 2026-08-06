"""Final-response contract for requested downloadable artifacts.

The gateway already has native file delivery.  This guard prevents a model from
claiming that the environment is unavailable without first exercising an
available generation tool.  It does not turn failures into successes: once a
real tool attempt exists, the model may report that attempt's concrete error.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any


_MESSAGING_PLATFORMS = frozenset({"weixin", "wecom", "qqbot", "telegram"})
_GENERATION_TOOLS = frozenset({"terminal", "execute_code", "write_file"})
_ARTIFACT_REQUESTS = {
    "xlsx": re.compile(
        r"(?:\.xlsx\b|\bxlsx\b|excel|电子表格|工作簿)", re.IGNORECASE
    ),
    "csv": re.compile(r"(?:\.csv\b|\bcsv\b)", re.IGNORECASE),
    "docx": re.compile(r"(?:\.docx\b|\bdocx\b|word\s*文档)", re.IGNORECASE),
    "pdf": re.compile(r"(?:\.pdf\b|\bpdf\b)", re.IGNORECASE),
    "pptx": re.compile(
        r"(?:\.pptx\b|\bpptx\b|powerpoint|幻灯片)", re.IGNORECASE
    ),
}
_DELIVERY_INTENT_RE = re.compile(
    r"(?:生成|制作|整理|导出|创建|做成|发给|发送|给我|下载|附件|文件|"
    r"generate|create|export|send|attach|download)",
    re.IGNORECASE,
)
_UNVERIFIED_REFUSAL_RE = re.compile(
    r"(?:环境.{0,12}(?:缺失|不支持|不可用|没有)|"
    r"(?:缺少|没有).{0,16}(?:环境|依赖|工具|能力)|"
    r"(?:无法|不能|没法).{0,20}(?:生成|创建|制作|导出|发送|提供)|"
    r"environment.{0,16}(?:missing|unavailable|unsupported)|"
    r"(?:cannot|can't|unable to).{0,20}(?:generate|create|export|send))",
    re.IGNORECASE,
)


def _requested_artifact(message: str) -> str:
    if not _DELIVERY_INTENT_RE.search(message):
        return ""
    for artifact, pattern in _ARTIFACT_REQUESTS.items():
        if pattern.search(message):
            return artifact
    return ""


def _tool_name(call: Any) -> str:
    if not isinstance(call, dict):
        return ""
    function = call.get("function")
    if isinstance(function, dict):
        return str(function.get("name") or "")
    return str(call.get("name") or "")


def _current_turn_has_generation_attempt(messages: Iterable[dict[str, Any]]) -> bool:
    current: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "user" and not message.get(
            "_final_validation_synthetic"
        ):
            current = []
            continue
        current.append(message)
    return any(
        _tool_name(call) in _GENERATION_TOOLS
        for message in current
        for call in (message.get("tool_calls") or ())
    )


def available_tool_names(tools: Iterable[dict[str, Any]] | None) -> set[str]:
    """Extract callable names from the schemas attached to an agent."""

    names: set[str] = set()
    for tool in tools or ():
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if isinstance(function, dict) and function.get("name"):
            names.add(str(function["name"]))
        elif tool.get("name"):
            names.add(str(tool["name"]))
    return names


class ArtifactDeliveryValidator:
    def __init__(
        self,
        *,
        user_message,
        platform,
        messages_provider,
        available_tool_names,
    ):
        self.user_message = user_message
        self.platform = platform
        self.messages_provider = messages_provider
        self.available_tool_names = frozenset(available_tool_names)

    def __call__(self, answer):
        artifact = _requested_artifact(str(self.user_message or ""))
        if (
            self.platform not in _MESSAGING_PLATFORMS
            or not artifact
            or not (_GENERATION_TOOLS & self.available_tool_names)
            or not _UNVERIFIED_REFUSAL_RE.search(str(answer or ""))
            or _current_turn_has_generation_attempt(self.messages_provider() or ())
        ):
            return {"ok": True, "reasons": [], "fallback": ""}
        return {
            "ok": False,
            "reasons": [f"artifact_capability_refusal_without_attempt:{artifact}"],
            "fallback": (
                "这次未能完成文件生成或发送。请稍后重试；系统需要分别记录"
                "生成阶段和发送阶段的具体失败原因，不能笼统归为环境缺失。"
            ),
        }


class CompositeFinalResponseValidator:
    """Run independent response contracts without replacing existing guards."""

    fail_closed = True
    error_fallback = "当前回答校验暂时不可用，已停止发送未校验结果。请稍后重试。"

    def __init__(self, validators: Iterable[Callable[[str], dict[str, Any]]]):
        self.validators = tuple(validator for validator in validators if validator)

    def __call__(self, answer):
        for validator in self.validators:
            result = validator(answer)
            if result.get("ok") is not True:
                return result
        return {"ok": True, "reasons": [], "fallback": ""}
