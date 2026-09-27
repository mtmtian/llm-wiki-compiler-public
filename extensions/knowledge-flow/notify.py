"""Notify the local user of new actionable state without starting a model.

Only counts and generic operational messages leave private state. The saved
fingerprint suppresses repeated alerts while an issue remains unchanged;
failed notification delivery is retained locally for diagnosis and retried.
"""

import json
import platform
import subprocess
from pathlib import Path

from common import digest, load_json, save_json


def actionable_state(config, result):
    """Fingerprint new review, failed, and exchange-error records by identity."""
    state = Path(config["stateDir"])
    records = {name: sorted(path.stem for path in (state / name).glob("*.json"))
               for name in ("review", "failed", "exchange-errors", "capture-errors", "replica-errors")}
    replica = load_json(state / "replica/status.json", {}) or {}
    records["replica-conflicts"] = sorted(json.dumps(item, sort_keys=True) for item in replica.get("conflicts", []))
    records["processing-error"] = "processing-error" in result.get("reasons", [])
    return records


def notify_actionable(config, result, runner=None, system=None):
    """Deliver one native notification for changed actionable state on this Mac."""
    if (system or platform.system()) != "Darwin":
        return {"supported": False, "sent": False}
    state = actionable_state(config, result)
    fingerprint = digest(json.dumps(state, sort_keys=True))
    path = Path(config["stateDir"]) / "notification-state.json"
    previous = load_json(path, {}) or {}
    if previous.get("fingerprint") == fingerprint:
        return {"sent": False, "reason": "unchanged"}
    if not any(state.values()):
        save_json(path, {"fingerprint": fingerprint})
        return {"sent": False, "reason": "no-action"}
    message = (f"待审 {len(state['review'])}，失败 {len(state['failed'])}，"
               f"处理异常 {int(state['processing-error'])}，"
               f"采集异常 {len(state['capture-errors'])}，同步异常 {len(state['exchange-errors']) + len(state['replica-errors'])}，"
               f"知识冲突 {len(state['replica-conflicts'])}。"
               "请查看本机 Wiki 维护状态。")
    script = 'on run argv\ndisplay notification (item 1 of argv) with title "Wiki 维护需要关注"\nend run'
    try:
        response = (runner or subprocess.run)(["/usr/bin/osascript", "-e", script, message],
                                              capture_output=True, text=True, timeout=10, check=False)
        sent = response.returncode == 0
    except (OSError, subprocess.SubprocessError):
        sent = False
    save_json(path, {"fingerprint": fingerprint if sent else previous.get("fingerprint"),
                     "deliveryFailed": not sent})
    return {"sent": sent, "reason": "actionable" if sent else "delivery-failed"}
