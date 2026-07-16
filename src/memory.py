import json
import logging
import time
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Optional

import chromadb
from openai import OpenAI

import src.config
from src.llm import query_agent

logger = logging.getLogger("JanusMemory")

class VectorStoreAdapter(ABC):
    @abstractmethod
    def add(self, documents, metadatas, ids, embeddings=None):
        pass

    @abstractmethod
    def query(self, query_embeddings, n_results, where=None) -> dict:
        pass

    @abstractmethod
    def get(self, ids=None, where=None) -> dict:
        pass

    @abstractmethod
    def update(self, ids, metadatas):
        pass

    @abstractmethod
    def upsert(self, documents, metadatas, ids, embeddings=None):
        pass

    @abstractmethod
    def delete(self, ids):
        pass

class ChromaCollectionWrapper(VectorStoreAdapter):
    def __init__(self, collection):
        self._collection = collection

    def add(self, documents, metadatas, ids, embeddings=None):
        self._collection.add(documents=documents, metadatas=metadatas, ids=ids, embeddings=embeddings)

    def query(self, query_embeddings, n_results, where=None) -> dict:
        return self._collection.query(query_embeddings=query_embeddings, n_results=n_results, where=where)

    def get(self, ids=None, where=None) -> dict:
        return self._collection.get(ids=ids, where=where)

    def update(self, ids, metadatas):
        self._collection.update(ids=ids, metadatas=metadatas)

    def upsert(self, documents, metadatas, ids, embeddings=None):
        self._collection.upsert(documents=documents, metadatas=metadatas, ids=ids, embeddings=embeddings)

    def delete(self, ids):
        self._collection.delete(ids=ids)

