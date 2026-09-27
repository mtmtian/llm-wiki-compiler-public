---
title: Incremental Compilation
summary: A technique that only reprocesses changed sources to save time and API costs, tracked through SHA-256 hashes
sources:
  - knowledge-compilation.md
createdAt: "2026-04-06T07:53:55.604Z"
updatedAt: "2026-04-06T07:53:55.604Z"
---

# Incremental Compilation

**Incremental Compilation** is a key optimization technique in [[knowledge compilation]] that processes only changed sources rather than recompiling the entire knowledge base from scratch. This approach significantly reduces processing time and API costs by avoiding redundant work.

## How It Works

Incremental compilation operates through a [[Change Detection]] system that uses **SHA-256 hashes** to identify which sources have been modified since the last compilation run. The system maintains a state file that tracks these hashes, allowing it to skip unchanged sources entirely during subsequent compilation cycles.

The process follows these steps:
1. Compare current source hashes against stored hashes from the previous run
2. Identify sources that have changed, been added, or removed
3. Process only the modified sources through the [[Compilation Pipeline]]
4. Update the state file with new hashes for the next compilation cycle

## Cross-Source Dependencies

When multiple sources contribute to the same concept, incremental compilation becomes more sophisticated. The system implements **semantic dependency tracking** to handle these relationships:

- Changes to one source trigger recompilation of shared concepts
- All contributing sources for affected concepts are included in the recompilation
- This ensures that concept pages remain accurate and complete even when only one source changes

## Benefits

Incremental compilation provides several advantages:
- **Time Efficiency**: Only processing changed content dramatically reduces compilation time
- **Cost Optimization**: Fewer API calls to language models result in lower operational costs
- **Scalability**: Large knowledge bases remain manageable as they grow
- **Responsiveness**: Quick updates enable more frequent compilation cycles

## Implementation

The incremental compilation system integrates seamlessly with the broader [[knowledge compilation]] pipeline. It operates during the **Change Detection** stage, which occurs after **Ingestion** but before **Concept Extraction**. This positioning ensures that the optimization benefits all subsequent stages of the compilation process.

## Sources

knowledge-compilation.md
