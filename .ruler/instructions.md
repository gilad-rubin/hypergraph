<project_mode>
This project is in design mode. Only markdown is needed.
Updated design specs are in specs/reviewed - search there when uncertain.
</project_mode>

See @specs/reviewed/README.md for the overview.

## Workflow

- Use `uv run X` for scripts (ensures consistent dependencies)
- Use `trash X` instead of `rm X` (allows file recovery)
- Commit frequently using Conventional Commits format
- When discussing design, use ELI5 (closer to ELI20) explanations with concrete examples - one good example is worth a thousand words
- Run code changes to verify they work before moving on
- Check `.env` for API keys before requesting them; use dotenv to load
- Search documentation before implementing unfamiliar patterns
- Place tests in `tests/`, scripts in `scripts/`
- Provide one summary when finishing a task (not multiple)

## Coding Principles

Follow SOLID principles. Use simple, readable functions rather than deeply nested ones. Split large functions into focused helpers when needed.

## Tools

Use Context7 and MCP servers to understand unfamiliar libraries.

## Jupyter

<jupyter_guidelines>
Keep cells concise and eliminate redundancy. Use only basic emojis (checkmarks, X marks) - special emojis can crash notebooks.

Jupyter has its own async handling - use appropriate syntax. When editing modules, restart the kernel or reload to see changes.

The notebook's working directory is the project root - no sys.path manipulation needed. Run cells after creation to verify output, then iterate by examining output and refining.
</jupyter_guidelines>

## Maintaining Instructions

After making significant code structure changes, update the relevant .ruler/ markdown files and run:
```bash
ruler apply --agents cursor,claude
```
This regenerates CLAUDE.md, AGENTS.md and syncs instructions to all configured agents.
