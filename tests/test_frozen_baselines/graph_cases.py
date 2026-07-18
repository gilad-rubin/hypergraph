"""Graph matrix feeding the byte-frozen baselines (issue #263).

Every builder below feeds two frozen-baseline suites in this package:

- ``test_hash_baselines.py`` freezes the exact ``Graph.definition_hash`` and
  ``Graph.structural_hash`` hex BYTES per case (``baselines/hashes/*.json``).
- ``test_mermaid_baselines.py`` freezes the exact Mermaid source TEXT per
  case (``baselines/mermaid/*.mmd``).

DO NOT casually edit this module. ``definition_hash`` hashes the *source
text* of every node function below (decorators and formatting included), so
any edit here — even whitespace — legitimately changes frozen hash bytes.
If you edit this module intentionally, regenerate the baselines
(``HYPERGRAPH_UPDATE_BASELINES=1 uv run pytest tests/test_frozen_baselines``)
and explain the change in your PR.
"""

from hypergraph import END, Graph, ifelse, node, route

# ---------------------------------------------------------------------------
# flat DAG / nested
# ---------------------------------------------------------------------------


@node(output_name="embedding")
def embed(text: str) -> list:
    return [len(text)]


@node(output_name="docs")
def retrieve(embedding: list) -> list:
    return ["doc"]


@node(output_name="answer")
def generate(docs: list, query: str) -> str:
    return "answer"


@node(output_name="answer")
def generate_from_documents(documents: list, query: str) -> str:
    return "answer"


def build_flat_dag() -> Graph:
    """Three-node linear DAG: embed -> retrieve -> generate."""
    return Graph([embed, retrieve, generate], name="flat_dag")


def build_nested() -> Graph:
    """GraphNode composition with a renamed output port (docs -> documents)."""
    inner = Graph([embed, retrieve], name="retrieval")
    retrieval = inner.as_node().rename_outputs(docs="documents")
    return Graph([retrieval, generate_from_documents], name="nested")


# ---------------------------------------------------------------------------
# gated (Route + IfElse)
# ---------------------------------------------------------------------------


@node(output_name="draft")
def draft_answer(query: str) -> str:
    return "draft"


@ifelse(when_true="polish", when_false="deliver")
def needs_polish(draft: str) -> bool:
    return len(draft) > 10


@node(output_name="polished")
def polish(draft: str) -> str:
    return draft.upper()


@node(output_name="delivery")
def deliver(draft: str) -> str:
    return draft


@route(targets=["archive", END])
def route_delivery(delivery: str) -> str:
    return "archive"


@node(output_name="archived")
def archive(delivery: str) -> bool:
    return True


def build_gated() -> Graph:
    """IfElse branch plus Route gate with an END target."""
    return Graph(
        [draft_answer, needs_polish, polish, deliver, route_delivery, archive],
        name="gated",
    )


# ---------------------------------------------------------------------------
# mapped (GraphNode.map_over)
# ---------------------------------------------------------------------------


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


@node(output_name="scaled")
def scale(doubled: int, factor: int) -> int:
    return doubled * factor


@node(output_name="report")
def collect(scaled: list) -> str:
    return str(scaled)


def build_mapped() -> Graph:
    """Mapped GraphNode: product mode, continue-on-error."""
    inner = Graph([double, scale], name="scaler")
    mapped = inner.as_node().map_over("x", "factor", mode="product", error_handling="continue")
    return Graph([mapped, collect], name="mapped")


# ---------------------------------------------------------------------------
# exposed / renamed ports (namespaced=True + expose)
# ---------------------------------------------------------------------------


@node(output_name="cleaned")
def clean_text(text: str) -> str:
    return text.strip()


@node(output_name="normalized")
def normalize(cleaned: str) -> str:
    return cleaned.lower()


@node(output_name="published")
def publish(final_text: str) -> str:
    return final_text


def build_exposed_ports() -> Graph:
    """Namespaced GraphNode with an exposed input alias and output alias."""
    inner = Graph([clean_text, normalize], name="prep")
    prep = inner.as_node(namespaced=True).expose(text="raw_text", normalized="final_text")
    return Graph([prep, publish], name="exposed_ports")


# ---------------------------------------------------------------------------
# cycle
# ---------------------------------------------------------------------------


