## Workflow

- Use `uv run X` for scripts (ensures consistent dependencies)
- Use `trash X` instead of `rm X` (allows file recovery)
- Commit frequently and autonomously using `Conventional Commits` format. Use this like a "save" button.

## Planning
- When discussing design, use 'outside-in' explanations with concrete, user-facing examples - one good example is worth a thousand words
- If you need to read a lot of content - use subagents (with haiku, sonnet) to summarize or answer questions in order to keep the context window clean
- Read the relevant code snippets and search online (using the tools) before answering

## Tools
- Use context7 to query docs
- Use deepwiki to query github repos
- Use Perplexity to ask questions and perform research and get LLM-powered search results. This saves us tokens and time

## Coding Principles

- Follow SOLID principles. 
- Use simple, readable functions rather than deeply nested ones. 
- Split large functions into focused helpers when needed.

## Tools

Use Context7 and MCP servers to understand unfamiliar libraries.

## Maintaining Instructions

After making significant code structure changes, update the relevant .ruler/ markdown files and run:
```bash
ruler apply --agents cursor,claude
```
This regenerates CLAUDE.md, AGENTS.md and syncs instructions to all configured agents.
