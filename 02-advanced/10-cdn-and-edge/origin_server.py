#!/usr/bin/env python3
"""
Origin server with artificial latency to simulate a real origin behind a CDN.
"""
import time
import os
from flask import Flask, Response, request, jsonify

app = Flask(__name__)

LATENCY_MS = int(os.environ.get("LATENCY_MS", "200"))


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/content/<path:filename>")
def content(filename):
    """Cacheable content with Cache-Control headers."""
    time.sleep(LATENCY_MS / 1000.0)  # simulate origin latency
    version = request.args.get("v", "1")
    body = f"Content: {filename} (version={version}, served at {time.time():.3f})"
    resp = Response(body, content_type="text/plain")
    resp.headers["Cache-Control"] = "public, max-age=30"
    resp.headers["X-Origin-Time"] = str(time.time())
    resp.headers["X-Version"] = version
    return resp


@app.route("/nocache/<path:filename>")
def nocache(filename):
    """Content that should never be cached."""
    time.sleep(LATENCY_MS / 1000.0)
    body = f"No-cache content: {filename} at {time.time():.3f}"
    resp = Response(body, content_type="text/plain")
    resp.headers["Cache-Control"] = "no-cache, no-store"
    return resp


@app.route("/short-ttl/<path:filename>")
def short_ttl(filename):
    """Content with a very short TTL to demonstrate expiry."""
    time.sleep(LATENCY_MS / 1000.0)
    body = f"Short-TTL content: {filename} at {time.time():.3f}"
    resp = Response(body, content_type="text/plain")
    resp.headers["Cache-Control"] = "public, max-age=5"
    return resp


if __name__ == "__main__":
    print(f"Origin server starting with {LATENCY_MS}ms artificial latency")
    app.run(host="0.0.0.0", port=8000)
