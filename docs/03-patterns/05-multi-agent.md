# Multi-Agent Orchestration

Build agent teams by composing graphs. Each agent is a graph. The orchestrator is a graph of agents.

## The Pattern

```python
# Each agent is its own graph
researcher = Graph([search, analyze, summarize], name="researcher")
writer = Graph([draft, refine, format], name="writer")
reviewer = Graph([check_facts, check_style, score], name="reviewer")

# Compose into a team
team = Graph([
    researcher.as_node(),
    writer.as_node(),
    reviewer.as_node(),
    review_gate,  # Decides if done or needs revision
])
```

## Why Graphs as Agents?

Each agent encapsulates:
- **Internal workflow** — The agent's reasoning process
- **Clear interface** — Defined inputs and outputs
- **Testability** — Test agents in isolation
- **Reusability** — Same agent in different teams

## Research Team Example

A team that researches a topic and produces a report:

```python
from hypergraph import Graph, node, route, END, SyncRunner, AsyncRunner

# ═══════════════════════════════════════════════════════════════
# RESEARCHER AGENT
# ═══════════════════════════════════════════════════════════════

@node(output_name="search_results")
def search(topic: str) -> list[dict]:
    """Search for relevant sources."""
    return search_api.query(topic, max_results=10)

@node(output_name="analysis")
def analyze(search_results: list[dict]) -> dict:
    """Analyze and synthesize search results."""
    return llm.analyze(
        f"Analyze these sources and identify key themes:\n{search_results}"
    )

@node(output_name="research_summary")
def summarize_research(analysis: dict, topic: str) -> str:
    """Produce research summary."""
    return llm.generate(
        f"Summarize the research on '{topic}':\n{analysis}"
    )

researcher = Graph([search, analyze, summarize_research], name="researcher")

# ═══════════════════════════════════════════════════════════════
# WRITER AGENT
# ═══════════════════════════════════════════════════════════════

@node(output_name="draft")
def write_draft(research_summary: str, outline: str = "") -> str:
    """Write initial draft from research."""
    prompt = f"Research:\n{research_summary}"
    if outline:
        prompt += f"\n\nOutline to follow:\n{outline}"
    return llm.generate(prompt)

@node(output_name="refined_draft")
def refine(draft: str, feedback: str = "") -> str:
    """Refine draft based on feedback."""
    if not feedback:
        return draft
    return llm.generate(f"Revise this draft:\n{draft}\n\nFeedback:\n{feedback}")

@node(output_name="report")
def format_report(refined_draft: str) -> str:
    """Format the final report."""
    return formatter.apply_template(refined_draft)

writer = Graph([write_draft, refine, format_report], name="writer")

# ═══════════════════════════════════════════════════════════════
# REVIEWER AGENT
# ═══════════════════════════════════════════════════════════════

@node(output_name="fact_check")
def check_facts(report: str, research_summary: str) -> dict:
    """Verify claims against research."""
    return fact_checker.verify(report, sources=research_summary)

@node(output_name="style_check")
def check_style(report: str) -> dict:
    """Check writing quality."""
    return style_analyzer.analyze(report)

@node(output_name="review_score")
def score_report(fact_check: dict, style_check: dict) -> float:
    """Overall quality score."""
    fact_score = fact_check["accuracy"]
    style_score = style_check["quality"]
    return (fact_score + style_score) / 2

@node(output_name="feedback")
def generate_feedback(fact_check: dict, style_check: dict, review_score: float) -> str:
    """Generate feedback for revision."""
    if review_score >= 0.9:
        return ""
    issues = []
    if fact_check["issues"]:
        issues.extend(fact_check["issues"])
    if style_check["issues"]:
        issues.extend(style_check["issues"])
    return "\n".join(issues)

reviewer = Graph([check_facts, check_style, score_report, generate_feedback], name="reviewer")

# ═══════════════════════════════════════════════════════════════
# TEAM ORCHESTRATION
# ═══════════════════════════════════════════════════════════════

@route(targets=["writer", END])
def review_gate(review_score: float, revision_count: int = 0) -> str:
    """Decide if report is ready or needs revision."""
    if review_score >= 0.9:
        return END
    if revision_count >= 3:
        return END  # Accept after 3 revisions
    return "writer"

@node(output_name="revision_count")
def track_revisions(revision_count: int = 0) -> int:
    return revision_count + 1

# Compose the team
research_team = Graph([
    researcher.as_node(),
    writer.as_node(),
    reviewer.as_node(),
    track_revisions,
    review_gate,
], name="research_team")

# Run the team
runner = SyncRunner()
result = runner.run(research_team, {"topic": "Quantum Computing in 2024"})
print(result["report"])
```

## Agent Handoff Pattern

Sequential agents where one's output becomes another's input:

```python
# Planner → Executor → Verifier
planner = Graph([analyze_task, create_plan], name="planner")
executor = Graph([execute_steps, collect_results], name="executor")
verifier = Graph([check_results, generate_report], name="verifier")

# Wire them together
# planner produces "plan" → executor takes "plan"
# executor produces "results" → verifier takes "results"
pipeline = Graph([
    planner.as_node(),
    executor.as_node(),
    verifier.as_node(),
])
```

## Specialist Selection

Route to different specialists based on the task:

```python
@node(output_name="task_type")
def classify_task(task: str) -> str:
    """Determine which specialist should handle this."""
    return classifier.predict(task)

@route(targets=["code_agent", "writing_agent", "research_agent"])
def route_to_specialist(task_type: str) -> str:
    """Route to the appropriate specialist."""
    return f"{task_type}_agent"

code_agent = Graph([...], name="code_agent")
writing_agent = Graph([...], name="writing_agent")
research_agent = Graph([...], name="research_agent")

specialist_team = Graph([
    classify_task,
    route_to_specialist,
    code_agent.as_node(),
    writing_agent.as_node(),
    research_agent.as_node(),
])
```

## Parallel Agent Execution

Independent agents can run concurrently:

```python
# These agents don't depend on each other's outputs
sentiment_agent = Graph([...], name="sentiment")
entity_agent = Graph([...], name="entities")
topic_agent = Graph([...], name="topics")

@node(output_name="combined_analysis")
def combine(sentiment: dict, entities: list, topics: list) -> dict:
    """Combine results from all agents."""
    return {
        "sentiment": sentiment,
        "entities": entities,
        "topics": topics,
    }

# All three agents run in parallel, then combine
analysis_team = Graph([
    sentiment_agent.as_node(),
    entity_agent.as_node(),
    topic_agent.as_node(),
    combine,
])

runner = AsyncRunner()
result = await runner.run(analysis_team, {"text": "..."}, max_concurrency=3)
```

## Testing Agents Independently

Each agent is testable in isolation:

```python
def test_researcher():
    runner = SyncRunner()
    result = runner.run(researcher, {"topic": "test topic"})

    assert "research_summary" in result
    assert len(result["research_summary"]) > 100

def test_writer():
    runner = SyncRunner()
    result = runner.run(writer, {"research_summary": "Test research..."})

    assert "report" in result

def test_team():
    runner = SyncRunner()
    result = runner.run(research_team, {"topic": "test topic"})

    assert result["review_score"] >= 0.7
```

## What's Next?

- [Hierarchical Composition](04-hierarchical.md) — More on nesting patterns
- [Real-World: Evaluation Harness](../04-real-world/evaluation-harness.md) — Test agent teams at scale
