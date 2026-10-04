"""Recognize complete Codex ambient templates without inferring human intent.

The host currently exposes no verified task-kind field at this hook boundary.
Only two fixed templates qualify, including their final instruction. Partial
templates, quoted templates and turns with another current user remain normal
evidence. A person submitting an identical template cannot be distinguished.
"""

SUGGESTION_START = "# Overview"
SUGGESTION_PARTS = (
    "Get an understanding of the user's intent and goals by deeply viewing their connected apps. "
    "Suggest actionable tasks that they would actually act on/click.",
    "\n# Rules\n", "Return 0 to 3 fresh suggestions.", "\n# Examples\n", "\n# Response format\n",
    "- prompt: the user message to send", "- pluginId: null.",
)
SUGGESTION_END = "- write the prompt as something that should launch as a new Codex task in this project"
SAFETY_START = "You are an expert at upholding safety and compliance standards for Codex ambient suggestions."
SAFETY_PARTS = (
    "Your task is to determine if any suggestions should be excluded in order to adhere to the safety and compliance policies.",
    "\n## 1. Policies to always exclude\n",
    "\n## 2. Categories **about the user** to exclude **unless the user has specifically asked for it in recent context**\n",
    "\n# Ambient suggestion candidates\n", "\n# Output Format\n", "Return a JSON object with one field:", "`exclude`",
)
SAFETY_END = "You must not output any other text. Only output the JSON object."


def _matches(text, start, parts, end):
    """Require both boundaries and every ordered fixed instruction."""
    if not text.startswith(start + "\n") or not text.endswith(end):
        return False
    position = len(start)
    for part in parts:
        found = text.find(part, position)
        if found < 0:
            return False
        position = found + len(part)
    return position <= len(text) - len(end)


def prompt_automation_reason(session_id, prompt):
    """Classify the original, untruncated prompt only at the Codex boundary."""
    if not session_id or str(session_id).startswith(("claude:", "pi:")) or not isinstance(prompt, str):
        return ""
    text = prompt.replace("\r\n", "\n").strip()
    if _matches(text, SUGGESTION_START, SUGGESTION_PARTS, SUGGESTION_END):
        return "host-ambient-suggestions"
    if _matches(text, SAFETY_START, SAFETY_PARTS, SAFETY_END):
        return "host-ambient-safety"
    return ""


def _codex_user(evidence):
    """Require one current user and a Codex locator, excluding other host adapters."""
    users = [item for item in evidence if isinstance(item, dict) and item.get("kind") == "user"
             and item.get("current") is not False and not item.get("historical")]
    return users[0] if len(users) == 1 and str(users[0].get("locator", "")).startswith("codex://") else None


def job_automation_reason(job):
    """Also protect explicit requeue when complete old evidence identifies a template."""
    user = _codex_user(job.get("evidence", []))
    return prompt_automation_reason(job.get("sessionId"), user.get("text")) if user else ""


def capture_automation_reason(event, record, captured):
    """Prefer complete evidence; a hook fallback may reuse its untruncated prompt verdict."""
    user = _codex_user(captured.get("evidence", []))
    if captured.get("status") != "ok" or not user:
        return ""
    reason = prompt_automation_reason(event.get("session_id"), user.get("text"))
    if reason or captured.get("source") != "hook-fallback":
        return reason
    recorded = record.get("hostAutomationReason", "")
    if recorded not in {"host-ambient-suggestions", "host-ambient-safety"}:
        return ""
    if user.get("text") != str(record.get("prompt", "")).strip():
        return ""
    if event.get("prompt") and prompt_automation_reason(event.get("session_id"), event["prompt"]) != recorded:
        return ""
    return recorded
