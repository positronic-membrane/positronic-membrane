import html
import json
import logging
import re
import urllib.parse
import urllib.request

import src.config
from src.database import get_connection
from src.epistemic import neo4j_available, run_epistemic_pipeline
from src.llm import query_agent
from src.middleware import validate_action

logger = logging.getLogger("JanusExplorer")

# Truncation bound for content handed to the fact-extraction LLM call.
EXTRACTION_CONTENT_MAX_CHARS = 6000

# Fallbacks when the seeded system_config rows are missing/unreadable.
DEFAULT_MAX_FACTS_PER_CYCLE = 3
DEFAULT_MAX_FACTS_PER_DAY = 25

# Sources written by autonomous ingestion; the daily volume cap counts these.
AUTONOMOUS_SOURCES = ("web_search", "web_fetch")

def clean_html(html_content: str) -> str:
    """
    Cleans raw HTML content by removing scripts, styling blocks, 
    and stripping all HTML tags to return readable plain text.
    """
    # Unescape HTML entities first
    text = html.unescape(html_content)
    
    # Remove script and style tags and their contents
    text = re.sub(r"<(script|style)\b[^>]*>([\s\S]*?)<\/\1>", "", text, flags=re.IGNORECASE)
    
    # Replace block-level tags with newlines to preserve spacing
    text = re.sub(r"<br\s*\/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<\/?(p|div|h[1-6]|li|tr|table|ul|ol)\b[^>]*>", "\n", text, flags=re.IGNORECASE)
    
    # Strip all remaining HTML tags
    text = re.sub(r"<[^>]+>", "", text)
    
    # Clean up excess whitespace and redundant newlines
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    
    return text.strip()

def fetch_webpage(url: str, timeout: int = 10) -> str:
    """
    Verifies the URL against the safety middleware, fetches the webpage 
    using urllib, and sanitizes the HTML to return clean text.
    """
    # 1. Enforce strict safety boundary check via middleware
    validate_action(url)
    
    logger.info(f"Fetching webpage: {url}")
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            raw_html = response.read().decode(charset, errors="ignore")
            
        plain_text = clean_html(raw_html)
        logger.info(f"Successfully fetched and parsed webpage. Character length: {len(plain_text)}")
        return plain_text
        
    except Exception as e:
        logger.error(f"Error fetching webpage from {url}: {e}", exc_info=True)
        raise RuntimeError(f"Webpage fetch failed: {e}") from e

