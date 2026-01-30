"""Comprehensive test graph with complex layout patterns.

This creates a test case focused on layout challenges:
- Multiple nested graphs (2-3 levels deep)
- Many input nodes with different consumption patterns
- Complex edge routing scenarios
- Wide and deep graph structures
"""
from __future__ import annotations
from hypergraph import Graph, node


# =============================================================================
# Data Ingestion Layer (Nested Level 1)
# =============================================================================

@node(output_name="raw_documents")
def fetch_documents(api_key: str, query: str) -> list:
    """Fetch documents from API."""
    return []


@node(output_name="raw_images")
def fetch_images(api_key: str, search_term: str) -> list:
    """Fetch images from API."""
    return []


@node(output_name="raw_metadata")
def fetch_metadata(api_key: str, doc_ids: list) -> dict:
    """Fetch metadata for documents."""
    return {}


def make_ingestion_graph() -> Graph:
    """Create data ingestion subgraph."""
    return Graph(
        nodes=[fetch_documents, fetch_images, fetch_metadata],
        name="data_ingestion"
    )


# =============================================================================
# Processing Layer (Nested Level 2)
# =============================================================================

@node(output_name="cleaned_docs")
def clean_documents(raw_documents: list, config: dict) -> list:
    """Clean documents."""
    return []


@node(output_name="processed_images")
def process_images(raw_images: list, config: dict) -> list:
    """Process images."""
    return []


@node(output_name="enriched_metadata")
def enrich_metadata(raw_metadata: dict, external_data: dict) -> dict:
    """Enrich metadata."""
    return {}


@node(output_name="validated_docs")
def validate_documents(cleaned_docs: list, schema: dict) -> list:
    """Validate documents."""
    return []


@node(output_name="indexed_docs")
def index_documents(validated_docs: list, index_config: dict) -> dict:
    """Index documents."""
    return {}


def make_processing_graph() -> Graph:
    """Create processing subgraph."""
    return Graph(
        nodes=[
            clean_documents,
            process_images,
            enrich_metadata,
            validate_documents,
            index_documents
        ],
        name="document_processing"
    )


# =============================================================================
# Analysis Layer (Nested Level 2)
# =============================================================================

@node(output_name="sentiment_scores")
def analyze_sentiment(validated_docs: list, model_name: str) -> dict:
    """Analyze document sentiment."""
    return {}


@node(output_name="topic_labels")
def extract_topics(validated_docs: list, num_topics: int) -> dict:
    """Extract topics from documents."""
    return {}


@node(output_name="entity_mentions")
def extract_entities(validated_docs: list, entity_types: list) -> dict:
    """Extract named entities."""
    return {}


@node(output_name="summary_text")
def generate_summaries(validated_docs: list, max_length: int) -> dict:
    """Generate document summaries."""
    return {}


@node(output_name="analysis_report")
def compile_analysis(
    sentiment_scores: dict,
    topic_labels: dict,
    entity_mentions: dict,
    summary_text: dict
) -> dict:
    """Compile all analysis results."""
    return {}


def make_analysis_graph() -> Graph:
    """Create analysis subgraph."""
    return Graph(
        nodes=[
            analyze_sentiment,
            extract_topics,
            extract_entities,
            generate_summaries,
            compile_analysis
        ],
        name="document_analysis"
    )


# =============================================================================
# Retrieval Layer (Nested Level 1)
# =============================================================================

@node(output_name="search_results")
def search_index(indexed_docs: dict, user_query: str, top_k: int) -> list:
    """Search the document index."""
    return []


@node(output_name="reranked_results")
def rerank_results(search_results: list, rerank_model: str) -> list:
    """Rerank search results."""
    return []


@node(output_name="filtered_results")
def filter_results(reranked_results: list, filters: dict) -> list:
    """Filter results based on criteria."""
    return []


def make_retrieval_graph() -> Graph:
    """Create retrieval subgraph."""
    return Graph(
        nodes=[search_index, rerank_results, filter_results],
        name="retrieval"
    )


# =============================================================================
# Generation Layer (Nested Level 1)
# =============================================================================

@node(output_name="prompt")
def build_prompt(
    filtered_results: list,
    user_query: str,
    template: str,
    analysis_report: dict
) -> str:
    """Build LLM prompt."""
    return ""


@node(output_name="llm_response")
def call_llm(prompt: str, model: str, temperature: float) -> str:
    """Call LLM for generation."""
    return ""


@node(output_name="formatted_response")
def format_response(llm_response: str, format_spec: dict) -> dict:
    """Format the response."""
    return {}


def make_generation_graph() -> Graph:
    """Create generation subgraph."""
    return Graph(
        nodes=[build_prompt, call_llm, format_response],
        name="generation"
    )


# =============================================================================
# Main Pipeline Assembly
# =============================================================================

@node(output_name="system_config")
def load_config(config_path: str) -> dict:
    """Load system configuration."""
    return {}


@node(output_name="final_output")
def finalize_output(
    formatted_response: dict,
    enriched_metadata: dict,
    analysis_report: dict
) -> dict:
    """Finalize the output."""
    return {}


@node(output_name="audit_log")
def log_execution(final_output: dict, system_config: dict) -> dict:
    """Log execution for audit."""
    return {}


def make_rag_pipeline() -> Graph:
    """Create the main RAG pipeline with nested graphs."""
    
    # Create nested subgraphs
    ingestion = make_ingestion_graph()
    processing = make_processing_graph()
    analysis = make_analysis_graph()
    retrieval = make_retrieval_graph()
    generation = make_generation_graph()
    
    # Assemble main pipeline
    return Graph(
        nodes=[
            load_config,
            ingestion.as_node(),
            processing.as_node(),
            analysis.as_node(),
            retrieval.as_node(),
            generation.as_node(),
            finalize_output,
            log_execution
        ],
        name="rag_pipeline"
    )


# =============================================================================
# Visualization
# =============================================================================

def visualize_pipeline(depth: int = 2) -> None:
    """Visualize the RAG pipeline."""
    from hypergraph.viz import visualize
    
    pipeline = make_rag_pipeline()
    print(f"\nVisualizing RAG pipeline at depth={depth}")
    print("=" * 60)
    
    visualize(
        pipeline,
        depth=depth,
        separate_outputs=False,
        filepath=f"/home/ubuntu/comprehensive_test_depth{depth}.html"
    )
    print(f"Saved to /home/ubuntu/comprehensive_test_depth{depth}.html")


if __name__ == "__main__":
    # Test at different depths
    visualize_pipeline(depth=1)  # Collapsed subgraphs
    visualize_pipeline(depth=2)  # One level expanded
    visualize_pipeline(depth=3)  # Fully expanded
