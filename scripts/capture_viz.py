"""Capture visualization screenshots using Playwright."""

import asyncio
from pathlib import Path
from hypergraph import Graph, node


# ===============================
# Simple graphs
# ===============================
@node(output_name="y")
def double(x: int) -> int:
    return x * 2

@node(output_name="z")
def square(y: int) -> int:
    return y ** 2

# ===============================
# RAG pipeline
# ===============================
@node(output_name="embedding")
def embed(text: str) -> list[float]:
    return [0.1] * 10

@node(output_name="docs")
def retrieve(embedding: list[float]) -> list[str]:
    return ["doc1", "doc2"]

@node(output_name="answer")
def generate(docs: list[str], query: str) -> str:
    return "Answer"

# ===============================
# Diamond pattern
# ===============================
@node(output_name="a")
def start(x: int) -> int:
    return x

@node(output_name="b")
def left(a: int) -> int:
    return a + 1

@node(output_name="c")
def right(a: int) -> int:
    return a * 2

@node(output_name="d")
def merge(b: int, c: int) -> int:
    return b + c

# ===============================
# Complex RAG (19 nodes)
# ===============================
@node(output_name="raw_text")
def load_data(filepath: str) -> str:
    return "raw content"

@node(output_name="cleaned_text")
def clean(raw_text: str) -> str:
    return raw_text.strip()

@node(output_name="tokens")
def tokenize(cleaned_text: str) -> list[str]:
    return cleaned_text.split()

@node(output_name="chunks")
def chunk(tokens: list[str], chunk_size: int) -> list[list[str]]:
    return [tokens[i : i + chunk_size] for i in range(0, len(tokens), chunk_size)]

@node(output_name="embeddings")
def embed_chunks(chunks: list[list[str]], model_name: str) -> list[list[float]]:
    return [[0.1] * 768 for _ in chunks]

@node(output_name="normalized_embeddings")
def normalize(embeddings: list[list[float]]) -> list[list[float]]:
    return embeddings

@node(output_name="index")
def build_index(normalized_embeddings: list[list[float]]) -> dict:
    return {"vectors": normalized_embeddings}

@node(output_name="query_text")
def parse_query(user_input: str) -> str:
    return user_input.strip()

@node(output_name="query_embedding")
def embed_query(query_text: str, model_name: str) -> list[float]:
    return [0.1] * 768

@node(output_name="expanded_queries")
def expand_query(query_text: str) -> list[str]:
    return [query_text, f"{query_text} synonym"]

@node(output_name="query_embeddings")
def embed_expanded(expanded_queries: list[str], model_name: str) -> list[list[float]]:
    return [[0.1] * 768 for _ in expanded_queries]

@node(output_name="candidates")
def search_index(index: dict, query_embedding: list[float], top_k: int) -> list[int]:
    return list(range(top_k))

@node(output_name="expanded_candidates")
def search_expanded(index: dict, query_embeddings: list[list[float]], top_k: int) -> list[int]:
    return list(range(top_k * 2))

@node(output_name="merged_candidates")
def merge_results(candidates: list[int], expanded_candidates: list[int]) -> list[int]:
    return list(set(candidates + expanded_candidates))

@node(output_name="retrieved_docs")
def fetch_documents(merged_candidates: list[int], chunks: list[list[str]]) -> list[str]:
    return [" ".join(chunks[i]) for i in merged_candidates if i < len(chunks)]

@node(output_name="context")
def format_context(retrieved_docs: list[str]) -> str:
    return "\n\n".join(retrieved_docs)

@node(output_name="prompt")
def build_prompt(context: str, query_text: str, system_prompt: str) -> str:
    return f"{system_prompt}\n\nContext:\n{context}\n\nQuery: {query_text}"

@node(output_name="raw_response")
def call_llm(prompt: str, temperature: float, max_tokens: int) -> str:
    return "Generated response..."

@node(output_name="final_answer")
def postprocess(raw_response: str) -> str:
    return raw_response.strip()


async def capture_graph(graph, name, theme="auto", width=1200, height=900):
    from playwright.async_api import async_playwright
    from hypergraph.viz.widget import visualize

    # Ensure outputs directory exists
    output_dir = Path("outputs")
    output_dir.mkdir(exist_ok=True)

    widget = visualize(graph, theme=theme, width=width, height=height)
    html = widget.html_content

    # Save HTML for debugging
    html_path = output_dir / f"viz_{name}.html"
    html_path.write_text(html)

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": width, "height": height})
        await page.set_content(html)
        await page.wait_for_timeout(3000)

        screenshot_path = output_dir / f"viz_{name}.png"
        await page.screenshot(path=str(screenshot_path))
        print(f"Saved {screenshot_path}")
        await browser.close()


async def main():
    # Simple 2-node graph
    simple = Graph(nodes=[double, square])
    await capture_graph(simple, "simple", theme="light", width=800, height=600)

    # RAG pipeline (3 nodes)
    rag = Graph(nodes=[embed, retrieve, generate])
    await capture_graph(rag, "rag", theme="light", width=800, height=700)

    # Diamond pattern (4 nodes)
    diamond = Graph(nodes=[start, left, right, merge])
    await capture_graph(diamond, "diamond", theme="light", width=900, height=700)

    # Complex RAG (19 nodes)
    complex_rag = Graph(
        nodes=[
            load_data, clean, tokenize, chunk,
            embed_chunks, normalize, build_index,
            parse_query, embed_query, expand_query, embed_expanded,
            search_index, search_expanded, merge_results, fetch_documents,
            format_context, build_prompt, call_llm, postprocess,
        ],
        name="rag_pipeline",
    )
    print(f"Complex graph has {len(complex_rag.nodes)} nodes")
    await capture_graph(complex_rag, "complex_light", theme="light", width=1400, height=1000)
    await capture_graph(complex_rag, "complex_dark", theme="dark", width=1400, height=1000)


if __name__ == "__main__":
    asyncio.run(main())
