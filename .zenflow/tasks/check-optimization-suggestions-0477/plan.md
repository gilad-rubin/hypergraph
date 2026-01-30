# Spec and build

## Configuration
- **Artifacts Path**: {@artifacts_path} → `.zenflow/tasks/{task_id}`

---

## Agent Instructions

Ask the user questions when anything is unclear or needs their input. This includes:
- Ambiguous or incomplete requirements
- Technical decisions that affect architecture or user experience
- Trade-offs that require business context

Do not make assumptions on important decisions — get clarification first.

---

## Workflow Steps

### [x] Step: Technical Specification
<!-- chat-id: 4960b368-b2ef-4844-bf32-a1695ad39321 -->

Assess the task's difficulty, as underestimating it leads to poor outcomes.
- easy: Straightforward implementation, trivial bug fix or feature
- medium: Moderate complexity, some edge cases or caveats to consider
- hard: Complex logic, many caveats, architectural considerations, or high-risk changes

Create a technical specification for the task that is appropriate for the complexity level:
- Review the existing codebase architecture and identify reusable components.
- Define the implementation approach based on established patterns in the project.
- Identify all source code files that will be created or modified.
- Define any necessary data model, API, or interface changes.
- Describe verification steps using the project's test and lint commands.

Save the output to `{@artifacts_path}/spec.md` with:
- Technical context (language, dependencies)
- Implementation approach
- Source code structure changes
- Data model / API / interface changes
- Verification approach

If the task is complex enough, create a detailed implementation plan based on `{@artifacts_path}/spec.md`:
- Break down the work into concrete tasks (incrementable, testable milestones)
- Each task should reference relevant contracts and include verification steps
- Replace the Implementation step below with the planned tasks

Rule of thumb for step size: each step should represent a coherent unit of work (e.g., implement a component, add an API endpoint, write tests for a module). Avoid steps that are too granular (single function).

Save to `{@artifacts_path}/plan.md`. If the feature is trivial and doesn't warrant this breakdown, keep the Implementation step below as is.

---

### [ ] Step: Build benchmarks for valid PRs (#20, #22, #24)

Create `benchmarks/test_optimization_prs.py` with:
- PR #20: Benchmark `_compute_exclusive_reachability` old (O(N²)) vs new (Counter-based) with varying branch counts
- PR #22: Benchmark `AsyncRunner.map` old (all-tasks) vs new (worker-pool) measuring memory and time
- PR #24: Benchmark `_get_activated_nodes` with controlled_by computed per-superstep vs cached
- Include sanity checks for PR #19 and #21 at realistic scales to confirm negligible difference

### [ ] Step: Run benchmarks and write report

1. Run all benchmarks and collect results
2. Write `{@artifacts_path}/report.md` with:
   - Per-PR verdict (accept/reject/flag)
   - Benchmark data supporting each verdict
   - Recommendations for which PRs to merge
