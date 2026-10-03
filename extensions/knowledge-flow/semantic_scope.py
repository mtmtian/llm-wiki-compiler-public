"""Activate shared semantic topics only after every replica can read them.

Source project admission stays unchanged. The shared policy selects the page
organization contract, while runtime manifests and peer announcements attest
reader compatibility before the first semantic publication is accepted. The
attestation and activation rules are shared with other gates in
``capability_gate``.
"""

import capability_gate
from capability_gate import Gate

SEMANTIC = Gate(name="semantic topics", capability="semantic-topic-revisions-v1",
                policy_path="v2/topic-scope.json", field="topicScope", value="semantic")
CAPABILITY = SEMANTIC.capability
POLICY_PATH = SEMANTIC.policy_path


def readiness(config):
    """Report all semantic-reader blockers without treating a missing peer as upgraded."""
    return capability_gate.readiness(config, SEMANTIC)


def require_ready(config):
    """Fail before model work or publication if any declared reader is incompatible."""
    return capability_gate.require_ready(config, SEMANTIC)


def _policy(config):
    """Validate the shared semantic activation contract."""
    return capability_gate.policy(config, SEMANTIC)


def status(config):
    """Report the effective page scope and reader readiness for operators."""
    return {"topicScope": config.get("topicScope", "project"), **readiness(config)}


def activate(config, at, apply=False):
    """Preview or atomically enable semantic topics after all reader attestations."""
    return capability_gate.activate(config, SEMANTIC, at, apply)


def apply_scope(config):
    """Derive effective scope solely from the shared policy, not a local toggle."""
    result = {key: value for key, value in config.items() if key != "topicScope"}
    if config.get("exchange", {}).get("protocolVersion") == 2 and _policy(config) is not None:
        result["topicScope"] = "semantic"
    return result


def require_semantic_job(config, job):
    """Keep a queued job's contract frozen and verify activation before processing."""
    if "topicScope" not in job:
        return
    if job["topicScope"] != "semantic" or apply_scope(config).get("topicScope") != "semantic":
        raise ValueError("semantic job requires shared topic activation")
    require_ready(config)


def require_publication(config, job, result):
    """Reject semantic revisions injected through a legacy or unactivated job."""
    revisions = result.get("contribution", {}).get("topicRevisions", [])
    if any(isinstance(revision, dict) and "topicScope" in revision for revision in revisions):
        if job.get("topicScope") != "semantic":
            raise ValueError("semantic revision requires a semantic job")
    if job.get("topicScope") == "semantic" and (not revisions or any(
            not isinstance(revision, dict) or revision.get("topicScope") != "semantic" for revision in revisions)):
        raise ValueError("semantic job requires semantic revisions")
    require_semantic_job(config, job)
