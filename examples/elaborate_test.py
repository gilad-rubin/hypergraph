"""Elaborate test graph with complex structures.

This creates a comprehensive test case with:
- Multiple nested graphs (3 levels deep)
- IfElse conditional nodes
- Route nodes for dynamic routing
- Multiple inputs and outputs
- Complex data flow patterns
"""
from __future__ import annotations
from hypergraph import Graph, node, ifelse, route


# =============================================================================
# Data Processing Layer (Nested Level 1)
# =============================================================================

@node(output_name="cleaned_data")
def clean_data(raw_data: str, config: dict) -> dict:
    """Clean and normalize raw data."""
    return {"cleaned": True}


@node(output_name="validated_data")
def validate_data(cleaned_data: dict, schema: dict) -> dict:
    """Validate data against schema."""
    return {"valid": True}


@node(output_name="enriched_data")
def enrich_data(validated_data: dict, metadata: dict) -> dict:
    """Enrich data with metadata."""
    return {"enriched": True}


def make_data_processing_graph() -> Graph:
    """Create data processing subgraph."""
    return Graph(
        nodes=[clean_data, validate_data, enrich_data],
        name="data_processing"
    )


# =============================================================================
# Feature Engineering Layer (Nested Level 2)
# =============================================================================

@node(output_name="numeric_features")
def extract_numeric_features(enriched_data: dict) -> dict:
    """Extract numeric features."""
    return {"features": []}


@node(output_name="text_features")
def extract_text_features(enriched_data: dict, nlp_model: str) -> dict:
    """Extract text features using NLP."""
    return {"features": []}


@node(output_name="combined_features")
def combine_features(numeric_features: dict, text_features: dict) -> dict:
    """Combine all features."""
    return {"combined": True}


@node(output_name="scaled_features")
def scale_features(combined_features: dict, scaler_params: dict) -> dict:
    """Scale features for model input."""
    return {"scaled": True}


def make_feature_engineering_graph() -> Graph:
    """Create feature engineering subgraph."""
    return Graph(
        nodes=[
            extract_numeric_features,
            extract_text_features,
            combine_features,
            scale_features
        ],
        name="feature_engineering"
    )


# =============================================================================
# Model Training Layer (Nested Level 2)
# =============================================================================

@node(output_name="train_split")
def split_train(scaled_features: dict, split_ratio: float) -> dict:
    """Split data for training."""
    return {"train": True}


@node(output_name="val_split")
def split_val(scaled_features: dict, split_ratio: float) -> dict:
    """Split data for validation."""
    return {"val": True}


@node(output_name="trained_model")
def train_model(train_split: dict, val_split: dict, hyperparams: dict) -> dict:
    """Train the model."""
    return {"model": "trained"}


@node(output_name="model_metrics")
def evaluate_model(trained_model: dict, val_split: dict) -> dict:
    """Evaluate model performance."""
    return {"metrics": {}}


def make_model_training_graph() -> Graph:
    """Create model training subgraph."""
    return Graph(
        nodes=[split_train, split_val, train_model, evaluate_model],
        name="model_training"
    )


# =============================================================================
# Model Selection with IfElse
# =============================================================================

@node(output_name="complex_model")
def train_complex_model(train_split: dict, hyperparams: dict) -> dict:
    """Train complex model (neural network)."""
    return {"type": "complex"}


@node(output_name="simple_model")
def train_simple_model(train_split: dict, hyperparams: dict) -> dict:
    """Train simple model (linear)."""
    return {"type": "simple"}


@ifelse(when_true="train_complex_model", when_false="train_simple_model", name="model_selector")
def should_use_complex_model(model_metrics: dict, threshold: float) -> bool:
    """Decide whether to use complex model based on metrics."""
    return model_metrics.get("accuracy", 0) > threshold


# =============================================================================
# Deployment Strategy with Route
# =============================================================================

@node(output_name="cloud_deployment")
def deploy_to_cloud(final_model: dict, cloud_config: dict) -> dict:
    """Deploy model to cloud."""
    return {"deployed": "cloud"}


