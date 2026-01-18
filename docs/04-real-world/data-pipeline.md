# Data Pipeline (ETL)

A classic Extract-Transform-Load pipeline. No LLMs, just pure data processing. Shows hypergraph works for traditional workflows too.

## When to Use

- Data ingestion and processing
- Feature engineering
- Report generation
- Any batch data workflow

## The Pipeline

```
extract → validate → transform → enrich → load
```

## Complete Implementation

```python
from hypergraph import Graph, node, SyncRunner
import json
from datetime import datetime

# ═══════════════════════════════════════════════════════════════
# EXTRACT
# ═══════════════════════════════════════════════════════════════

@node(output_name="raw_data")
def extract(source_path: str) -> list[dict]:
    """
    Extract raw data from source.
    Supports JSON, CSV, or API endpoints.
    """
    if source_path.endswith(".json"):
        with open(source_path) as f:
            return json.load(f)

    elif source_path.endswith(".csv"):
        import csv
        with open(source_path) as f:
            reader = csv.DictReader(f)
            return list(reader)

    elif source_path.startswith("http"):
        import httpx
        response = httpx.get(source_path)
        return response.json()

    raise ValueError(f"Unknown source format: {source_path}")


# ═══════════════════════════════════════════════════════════════
# VALIDATE
# ═══════════════════════════════════════════════════════════════

@node(output_name=("valid_records", "invalid_records"))
def validate(raw_data: list[dict], required_fields: list[str]) -> tuple[list, list]:
    """
    Validate records, separating valid from invalid.
    """
    valid = []
    invalid = []

    for record in raw_data:
        missing = [f for f in required_fields if f not in record or record[f] is None]

        if missing:
            invalid.append({
                "record": record,
                "errors": [f"Missing field: {f}" for f in missing],
            })
        else:
            valid.append(record)

    return valid, invalid


# ═══════════════════════════════════════════════════════════════
# TRANSFORM
# ═══════════════════════════════════════════════════════════════

@node(output_name="transformed")
def transform(valid_records: list[dict], transformations: dict) -> list[dict]:
    """
    Apply transformations to records.

    transformations = {
        "email": str.lower,
        "price": lambda x: round(float(x), 2),
        "date": lambda x: datetime.fromisoformat(x).date().isoformat(),
    }
    """
    result = []

    for record in valid_records:
        transformed = record.copy()

        for field, transform_fn in transformations.items():
            if field in transformed:
                try:
                    transformed[field] = transform_fn(transformed[field])
                except Exception as e:
                    transformed[f"{field}_error"] = str(e)

        result.append(transformed)

    return result


# ═══════════════════════════════════════════════════════════════
# ENRICH
# ═══════════════════════════════════════════════════════════════

@node(output_name="enriched")
def enrich(transformed: list[dict], lookup_table: dict) -> list[dict]:
    """
    Enrich records with data from lookup tables.

    lookup_table = {
        "category_names": {"A": "Electronics", "B": "Clothing"},
        "region_codes": {"US": "United States", "UK": "United Kingdom"},
    }
    """
    result = []

    for record in transformed:
        enriched = record.copy()

        # Add category name
        if "category" in enriched and "category_names" in lookup_table:
            code = enriched["category"]
            enriched["category_name"] = lookup_table["category_names"].get(code, "Unknown")

        # Add region name
        if "region" in enriched and "region_codes" in lookup_table:
            code = enriched["region"]
            enriched["region_name"] = lookup_table["region_codes"].get(code, "Unknown")

        # Add processing timestamp
        enriched["processed_at"] = datetime.utcnow().isoformat()

        result.append(enriched)

    return result


# ═══════════════════════════════════════════════════════════════
# LOAD
# ═══════════════════════════════════════════════════════════════

@node(output_name="load_result")
def load(enriched: list[dict], destination: str) -> dict:
    """
    Load processed data to destination.
    """
    if destination.endswith(".json"):
        with open(destination, "w") as f:
            json.dump(enriched, f, indent=2)

    elif destination.startswith("postgres://"):
        # Insert to database
        import psycopg2
        conn = psycopg2.connect(destination)
        # ... insert logic
        conn.close()

    return {
        "records_loaded": len(enriched),
        "destination": destination,
        "timestamp": datetime.utcnow().isoformat(),
    }


# ═══════════════════════════════════════════════════════════════
# COMPOSE THE PIPELINE
# ═══════════════════════════════════════════════════════════════

etl_pipeline = Graph([
    extract,
    validate,
    transform,
    enrich,
    load,
], name="etl")


# ═══════════════════════════════════════════════════════════════
# RUN THE PIPELINE
# ═══════════════════════════════════════════════════════════════

def main():
    runner = SyncRunner()

    result = runner.run(etl_pipeline, {
        "source_path": "data/raw_orders.json",
        "required_fields": ["id", "email", "amount"],
        "transformations": {
            "email": str.lower,
            "amount": lambda x: round(float(x), 2),
        },
        "lookup_table": {
            "category_names": {"A": "Electronics", "B": "Clothing"},
        },
        "destination": "data/processed_orders.json",
    })

    print(f"Loaded {result['load_result']['records_loaded']} records")
    print(f"Invalid records: {len(result['invalid_records'])}")

    # Log invalid records for review
    for invalid in result["invalid_records"]:
        print(f"  - {invalid['errors']}")
```