def search_web(query: str, max_results: int = 5) -> list:
    """
    Queries DuckDuckGo HTML search page, parses results, and checks safety middleware.
    Returns list of dicts containing 'title', 'url', and 'snippet'.
    """
    logger.info(f"Searching web for query: '{query}'")
    
    import urllib.parse
    import urllib.request
    
    # URL encode query
    query_encoded = urllib.parse.quote_plus(query)
    search_url = f"https://html.duckduckgo.com/html/?q={query_encoded}"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    try:
        req = urllib.request.Request(search_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            html_content = response.read().decode(charset, errors="ignore")
    except Exception as e:
        logger.error(f"Failed to query DuckDuckGo search: {e}", exc_info=True)
        raise RuntimeError(f"Search query failed: {e}") from e
        
    # Find all result divs
    result_blocks = html_content.split('class="result ')
    results = []
    
    for block in result_blocks[1:]:
        url_match = re.search(r'href="([^"]*)"', block)
        title_match = re.search(r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', block, re.DOTALL)
        # Fallback to result__title h2 link
        if not title_match:
            title_match = re.search(r'<h2 class="result__title">.*?<a[^>]*>(.*?)</a>', block, re.DOTALL)
        snippet_match = re.search(r'<span class="result__snippet"[^>]*>(.*?)</span>', block, re.DOTALL)
        
        if url_match and title_match:
            url = url_match.group(1)
            # Clean redirect links
            if "uddg=" in url:
                parsed_url = urllib.parse.urlparse(url)
                query_params = urllib.parse.parse_qs(parsed_url.query)
                if "uddg" in query_params:
                    url = query_params["uddg"][0]
                    
            # Strip tags
            title = re.sub(r'<[^>]+>', '', title_match.group(1))
            title = html.unescape(title).strip()
            
            snippet = ""
            if snippet_match:
                snippet = re.sub(r'<[^>]+>', '', snippet_match.group(1))
                snippet = html.unescape(snippet).strip()
                
            # Filter results using safety middleware
            try:
                validate_action(url)
                results.append({
                    "title": title,
                    "url": url,
                    "snippet": snippet
                })
            except Exception:
                continue
                
            if len(results) >= max_results:
                break
                
    logger.info(f"Web search retrieved {len(results)} safe results.")
    return results


def _get_config_int(config_key: str, default: int) -> int:
    """Reads an integer value from system_config, falling back to `default`."""
    try:
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT config_value FROM system_config WHERE config_key = ?;",
                (config_key,),
            ).fetchone()
        finally:
            conn.close()
        if row and str(row[0]).strip():
            return max(0, int(row[0]))
    except Exception as e:
        logger.error(f"Failed to read {config_key}, using default {default}: {e}")
    return default


def get_max_facts_per_cycle() -> int:
    """
    Per-call cap on facts submitted to the epistemic pipeline. Phases 2-3 each
    cost an LLM call per fact, so this is the budget guard for a single
    exploration action. 0 disables ingestion.
    """
    return _get_config_int("epistemic.max_facts_per_cycle", DEFAULT_MAX_FACTS_PER_CYCLE)


def get_max_facts_per_day() -> int:
    """
    Rolling 24h cap on autonomously ingested facts, counted from
    janus_sandbox_facts rows. Bounds total spend across however many
    exploration actions the daemon issues. 0 disables ingestion.
    """
    return _get_config_int("epistemic.max_facts_per_day", DEFAULT_MAX_FACTS_PER_DAY)


def _facts_staged_last_day() -> int:
    """Counts autonomously staged facts in the last 24h (any status)."""
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    placeholders = ", ".join("?" for _ in AUTONOMOUS_SOURCES)
    conn = get_connection()
    try:
        row = conn.execute(
            f"SELECT COUNT(*) FROM janus_sandbox_facts WHERE source IN ({placeholders}) AND created_at >= ?;",
            (*AUTONOMOUS_SOURCES, cutoff),
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _fact_already_staged(fact_text: str) -> bool:
    """Exact-match dedupe so re-encountered facts don't re-spend pipeline LLM
    calls or mint duplicate Neo4j nodes."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM janus_sandbox_facts WHERE fact_text = ? LIMIT 1;",
            (fact_text,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _parse_fact_array(raw: str) -> list:
    """Finds and decodes the first JSON array in an LLM reply, tolerating code
    fences and surrounding prose (including bracketed prose before the array)."""
    decoder = json.JSONDecoder()
    idx = raw.find("[")
    attempts = 0
    while idx != -1 and attempts < 5:
        try:
            parsed, _ = decoder.raw_decode(raw, idx)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass
        idx = raw.find("[", idx + 1)
        attempts += 1
    return []


def extract_candidate_facts(content: str, max_facts: int) -> list:
    """
    Uses the Explorer agent to distill exploration content (search results or
    fetched page text) into at most `max_facts` discrete candidate facts.
    Returns a list of fact strings; returns [] if nothing parseable comes back.
    """
    if not content or not content.strip():
        return []

    snippet = content[:EXTRACTION_CONTENT_MAX_CHARS]
    prompt = (
        "Extract the most significant discrete factual claims from the following "
        "exploration content. Only include verifiable statements of fact — no opinions, "
        "instructions, navigation text, or speculation.\n\n"
        f"CONTENT:\n{snippet}\n\n"
        f"Respond with a JSON array of at most {max_facts} strings, one fact per string. "
        'Example: ["Fact one.", "Fact two."] '
        "If the content contains no noteworthy facts, respond with []."
    )
    raw = query_agent("explorer", prompt)

    parsed = _parse_fact_array(raw)
    if not parsed:
        logger.info("Fact extraction returned no parseable JSON array; skipping ingestion.")
        return []

    facts = [f.strip() for f in parsed if isinstance(f, str) and f.strip()]
    return facts[:max_facts]


def ingest_discoveries(
    content: str,
    source: str,
    source_url: str = None,
    raw_metadata: dict = None,
) -> dict:
    """
    Routes facts discovered during web research / curiosity exploration into the
    Epistemic Ingestion Pipeline (V2-T5b, issue #74).

    Never raises: a Neo4j outage or LLM failure must not break the exploration
    cycle that triggered it. Failed facts remain staged in janus_sandbox_facts
    at their last non-assimilated status for later retry/inspection.
    """
    summary = {
        "extracted": 0, "assimilated": 0, "rejected": 0, "failed": 0,
        "duplicates": 0, "blocked": 0, "row_ids": [],
    }
    try:
        if not src.config.NEO4J_URI:
            summary["skipped"] = "neo4j_not_configured"
            return summary

        max_facts = get_max_facts_per_cycle()
        max_daily = get_max_facts_per_day()
        if max_facts <= 0 or max_daily <= 0:
            summary["skipped"] = "ingestion_disabled"
            return summary

        staged_today = _facts_staged_last_day()
        if staged_today >= max_daily:
            logger.info(
                "Epistemic ingestion skipped: daily volume cap reached (%d/%d).",
                staged_today, max_daily,
            )
            summary["skipped"] = "daily_cap_reached"
            return summary
        max_facts = min(max_facts, max_daily - staged_today)

        # Probe Neo4j before spending the extraction LLM call — facts staged
        # while the graph store is down can only die at Phase 2.
        if not neo4j_available():
            summary["skipped"] = "neo4j_unreachable"
            return summary

        facts = extract_candidate_facts(content, max_facts)
        summary["extracted"] = len(facts)

        for fact in facts:
            try:
                # Same content-scanning convention as other agent-facing write
                # paths: banned-boundary facts never reach the pipeline.
                validate_action(fact)
            except Exception as e:
                summary["blocked"] += 1
                logger.warning(f"Middleware blocked discovered fact '{fact[:80]}': {e}")
                continue

            if _fact_already_staged(fact):
                summary["duplicates"] += 1
                continue

            try:
                result = run_epistemic_pipeline(
                    fact,
                    source=source,
                    source_url=source_url,
                    raw_metadata=raw_metadata or {},
                )
                summary["row_ids"].append(result.get("row_id"))
                if result.get("outcome") == "assimilated":
                    summary["assimilated"] += 1
                else:
                    summary["rejected"] += 1
            except Exception as e:
                # Staged row (if phase 1 completed) stays at a non-assimilated
                # status — do not abort the remaining facts or the caller.
                summary["failed"] += 1
                logger.error(f"Epistemic pipeline failed for discovered fact '{fact[:80]}': {e}")

        if summary["extracted"]:
            logger.info(
                "Epistemic ingestion from '%s': %d extracted, %d assimilated, %d rejected, "
                "%d failed, %d duplicates, %d blocked",
                source, summary["extracted"], summary["assimilated"], summary["rejected"],
                summary["failed"], summary["duplicates"], summary["blocked"],
            )
    except Exception as e:
        summary["error"] = str(e)
        logger.error(f"Epistemic ingestion from '{source}' aborted: {e}")
    return summary