@node(output_name="edge_deployment")
def deploy_to_edge(final_model: dict, edge_config: dict) -> dict:
    """Deploy model to edge devices."""
    return {"deployed": "edge"}


@node(output_name="hybrid_deployment")
def deploy_hybrid(final_model: dict, hybrid_config: dict) -> dict:
    """Deploy model in hybrid mode."""
    return {"deployed": "hybrid"}


@route(
    targets={"cloud": "deploy_to_cloud", "edge": "deploy_to_edge", "hybrid": "deploy_hybrid"},
    name="deployment_router"
)
def choose_deployment_strategy(model_metrics: dict, infrastructure: dict) -> str:
    """Choose deployment strategy based on metrics and infrastructure."""
    # Simplified logic - in reality would be more complex
    if infrastructure.get("cloud_available", True):
        return "cloud"
    elif infrastructure.get("edge_available", False):
        return "edge"
    else:
        return "hybrid"


# =============================================================================
# Monitoring and Feedback (Nested Level 1)
# =============================================================================

@node(output_name="monitoring_data")
def collect_monitoring_data(deployment_result: dict, metrics_config: dict) -> dict:
    """Collect monitoring data from deployment."""
    return {"monitoring": True}


@node(output_name="alerts")
def generate_alerts(monitoring_data: dict, alert_rules: dict) -> list:
    """Generate alerts based on monitoring."""
    return []


@node(output_name="feedback_report")
def create_feedback_report(monitoring_data: dict, alerts: list) -> dict:
    """Create feedback report for model improvement."""
    return {"report": True}


def make_monitoring_graph() -> Graph:
    """Create monitoring subgraph."""
    return Graph(
        nodes=[collect_monitoring_data, generate_alerts, create_feedback_report],
        name="monitoring"
    )


# =============================================================================
# Main ML Pipeline Assembly
# =============================================================================

@node(output_name="pipeline_config")
def initialize_pipeline(config_file: str) -> dict:
    """Initialize pipeline configuration."""
    return {"config": "loaded"}


@node(output_name="final_report")
def generate_final_report(
    feedback_report: dict,
    deployment_result: dict,
    model_metrics: dict
) -> dict:
    """Generate final pipeline report."""
    return {"report": "complete"}


def make_ml_pipeline() -> Graph:
    """Create the main ML pipeline with nested graphs."""
    
    # Create nested subgraphs
    data_proc = make_data_processing_graph()
    feature_eng = make_feature_engineering_graph()
    model_train = make_model_training_graph()
    monitoring = make_monitoring_graph()
    
    # IfElse and Route nodes are already created via decorators
    
    # Assemble main pipeline
    return Graph(
        nodes=[
            initialize_pipeline,
            data_proc.as_node(),
            feature_eng.as_node(),
            model_train.as_node(),
            should_use_complex_model,  # IfElse node
            train_complex_model,
            train_simple_model,
            choose_deployment_strategy,  # Route node
            deploy_to_cloud,
            deploy_to_edge,
            deploy_hybrid,
            monitoring.as_node(),
            generate_final_report
        ],
        name="ml_pipeline"
    )


# =============================================================================
# Visualization
# =============================================================================

def visualize_pipeline(depth: int = 2) -> None:
    """Visualize the ML pipeline."""
    from hypergraph.viz import visualize
    
    pipeline = make_ml_pipeline()
    print(f"\nVisualizing ML pipeline at depth={depth}")
    print("=" * 60)
    
    visualize(
        pipeline,
        depth=depth,
        separate_outputs=False,
        filepath=f"/home/ubuntu/elaborate_test_depth{depth}.html"
    )
    print(f"Saved to /home/ubuntu/elaborate_test_depth{depth}.html")


if __name__ == "__main__":
    # Test at different depths
    visualize_pipeline(depth=1)  # Collapsed subgraphs
    visualize_pipeline(depth=2)  # One level expanded
    visualize_pipeline(depth=3)  # Fully expanded
