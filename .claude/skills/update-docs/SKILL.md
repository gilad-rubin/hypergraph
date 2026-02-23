---
description: Update the documentation under docs/ and README.md to make sure it's synced with implementation.
user_invocable: true
model: opus
---
## Flow

1. **Detect changes**: Find base branch with `git remote show origin | grep 'HEAD branch'` or fall back to `master`/`main`. Then `git diff <base>...HEAD` + read `.claude/plans/` + use subagents to analyze `src/` directly
2. **Capabilities**: Read `tests/capabilities/` matrix to know what's implemented
3. **Gap analysis**: Compare implemented features vs documented features in `docs/`
4. **Plan**: Write update plan focused on real-world scenarios (RAG, agentic loops, ML pipelines, ETL)
5. **Draft**: Write/update pages in `docs/` (tutorials, guides, concepts) + and project's README.md 
6. **API docs**: Update `docs/api/` from docstrings + code signatures

## Style Rules

- **Follow README.md style**: Read the project's root README.md first. Match its tone, structure, and formatting (bullet points with bold labels, tables for comparisons, concise intros)
- Code-first: every concept needs a runnable example
- Real-world domains only: RAG, LLM streaming, data pipelines — never foo/bar
- Error messages: show exact error + "How to fix:" guidance
- Motivation first: explain "why" before "how"
- Bullet-point intros: each page should start with 3-4 bullets summarizing key points (bold label - description format)
- Minimal comments: only non-obvious lines
- Extract docstrings into API docs
- Maintain cross-references and SUMMARY.md TOC

## Output

Direct file edits to `docs/`. User reviews via `git diff`.

## Learning

Store patterns in `docs/DOC_STYLE.md` (version controlled).

## Out of Scope

`specs/` directory
