# OpenClaw Implementation Design for Hypergraph

This document outlines the proposed architecture for implementing a multi-agent OpenClaw setup within the `hypergraph` repository, based on the principles from the "One-Person Dev Team" article.

---

## 1. Guiding Principles

- **Leverage, Don't Replace:** The existing `.claude` directory and skills provide a strong foundation. This design aims to adapt and enhance that structure for OpenClaw, not discard it.
- **Two-Tier Architecture:** A clear separation between a high-level orchestrator and specialized, task-oriented agents.
- **Configuration as Code:** The entire setup will be defined in version-controlled files within the repository.
- **Incremental Adoption:** The setup will be self-contained within a new `.openclaw` directory, allowing for a gradual transition from the old system.

---

## 2. Directory Structure

A new `.openclaw` directory will be created in the project root to house the entire configuration.

```
.openclaw/
├── openclaw.json       # Main configuration file for agents, bindings, and settings
├── workspace/            # Central workspace for the orchestrator agent
│   ├── AGENTS.md         # Main context file (symlinked from root AGENTS.md)
│   ├── MEMORY.md         # Long-term memory for the orchestrator
│   └── memory/           # Directory for daily, append-only memory logs
└── skills/               # Adapted and new skills for the agent swarm
    ├── orchestrate-feature/ # New skill for the main development workflow
    │   └── SKILL.md
    ├── run-review/          # New skill to trigger the multi-agent review pipeline
    │   └── SKILL.md
    └── ... (adapted versions of existing skills)
```

---

## 3. Agent Definitions (`openclaw.json`)

We will define four primary agents, each with a specific role and model choice.

| Agent ID | Name | Model | Role & Responsibilities |
|---|---|---|---|
| `orchestrator` | Zoe | `anthropic/claude-opus-4-6` | **Chief of Staff.** The main entry point. Parses user requests, creates high-level plans, and delegates tasks to other agents. Holds the business context. |
| `planner` | Claude-Planner | `anthropic/claude-sonnet-4-5` | **Strategist.** Takes a feature request from the orchestrator and breaks it down into a detailed, step-by-step implementation plan with a TDD spec. |
| `coder` | Codex-Agent | `openai/gpt-5.3-codex-xhigh` | **Implementer.** The workhorse. Writes and fixes code based on the plan from the planner. Optimized for code correctness. |
| `reviewer` | Gemini-Reviewer | `google/gemini-2.5-pro` | **Quality Assurance.** Performs deep code reviews, focusing on security, scalability, and adherence to best practices. |

**Configuration Snippet (`openclaw.json`):**

```json5
{
  "agents": {
    "defaults": {
      "workspace": "~/.openclaw/workspaces/default"
    },
    "list": [
      {
        "id": "orchestrator",
        "name": "Zoe",
        "model": "anthropic/claude-opus-4-6",
        "workspace": "./.openclaw/workspace",
        "tools": { "allow": ["group:core", "group:plugins"] } // Full access
      },
      {
        "id": "planner",
        "name": "Claude-Planner",
        "model": "anthropic/claude-sonnet-4-5",
        "tools": { "allow": ["read", "memory_search"] }
      },
      {
        "id": "coder",
        "name": "Codex-Agent",
        "model": "openai/gpt-5.3-codex-xhigh",
        "tools": { "allow": ["read", "write", "edit", "exec", "git_diff", "git_commit"] }
      },
      {
        "id": "reviewer",
        "name": "Gemini-Reviewer",
        "model": "google/gemini-2.5-pro",
        "tools": { "allow": ["read", "memory_search", "github_pr_comments"] }
      }
    ]
  },
  "bindings": [
    // Route all initial interactions to the orchestrator
    { "agentId": "orchestrator", "match": {} }
  ]
}
```

---

## 4. Skills & Workflows

The existing skills in `.claude/skills/` will be adapted to the OpenClaw format and placed in `.openclaw/skills/`. We will introduce a new primary workflow.

### New Skill: `/orchestrate-feature`

This skill will be the main entry point for new feature development, executed by the `orchestrator` agent. It codifies the multi-agent workflow:

1.  **Delegate to Planner:** The orchestrator will invoke the `planner` agent with the feature description. The planner's output will be a detailed `PLAN.md` file, including a TDD specification.
2.  **Delegate to Coder:** The orchestrator will then invoke the `coder` agent, providing it with the `PLAN.md`. The coder will implement the feature, writing failing tests first and committing its progress incrementally.
3.  **Delegate to Reviewer:** Once the implementation is complete, the orchestrator will trigger the `reviewer` agent to perform a code review on the diff.
4.  **Iterate or Proceed:** If the reviewer finds issues, the feedback is passed back to the `coder` for fixes. If approved, the orchestrator proceeds to create a pull request.

This workflow replaces the more complex, team-based simulation in the old `/feature` skill with a simpler, more direct delegation model that aligns with OpenClaw's agent-binding capabilities.

### Adapting Existing Skills

-   `/review-pr`: This skill will be simplified. Instead of managing a complex multi-source polling loop, it will be a straightforward trigger for the `reviewer` agent.
-   `/code-smells`, `/update-docs`: These will be adapted as standalone skills that can be called by the orchestrator as needed.

---

## 5. CI/CD Integration

We will introduce a new GitHub Actions workflow, `openclaw-ci.yml`, to integrate the agent swarm with the existing CI pipeline.

**Workflow Trigger:**

The workflow can be triggered manually or automatically on new issue creation with a specific label (e.g., `autobuild`).

**Workflow Steps:**

1.  **Checkout & Setup:** Checks out the repository and installs OpenClaw and its dependencies.
2.  **Run Orchestrator:** Invokes the OpenClaw CLI to run the `orchestrator` agent with a prompt to execute the `/orchestrate-feature` skill, using the issue body as the feature description.
    ```bash
    openclaw agent --agent-id orchestrator --message "/orchestrate-feature: Implement issue #${{ github.event.issue.number }}"
    ```
3.  **PR Creation:** If the workflow completes successfully, the final step will be the creation of a pull request, ready for human review.

This approach automates the entire development cycle for well-defined issues, from planning to a ready-to-merge PR.

---

## 6. Next Steps

1.  Receive user feedback on this design.
2.  Proceed with Phase 4: Implementation.
