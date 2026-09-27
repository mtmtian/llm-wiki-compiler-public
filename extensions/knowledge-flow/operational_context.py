"""Read a small, current operational snapshot for explicitly operational prompts."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from writer_handoff import status as writer_status


MAX_OBSERVATION_AGE = 6 * 60 * 60
OPERATIONAL_ANCHOR = re.compile(r"(?:wiki|obsidian|knowledge[- ]flow|llmwiki)", re.I)
OPERATIONAL_SIGNAL = re.compile(r"(?:状态|运行|同步|worker|队列|hooks?|安装|版本|写入|运维|维护|排队|失败|runtime|commit)", re.I)


def _load(path: Path) -> Any:
    """Read a bounded regular JSON file without following symlinks."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 128 * 1024:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None


def _timestamp(value: Any, path: Path) -> float | None:
    """Use an explicit observation timestamp, falling back to the file mtime."""
    raw = value.get("checkedAt") or value.get("at") if isinstance(value, dict) else None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() if raw else path.stat().st_mtime
    except (OSError, TypeError, ValueError):
        return None


def _current(value: Any, path: Path) -> bool:
    """Reject missing and old snapshots instead of presenting history as live state."""
    stamp = _timestamp(value, path)
    return stamp is not None and datetime.now(timezone.utc).timestamp() - stamp <= MAX_OBSERVATION_AGE


def _observed_at(value: Any, path: Path) -> str:
    stamp = _timestamp(value, path)
    return "未知" if stamp is None else datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def _word(value: Any, true="开启", false="停用") -> str:
    return true if value is True else false if value is False else "未知"


def _runtime_commit(config: dict[str, Any]) -> str:
    worker = config.get("worker")
    if not isinstance(worker, str):
        return "未知"
    value = _load(Path(worker).parent.parent / "build-manifest.json")
    commit = value.get("commit") if isinstance(value, dict) else None
    return commit if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit) else "未知"


def _writer_observation(config: dict[str, Any]) -> tuple[str, str, str, str]:
    """Read validated shared ownership without treating it as local adoption."""
    try:
        value = writer_status(config)
    except (OSError, ValueError, KeyError, TypeError):
        value = {}
    if not isinstance(value, dict) or value.get("status") in ("legacy", "awaiting-handoff-bootstrap"):
        return ("未知", "未知", "未知", "未知")
    owner = value.get("ownerMachineId")
    mode = value.get("mode")
    receipt = value.get("visibleReceiptId")
    if not isinstance(owner, str) or not isinstance(mode, str) or not isinstance(receipt, str):
        return ("未知", "未知", "未知", "未知")
    return (owner, mode, datetime.now(timezone.utc).isoformat(), receipt[:12])


def _count(state: Path | None, name: str) -> str:
    if state is None:
        return "未知"
    path = state / name
    if not path.is_dir():
        return "未知"
    try:
        return str(sum(item.is_file() and item.suffix == ".json" for item in path.iterdir()))
    except OSError:
        return "未知"


def _state_lines(config: dict[str, Any]) -> list[str]:
    state = Path(config["stateDir"]) if isinstance(config.get("stateDir"), str) else None
    maintenance_path = state / "maintenance.json" if state else None
    maintenance = _load(maintenance_path) if maintenance_path else None
    maintenance_text = "未知"
    if isinstance(maintenance, dict) and maintenance_path:
        stamp = _observed_at(maintenance, maintenance_path)
        if _current(maintenance, maintenance_path):
            counts = maintenance.get("counts", {})
            maintenance_text = f"当前@{stamp} review={counts.get('review', '未知')} capture-errors={counts.get('capture-errors', '未知')}"
        elif stamp != "未知":
            maintenance_text = f"历史@{stamp}"
    replica_path = state / "replica/status.json" if state else None
    replica = _load(replica_path) if replica_path else None
    replica_text = "未知"
    if isinstance(replica, dict) and replica_path:
        stamp = _observed_at(replica, replica_path)
        if _current(replica, replica_path):
            replica_text = f"当前@{stamp} records={replica.get('count', '未知')} conflicts={len(replica.get('conflicts', [])) if isinstance(replica.get('conflicts'), list) else '未知'}"
        elif stamp != "未知":
            replica_text = f"历史@{stamp}"
    owner, mode, observed, receipt = _writer_observation(config)
    exchange = config.get("exchange") if isinstance(config.get("exchange"), dict) else {}
    default = exchange.get("materializerMachineId", "未知")
    return [
        f"共享写入：默认={default} 共享目录可见owner={owner} receipt={receipt} 模式={mode} 观测={observed}",
        f"角色：intake={_word(config.get('intakeEnabled'))} publish={_word(config.get('publishEnabled'))} 机器={config.get('machineId', '未知')}",
        f"队列：queue={_count(state, 'queue')} failed={_count(state, 'failed')} review={_count(state, 'review')} capture={_count(state, 'capture-errors')} exchange={_count(state, 'exchange-errors')} replica={_count(state, 'replica-errors')}",
        f"维护观测：{maintenance_text}；副本：{replica_text}",
    ]


def is_operational_query(prompt: str) -> bool:
    """Recognize explicit Wiki operations without reading any machine state."""
    text = str(prompt or "")
    return bool(OPERATIONAL_ANCHOR.search(text) and OPERATIONAL_SIGNAL.search(text))


def operational_context(config: dict[str, Any], prompt: str) -> str:
    """Return at most 1000 Chinese characters for an explicit Wiki ops query."""
    if not is_operational_query(prompt):
        return ""
    exchange = config.get("exchange") if isinstance(config.get("exchange"), dict) else {}
    budget = config.get("maxDailyJobs", "未知")
    lines = [
        f"Wiki运维只读观测：提交={_runtime_commit(config)} 机器={config.get('machineId', '未知')} 日预算={budget}",
        f"交换协议={exchange.get('protocolVersion', '未知')} 参与者={','.join(exchange.get('participants', [])) if isinstance(exchange.get('participants'), list) else '未知'}",
    ]
    lines.extend(_state_lines(config))
    return "\n".join(lines)[:1000]
