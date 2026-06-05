import time
import logging
import uuid
import chromadb
from openai import OpenAI
import src.config
from src.llm import query_agent

logger = logging.getLogger("JanusMemory")

# Persistent ChromaDB client initialized lazily
_chroma_client = None
_collections = {}

def get_chroma_client():
    """Lazily initializes and returns the ChromaDB persistent client."""
    global _chroma_client
    if _chroma_client is None:
        logger.info(f"Initializing ChromaDB client at: {src.config.VECTOR_DB_PATH}")
        _chroma_client = chromadb.PersistentClient(path=src.config.VECTOR_DB_PATH)
    return _chroma_client

def get_collection(name: str = "janus_long_term"):
    """Lazily retrieves or creates the requested ChromaDB collection."""
    global _collections
    if name not in _collections:
        client = get_chroma_client()
        _collections[name] = client.get_or_create_collection(name=name)
    return _collections[name]

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

def add_memory(content: str, metadata: dict, memory_id: str, collection_name: str = "janus_long_term"):
    """
    Generates embedding for the content and stores it in the specified vector collection.
    """
    logger.info(f"Ingesting semantic memory [{memory_id}] into collection '{collection_name}'...")
    embeddings = get_embeddings([content])
    embedding = embeddings[0]
    
    collection = get_collection(collection_name)
    collection.add(
        documents=[content],
        metadatas=[metadata],
        ids=[memory_id],
        embeddings=[embedding]
    )
    logger.info(f"Memory ingestion into '{collection_name}' complete.")

def query_memories(query_text: str, limit: int = 5, collection_name: str = "janus_long_term") -> list:
    """
    Queries the specified ChromaDB collection using a dynamically generated embedding of the query_text.
    Returns a list of dictionaries with content, metadata, ID, and distance score.
    """
    logger.info(f"Querying semantic memory in '{collection_name}' for: '{query_text}'")
    embeddings = get_embeddings([query_text])
    embedding = embeddings[0]
    
    collection = get_collection(collection_name)
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

def consolidate_memories(batch_size: int = 5):
    """
    Fetches unconsolidated detailed memories from janus_details,
    sends them to the Archivist to generate a high-level Primary Concept,
    stores the Primary Concept in janus_long_term, and marks the details as consolidated.
    """
    logger.info("Checking for detailed memories to consolidate...")
    details_collection = get_collection("janus_details")
    
    # Retrieve unconsolidated detailed memories
    try:
        results = details_collection.get(where={"consolidated": "false"})
    except Exception as e:
        logger.error(f"Failed to retrieve unconsolidated memories: {e}")
        return
        
    if not results or "documents" not in results or not results["documents"]:
        logger.info("No unconsolidated memories found in janus_details.")
        return
        
    documents = results["documents"]
    ids = results["ids"]
    metadatas = results["metadatas"]
    
    # Process in batches
    for i in range(0, len(ids), batch_size):
        batch_ids = ids[i:i+batch_size]
        batch_docs = documents[i:i+batch_size]
        batch_metas = metadatas[i:i+batch_size]
        
        logger.info(f"Consolidating batch of {len(batch_ids)} detailed memories...")
        memories_summary = "\n".join([f"- {doc}" for doc in batch_docs])
        
        archivist_prompt = f"""
        You are the Archivist. Synthesize the following granular background memory entries into a single, cohesive, high-level Primary Concept (under 2 sentences).
        
        GRANULAR BACKGROUND MEMORIES:
        {memories_summary}
        
        Respond with the Primary Concept directly. Do not include agent names, prefixes, or JSON.
        """
        
        try:
            primary_concept = query_agent("archivist", archivist_prompt).strip()
            
            # Save Primary Concept to janus_long_term
            concept_id = f"concept_{uuid.uuid4()}"
            concept_metadata = {
                "type": "primary_concept",
                "detail_ids": ",".join(batch_ids),
                "timestamp": time.time()
            }
            add_memory(primary_concept, concept_metadata, concept_id, "janus_long_term")
            
            # Update detail metadatas to consolidated = "true"
            updated_metas = []
            for meta in batch_metas:
                meta = meta.copy() if meta else {}
                meta["consolidated"] = "true"
                meta["primary_concept_id"] = concept_id
                updated_metas.append(meta)
                
            details_collection.update(ids=batch_ids, metadatas=updated_metas)
            logger.info(f"Consolidated batch successfully. Created Primary Concept: '{primary_concept}'")
            
        except Exception as e:
            logger.error(f"Error during consolidation batch: {e}", exc_info=True)

