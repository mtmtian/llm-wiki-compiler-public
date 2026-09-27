---
title: Wikilinks
summary: A linking format using double brackets to connect related concepts across wiki pages, compatible with tools like Obsidian
sources:
  - knowledge-compilation.md
createdAt: "2026-04-06T07:54:06.957Z"
updatedAt: "2026-04-06T07:54:06.957Z"
---

# Wikilinks

**Wikilinks** are a markup syntax used to create internal links between pages in a wiki system. They are enclosed in double square brackets, such as `[[concept name]]`, and enable easy cross-referencing between related topics within a knowledge base.

## Syntax and Format

Wikilinks use the double bracket notation: `[[target page]]`. This simple syntax allows users to quickly link to other concepts without needing complex URL structures or file paths. The linked text typically matches the title of the target page, creating intuitive navigation pathways.

## Role in [[Knowledge Compilation]]

Within the [[knowledge compilation]] process, wikilinks serve a crucial function in the **Interlink Resolution** stage. After individual concept pages are generated, the system automatically identifies concept mentions across pages and wraps them in wikilinks. This creates a web of interconnected knowledge where related concepts are properly linked together.

The [[incremental compilation]] system also considers wikilinks when tracking dependencies between sources. When multiple sources contribute to concepts that are linked together, changes to one source may trigger updates to related pages to maintain consistency across the knowledge graph.

## Obsidian Compatibility

The [[knowledge compilation]] output format specifically uses wikilinks to ensure compatibility with Obsidian and similar knowledge management tools. This standardized approach allows the compiled wiki to be directly imported and used within these platforms, preserving all internal links and relationships.

## Benefits

Wikilinks provide several advantages in knowledge management:

- **Easy Navigation**: Users can quickly jump between related concepts
- **Discoverability**: Related information becomes more accessible through linked connections
- **Semantic Structure**: The link network reveals relationships between different topics
- **Low Friction**: Simple syntax encourages frequent cross-referencing

## Implementation in Generated Pages

During the **Page Generation** stage of [[Knowledge Compilation]], wikilinks are embedded directly into the content as concepts are mentioned. The system recognizes when text refers to other concepts in the knowledge base and automatically creates the appropriate wikilink markup.

## Sources

knowledge-compilation.md
