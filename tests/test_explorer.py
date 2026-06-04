import pytest
from unittest.mock import MagicMock, patch
import src.config
from src.database import init_db, add_constitution_rule
from src.middleware import SafetyViolationError
from src.explorer import clean_html, fetch_webpage

@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Isolate DB settings for testing."""
    temp_db = tmp_path / "test_janus.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)
    init_db()
    yield
    src.config.DB_PATH = orig_db_path

def test_clean_html():
    """Verify script tags, style blocks, and HTML structures are cleanly stripped."""
    raw_html = """
    <html>
      <head>
        <style>body { font-family: sans-serif; }</style>
      </head>
      <body>
        <h1>Janus Swarm</h1>
        <p>Testing HTML parsing capabilities &amp; regex stripping speed.</p>
        <script>console.log("Bypassed Script");</script>
        <br/>
        <div>Footer content</div>
      </body>
    </html>
    """
    cleaned = clean_html(raw_html)
    
    # Check that style and script blocks are gone
    assert "font-family" not in cleaned
    assert "console.log" not in cleaned
    assert "Bypassed Script" not in cleaned
    
    # Check that plain text contents remain and entities are unescaped
    assert "Janus Swarm" in cleaned
    assert "Testing HTML parsing capabilities & regex stripping speed." in cleaned
    assert "Footer content" in cleaned

def test_restricted_domain_veto():
    """Verify that fetch_webpage raises SafetyViolationError for banned URLs."""
    # Commit banned boundary
    add_constitution_rule("banned_boundaries", "spy-domain.ru")
    
    # Attempting to fetch a banned URL must raise SafetyViolationError immediately
    with pytest.raises(SafetyViolationError):
        fetch_webpage("http://spy-domain.ru/leaks.txt")

@patch("urllib.request.urlopen")
def test_successful_webpage_fetch(mock_urlopen):
    """Verify fetch_webpage runs standard HTTP calls, parses response, and returns plain text."""
    # Setup mock urlopen context manager
    mock_response = MagicMock()
    mock_response.__enter__.return_value = mock_response
    mock_response.headers.get_content_charset.return_value = "utf-8"
    mock_response.read.return_value = b"<html><body><h1>SAFE WEB PAGE</h1></body></html>"
    mock_urlopen.return_value = mock_response
    
    # Fetch safe URL
    result = fetch_webpage("http://safe-site.com/page")
    
    assert result == "SAFE WEB PAGE"
    mock_urlopen.assert_called_once()

@patch("urllib.request.urlopen")
def test_search_web(mock_urlopen):
    """Verify search_web scrapes DuckDuckGo HTML and filters out restricted results."""
    mock_response = MagicMock()
    mock_response.__enter__.return_value = mock_response
    mock_response.headers.get_content_charset.return_value = "utf-8"
    
    # Mock HTML with one safe result and one default blocked result (facebook.com)
    mock_response.read.return_value = b"""
    <html>
      <body>
        <div class="result ">
          <a class="result__snippet" href="//duckduckgo.com/l/?uddg=http%3A%2F%2Fsafe-result.com%2Fpage">Safe Title</a>
          <span class="result__snippet">Safe description snippet</span>
        </div>
        <div class="result ">
          <a class="result__snippet" href="//duckduckgo.com/l/?uddg=http%3A%2F%2Ffacebook.com%2Fpage">Unsafe Title</a>
          <span class="result__snippet">Unsafe description snippet</span>
        </div>
      </body>
    </html>
    """
    mock_urlopen.return_value = mock_response
    
    from src.explorer import search_web
    results = search_web("test query")
    
    # Should only return the safe result because facebook.com is default-blocked
    assert len(results) == 1
    assert results[0]["title"] == "Safe Title"
    assert results[0]["url"] == "http://safe-result.com/page"
    assert results[0]["snippet"] == "Safe description snippet"

