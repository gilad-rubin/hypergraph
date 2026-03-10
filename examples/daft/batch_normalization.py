"""Batch normalization — vectorized UDFs with batch=True.

Demonstrates:
- batch=True on @node() for vectorized processing
- Batch UDFs receive daft.Series instead of scalars
- Useful for NumPy/Arrow-based operations

Inspired by Daft's batch UDF documentation.
"""

import daft

from hypergraph import DaftRunner, Graph, node


@node(output_name="normalized", batch=True)
def normalize(values: daft.Series) -> daft.Series:
    """Z-score normalize a column of values.

    This is a batch UDF: it receives and returns daft.Series,
    processing all rows at once instead of one-by-one.
    """
    arr = values.to_pylist()
    mean = sum(arr) / len(arr)
    std = (sum((x - mean) ** 2 for x in arr) / len(arr)) ** 0.5
    if std == 0:
        return daft.Series.from_pylist([0.0] * len(arr))
    return daft.Series.from_pylist([round((x - mean) / std, 4) for x in arr])


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