## Batch Processing

Process multiple files:

```python
def process_all_files():
    runner = SyncRunner()

    files = ["orders_jan.json", "orders_feb.json", "orders_mar.json"]

    results = runner.map(
        etl_pipeline,
        {
            "source_path": files,
            "required_fields": ["id", "email", "amount"],
            "transformations": {"email": str.lower},
            "lookup_table": {},
            "destination": [f"processed_{f}" for f in files],
        },
        map_over=["source_path", "destination"],
        map_mode="zip",
    )

    total = sum(r["load_result"]["records_loaded"] for r in results)
    print(f"Total records processed: {total}")
```

## With Error Reporting

Add a step to generate an error report:

```python
@node(output_name="error_report")
def generate_error_report(invalid_records: list[dict], report_path: str) -> str:
    """Generate a detailed error report."""

    lines = ["# Data Validation Errors", ""]

    for i, invalid in enumerate(invalid_records, 1):
        lines.append(f"## Record {i}")
        lines.append(f"Errors: {', '.join(invalid['errors'])}")
        lines.append(f"Data: {json.dumps(invalid['record'], indent=2)}")
        lines.append("")

    report = "\n".join(lines)

    with open(report_path, "w") as f:
        f.write(report)

    return report_path


etl_with_reporting = Graph([
    extract,
    validate,
    transform,
    enrich,
    load,
    generate_error_report,
])
```

## Testing

```python
def test_validate():
    raw = [
        {"id": 1, "email": "test@example.com", "amount": 10},
        {"id": 2, "email": None, "amount": 20},  # Invalid - null email
        {"id": 3, "amount": 30},  # Invalid - missing email
    ]

    valid, invalid = validate.func(raw, ["id", "email", "amount"])

    assert len(valid) == 1
    assert len(invalid) == 2

def test_transform():
    records = [{"email": "TEST@Example.COM", "price": "99.999"}]
    transforms = {"email": str.lower, "price": lambda x: round(float(x), 2)}

    result = transform.func(records, transforms)

    assert result[0]["email"] == "test@example.com"
    assert result[0]["price"] == 100.0

def test_full_pipeline():
    runner = SyncRunner()

    result = runner.run(etl_pipeline, {
        "source_path": "test_data.json",
        "required_fields": ["id"],
        "transformations": {},
        "lookup_table": {},
        "destination": "test_output.json",
    })

    assert result["load_result"]["records_loaded"] >= 0
```

## What's Next?

- [Batch Processing](../05-how-to/batch-processing.md) — Process multiple inputs
- [Hierarchical Composition](../03-patterns/04-hierarchical.md) — Nest this pipeline in larger workflows
