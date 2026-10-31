#!/usr/bin/env python3
"""Alert when the IVD bot has quietly stopped answering.

The 2026-10-31 incident produced 12 hours of "请补充产品名称、版本或 SOP 编号"
with `api_calls=0`: the model never ran, but every health check stayed green
because the gateway process, heartbeat and platforms were all fine.  Health is
not answer quality, so this watchdog watches answer quality.

Silent when healthy (cron delivers nothing), prints a short alert otherwise.

Grouping rule (2026-10-31 second incident): before the gateway recorded
`question_has_user_text`, every telemetry preview began with the inbound
sender-identity note, so a window of unrelated turns collapsed into one
"same question repeated N times" alert that named the scaffolding instead of
the real question.  Repeat detection therefore requires rows that carry the
user-authored text signal; rows without the field are history and are only
counted by the blocked-ratio rule.

Textless rule (2026-10-31 third incident): a caption-less screenshot, a runtime
notice and a social turn all record a telemetry row while never reaching the
governed engine -- and they record it with the recorder's defaults
(`route_id="standard"`, empty `route_version`).  Counting "no user text" alone
therefore alerts on every correctly bypassed media turn.  A textless turn is
only news when the engine actually handled it, which the gateway now records
directly as `ivd_engine_entered`.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path

DEFAULT_EVENTS = Path(
    os.environ.get("IVD_TELEMETRY")
    or Path.home() / ".hermes" / "ivd-live-data" / "telemetry" / "runtime-events.jsonl"
)


def _load(path: Path, limit: int) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - 512_000))
        chunk = handle.read().decode("utf-8", "replace")
    for line in chunk.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("event_type") != "ivd_answer_turn":
            continue
        rows.append(record)
    return rows[-limit:]


def _user_text_signal(row: dict) -> bool | None:
    """Return whether this turn carried user-authored text.

    ``None`` means the row predates the field, so the value is unknown and the
    row cannot join a text-based grouping rule.
    """
    if "question_has_user_text" not in row:
        return None
    return bool(row.get("question_has_user_text"))


def _engine_entered(row: dict) -> bool | None:
    """Return whether the governed IVD engine handled this turn.

    ``None`` means the row cannot be judged.  A bypassed turn still records a
    row: the recorder fills `route_id` with "standard" and `route_version` with
    "" because no route ran, so those two fields describe a *bypass*, not an
    answer.  Rows written before the gateway recorded `ivd_engine_entered` are
    read through that fallback; new rows are read from the field itself.
    """
    value = row.get("ivd_engine_entered")
    if isinstance(value, bool):
        return value
    route_id = str(row.get("route_id") or "").strip()
    if not route_id:
        return None
    if route_id != "standard" or str(row.get("route_version") or "").strip():
        return True
    if str(row.get("validation_status") or "") == "preflight_blocked":
        # A governed turn that stopped before generation carries no route
        # version, but it did reach the engine and must stay visible here.
        return True
    return False


def _group_key(row: dict) -> str:
    fingerprint = str(row.get("question_fingerprint") or "").strip()
    if fingerprint:
        return fingerprint
    return str(row.get("question_preview") or "").strip()


def diagnose(
    rows: list[dict], *, window: int = 20, silent_ratio: float = 0.8, repeat_limit: int = 3
) -> list[str]:
    turns = rows[-window:]
    if len(turns) < 5:
        return []
    alerts: list[str] = []
    blocked = [
        row for row in turns
        if str(row.get("validation_status") or "") == "preflight_blocked"
        and int(row.get("api_calls") or 0) == 0
    ]
    if len(blocked) / len(turns) >= silent_ratio:
        clarifications = sum(
            1 for row in blocked if str(row.get("route_id") or "") == "clarification"
        )
        alerts.append(
            f"最近 {len(turns)} 轮里 {len(blocked)} 轮在生成前被拦截、0 次 API 调用"
            f"（其中 {clarifications} 轮为澄清话术）。agent 可能又没启动。"
        )
    # A turn with no user text (screenshot-only, or a runtime-injected notice)
    # must never reach the IVD engine. When it does, that is the media-routing
    # regression, not a repeated question -- name it as such.
    textless = [
        row for row in turns
        if _user_text_signal(row) is False and _engine_entered(row) is True
    ]
    if len(textless) >= 2:
        alerts.append(
            f"最近 {len(turns)} 轮里 {len(textless)} 轮没有用户正文（图片或系统注入），"
            "却进入了业务链路——媒体轮分流可能又失效了。"
        )
    repeated: Counter[str] = Counter()
    sample: dict[str, str] = {}
    for row in turns:
        if str(row.get("route_id") or "") != "clarification":
            continue
        if _user_text_signal(row) is not True:
            continue
        key = _group_key(row)
        if not key:
            continue
        repeated[key] += 1
        sample.setdefault(key, str(row.get("question_preview") or "").strip())
    for key, count in repeated.most_common(1):
        if count >= repeat_limit:
            alerts.append(
                f"同一澄清追问重复 {count} 次：{sample.get(key, '')[:40]}"
            )
    return alerts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS)
    parser.add_argument("--window", type=int, default=20)
    parser.add_argument("--silent-ratio", type=float, default=0.8)
    args = parser.parse_args()
    alerts = diagnose(_load(args.events, args.window * 3), window=args.window,
                      silent_ratio=args.silent_ratio)
    if not alerts:
        return 0
    print("【IVD 回答链路告警】")
    for item in alerts:
        print("- " + item)
    print("排查：先看遥测 route_id / validation_status / api_calls / "
          "ivd_engine_entered，再按 ivd-system-operations 技能里的“连续乱答”条目定位。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