class PgVectorCollectionWrapper(VectorStoreAdapter):
    def __init__(self, name):
        self.name = name

    def add(self, documents, metadatas, ids, embeddings=None):
        from src.database import get_connection
        if embeddings is None:
            from src.memory import get_embeddings
            embeddings = get_embeddings(documents)

        conn = get_connection(read_only_constitution=False)
        try:
            with conn.cursor() as cur:
                for doc_id, doc, meta, emb in zip(ids, documents, metadatas, embeddings, strict=True):
                    emb_str = "[" + ",".join(map(str, emb)) + "]"
                    meta_str = json.dumps(meta)
                    cur.execute(
                        "INSERT INTO janus_embeddings (collection_name, id, document, metadata, embedding) VALUES (%s, %s, %s, %s, %s::vector)",
                        (self.name, doc_id, doc, meta_str, emb_str)
                    )
            conn.commit()
        finally:
            conn.close()

    def query(self, query_embeddings, n_results, where=None) -> dict:
        from src.database import get_connection
        emb = query_embeddings[0]
        emb_str = "[" + ",".join(map(str, emb)) + "]"

        where_clause, params = self._build_where_clause(where)
        sql = f"""
            SELECT id, document, metadata, (embedding <=> %s::vector) AS distance
            FROM janus_embeddings
            WHERE collection_name = %s {where_clause}
            ORDER BY distance ASC
            LIMIT %s
        """
        all_params = [emb_str, self.name] + params + [n_results]

        conn = get_connection(read_only_constitution=True)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, all_params)
                rows = cur.fetchall()
        finally:
            conn.close()

        ids = []
        documents = []
        metadatas = []
        distances = []
        for row in rows:
            ids.append(row[0])
            documents.append(row[1])
            meta = row[2]
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            elif not isinstance(meta, dict):
                meta = {}
            metadatas.append(meta)
            distances.append(row[3])

        return {
            "ids": [ids],
            "documents": [documents],
            "metadatas": [metadatas],
            "distances": [distances]
        }

    def get(self, ids=None, where=None) -> dict:
        from src.database import get_connection
        where_clause, params = self._build_where_clause(where)

        if ids:
            id_placeholders = ",".join(["%s"] * len(ids))
            where_clause += f" AND id IN ({id_placeholders})"
            params.extend(ids)

        sql = f"""
            SELECT id, document, metadata
            FROM janus_embeddings
            WHERE collection_name = %s {where_clause}
        """
        all_params = [self.name] + params

        conn = get_connection(read_only_constitution=True)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, all_params)
                rows = cur.fetchall()
        finally:
            conn.close()

        ret_ids = []
        documents = []
        metadatas = []
        for row in rows:
            ret_ids.append(row[0])
            documents.append(row[1])
            meta = row[2]
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            elif not isinstance(meta, dict):
                meta = {}
            metadatas.append(meta)

        return {
            "ids": ret_ids,
            "documents": documents,
            "metadatas": metadatas
        }

    def update(self, ids, metadatas):
        from src.database import get_connection
        conn = get_connection(read_only_constitution=False)
        try:
            with conn.cursor() as cur:
                for item_id, meta in zip(ids, metadatas, strict=True):
                    cur.execute(
                        "SELECT metadata FROM janus_embeddings WHERE collection_name = %s AND id = %s",
                        (self.name, item_id)
                    )
                    row = cur.fetchone()
                    existing_meta = {}
                    if row:
                        raw_meta = row[0]
                        if isinstance(raw_meta, str):
                            try:
                                existing_meta = json.loads(raw_meta)
                            except Exception:
                                pass
                        elif isinstance(raw_meta, dict):
                            existing_meta = raw_meta
                    existing_meta.update(meta)
                    cur.execute(
                        "UPDATE janus_embeddings SET metadata = %s WHERE collection_name = %s AND id = %s",
                        (json.dumps(existing_meta), self.name, item_id)
                    )
            conn.commit()
        finally:
            conn.close()

    def upsert(self, documents, metadatas, ids, embeddings=None):
        from src.database import get_connection
        if embeddings is None:
            from src.memory import get_embeddings
            embeddings = get_embeddings(documents)

        conn = get_connection(read_only_constitution=False)
        try:
            with conn.cursor() as cur:
                for doc_id, doc, meta, emb in zip(ids, documents, metadatas, embeddings, strict=True):
                    emb_str = "[" + ",".join(map(str, emb)) + "]"
                    meta_str = json.dumps(meta)
                    cur.execute(
                        """
                        INSERT INTO janus_embeddings (collection_name, id, document, metadata, embedding)
                        VALUES (%s, %s, %s, %s, %s::vector)
                        ON CONFLICT (collection_name, id) DO UPDATE SET
                            document = EXCLUDED.document,
                            metadata = EXCLUDED.metadata,
                            embedding = EXCLUDED.embedding
                        """,
                        (self.name, doc_id, doc, meta_str, emb_str)
                    )
            conn.commit()
        finally:
            conn.close()

    def delete(self, ids):
        from src.database import get_connection
        if not ids:
            return
        conn = get_connection(read_only_constitution=False)
        try:
            with conn.cursor() as cur:
                id_placeholders = ",".join(["%s"] * len(ids))
                cur.execute(
                    f"DELETE FROM janus_embeddings WHERE collection_name = %s AND id IN ({id_placeholders})",
                    [self.name] + list(ids)
                )
            conn.commit()
        finally:
            conn.close()

    def _build_where_clause(self, where_dict):
        if not where_dict:
            return "", []
        clauses = []
        params = []
        for k, v in where_dict.items():
            clauses.append("metadata ->> %s = %s")
            params.extend([k, str(v)])
        return " AND " + " AND ".join(clauses), params

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
    """Lazily retrieves or creates the requested vector collection."""
    global _collections
    if name not in _collections:
        db_type = getattr(src.config, "DB_TYPE", "sqlite").lower()
        if db_type == "postgres":
            _collections[name] = PgVectorCollectionWrapper(name)
        else:
            client = get_chroma_client()
            _collections[name] = ChromaCollectionWrapper(client.get_or_create_collection(name=name))
    return _collections[name]

def check_vector_connection() -> bool:
    """
    Liveness check for the vector store (ChromaDB or pgvector, per DB_TYPE).
    Used by health-check endpoints.
    """
    db_type = getattr(src.config, "DB_TYPE", "sqlite").lower()
    if db_type == "postgres":
        # Query janus_embeddings directly rather than delegating to the primary
        # DB's check_connection() — a healthy Postgres connection doesn't imply
        # the pgvector extension/table is actually present and queryable.
        from src.database import get_connection
        conn = None
        try:
            conn = get_connection(read_only_constitution=True)
            conn.execute("SELECT 1 FROM janus_embeddings LIMIT 1")
            return True
        except Exception as e:
            logger.error(f"Vector DB (pgvector) connectivity check failed: {e}")
            return False
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    try:
        get_chroma_client().heartbeat()
        return True
    except Exception as e:
        logger.error(f"Vector DB (Chroma) connectivity check failed: {e}")
        return False

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

