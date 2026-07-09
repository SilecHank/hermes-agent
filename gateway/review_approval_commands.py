"""Config-driven review approval command intercepts for messaging gateways."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
import shlex
from pathlib import Path
from typing import Any

from gateway.platforms.base import MessageEvent
from hermes_cli.config import cfg_get, read_raw_config


logger = logging.getLogger(__name__)

_ACTION_RE = re.compile(r"^[ASQUasqu]\s*\d+$")
_DETAIL_RE = re.compile(r"^[Dd]\s*\d+$")


@dataclasses.dataclass(frozen=True)
class ReviewApprovalConfig:
    enabled: bool = False
    platform: str = "weixin"
    chat_type: str = "dm"
    command: str = ""
    cwd: str = ""
    timeout_seconds: float = 15.0


def review_approval_config(raw_config: dict[str, Any] | None = None) -> ReviewApprovalConfig:
    raw = read_raw_config() if raw_config is None else raw_config
    section = cfg_get(raw, "review_approval_commands", default={})
    if not isinstance(section, dict):
        section = {}
    return ReviewApprovalConfig(
        enabled=bool(section.get("enabled", False)),
        platform=str(section.get("platform", "weixin") or "weixin").strip().lower(),
        chat_type=str(section.get("chat_type", "dm") or "dm").strip().lower(),
        command=str(section.get("command", "") or "").strip(),
        cwd=str(section.get("cwd", "") or "").strip(),
        timeout_seconds=float(section.get("timeout_seconds", 15.0) or 15.0),
    )


def _normalized_platform(event: MessageEvent) -> str:
    platform = getattr(event.source, "platform", "")
    return str(getattr(platform, "value", platform) or "").strip().lower()


def _normalized_chat_type(event: MessageEvent) -> str:
    return str(getattr(event.source, "chat_type", "") or "").strip().lower()


def is_review_approval_command(event: MessageEvent, config: ReviewApprovalConfig | None = None) -> bool:
    cfg = config or review_approval_config()
    if not cfg.enabled or not cfg.command:
        return False
    if _normalized_platform(event) != cfg.platform:
        return False
    if cfg.chat_type and _normalized_chat_type(event) != cfg.chat_type:
        return False
    text = (event.text or "").strip()
    if not text:
        return False
    upper = text.upper()
    if upper in {"N", "P", "NEXT", "PREV", "PREVIOUS"} or text in {"下一页", "上一页"}:
        return True
    if _DETAIL_RE.fullmatch(text):
        return True
    parts = text.split()
    return bool(parts) and all(_ACTION_RE.fullmatch(part) for part in parts)


def build_review_approval_shell_command(config: ReviewApprovalConfig, message: str) -> str:
    quoted_message = shlex.quote(message)
    if "{message}" in config.command:
        return config.command.replace("{message}", quoted_message)
    return f"{config.command} {quoted_message}"


async def run_review_approval_command(event: MessageEvent, config: ReviewApprovalConfig | None = None) -> str:
    cfg = config or review_approval_config()
    command = build_review_approval_shell_command(cfg, (event.text or "").strip())
    cwd = cfg.cwd or None
    if cwd and not Path(cwd).exists():
        raise FileNotFoundError(f"review approval cwd does not exist: {cwd}")
    logger.info(
        "Handling review approval command outside LLM: platform=%s chat=%s text=%r",
        _normalized_platform(event),
        getattr(event.source, "chat_id", ""),
        event.text,
    )
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=cfg.timeout_seconds)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise TimeoutError(f"review approval command timed out after {cfg.timeout_seconds:.0f}s")
    out = stdout.decode("utf-8", errors="replace").strip()
    err = stderr.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        raise RuntimeError(err or f"review approval command failed with exit {proc.returncode}")
    return out
