#!/usr/bin/env python3
"""
mock_server.py — Flask app serving 50 mock web pages with links between them.

Simulates a small web for the crawler lab:
  - GET /            → index page with links to first 10 pages
  - GET /page/<n>    → page N with links to other pages (deterministic graph)
  - GET /robots.txt  → disallows /private/*; sets Crawl-delay: 1
  - GET /sitemap.xml → XML sitemap listing all 50 pages
  - GET /private/<p> → page that should NOT be crawled (robots.txt blocked)
  - GET /trap        → links to /trap?page=1, /trap?page=2, etc (crawler trap)
  - GET /health      → 200 OK (used by Docker healthcheck)
"""

import random
from flask import Flask, Response, request

app = Flask(__name__)

NUM_PAGES = 50

random.seed(42)

# Pre-generate a fixed link graph so crawls are deterministic
LINKS = {}
for i in range(NUM_PAGES):
    # Each page links to 3-7 other pages
    targets = random.sample([j for j in range(NUM_PAGES) if j != i], k=random.randint(3, 7))
    LINKS[i] = targets


def page_html(page_id: int, title: str, body: str, extra_links: list[str] = None) -> str:
    links_html = ""
    for target in LINKS.get(page_id, []):
        links_html += f'  <a href="/page/{target}">Page {target}</a>\n'
    if extra_links:
        for href in extra_links:
            links_html += f'  <a href="{href}">{href}</a>\n'
    return f"""<!DOCTYPE html>
<html>
<head><title>{title}</title></head>
<body>
<h1>{title}</h1>
<p>{body}</p>
<nav>
{links_html}
</nav>
</body>
</html>"""


@app.route("/health")
def health():
    return "OK", 200


@app.route("/sitemap.xml")
def sitemap():
    """Sitemap listing all pages — crawlers use this for explicit URL discovery."""
    urls = "\n".join(
        f"  <url><loc>http://localhost:5100/page/{i}</loc></url>"
        for i in range(NUM_PAGES)
    )
    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{urls}
</urlset>"""
    return Response(content, mimetype="application/xml")


@app.route("/robots.txt")
def robots():
    content = """User-agent: *
Disallow: /private/
Crawl-delay: 1
"""
    return Response(content, mimetype="text/plain")


@app.route("/")
def index():
    links = [f'<a href="/page/{i}">Page {i}</a>' for i in range(10)]
    html = f"""<!DOCTYPE html>
<html>
<head><title>Mock Web Index</title></head>
<body>
<h1>Mock Web Index</h1>
<p>Entry point for 50-page mock web graph.</p>
<nav>
{''.join(links)}
</nav>
</body>
</html>"""
    return html


@app.route("/page/<int:page_id>")
def page(page_id: int):
    if page_id < 0 or page_id >= NUM_PAGES:
        return "Not Found", 404
    body = (
        f"This is page {page_id}. It contains some content about topic {page_id % 10}. "
        f"It was last updated on 2024-01-{(page_id % 28) + 1:02d}."
    )
    return page_html(page_id, f"Page {page_id}", body)


@app.route("/private/<path:subpath>")
def private(subpath: str):
    # These pages exist but robots.txt disallows crawling them
    return page_html(-1, f"Private: {subpath}", "This is private content — should not be crawled.", [])


@app.route("/trap")
def trap():
    """Crawler trap: links to /trap?page=N for increasing N."""
    page_num = request.args.get("page", 0, type=int)
    next_page = page_num + 1
    extra_links = [f"/trap?page={next_page}", f"/trap?page={next_page + 1}"]
    html = f"""<!DOCTYPE html>
<html>
<head><title>Trap Page {page_num}</title></head>
<body>
<h1>Trap Page {page_num}</h1>
<p>This page links to incrementally deeper pages — a classic crawler trap.</p>
<nav>
  <a href="/trap?page={next_page}">Next page</a>
  <a href="/trap?page={next_page + 1}">Skip page</a>
</nav>
</body>
</html>"""
    return html


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5100, debug=False)
