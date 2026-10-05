"""Create synthetic Python-projected and sealed records for the real TS reader.

This fixture uses packet validation, projection and sealing without a model,
shared state or topic page. Its output is confined to the supplied temp root.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "extensions/knowledge-flow"))

from common import digest
from ledger_projection import write_reviewed_claims_projection
from replica_integrity import seal_generation
from replica_records import canonical, validate_packet
from test_topic_contract import BASELINE_ID, claim, packet


def main():
    """Validate and seal one reviewed decision, retaining its exact source quote."""
    root = Path(sys.argv[1])
    text = "样例小游戏更新必须保留玩家存档。"
    record = packet(claim(text=text, quote=text, title="存档保留", topic="小游戏存档",
                          decisionObject="更新与存档", useWhen="仅用于样例小游戏更新"))
    record["payload"].update(version=3, projectId="sample-game", projectLabel="Sample Game")
    record["id"] = digest(canonical(record["payload"]))
    validate_packet(record, "a", BASELINE_ID)
    write_reviewed_claims_projection(root, [record], [], root.name)
    (root / ".llmwiki/replica-response.json").write_text("{}\n", encoding="utf-8")
    seal_generation(root, root.name)
    print(record["id"] + ":0")


if __name__ == "__main__":
    main()
