import re
import html
import urllib.request
import urllib.parse
import logging
from src.middleware import validate_action

logger = logging.getLogger("JanusExplorer")

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
