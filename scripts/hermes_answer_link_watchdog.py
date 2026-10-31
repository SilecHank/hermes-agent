#!/usr/bin/env python3
"""Alert when the IVD bot has quietly stopped answering.

The 2026-10-31 incident produced 12 hours of "请补充产品名称、版本或 SOP 编号"
with `api_calls=0`: the model never ran, but every health check stayed green
because the gateway process, heartbeat and platforms were all fine.  Health is
not answer quality, so this watchdog watches answer quality.

Silent when healthy (cron delivers nothing), prints a short alert otherwise.
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
    repeated = Counter(
        str(row.get("question_preview") or "").strip()
        for row in turns
        if str(row.get("route_id") or "") == "clarification"
    )
    for question, count in repeated.most_common(1):
        if question and count >= repeat_limit:
            alerts.append(f"同一澄清追问重复 {count} 次：{question[:40]}")
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
    print("排查：先看遥测 route_id / validation_status / api_calls，再按 "
          "ivd-system-operations 技能里的“连续乱答”条目定位。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
