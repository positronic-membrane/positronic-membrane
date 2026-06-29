def web_search(query):
    results = sdk['explorer'].search(query)
    if not results:
        return f"No results found for '{query}'."
    return "\n".join([f"- Title: {r['title']}\n  URL: {r['url']}\n  Snippet: {r['snippet']}" for r in results])