@node(output_name="ping")
def forward(pong: str) -> str:
    return pong + "ping"


@node(output_name="pong")
def backward(ping: str) -> str:
    return ping + "pong"


def build_cycle() -> Graph:
    """Pure two-node data cycle (cyclic graphs require an explicit entrypoint)."""
    return Graph([forward, backward], name="cycle", entrypoint="forward")


# ---------------------------------------------------------------------------
# multi-output
# ---------------------------------------------------------------------------


@node(output_name=("quotient", "remainder"))
def divide(numerator: int, denominator: int) -> tuple[int, int]:
    return numerator // denominator, numerator % denominator


@node(output_name="summary")
def summarize_division(quotient: int, remainder: int) -> str:
    return f"{quotient} r{remainder}"


def build_multi_output() -> Graph:
    """Single node producing two outputs consumed downstream."""
    return Graph([divide, summarize_division], name="multi_output")


# ---------------------------------------------------------------------------
# container entrypoint (the #211 divergence case, now unified)
# ---------------------------------------------------------------------------
#
# The inner "worker" container is deliberately shaped so the historical
# self-INCLUSIVE container-entrypoint derivation (pre-#211 Mermaid path)
# picked a DIFFERENT entrypoint than the canonical self-EXCLUSIVE derivation
# that #211 made authoritative (``compute_container_entrypoints`` in
# viz/renderer/scope.py, stamped on ``GraphIR.container_entrypoints``):
#
# - ``accumulate`` consumes its own output (self-loop on ``history``). The
#   old self-inclusive rule disqualified it and picked ``kickoff``.
# - The canonical self-exclusive rule ignores a node's own outputs, so it
#   picks ``accumulate`` (declared first) — and keeps ``kickoff`` as a
#   second independent entrypoint.
#
# The self-loop makes the inner graph cyclic, so it declares both children
# as execution entrypoints (required at construction; keeps both active).
#
# #211 landed that unification: the frozen Mermaid baseline flipped exactly
# one edge (``dispatch -.-> worker__kickoff`` → ``dispatch -.->
# worker__accumulate``) as the deliberate, visible before/after evidence.
# This case now pins the canonical behavior against future drift.


@node(output_name="history")
def accumulate(history: str, raw: str) -> str:
    return history + raw


@node(output_name="status")
def kickoff(seed: str) -> str:
    return seed


@node(output_name="signal")
def intake(request: str) -> str:
    return request


@route(targets=["worker", END])
def dispatch(signal: str) -> str:
    return "worker"


def build_container_entrypoint() -> Graph:
    """Route gate targeting a container whose entrypoint derivation diverges."""
    inner = Graph(
        [accumulate, kickoff],
        name="worker",
        entrypoint=["accumulate", "kickoff"],
    )
    # The seed cycle projects ``history`` as both input and output of the
    # GraphNode, so the outer graph is cyclic too and needs an entrypoint
    # (a non-gate one: ``intake`` feeds the ``dispatch`` route gate).
    return Graph(
        [intake, dispatch, inner.as_node()],
        name="container_entrypoint",
        entrypoint="intake",
    )


# ---------------------------------------------------------------------------
# Case registries
# ---------------------------------------------------------------------------

# Hash matrix: case name -> Graph builder. One baseline file per case under
# baselines/hashes/<case>.json so #208 updates are surgical per graph shape.
HASH_CASES = {
    "flat_dag": build_flat_dag,
    "nested": build_nested,
    "gated": build_gated,
    "mapped": build_mapped,
    "exposed_ports": build_exposed_ports,
    "cycle": build_cycle,
    "multi_output": build_multi_output,
    "container_entrypoint": build_container_entrypoint,
}

# Mermaid matrix: case name -> (Graph builder, expansion depth). One baseline
# file per case under baselines/mermaid/<case>.mmd. Depth 1 cases render
# expanded containers; "container_entrypoint_expanded" is the case #211 must
# update deliberately.
MERMAID_CASES = {
    "flat_dag": (build_flat_dag, 0),
    "gated": (build_gated, 0),
    "nested_expanded": (build_nested, 1),
    "exposed_ports_expanded": (build_exposed_ports, 1),
    "container_entrypoint_expanded": (build_container_entrypoint, 1),
}
