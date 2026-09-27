---
title: Cross-Source Concepts
summary: Concepts that appear in multiple sources, tracked through semantic dependency analysis to trigger recompilation when any contributing source changes
sources:
  - knowledge-compilation.md
createdAt: "2026-04-06T07:53:58.574Z"
updatedAt: "2026-04-06T07:53:58.574Z"
---

# Cross-Source Concepts

Cross-source concepts are concepts that appear and are discussed across multiple source documents in a [[knowledge compilation]] system. When the same concept is mentioned or explained in different sources, the knowledge compiler must detect this overlap and merge information from all contributing sources into a unified concept page.

## How Cross-Source Detection Works

The [[Knowledge Compilation]] system uses **semantic dependency tracking** to identify when multiple sources discuss the same concept. This allows the compiler to recognize that different documents are referring to the same underlying idea, even when they use different terminology or approaches.

When a cross-source concept is detected, the system ensures that the generated wiki page incorporates content from all relevant sources, providing a comprehensive view that spans multiple documents.

## Triggering Recompilation

Cross-source concepts have an important property in the [[Compilation Pipeline]]: **changes to one source can trigger recompilation of shared concepts**. When any source document that contributes to a cross-source concept is modified, the compiler will regenerate the concept page using updated content from all contributing sources.

This ensures that cross-source concept pages remain current and reflect the most recent information from all their source documents.

## Benefits

Cross-source concepts provide several advantages:

- **Comprehensive Coverage**: Information about a concept is gathered from multiple perspectives and sources
- **Reduced Duplication**: Instead of having separate pages for the same concept from different sources, information is consolidated
- **Maintained Consistency**: Updates to any source automatically propagate to shared concept pages

## Relationship to [[Knowledge Compilation]]

Cross-source concepts are a key feature of the [[knowledge compilation]] process, working alongside other pipeline stages like [[concept extraction]], [[incremental compilation]], and interlink resolution to create a cohesive knowledge base from distributed sources.

## Sources

- knowledge-compilation.md
