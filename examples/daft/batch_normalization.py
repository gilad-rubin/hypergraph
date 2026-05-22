"""Batch normalization — vectorized Daft UDFs with daft_node(..., batch=True).

Demonstrates:
- daft_node(..., batch=True) for vectorized processing
- Batch UDFs receive daft.Series instead of scalars
- Useful for NumPy/Arrow-based operations

Inspired by Daft's batch UDF documentation.
"""

import daft

from hypergraph import Graph
from hypergraph.integrations.daft import DaftRunner
from hypergraph.integrations.daft import node as daft_node


@daft_node(output_name="normalized", batch=True, return_dtype=daft.DataType.float64())
def normalize(values: daft.Series) -> list[float]:
    """Z-score normalize a column of values.

    This is a batch UDF: it receives a daft.Series and returns a list
    aligned to the batch rows.
    """
    arr = values.to_pylist()
    mean = sum(arr) / len(arr)
    std = (sum((x - mean) ** 2 for x in arr) / len(arr)) ** 0.5
    if std == 0:
        return [0.0] * len(arr)
    return [round((x - mean) / std, 4) for x in arr]


graph = Graph([normalize], name="batch_norm")


def main():
    runner = DaftRunner()

    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    results = runner.map(graph, {"values": values}, map_over="values")

    print("Batch normalization:")
    for v, r in zip(values, results, strict=True):
        print(f"  {v} → {r['normalized']}")


if __name__ == "__main__":
    main()
