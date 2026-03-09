"""Daft quickstart-style batch enrichment with Hypergraph.

Inspired by Daft's tabular quickstart examples, but expressed in Hypergraph's
"think singular, scale with map" style.
"""

from __future__ import annotations

from hypergraph import DaftRunner, Graph, node


@node(output_name="full_name")
def full_name(first_name: str, last_name: str) -> str:
    return f"{first_name.strip().title()} {last_name.strip().title()}"


@node(output_name="age_band")
def age_band(age: int) -> str:
    if age < 30:
        return "early-career"
    if age < 50:
        return "mid-career"
    return "senior"


@node(output_name="owner_profile")
def owner_profile(has_dog: bool, country: str) -> str:
    prefix = "dog-owner" if has_dog else "non-owner"
    return f"{prefix}:{country.lower().replace(' ', '_')}"


@node(output_name="summary")
def summarize_customer(full_name: str, age_band: str, owner_profile: str) -> str:
    return f"{full_name} | {age_band} | {owner_profile}"


graph = Graph(
    [
        full_name,
        age_band,
        owner_profile,
        summarize_customer,
    ],
    name="customer_enrichment",
)


def main() -> None:
    runner = DaftRunner()
    results = runner.map(
        graph,
        {
            "first_name": ["shandra", " zaya ", "wolfgang"],
            "last_name": ["shamas", "zaphora", "winter"],
            "age": [57, 40, 23],
            "country": ["United Kingdom", "United Kingdom", "Germany"],
            "has_dog": [True, True, False],
        },
        map_over=["first_name", "last_name", "age", "country", "has_dog"],
    )

    print(results.summary())
    for summary in results["summary"]:
        print(summary)


if __name__ == "__main__":
    main()
