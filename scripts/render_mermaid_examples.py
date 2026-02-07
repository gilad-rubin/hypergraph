#!/usr/bin/env python3
"""Render example Mermaid diagrams for all major graph types.

Generates a single Markdown file with Mermaid blocks for each example,
and optionally opens it in the browser via GitHub's Mermaid rendering.

Usage:
    uv run scripts/render_mermaid_examples.py                # print to stdout
    uv run scripts/render_mermaid_examples.py -o examples.md # save to file
    uv run scripts/render_mermaid_examples.py --open          # save + open
"""

from __future__ import annotations

import argparse
import webbrowser
from pathlib import Path

from hypergraph import END, Graph, ifelse, node, route


# =============================================================================
# Example Graphs
# =============================================================================


@node(output_name="embedding")
def embed(text: str) -> list[float]:
    return []


@node(output_name="docs")
def retrieve(embedding: list[float]) -> list[str]:
    return []


@node(output_name="answer")
def generate(docs: list[str], query: str) -> str:
    return ""


def rag_pipeline() -> Graph:
    return Graph(nodes=[embed, retrieve, generate], name="rag")


# --- Branching ---

@ifelse(when_true="fast_path", when_false="full_rag")
def check_cache(query: str) -> bool:
    return False


@node(output_name="fast_answer")
def fast_path(query: str) -> str:
    return "cached"


@node(output_name="slow_answer")
def full_rag(query: str) -> str:
    return "computed"


@node(output_name="final_answer")
def merge(fast_answer: str = "", slow_answer: str = "") -> str:
    return fast_answer or slow_answer


def branching_graph() -> Graph:
    return Graph(nodes=[check_cache, fast_path, full_rag, merge], name="branching")


# --- Agentic Loop ---

@node(output_name="response")
def respond(docs: list[str], messages: list[str]) -> str:
    return ""


@node(output_name="messages")
def accumulate(messages: list[str], response: str) -> list[str]:
    return messages + [response]


@route(targets=["retrieve", END])
def should_continue(messages: list[str]) -> str:
    return END


def agentic_loop() -> Graph:
    return Graph(
        nodes=[retrieve, respond, accumulate, should_continue],
        name="agent_loop",
    )


# --- Nested / Hierarchical ---

@node(output_name="cleaned")
def clean(text: str) -> str:
    return text.strip()


@node(output_name="normalized")
def normalize(cleaned: str) -> str:
    return cleaned.lower()


@node(output_name="tokens")
def tokenize(normalized: str) -> list[str]:
    return normalized.split()


@node(output_name="result")
def analyze(tokens: list[str]) -> dict:
    return {"count": len(tokens)}


def nested_graph() -> Graph:
    preprocess = Graph(nodes=[clean, normalize, tokenize], name="preprocess")
    return Graph(nodes=[preprocess.as_node(), analyze], name="pipeline")


# --- Emit / Wait_for ---

@node(output_name="data", emit="ready")
def producer(x: int) -> int:
    return x * 2


@node(output_name="result", wait_for="ready")
def consumer(x: int) -> int:
    return x + 1


def ordering_graph() -> Graph:
    return Graph(nodes=[producer, consumer], name="ordering")


# =============================================================================
# Rendering
# =============================================================================

EXAMPLES = [
    ("RAG Pipeline", rag_pipeline, {}),
    ("RAG with Types", rag_pipeline, {"show_types": True}),
    ("RAG Left-to-Right", rag_pipeline, {"direction": "LR"}),
    ("RAG Separate Outputs", rag_pipeline, {"separate_outputs": True}),
    ("Branching (ifelse)", branching_graph, {}),
    ("Agentic Loop (route + END)", agentic_loop, {}),
    ("Nested Graph (collapsed)", nested_graph, {"depth": 0}),
    ("Nested Graph (expanded)", nested_graph, {"depth": 1, "show_types": True}),
    ("Ordering (emit/wait_for)", ordering_graph, {}),
    ("Custom Colors", rag_pipeline, {
        "colors": {
            "function": {"fill": "#F3E5F5", "stroke": "#7B1FA2", "stroke-width": "2px"},
            "input": {"fill": "#E3F2FD", "stroke": "#1976D2", "stroke-width": "2px"},
        }
    }),
]


def render_all() -> str:
    sections: list[str] = ["# Hypergraph Mermaid Examples\n"]

    for title, factory, kwargs in EXAMPLES:
        graph = factory()
        mermaid = graph.to_mermaid(**kwargs)

        sections.append(f"## {title}\n")

        # Show the kwargs used
        if kwargs:
            params = ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
            sections.append(f"```python\ngraph.to_mermaid({params})\n```\n")
        else:
            sections.append("```python\ngraph.to_mermaid()\n```\n")

        sections.append(f"```mermaid\n{mermaid}\n```\n")

    return "\n".join(sections)


def main() -> None:
    parser = argparse.ArgumentParser(description="Render Mermaid diagram examples.")
    parser.add_argument("-o", "--output", help="Output markdown file path")
    parser.add_argument("--open", action="store_true", help="Open output in browser")
    args = parser.parse_args()

    content = render_all()

    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        print(f"Wrote {path}")
        if args.open:
            webbrowser.open(path.resolve().as_uri())
    else:
        print(content)


if __name__ == "__main__":
    main()
