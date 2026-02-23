# Page Templates

Concrete structures for each documentation page type. Adapt sections as needed — these are starting frameworks, not rigid formats.

## Table of Contents

1. [Landing / Introduction Page](#landing--introduction-page)
2. [Getting Started / Quick Start](#getting-started--quick-start)
3. [Core Concepts](#core-concepts)
4. [When to Use](#when-to-use)
5. [Tutorial Page](#tutorial-page)
6. [Pattern / How-to Page](#pattern--how-to-page)
7. [API Reference Page](#api-reference-page)
8. [Comparison Page](#comparison-page)

---

## Landing / Introduction Page

**Goal**: In 30 seconds, the reader should know what this library does, see it working, and decide whether to keep reading.

```markdown
# {Library Name}

{One sentence: what it is and what problem it solves.}

{2-4 value propositions as short labeled items — each is a word/phrase + one sentence.}
- **{Label}** - {Benefit in one sentence.}
- **{Label}** - {Benefit in one sentence.}
- **{Label}** - {Benefit in one sentence.}

## Quick Start

{One sentence setup: "Define X, do Y, and the library does Z automatically."}

```python
{Minimal working example — 10-20 lines, copy-pasteable}
```

{1-2 sentences explaining what just happened.}

## Why {Library Name}?

{2-3 subsections, each showing a specific benefit with a SHORT code example.}

### {Benefit 1}

```python
{3-8 line code example demonstrating this benefit}
```

{1 sentence explaining what this shows.}

### {Benefit 2}

```python
{3-8 line code example}
```

{1 sentence explanation.}

## Documentation

### Getting Started
- [{Page}]({link}) - {One-line description}
- [{Page}]({link}) - {One-line description}

### Core Concepts
- [{Page}]({link}) - {One-line description}

### Patterns
- [{Page}]({link}) - {One-line description}
- [{Page}]({link}) - {One-line description}

### Examples
- [{Page}]({link}) - {One-line description}

## Design Principles

{Short numbered list of 4-6 principles — each is a bold phrase + one sentence.}

1. **{Principle}** - {One sentence.}
2. **{Principle}** - {One sentence.}

## Beyond {Primary Use Case}

{Short paragraph noting that the library is general-purpose if applicable.}
```

**Key techniques from best docs**:
- Open with social proof or credibility numbers when available
- Show code within 30 seconds of the page loading
- End with a "you just built X" recap after the first example

---

## Getting Started / Quick Start

**Goal**: Reader goes from zero to running code in under 5 minutes.

```markdown
# Quick Start

{One sentence: what you'll build in this guide.}

## Install

```bash
pip install {library}
```

## Your First {Core Concept}

### Step 1: {Action}

{1-2 sentences explaining what this step does.}

```python
{Code for step 1}
```

### Step 2: {Action}

{1-2 sentences.}

```python
{Code for step 2}
```

### Step 3: {Run It}

```python
{Execution code}
```

Output:
```
{Expected output}
```

## Complete Example

{Full working example combining all steps — copy-paste and run.}

```python
{15-30 line complete example}
```

## How It Works

{2-3 sentences explaining the key mechanism. This is the "aha" moment.}

{Optional: ASCII diagram or flow visualization.}

## Next Steps

- **Learn the concepts**: [{Core Concepts}]({link})
- **See patterns**: [{Patterns}]({link})
- **Try a real example**: [{Example}]({link})
```

**Key techniques**:
- "Create a file main.py" → run → explain each piece → recap
- Consider starting WITHOUT the library (plain Python), then show what you gain by adding it
- Every step should be verifiable — show expected output

---

## Core Concepts

**Goal**: Build the reader's mental model of how the library thinks. After reading, they should be able to predict how new features work.

```markdown
# Core Concepts

{1-2 sentences: the core mental model in plain English.}

{Overview of what this page covers — 3-4 bullet points.}

## {Concept 1: The Most Fundamental Abstraction}

{What it is — 1-2 sentences with inline jargon definition.}

```python
{Simplest example of this concept}
```

{What just happened — explain the key behavior.}

### {Sub-concept 1a}

{Explanation + example.}

### {Sub-concept 1b}

{Explanation + example.}

## {Concept 2: The Next Abstraction Layer}

{How it builds on Concept 1.}

```python
{Example showing Concept 2 using Concept 1}
```

### Properties

| Property | Type | Description |
|----------|------|-------------|
| `{prop}` | `{type}` | {What it does} |

## {Concept 3: Execution / Runtime}

{How concepts 1 and 2 come together at runtime.}

```python
{Example showing the full flow}
```

Output:
```
{What happens when you run it}
```

## Common Patterns

{2-3 short patterns the reader will use repeatedly.}

## Next Steps

- **Apply these concepts**: [{Quick Start}]({link})
- **See advanced patterns**: [{Patterns}]({link})
```

**Key techniques**:
- Identify 3-5 core abstractions and layer them progressively
- Each concept builds on the previous one
- End with "common patterns" so the reader sees how the pieces fit together

---

## When to Use

**Goal**: Help the reader decide honestly whether this library fits their needs.

```markdown
# When to Use {Library}

## Use {Library} When...

### {Scenario 1}

{2-3 sentences describing the situation + why the library helps.}

```python
{Short code example showing the library solving this}
```

### {Scenario 2}

{2-3 sentences + example.}

## Don't Use {Library} When...

### {Anti-scenario 1}

{2-3 sentences explaining why the library isn't the right fit here. Suggest alternatives.}

### {Anti-scenario 2}

{Honest limitation + what to use instead.}

## Summary

| If you want... | Use {Library}? | Instead consider... |
|----------------|---------------|---------------------|
| {Use case 1} | Yes | — |
| {Use case 2} | Yes | — |
| {Anti-case 1} | No | {Alternative} |
| {Anti-case 2} | No | {Alternative} |
```

**Key techniques**:
- Include concrete "don't use when" scenarios — honesty builds trust
- Use a summary table for quick scanning
- Suggest specific alternatives, don't just say "other tools"

---

## Tutorial Page

**Goal**: Walk the reader through building something real, step by step. They should have a working project at the end.

```markdown
# Tutorial: {What You'll Build}

In this tutorial, you'll build {concrete thing}. By the end, you'll
understand {concept 1}, {concept 2}, and {concept 3}.

**Prerequisites**: [{Quick Start}]({link}), [{Core Concepts}]({link})

## What You'll Build

{2-3 sentences describing the end result. What will it do?}

## Step 1: {First Action}

{1-2 sentences of context.}

```python
{Code}
```

{Verify}: Run this and you should see:
```
{Output}
```

## Step 2: {Build On Step 1}

{1-2 sentences introducing the new concept.}

```python
{Code that extends Step 1}
```

{Verify}: {How to confirm it worked.}

## Step 3: {Add the Key Feature}

{Why you need this + what it does.}

```python
{Code}
```

## Complete Code

Here's everything together:

```python
{Full working code — all steps combined}
```

## Recap

You just built {thing} that:
- {Capability 1}
- {Capability 2}
- {Capability 3}

## Next Steps

- **Extend this**: [{Related pattern}]({link})
- **Go deeper**: [{Advanced topic}]({link})
```

**Key techniques**:
- Each step covers one new concept, includes verification
- Include a "recap" at the end listing what the reader gained
- Consider visualizing the structure after each step (diagram, ASCII art)

---

## Pattern / How-to Page

**Goal**: Teach one specific pattern. The reader has a problem; this page gives them the solution.

```markdown
# {Pattern Name}

{1-2 sentences: what problem this pattern solves.}

## When to Use

{Table or bullet list of scenarios where this pattern applies.}

| Scenario | Example |
|----------|---------|
| {When} | {Concrete example} |
| {When} | {Concrete example} |

## Basic Pattern

```python
{Minimal example — the simplest version of this pattern}
```

{Explanation of what each key piece does.}

## {Variation 1: Common Real-World Usage}

{1-2 sentences of context.}

```python
{More complete example}
```

Output:
```
{What you see}
```

## {Variation 2: Advanced Usage}

{When you'd need this variation.}

```python
{Advanced example}
```

## Patterns and Best Practices

- {Do this} because {reason}
- {Avoid this} because {consequence}

## Next Steps

- [{Related pattern}]({link})
- [{Example using this pattern}]({link})
```

**Key techniques**:
- Open with the problem, not the API
- Use a "when to use" decision table
- Show progressively complex variations (basic → realistic → production)

---

## API Reference Page

**Goal**: Exhaustive, searchable documentation of every parameter, method, and return type.

```markdown
# {Class/Function Name}

{1 sentence: what this does.}

## Constructor / Signature

```python
{Full signature with type hints}
```

## Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `{param}` | `{type}` | `{default}` | {What it does} |

## Methods

### `.{method_name}()`

{1 sentence: what this method does.}

```python
{Usage example}
```

**Parameters**: {inline or table}
**Returns**: `{type}` — {description}

## Properties

| Property | Type | Description |
|----------|------|-------------|
| `{prop}` | `{type}` | {What it gives you} |

## Examples

```python
{Complete usage example}
```

## See Also

- [{Related class}]({link})
- [{Pattern that uses this}]({link})
```

---

## Comparison Page

**Goal**: Help the reader understand how this library differs from alternatives. Be fair and specific.

```markdown
# {Library} vs. {Alternative}

{1-2 sentences: the key philosophical difference.}

## At a Glance

| Feature | {Library} | {Alternative} |
|---------|-----------|---------------|
| {Feature 1} | {How library does it} | {How alternative does it} |
| {Feature 2} | {How} | {How} |

## {Key Difference 1}

{Library}:
```python
{How you'd do this in the library}
```

{Alternative}:
```python
{How you'd do this in the alternative}
```

{1-2 sentences explaining the trade-off.}

## When to Choose {Library}

{2-3 scenarios where the library is a better fit.}

## When to Choose {Alternative}

{2-3 scenarios where the alternative is better. Be honest.}
```

**Key techniques**:
- Show the same task implemented in both libraries (side-by-side code)
- Be genuinely fair — recommend the alternative when it's the better fit
- Use an "at a glance" comparison table for quick scanning
