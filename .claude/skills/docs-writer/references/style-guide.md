# Documentation Style Guide

Rules for tone, formatting, structure, and quality — extracted from best-in-class Python library documentation.

## Table of Contents

1. [Voice and Tone](#voice-and-tone)
2. [Structure Rules](#structure-rules)
3. [Code Examples](#code-examples)
4. [Text-to-Code Ratio](#text-to-code-ratio)
5. [Explaining Concepts](#explaining-concepts)
6. [Navigation and Linking](#navigation-and-linking)
7. [Formatting](#formatting)
8. [Anti-Patterns](#anti-patterns)
9. [Quality Checklist](#quality-checklist)

---

## Voice and Tone

**Primary voice**: Direct, technical, second-person. Talk to the reader as a peer developer.

**Characteristics**:
- Declarative facts mixed with imperative instructions: "The framework infers edges automatically. Define your nodes like this:"
- Active voice: "The runner executes the graph" not "The graph is executed by the runner"
- Confident but not salesy: State what the library does. Don't oversell.
- Honest about limitations: "This won't work well if you need X"

**Sentence patterns that work well**:
- Task-focused: "Create a file `main.py` with:"
- Outcome-focused: "You'll see the JSON response:"
- Concept intro: "A *node* here means a pure function decorated with `@node`"
- Anticipation: "If you're not sure about X, see [section]"
- Validation: "You just built a pipeline that validates types, infers edges, and runs sync"

**Words to prefer**: "use", "build", "run", "create", "define", "returns", "produces"
**Words to avoid**: "utilize", "leverage", "facilitate", "robust", "seamless", "powerful"

---

## Structure Rules

### Page-Level Structure

Every page follows this skeleton:

1. **Hook** (1-2 sentences) — What this page covers and why it matters
2. **Content sections** — Organized by the page type (see page-templates.md)
3. **Next steps** — 2-4 links to logical follow-up pages

### Section-Level Structure

Within each section, follow this order:

1. **Why** (1-2 sentences) — Why would someone need this?
2. **Code** — Simplest working example
3. **Explanation** — What just happened, line by line if needed
4. **Output** — What the reader sees when they run it
5. **Variations** (optional) — Alternative approaches, edge cases

### Heading Hierarchy

- **H1**: Page title only (one per page)
- **H2**: Major sections (the primary navigation level)
- **H3**: Subsections within a topic (use sparingly)
- **H4+**: Avoid. If you need H4, restructure.

### One Concept Per Section Rule

Each H2 section introduces exactly ONE new idea. If a section teaches both "routing" and "error handling," split it into two sections. The reader should be able to skip any H2 without losing context for the next one.

---

## Code Examples

### Before Every Code Block

Provide 1-2 sentences of context. What is this code doing and why?

**Good**: "Define three nodes. The framework will connect them based on matching output/input names:"
**Bad**: [code block with no introduction]

### After Every Code Block

Show at least one of:
- The **output** when you run it
- A **plain-English explanation** of what happened
- A **"what to notice"** callout highlighting the key insight

### Code Style

```python
# Imports at top, grouped logically
from mylib import Graph, node, SyncRunner

# Comments explain WHY, not WHAT
# Connect embedding to retrieval automatically via name matching
@node(output_name="embedding")
def embed(text: str) -> list[float]:
    return [0.1, 0.2, 0.3]

# Use meaningful names that describe purpose
@node(output_name="docs")
def retrieve(embedding: list[float]) -> list[str]:
    return ["Document 1", "Document 2"]
```

### Progressive Example Pattern

For each major feature, show examples in this order:

1. **Minimal** (5-10 lines) — Core concept, nothing extra
2. **Realistic** (15-30 lines) — Real-world usage with context
3. **Production** (30+ lines, optional) — Full implementation with error handling, logging, etc.

### Code Must Be Runnable

Every code example should be copy-pasteable and runnable. No pseudo-code unless explicitly labeled as such. Include all necessary imports.

---

## Text-to-Code Ratio

The ratio of explanatory text to code depends on complexity:

| Concept Complexity | Text Before Code | Text After Code |
|--------------------|------------------|-----------------|
| Simple (install, run) | 1 sentence | 1 sentence |
| Medium (new API) | 2-3 sentences | 2-4 sentences |
| Complex (architecture) | 1 short paragraph | 1 paragraph + output |

**Rule of thumb**: If you have more than 5 sentences before a code block, you're over-explaining. Show the code and explain after.

---

## Explaining Concepts

### Define Jargon Inline

On first use of a term, define it in context:

**Good**: "A *node* — a pure Python function decorated with `@node` — produces a named output that other nodes can consume."

**Bad**: "Nodes are computational units in the graph execution framework that process inputs and produce outputs within the directed acyclic graph structure."

### Use Contrast to Clarify

Compare the new concept to something familiar or to how other tools handle it:

"Unlike frameworks where you manually wire edges, this library infers edges when an output name matches an input parameter name."

### Answer Objections Proactively

Anticipate "but why?" questions:

"Why a custom `echo()` instead of `print()`? Because `echo()` handles encoding and output stream differences across platforms consistently."

### The Problem-Solution Frame

For features that solve a non-obvious problem, state the problem first:

**Good**:
> "When your graph grows beyond 10 nodes, testing the full pipeline becomes slow and brittle. Hierarchical composition solves this — nest a tested sub-graph as a single node in the outer graph."

**Bad**:
> "The framework supports hierarchical composition. You can nest graphs as nodes."

---

## Navigation and Linking

### Cross-References

Place links where the reader needs them, not in a separate "Related" section:

**Good**: "For conditional branching, see [Routing](routing.md)."
**Bad**: A "See Also" section at the bottom with 10 links.

### Next Steps Section

Every page ends with 2-4 suggested next pages. Format as a decision tree when possible:

```markdown
## Next Steps

- **New here?** [Core Concepts](core-concepts.md)
- **Ready to build?** [Quick Start](quick-start.md)
- **Evaluating?** [When to Use](when-to-use.md) or [Comparison](comparison.md)
```

### Breadcrumb Context

At the top of tutorial or pattern pages, remind the reader what they should already know:

"This page assumes you've completed the [Quick Start](quick-start.md) and understand [nodes and graphs](core-concepts.md)."

---

## Formatting

### Callout Boxes

Use sparingly for:
- **Note**: Extra context that isn't essential
- **Tip**: Shortcuts or best practices
- **Warning**: Things that will break or cause confusion

### Tables

Use tables for decision matrices and comparisons:

```markdown
| If you want... | Use... |
|----------------|--------|
| Binary branching | `@ifelse` |
| Multiple targets | `@route` |
| Early exit | `END` |
```

### Inline Code

Use backtick formatting for:
- Function names: `embed()`
- Parameters: `output_name`
- File names: `main.py`
- Terminal commands: `pip install mylib`
- Values: `True`, `None`, `"embedding"`

### Bold

Use bold for:
- Key concept names on first introduction
- Decision labels in "pick your path" sections
- Section labels within a list

Do NOT use bold for emphasis in running prose. If a sentence needs emphasis, rewrite it to be clearer.

---

## Anti-Patterns

Things to actively avoid:

### 1. Wall of Text Before Code
**Bad**: Three paragraphs explaining what a function does, then the function.
**Good**: One sentence, then the function, then explain what happened.

### 2. Unexplained Code
**Bad**: A code block with no introduction or follow-up.
**Good**: "Define your nodes:" → code → "The `@node` decorator registers the function and names its output."

### 3. Abstract-First Ordering
**Bad**: "The Directed Acyclic Graph execution model uses a topological sort..." (on a getting started page)
**Good**: Show it working first, explain the model in Core Concepts.

### 4. Jargon Without Definition
**Bad**: "The orchestrator compiles the state graph into a runnable pregel instance."
**Good**: "The runner takes your graph and executes each node in dependency order."

### 5. Feature Lists Without Context
**Bad**: "The library supports streaming, caching, batching, HITL, async, sync, generators, composition, nesting, batch processing, map operations, events, validation, and more."
**Good**: Show each feature solving a real problem in its own section.

### 6. No Output Shown
**Bad**: Code that produces something, but the reader has to run it to see what.
**Good**: Show the expected output or result after every significant code block.

### 7. Passive Voice
**Bad**: "The graph is compiled and the nodes are executed in order."
**Good**: "The runner compiles the graph and executes nodes in dependency order."

### 8. Apologetic Tone
**Bad**: "This is a somewhat complex feature that may take some time to understand..."
**Good**: Just explain it clearly. Trust the reader.

---

## Revision and Polish

### Every Sentence Must Carry Weight

After drafting, re-read and ask of each sentence: "Does this teach something the reader doesn't already know?" Remove filler, hedging, and generic phrasing that adds words without adding information.

### The 80% Review

When ~80% of a document is drafted, stop and re-read the entire thing end-to-end. Check for:
- **Flow across sections** — Does the reading order make sense? Do transitions work?
- **Redundancy** — Are you explaining the same thing twice in different sections?
- **Contradictions** — Does section A promise something section B contradicts?
- **Filler** — Sentences that feel like "slop" or generic padding

### Use Appendices for Depth

Keep the main document focused on the primary audience. Move detailed reference material, extended examples, and edge cases into appendices or separate pages. This prevents bloating the core narrative while still making depth available.

### Start With the Hardest Section

When writing a multi-section document, don't start with the introduction. Start with the section that has the most unknowns — usually the core technical content. Save summary/intro sections for last, once you know what you're summarizing.

---

## Quality Checklist

Before publishing any documentation page, verify:

- [ ] **Hook exists**: First 1-2 sentences explain what this page covers and why it matters
- [ ] **Code runs**: Every code example is copy-pasteable and produces the shown output
- [ ] **Output shown**: Every significant code block shows its result
- [ ] **One concept per section**: No section teaches two unrelated things
- [ ] **Progressive complexity**: Simplest example comes first
- [ ] **Jargon defined**: Every technical term is defined on first use
- [ ] **Why before how**: Each feature section explains why you'd want this before showing how
- [ ] **Next steps exist**: Page ends with 2-4 links to logical next pages
- [ ] **No walls of text**: No more than 5 sentences before a code block
- [ ] **Meaningful names**: Code uses descriptive variable/function names
- [ ] **Active voice**: Prose uses active voice throughout
- [ ] **Links in context**: Cross-references appear where the reader needs them
- [ ] **Honest about limits**: Trade-offs and "when not to use" are mentioned
- [ ] **Prerequisites stated**: Reader knows what they should already understand
- [ ] **No filler**: Every sentence carries weight — remove hedging and generic padding
- [ ] **Cross-section coherence**: No redundancy or contradictions between sections
