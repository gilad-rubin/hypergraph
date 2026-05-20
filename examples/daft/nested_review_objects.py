"""Complex Python-object example for DaftRunner with nested mapping."""

from __future__ import annotations

from dataclasses import dataclass

from hypergraph import DaftRunner, Graph, node


@dataclass(frozen=True)
class Review:
    reviewer: str
    score: int
    text: str


@dataclass(frozen=True)
class ReviewAssessment:
    reviewer: str
    score: int
    label: str


@node(output_name="assessment")
def assess_review(review: Review) -> ReviewAssessment:
    if review.score >= 8:
        label = "promoter"
    elif review.score >= 5:
        label = "neutral"
    else:
        label = "detractor"
    return ReviewAssessment(reviewer=review.reviewer, score=review.score, label=label)


@node(output_name="needs_follow_up")
def needs_follow_up(assessment: ReviewAssessment) -> bool:
    return assessment.label == "detractor"


review_graph = Graph(
    [
        assess_review,
        needs_follow_up,
    ],
    name="review_graph",
)

assess_reviews = review_graph.as_node(name="assess_reviews").rename_inputs(review="reviews").map_over("reviews")


@node(output_name="summary")
def summarize_batch(assessment: list[ReviewAssessment], needs_follow_up: list[bool]) -> dict[str, object]:
    return {
        "total_reviews": len(assessment),
        "promoters": sum(1 for item in assessment if item.label == "promoter"),
        "follow_ups": sum(1 for flag in needs_follow_up if flag),
    }


workflow = Graph(
    [
        assess_reviews,
        summarize_batch,
    ],
    name="review_batch",
)


def main() -> None:
    runner = DaftRunner()
    result = runner.run(
        workflow,
        # `reviews` is projected flat from the `assess_reviews` GraphNode.
        {
            "reviews": [
                Review("Ava", 9, "Loved it"),
                Review("Ben", 4, "It broke during setup"),
                Review("Chen", 6, "Good, but not great"),
            ]
        },
    )

    print(result["summary"])
    print(result["assessment"])
    print(result["needs_follow_up"])


if __name__ == "__main__":
    main()
