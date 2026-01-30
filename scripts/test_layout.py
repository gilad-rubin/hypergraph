from hypergraph import Graph, node
from hypergraph.viz import visualize


@node(output_name="raw_documents")
def fetch_documents(query, search_term) -> list:
    return []


@node(output_name="raw_images")
def fetch_images(search_term: str, query) -> list:
    return []


@node(output_name="raw_metadata")
def fetch_metadata(api_key: str, doc_ids: list) -> dict:
    return {}


@node(output_name="combined_data")
def combine(raw_metadata, raw_images):
    return []


graph = Graph(
    nodes=[fetch_documents, fetch_images, fetch_metadata, combine],
    name="data_ingestion",
)

visualize(graph, filepath="outputs/test_layout.html")
print("Generated: outputs/test_layout.html")
