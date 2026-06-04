import logging
import chromadb
from openai import OpenAI
import src.config

logger = logging.getLogger("JanusMemory")

# Persistent ChromaDB client initialized lazily
_chroma_client = None
_collection = None

def get_chroma_client():
    """Lazily initializes and returns the ChromaDB persistent client."""
    global _chroma_client
    if _chroma_client is None:
        logger.info(f"Initializing ChromaDB client at: {src.config.VECTOR_DB_PATH}")
        _chroma_client = chromadb.PersistentClient(path=src.config.VECTOR_DB_PATH)
    return _chroma_client

def get_collection():
    """Lazily retrieves or creates the janus_long_term collection."""
    global _collection
    if _collection is None:
        client = get_chroma_client()
        _collection = client.get_or_create_collection(name="janus_long_term")
    return _collection

def get_embeddings(texts: list) -> list:
    """
    Queries the OpenAI-compatible endpoint to generate vectors for the input texts.
    """
    try:
        client = OpenAI(
            base_url=src.config.LLM_BASE_URL,
            api_key=src.config.LLM_API_KEY
        )
        response = client.embeddings.create(
            model=src.config.EMBEDDING_MODEL,
            input=texts
        )
        return [data.embedding for data in response.data]
    except Exception as e:
        logger.error(f"Failed to generate embeddings via endpoint: {e}", exc_info=True)
        raise RuntimeError(f"Embedding generation failed: {e}") from e

def add_memory(content: str, metadata: dict, memory_id: str):
    """
    Generates embedding for the content and stores it in the vector collection.
    """
    logger.info(f"Ingesting semantic memory [{memory_id}] into vector DB...")
    embeddings = get_embeddings([content])
    embedding = embeddings[0]
    
    collection = get_collection()
    collection.add(
        documents=[content],
        metadatas=[metadata],
        ids=[memory_id],
        embeddings=[embedding]
    )
    logger.info("Memory ingestion complete.")

def query_memories(query_text: str, limit: int = 5) -> list:
    """
    Queries the ChromaDB collection using a dynamically generated embedding of the query_text.
    Returns a list of dictionaries with content, metadata, ID, and distance score.
    """
    logger.info(f"Querying semantic memory for: '{query_text}'")
    embeddings = get_embeddings([query_text])
    embedding = embeddings[0]
    
    collection = get_collection()
    results = collection.query(
        query_embeddings=[embedding],
        n_results=limit
    )
    
    formatted = []
    if results and "documents" in results and results["documents"] and len(results["documents"]) > 0:
        docs = results["documents"][0]
        metas = results["metadatas"][0] if "metadatas" in results and results["metadatas"] else [{}] * len(docs)
        ids = results["ids"][0] if "ids" in results and results["ids"] else [str(i) for i in range(len(docs))]
        distances = results["distances"][0] if "distances" in results and results["distances"] else [0.0] * len(docs)
        
        for doc, meta, memory_id, dist in zip(docs, metas, ids, distances):
            if dist <= src.config.MEMORY_RELEVANCE_THRESHOLD:
                formatted.append({
                    "id": memory_id,
                    "content": doc,
                    "metadata": meta,
                    "distance": dist
                })
            
    logger.info(f"Memory query returned {len(formatted)} matches.")
    return formatted
