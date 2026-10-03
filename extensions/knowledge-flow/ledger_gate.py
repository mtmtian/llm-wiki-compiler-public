"""Shared activation gate for knowledge-ledger records (deployment/KNOWLEDGE-LEDGER.md §7.4).

Ledger records (publication version 3) are refused by readers that predate them,
so none may be produced until every replica participant runs a runtime that
announces ``knowledge-ledger-v1`` and one operator writes the shared policy
``v2/knowledge-ledger.json``. The rules are those of ``capability_gate``; this
gate is independent of semantic topics. Disabling is not supported here: once
records exist, readers must keep reading them.
"""

import capability_gate
from capability_gate import Gate

LEDGER = Gate(name="knowledge ledger", capability="knowledge-ledger-v1",
              policy_path="v2/knowledge-ledger.json", field="knowledgeLedger", value="enabled")
CAPABILITY = LEDGER.capability


def enabled(config):
    """True only when the shared policy exists and is valid; a missing exchange means disabled."""
    if config.get("exchange", {}).get("protocolVersion") != 2:
        return False
    return capability_gate.policy(config, LEDGER) is not None


def require_ready(config):
    """Fail before producing a ledger record if any declared reader is incompatible."""
    return capability_gate.require_ready(config, LEDGER)


def status(config):
    """Report whether the ledger is enabled and which readers block it."""
    return {"knowledgeLedger": "enabled" if enabled(config) else "disabled",
            **capability_gate.readiness(config, LEDGER)}


def activate(config, at, apply=False):
    """Preview or atomically enable ledger records after all reader attestations."""
    return capability_gate.activate(config, LEDGER, at, apply)
