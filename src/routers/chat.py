import asyncio
import concurrent.futures
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

import src.persona
from src.daemon import reset_governor_state
from src.database import get_recent_episodic_memories, log_episodic_memory
from src.routers.dependencies import (
    ChatRequest,
    MemorySetRequest,
    get_connection,
    get_websocket_party,
    memory_orch,
    process_sandbox_updates,
    require_role,
    verify_role,
)

logger = logging.getLogger("JanusWebServer")
router = APIRouter()

@router.get("/api/history")
def get_chat_history(current_party = Depends(require_role('user'))):
    """Returns the last 50 user-visible episodic memories."""
    try:
        rows = get_recent_episodic_memories(limit=50, context_type="user_visible")
        history = []
        for speaker, msg, ts in reversed(rows):
            if speaker in ("user", "persona", "sandbox_automation", "system"):
                history.append({
                    "speaker": speaker,
                    "message": msg,
                    "timestamp": ts
                })
        return history
    except Exception as e:
        logger.error(f"Error fetching history: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/deliberations")
def get_deliberations(current_party = Depends(require_role('user'))):
    """Returns the last 20 internal swarm deliberations."""
    try:
        conn = get_connection(read_only_constitution=True)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, timestamp, proposed_action, agent_debate_json, critic_decision, utility_score, justification
            FROM internal_deliberations
            ORDER BY id DESC
            LIMIT 20;
        """)
        rows = cursor.fetchall()
        conn.close()

        deliberations = []
        for r in rows:
            debate_details = {}
            try:
                if r[3]:
                    debate_details = json.loads(r[3])
            except Exception as json_err:
                logger.warning(f"Failed to parse agent_debate_json for ID {r[0]}: {json_err}")

            deliberations.append({
                "id": r[0],
                "timestamp": r[1],
                "action": r[2],
                "debate": debate_details,
                "decision": r[4],
                "utility": r[5],
                "justification": r[6]
            })
        return deliberations
    except Exception as e:
        logger.error(f"Error fetching deliberations: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/chat")
def post_chat(data: ChatRequest, current_party = Depends(require_role('user'))):
    """Processes user chat request, logs episodic memory, and triggers response generation."""
    try:
        user_msg = data.message.strip()
        if not user_msg:
            raise HTTPException(status_code=400, detail="Message cannot be empty")

        party_id = current_party["party_id"]
        log_episodic_memory("user", user_msg, "user_visible", party_id=party_id)
        reset_governor_state("user_chat")

        if user_msg.startswith("/"):
            response = asyncio.run(src.persona.handle_web_slash_command(user_msg))
        elif src.persona.detect_metacognitive_intent(user_msg):
            response = src.persona.generate_metacognitive_narrative(user_msg)
        else:
            # Hard wall-clock cap so the response escapes before a reverse-proxy
            # (Cloudflare default: 100 s) kills the connection and returns a 524.
            # Both this value and LLM_CALL_TIMEOUT are tunable via .env.
            _chat_timeout = getattr(src.config, "CHAT_TIMEOUT", 85)
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _pool:
                _fut = _pool.submit(
                    src.persona.generate_persona_response_autonomous,
                    user_msg,
                    party_id,
                )
                try:
                    response = _fut.result(timeout=_chat_timeout)
                except concurrent.futures.TimeoutError as timeout_err:
                    raise HTTPException(
                        status_code=503,
                        detail="Response timed out — the swarm is still thinking. Try again in a moment.",
                    ) from timeout_err

        log_episodic_memory("persona", response, "user_visible", party_id=party_id)
        process_sandbox_updates(response)

        return {"response": response}
    except Exception as e:
        logger.error(f"Error processing chat POST: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/chat/stream")
async def post_chat_stream(data: ChatRequest, current_party=Depends(require_role('user'))):
    """SSE streaming chat — yields tokens progressively to prevent proxy timeouts.

    Runs the blocking persona generator in a daemon thread and bridges events into
    an async generator via an asyncio.Queue.  An immediate heartbeat comment is sent
    the moment the connection opens so Cloudflare never sees an idle origin, and a
    periodic heartbeat is sent every 15 s while the LLM is generating.
    """
    import threading

    user_msg = data.message.strip()
    if not user_msg:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    party_id = current_party["party_id"]
    log_episodic_memory("user", user_msg, "user_visible", party_id=party_id)
    reset_governor_state("user_chat")

    async def _generate():
        collected = []
        q: asyncio.Queue = asyncio.Queue()
        _DONE = object()
        loop = asyncio.get_running_loop()

        def _worker():
            try:
                if user_msg.startswith("/"):
                    new_loop = asyncio.new_event_loop()
                    try:
                        resp = new_loop.run_until_complete(
                            src.persona.handle_web_slash_command(user_msg)
                        )
                    finally:
                        new_loop.close()
                    loop.call_soon_threadsafe(q.put_nowait, ("token", resp))
                elif src.persona.detect_metacognitive_intent(user_msg):
                    resp = src.persona.generate_metacognitive_narrative(user_msg)
                    loop.call_soon_threadsafe(q.put_nowait, ("token", resp))
                else:
                    for evt, content in src.persona.stream_persona_response(user_msg, party_id):
                        if evt == "done":
                            break
                        loop.call_soon_threadsafe(q.put_nowait, (evt, content))
            except Exception as exc:
                logger.error(f"Error in streaming chat worker: {exc}", exc_info=True)
                loop.call_soon_threadsafe(q.put_nowait, ("error", str(exc)))
            finally:
                loop.call_soon_threadsafe(q.put_nowait, _DONE)

        # Send a heartbeat comment immediately — establishes the SSE connection with
        # Cloudflare before the LLM generates its first token.
        yield ": heartbeat\n\n"

        threading.Thread(target=_worker, daemon=True).start()

        while True:
            try:
                item = await asyncio.wait_for(q.get(), timeout=15)
            except asyncio.TimeoutError:
                # Keep Cloudflare from timing out during slow generation.
                yield ": heartbeat\n\n"
                continue

            if item is _DONE:
                break

            evt_type, content = item
            if evt_type == "token":
                collected.append(content)
            yield f"data: {json.dumps({'type': evt_type, 'text': content})}\n\n"

        full_response = "".join(collected)
        if full_response:
            log_episodic_memory("persona", full_response, "user_visible", party_id=party_id)
            process_sandbox_updates(full_response)
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/v1/memory/{key}")
def get_v1_memory(key: str, namespace: str = "global", current_party = Depends(require_role('user'))):
    """Fetches custom memory slot for a party (Requires user role)."""
    value = memory_orch.get_memory(current_party["party_id"], key, namespace)
    if value is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"key": key, "value": value, "namespace": namespace}


@router.post("/api/v1/memory", status_code=201)
def post_v1_memory(data: MemorySetRequest, current_party = Depends(require_role('user'))):
    """Sets a customized key-value memory for a party (Requires user role)."""
    mem_id = memory_orch.set_memory(current_party["party_id"], data.key, data.value, data.namespace)
    return {"memory_id": mem_id}


@router.websocket("/ws/deliberations")
async def websocket_deliberations(websocket: WebSocket, token: Optional[str] = Query(None)):
    """WebSocket: Streams background swarm reflection deliberations in real-time."""
    try:
        current_party = await get_websocket_party(token)
        if not verify_role(current_party["role"], "user"):
            raise HTTPException(status_code=403, detail="Forbidden")
    except Exception:
        raise HTTPException(status_code=401, detail="Unauthorized") from None

    await websocket.accept()

    last_id = 0

    # Initialize last_id to max database id
    conn = get_connection()
    try:
        row = conn.execute("SELECT MAX(id) FROM internal_deliberations").fetchone()
        if row and row[0] is not None:
            last_id = max(0, row[0] - 1)
    finally:
        conn.close()

    try:
        while True:
            # Poll DB for new deliberations
            conn = get_connection()
            try:
                rows = conn.execute(
                    "SELECT id, timestamp, proposed_action, agent_debate_json, critic_decision, utility_score, justification "
                    "FROM internal_deliberations WHERE id > ? ORDER BY id ASC", (last_id,)
                ).fetchall()
                for r in rows:
                    last_id = r[0]
                    debate_details = {}
                    try:
                        if r[3]:
                            debate_details = json.loads(r[3])
                    except Exception:
                        pass
                    msg = {
                        "id": r[0],
                        "timestamp": r[1],
                        "action": r[2],
                        "debate": debate_details,
                        "decision": r[4],
                        "utility": r[5],
                        "justification": r[6]
                    }
                    await websocket.send_json(msg)
            finally:
                conn.close()
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"Error in deliberations WebSocket: {e}")


@router.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket, token: Optional[str] = Query(None)):
    """WebSocket: Bidirectional live agent chat interaction stream."""
    try:
        current_party = await get_websocket_party(token)
        if not verify_role(current_party["role"], "user"):
            raise HTTPException(status_code=403, detail="Forbidden")
    except Exception:
        raise HTTPException(status_code=401, detail="Unauthorized") from None

    await websocket.accept()

    try:
        while True:
            data = await websocket.receive_json()
            user_msg = data.get("message", "").strip()
            if not user_msg:
                continue

            party_id = current_party["party_id"]
            log_episodic_memory("user", user_msg, "user_visible", party_id=party_id)
            reset_governor_state("user_chat")

            # Send 'thinking' state back to client
            await websocket.send_json({"event": "thinking"})

            if user_msg.startswith("/"):
                response = await src.persona.handle_web_slash_command(user_msg)
            elif src.persona.detect_metacognitive_intent(user_msg):
                response = src.persona.generate_metacognitive_narrative(user_msg)
            else:
                response = src.persona.generate_persona_response_autonomous(user_msg, party_id=party_id)

            log_episodic_memory("persona", response, "user_visible", party_id=party_id)
            process_sandbox_updates(response)

            # Stream response back
            await websocket.send_json({"event": "response", "message": response})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"Error in chat WebSocket: {e}")
