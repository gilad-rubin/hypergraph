"""Hypergraph port of durable support / approval inbox examples.

This example is intentionally built around nested graphs and interrupt
propagation, borrowing the problem shape from Mastra, Inngest, DBOS, and
Restate examples while staying idiomatic Hypergraph.
"""

from __future__ import annotations

from hypergraph import AsyncRunner, Graph, interrupt, node, route


@node(output_name="ticket")
def load_ticket(ticket_id: str, tickets_db: dict[str, dict]) -> dict:
    ticket = tickets_db[ticket_id]
    return {"id": ticket_id, **ticket}


@node(output_name="support_track")
def classify_ticket(ticket: dict) -> str:
    return "technical_support" if ticket["kind"] == "technical" else "customer_support"


@route(targets=["customer_support", "technical_support"])
def route_ticket(support_track: str) -> str:
    return support_track


@node(output_name="knowledge_hits")
def search_knowledge_base(ticket: dict, knowledge_base: list[str]) -> list[str]:
    issue = ticket["issue"].lower()
    return [entry for entry in knowledge_base if any(word in entry.lower() for word in issue.split())][:2]


@node(output_name="resolution")
def compose_customer_reply(ticket: dict, knowledge_hits: list[str]) -> str:
    first_hit = knowledge_hits[0] if knowledge_hits else "We will follow up with documentation."
    return f"Customer reply for {ticket['id']}: {first_hit}"


customer_support_graph = Graph(
    [
        search_knowledge_base,
        compose_customer_reply,
    ],
    name="customer_support",
)


@node(output_name="release_note_hits")
def search_release_notes(ticket: dict, release_notes: list[str]) -> list[str]:
    issue = ticket["issue"].lower()
    return [entry for entry in release_notes if any(word in entry.lower() for word in issue.split())][:2]


@interrupt(output_name="developer_reply")
def request_developer_review(ticket: dict, release_note_hits: list[str]) -> str | None:
    if ticket["priority"] != "critical":
        return "No manual escalation required."
    return None


@node(output_name="resolution")
def compose_technical_reply(ticket: dict, release_note_hits: list[str], developer_reply: str) -> str:
    lead = release_note_hits[0] if release_note_hits else "No matching release notes."
    return f"Technical reply for {ticket['id']}: {lead} | Developer: {developer_reply}"


technical_support_graph = Graph(
    [
        search_release_notes,
        request_developer_review,
        compose_technical_reply,
    ],
    name="technical_support",
)


@node(output_name="customer_message")
def publish_reply(ticket: dict, resolution: str) -> str:
    return f"{ticket['customer']}: {resolution}"


def build_support_inbox_graph() -> Graph:
    return Graph(
        [
            load_ticket,
            classify_ticket,
            route_ticket,
            customer_support_graph.as_node(),
            technical_support_graph.as_node(),
            publish_reply,
        ],
        name="support_inbox_port",
    )


async def demo() -> dict[str, object]:
    graph = build_support_inbox_graph()
    runner = AsyncRunner()
    values = {
        "ticket_id": "T-100",
        "tickets_db": {
            "T-100": {
                "customer": "Acme",
                "kind": "technical",
                "priority": "critical",
                "issue": "refund API timeout after release",
            }
        },
        "knowledge_base": [
            "General account changes are applied within one hour.",
            "Password reset links expire after fifteen minutes.",
        ],
        "release_notes": [
            "Refund API timeout fixed in patch 2026.03.",
            "Release 2026.03 improves webhook retries.",
        ],
    }

    first_run = await runner.run(graph, values)
    if not first_run.paused or first_run.pause is None:
        return first_run.values

    return {
        "paused": True,
        "node_name": first_run.pause.node_name,
        "response_key": first_run.pause.response_key,
        "value": first_run.pause.value,
    }


if __name__ == "__main__":
    import asyncio

    print(asyncio.run(demo()))
