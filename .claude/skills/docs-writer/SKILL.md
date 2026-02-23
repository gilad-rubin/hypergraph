---
name: docs-writer
description: |
  Technical documentation writer for Python libraries and frameworks. Produces clear, well-structured documentation following patterns extracted from best-in-class open source projects.
  - MANDATORY TRIGGERS: documentation, docs, write docs, doc page, getting started page, introduction page, tutorial, how-to guide, API reference, quickstart, README, explain this library, write a guide, improve docs, rewrite documentation
  - Use when creating or improving documentation for any Python library, framework, tool, or API
  - Use when rewriting existing docs to be clearer, better structured, or more developer-friendly
  - Use when creating any of: landing pages, concept explanations, tutorials, how-to guides, API references, pattern cookbooks, comparison pages, "when to use" pages
---

# Documentation Writer

Write technical documentation for Python libraries following patterns extracted from best-in-class open source projects.

## Workflow

1. **Understand the library** — Read source code, existing docs, or user description. Identify core abstractions, target audience, and key differentiators
2. **Identify page type** — Determine which page type to write (see Page Types below)
3. **Consult references** — Read the appropriate reference file:
   - `references/style-guide.md` for tone, formatting, and anti-patterns
   - `references/page-templates.md` for the structure matching the page type
   - `references/examples.md` for annotated before/after samples
4. **Write the page** — Follow the template, adapting as needed
5. **Self-review** — Check against the quality checklist in `references/style-guide.md`

## Page Types

Documentation sites need these page types:

| Page Type | Purpose | Reader Asks |
|-----------|---------|-------------|
| **Landing/Intro** | Value prop + quick demo | "What is this? Should I care?" |
| **Getting Started** | Install + first working example | "How do I try this right now?" |
| **Core Concepts** | Mental model, key abstractions | "How does this thing think?" |
| **When to Use** | Decision guide, honest trade-offs | "Is this right for me?" |
| **Tutorial** | Guided build of a real thing | "Walk me through building X" |
| **Pattern/How-to** | Solve a specific problem | "How do I do X?" |
| **API Reference** | Exhaustive parameter/method docs | "What are the exact args?" |
| **Comparison** | vs. alternatives | "How does this compare to Z?" |

## Core Rules

These rules apply to ALL page types:

1. **Show before telling** — Working code before conceptual explanation
2. **One concept per section** — Each heading introduces exactly one idea
3. **Progressive complexity** — Simplest example first, then layer complexity
4. **Always show output** — After code blocks, show what running it produces
5. **Answer "why" early** — Why this matters before how it works
6. **Honest trade-offs** — "When NOT to use" alongside "when to use"
7. **Verify at each step** — Tell the reader how to confirm it worked
8. **Meaningful names** — `call_model`, `retrieve_docs` not `func1`, `step2`
9. **Define jargon inline** — First use of a term gets a plain-English gloss
10. **Link naturally** — Cross-references where the reader needs them, not in a "See Also" dump

## Collaborative Writing (Optional)

When co-authoring docs with a user, follow this 3-stage workflow:

### Stage 1: Context Gathering

Close the gap between what you know and what the user knows before writing anything.

1. Ask meta-context: doc type, audience, desired impact, template/format constraints
2. Let the user dump all context (stream-of-consciousness, links, files — however works for them)
3. Ask 5-10 numbered clarifying questions based on gaps. Let them answer in shorthand ("1: yes, 2: no because backwards compat, 3: see the design doc")
4. Exit when your questions show understanding — you can ask about edge cases and trade-offs without needing basics explained

### Stage 2: Section-by-Section Refinement

Build each section through brainstorming → curation → drafting → surgical edits.

1. Start with the section that has the **most unknowns** (not the introduction — save summary sections for last)
2. For each section:
   - Ask 5-10 clarifying questions about what to include
   - Brainstorm 5-20 candidate points (look for context the user shared but may have forgotten)
   - User curates: "Keep 1,4,7 — remove 3 (duplicates 1) — combine 11 and 12"
   - Draft the section, then refine through surgical edits
3. Ask the user to describe changes rather than editing directly — this teaches you their style for later sections
4. After 3 iterations with no substantial changes, ask what can be **removed** without losing value
5. At 80% completion, re-read the entire document and check for: flow across sections, redundancy, contradictions, and filler that doesn't carry weight

### Stage 3: Reader Testing

Test the doc with a fresh agent (no context from the writing session) to catch blind spots.

1. Predict 5-10 questions a reader would realistically ask
2. Have a separate agent answer those questions using only the document
3. Identify where the fresh agent gets confused, gives wrong answers, or surfaces gaps
4. Fix those sections, then re-test until the doc works standalone

## Reference Files

- **Full style guide**: See [references/style-guide.md](references/style-guide.md) — tone, formatting, text-to-code ratio, anti-patterns, quality checklist
- **Page templates**: See [references/page-templates.md](references/page-templates.md) — concrete structure for each page type
- **Annotated examples**: See [references/examples.md](references/examples.md) — before/after rewrites with commentary