def update_curiosity_topics(new_topics: list, similarity_threshold: float = 0.8):
    """
    Adds new curiosity topics to ChromaDB 'janus_curiosity' collection.
    If a topic is semantically similar to an existing active one, it merges them
    by incrementing the relevance count.
    """
    logger.info(f"Processing {len(new_topics)} new curiosity topics for semantic clustering...")
    curiosity_collection = get_collection("janus_curiosity")
    
    for i, topic in enumerate(new_topics):
        topic = topic.strip()
        if not topic:
            continue
            
        # Generate embedding
        try:
            embeddings = get_embeddings([topic])
            embedding = embeddings[0]
        except Exception as e:
            logger.error(f"Failed to generate embedding for curiosity topic '{topic}': {e}")
            continue
            
        # Search for similar active curiosity topics
        similar_found = False
        try:
            results = curiosity_collection.query(
                query_embeddings=[embedding],
                n_results=1,
                where={"resolved": "false"}
            )
            
            if results and "distances" in results and results["distances"] and results["distances"][0]:
                distance = results["distances"][0][0]
                matched_id = results["ids"][0][0]
                matched_doc = results["documents"][0][0]
                matched_meta = results["metadatas"][0][0]
                
                # Check cosine distance threshold (distance <= 1.0 - threshold)
                if distance <= (1.0 - similarity_threshold):
                    logger.info(f"Semantically merged topic '{topic}' with existing '{matched_doc}' (distance: {distance:.3f})")
                    # Increment relevance count
                    new_meta = matched_meta.copy() if matched_meta else {}
                    new_meta["relevance_count"] = new_meta.get("relevance_count", 1) + 1
                    new_meta["timestamp"] = time.time()
                    curiosity_collection.update(ids=[matched_id], metadatas=[new_meta])
                    similar_found = True
        except Exception as e:
            logger.error(f"Error querying similar curiosity topics: {e}")
            
        if not similar_found:
            # Add as a new curiosity topic
            topic_id = f"cur_{uuid.uuid4()}"
            metadata = {
                "relevance_count": 1,
                "timestamp": time.time(),
                "resolved": "false"
            }
            curiosity_collection.add(
                documents=[topic],
                metadatas=[metadata],
                ids=[topic_id],
                embeddings=[embedding]
            )
            logger.info(f"Added new curiosity topic: '{topic}'")

def get_active_curiosity_topics(limit: int = 5) -> list:
    """
    Retrieves active curiosity topics from ChromaDB, sorted by relevance_count
    and timestamp, returning a list of plain strings.
    """
    logger.info("Retrieving active curiosity topics...")
    curiosity_collection = get_collection("janus_curiosity")
    
    try:
        results = curiosity_collection.get(where={"resolved": "false"})
    except Exception as e:
        logger.error(f"Failed to fetch active curiosity topics: {e}")
        return []
        
    if not results or "documents" not in results or not results["documents"]:
        return []
        
    docs = results["documents"]
    metas = results["metadatas"]
    ids = results["ids"]
    
    # Pack into list of dicts for sorting
    packed = []
    for doc, meta, topic_id in zip(docs, metas, ids):
        packed.append({
            "id": topic_id,
            "document": doc,
            "relevance_count": meta.get("relevance_count", 1) if meta else 1,
            "timestamp": meta.get("timestamp", 0.0) if meta else 0.0
        })
        
    # Sort: relevance_count DESC, timestamp DESC
    packed.sort(key=lambda x: (x["relevance_count"], x["timestamp"]), reverse=True)
    
    return [item["document"] for item in packed[:limit]]

def orchestrate_workspace_snapshot(changes: dict) -> None:
    """
    Callback triggered by the DirectoryWatcher.
    Constructs a point-in-time JSON snapshot of changes and writes it to `.janus_snapshots/`.
    """
    import json
    from pathlib import Path
    
    logger.info(f"MemoryOrchestrator intercepting changes: {changes}")
    
    snapshots_dir = src.config.ROOT_DIR / ".janus_snapshots"
    snapshots_dir.mkdir(exist_ok=True)
    
    # Read content for added and modified files
    contents = {}
    for filepath in changes.get("added", []) + changes.get("modified", []):
        try:
            rel_path = Path(filepath).relative_to(src.config.ROOT_DIR)
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                contents[str(rel_path)] = f.read()
        except Exception as e:
            logger.error(f"Failed to read file content for snapshot {filepath}: {e}")
            
    # Relativize the added/removed/modified lists
    rel_changes = {
        "added": [],
        "removed": [],
        "modified": []
    }
    for key in ("added", "removed", "modified"):
        for path in changes.get(key, []):
            try:
                rel = Path(path).relative_to(src.config.ROOT_DIR)
                rel_changes[key].append(str(rel))
            except ValueError:
                # If file is not in workspace root, use basename
                rel_changes[key].append(Path(path).name)
                
    snapshot_data = {
        "timestamp": time.time(),
        "changes": rel_changes,
        "contents": contents
    }
    
    snapshot_filename = f"snapshot_{int(time.time())}_{uuid.uuid4().hex[:8]}.json"
    snapshot_path = snapshots_dir / snapshot_filename
    try:
        with open(snapshot_path, "w", encoding="utf-8") as f:
            json.dump(snapshot_data, f, indent=2)
        logger.info(f"Successfully wrote point-in-time snapshot to {snapshot_path}")
    except Exception as e:
        logger.error(f"Failed to write snapshot file {snapshot_path}: {e}")