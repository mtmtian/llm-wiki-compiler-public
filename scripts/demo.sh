#!/bin/bash
# llmwiki demo — run a full compilation pipeline on a sample source.
# Requires: ANTHROPIC_API_KEY set, llmwiki built (npm run build).
set -e

DEMO_DIR=$(mktemp -d)
echo "Working in $DEMO_DIR"
cd "$DEMO_DIR"

# Ingest the sample source
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Ingesting ==="
llmwiki ingest "$SCRIPT_DIR/../test/fixtures/sample-source.md"

echo ""
echo "=== Compiling ==="
llmwiki compile

echo ""
echo "=== Querying ==="
llmwiki query "What is knowledge compilation and how does it work?"

echo ""
echo "=== Output structure ==="
find wiki -type f | sort

echo ""
echo "Done. Wiki output is in $DEMO_DIR/wiki/"
