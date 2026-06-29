def fetch_url(url):
    content = sdk['explorer'].fetch(url)
    return content[:1500] + "..." if len(content) > 1500 else content