def add_memory(content: str, metadata: dict, memory_id: str, collection_name: str = "janus_long_term", upsert: bool = False):
    """
    Generates embedding for the content and stores it in the specified vector collection.

    With upsert=False (default), an existing memory_id is left untouched — ChromaDB's add
    silently skips duplicate IDs. Pass upsert=True when the caller re-derives content for a
    stable ID and needs the stored entry refreshed (e.g. the codebase index).
    """
    logger.info(f"Ingesting semantic memory [{memory_id}] into collection '{collection_name}'...")
    embeddings = get_embeddings([content])
    embedding = embeddings[0]

    collection = get_collection(collection_name)
    write = collection.upsert if upsert else collection.add
    write(
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

        for doc, meta, memory_id, dist in zip(docs, metas, ids, distances, strict=True):
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
        You are the Archivist. Synthesize the following granular background memory entries into a single,
        cohesive, high-level Primary Concept (under 2 sentences).

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

    for _i, topic in enumerate(new_topics):
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
    for doc, meta, topic_id in zip(docs, metas, ids, strict=True):
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

def index_skills_to_vector_db():
    """
    Reads active skills from SQLite and indexes them semantically in 'janus_skills' ChromaDB collection.
    """
    from src.database import get_connection
    logger.info("Semantic indexing of dynamic skills into ChromaDB 'janus_skills'...")
    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT skill_id, name, description, parameters_schema, required_role FROM agent_skills WHERE is_active = 1;")
        rows = cursor.fetchall()
    finally:
        conn.close()

    if not rows:
        logger.info("No active skills found to index.")
        return

    ids = []
    documents = []
    metadatas = []

    for skill_id, name, description, schema, role in rows:
        doc = f"Skill: {name}\nDescription: {description}\nParameters Schema: {schema}"
        ids.append(skill_id)
        documents.append(doc)
        metadatas.append({
            "skill_id": skill_id,
            "required_role": role,
            "name": name
        })

    try:
        embeddings = get_embeddings(documents)
        collection = get_collection("janus_skills")
        collection.upsert(
            documents=documents,
            metadatas=metadatas,
            ids=ids,
            embeddings=embeddings
        )
        logger.info(f"Successfully indexed {len(ids)} skills in vector DB.")
    except Exception as e:
        logger.error(f"Failed to semantically index dynamic skills: {e}", exc_info=True)


def _summarize_and_delete(conn, cursor, rows, batch_label: str):
    """Shared Archivist-summarize-then-delete step for one context_type batch."""
    if not rows:
        conn.close()
        return

    memories_summary = "\n".join([f"[{row[3]}] {row[1]}: {row[2]}" for row in rows])

    archivist_prompt = f"""
    You are the Archivist. Synthesize the following sequence of {batch_label} log entries into a single,
    cohesive, high-level Primary Concept summary (under 2 sentences).

    EPISODIC LOG ENTRIES:
    {memories_summary}

    Respond with the synthesized Primary Concept directly. Do not include agent names, prefixes, quotes, or JSON.
    """

    try:
        primary_concept = query_agent("archivist", archivist_prompt).strip()

        # Save Primary Concept to janus_long_term
        concept_id = f"episodic_{uuid.uuid4()}"
        concept_metadata = {
            "type": "episodic_summary",
            "context_type": batch_label,
            "timestamp": time.time(),
            "start_time": str(rows[0][3]),
            "end_time": str(rows[-1][3])
        }
        add_memory(primary_concept, concept_metadata, concept_id, "janus_long_term")

        # Delete summarized rows
        ids_to_delete = [row[0] for row in rows]
        placeholders = ",".join(["?"] * len(ids_to_delete))
        cursor.execute(f"DELETE FROM episodic_memory WHERE id IN ({placeholders});", ids_to_delete)
        conn.commit()
        logger.info(f"Successfully compressed {len(ids_to_delete)} '{batch_label}' episodic memories into Primary Concept: '{primary_concept}'")
    except Exception as e:
        logger.error(f"Error during {batch_label} episodic memory compression: {e}", exc_info=True)
    finally:
        conn.close()


def _compress_background_thoughts(limit: int = 50, keep_recent: int = 10):
    """
    Checks the row count of 'background_thought' episodic_memory rows. If it
    exceeds limit, summarizes the oldest (count - keep_recent) rows into a
    Primary Concept and deletes them.
    """
    from src.database import get_connection
    logger.info("Checking background_thought episodic memory for compression...")

    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT COUNT(*) FROM episodic_memory WHERE context_type = 'background_thought';")
        count = cursor.fetchone()[0]
    except Exception as e:
        logger.error(f"Failed to query background_thought count: {e}")
        conn.close()
        return

    if count <= limit:
        logger.info(f"background_thought count ({count}) does not exceed limit ({limit}). No compression needed.")
        conn.close()
        return

    num_to_compress = count - keep_recent
    logger.info(f"background_thought count ({count}) exceeds limit ({limit}). Compressing oldest {num_to_compress} records...")

    try:
        cursor.execute("""
            SELECT id, speaker, message_content, timestamp, context_type
            FROM episodic_memory
            WHERE context_type = 'background_thought'
            ORDER BY id ASC
            LIMIT ?;
        """, (num_to_compress,))
        rows = cursor.fetchall()
    except Exception as e:
        logger.error(f"Failed to fetch background_thought memories for compression: {e}")
        conn.close()
        return

    _summarize_and_delete(conn, cursor, rows, batch_label="background_thought")


def _compress_user_visible_chat(min_rows: Optional[int] = None, min_age_days: Optional[int] = None):
    """
    Compresses 'user_visible' episodic_memory rows using a row-count AND
    age-based threshold (issue #54) — a row is only eligible for compression
    if it is BOTH beyond the row-count keep-window AND older than
    min_age_days, so recent chat is never touched purely due to volume.
    """
    from src.database import get_connection
    logger.info("Checking user_visible episodic memory for compression...")

    conn = get_connection(read_only_constitution=True)
    cursor = conn.cursor()

    if min_rows is None or min_age_days is None:
        try:
            cursor.execute(
                "SELECT config_key, config_value FROM system_config "
                "WHERE config_key IN ('memory.chat_history_min_rows', 'memory.chat_history_min_age_days');"
            )
            config_rows = dict(cursor.fetchall())
        except Exception as e:
            logger.error(f"Failed to read chat history compression config, using defaults: {e}")
            config_rows = {}
        if min_rows is None:
            try:
                min_rows = int(config_rows.get("memory.chat_history_min_rows", 500))
            except (TypeError, ValueError) as e:
                logger.error(f"Invalid memory.chat_history_min_rows value {config_rows.get('memory.chat_history_min_rows')!r}, using default 500: {e}")
                min_rows = 500
        if min_age_days is None:
            try:
                min_age_days = int(config_rows.get("memory.chat_history_min_age_days", 30))
            except (TypeError, ValueError) as e:
                logger.error(f"Invalid memory.chat_history_min_age_days value {config_rows.get('memory.chat_history_min_age_days')!r}, using default 30: {e}")
                min_age_days = 30

    try:
        cursor.execute("SELECT COUNT(*) FROM episodic_memory WHERE context_type = 'user_visible';")
        count = cursor.fetchone()[0]
    except Exception as e:
        logger.error(f"Failed to query user_visible count: {e}")
        conn.close()
        return

    if count <= min_rows:
        logger.info(f"user_visible count ({count}) does not exceed min_rows ({min_rows}). No compression needed.")
        conn.close()
        return

    num_beyond_window = count - min_rows
    cutoff_str = (datetime.now(timezone.utc) - timedelta(days=min_age_days)).strftime('%Y-%m-%d %H:%M:%S')

    try:
        # Bounding by id ASC + LIMIT num_beyond_window restricts the candidate
        # set to the oldest rows beyond the row-count keep-window; the
        # timestamp < cutoff_str predicate additionally requires them to be
        # older than min_age_days. id and timestamp both increase
        # monotonically on insert, so this single query correctly implements
        # the AND of both thresholds.
        cursor.execute("""
            SELECT id, speaker, message_content, timestamp, context_type
            FROM episodic_memory
            WHERE context_type = 'user_visible' AND timestamp < ?
            ORDER BY id ASC
            LIMIT ?;
        """, (cutoff_str, num_beyond_window))
        rows = cursor.fetchall()
    except Exception as e:
        logger.error(f"Failed to fetch user_visible memories for compression: {e}")
        conn.close()
        return

    if not rows:
        logger.info(f"user_visible rows beyond the row window are not older than {min_age_days} days. No compression needed.")
        conn.close()
        return

    _summarize_and_delete(conn, cursor, rows, batch_label="user_visible")


def compress_episodic_memory(
    limit: int = 50,
    keep_recent: int = 10,
    chat_min_rows: Optional[int] = None,
    chat_min_age_days: Optional[int] = None,
):
    """
    Compresses old episodic_memory rows into vector-store Primary Concepts,
    running two independent passes so the daemon's background_thought volume
    never causes user-visible chat history to be summarized/deleted (issue #54):

    - background_thought: simple count-based threshold (limit/keep_recent).
    - user_visible: row-count AND age-based threshold, driven by
      system_config keys memory.chat_history_min_rows /
      memory.chat_history_min_age_days.
    """
    _compress_background_thoughts(limit=limit, keep_recent=keep_recent)
    _compress_user_visible_chat(min_rows=chat_min_rows, min_age_days=chat_min_age_days)
