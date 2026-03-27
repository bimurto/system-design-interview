#!/usr/bin/env python3
"""
Origin server with artificial latency to simulate a real origin behind a CDN.

Endpoints:
  /health                      — health check (no caching)
  /content/<filename>          — cacheable content (max-age=30, public)
  /nocache/<filename>          — never-cache content (no-cache, no-store)
  /short-ttl/<filename>        — short TTL content (max-age=5, public)
  /swr/<filename>              — stale-while-revalidate demo (max-age=5, swr=10)
  /vary/<filename>             — Vary: Accept-Language demo (cache fragmentation)
  /private/<filename>          — private/authenticated content (must not CDN-cache)

The origin hit counter is logged to stderr so the experiment can correlate
how many times the origin was actually contacted versus served from cache.
"""
import time
import os
import threading
from flask import Flask, Response, request, jsonify

app = Flask(__name__)

LATENCY_MS = int(os.environ.get("LATENCY_MS", "200"))

# Thread-safe origin hit counter — lets the experiment confirm how many
# requests actually escaped the CDN and reached the origin.
_hit_lock = threading.Lock()
_hit_counts: dict[str, int] = {}


def _record_hit(endpoint: str) -> int:
    with _hit_lock:
        _hit_counts[endpoint] = _hit_counts.get(endpoint, 0) + 1
        count = _hit_counts[endpoint]
    app.logger.info("ORIGIN HIT #%d  endpoint=%s", count, endpoint)
    return count


@app.route("/health")
def health():
    return jsonify({"status": "ok", "hit_counts": _hit_counts})


@app.route("/content/<path:filename>")
def content(filename):
    """Cacheable content: Cache-Control: public, max-age=30."""
    hit = _record_hit("content")
    time.sleep(LATENCY_MS / 1000.0)
    version = request.args.get("v", "1")
    body = (
        f"Content: {filename}  version={version}  "
        f"origin_hit=#{hit}  served_at={time.time():.3f}"
    )
    resp = Response(body, content_type="text/plain")
    resp.headers["Cache-Control"] = "public, max-age=30"
    resp.headers["X-Origin-Time"] = f"{time.time():.3f}"
    resp.headers["X-Origin-Hit"] = str(hit)
    resp.headers["X-Version"] = version
    return resp


@app.route("/nocache/<path:filename>")
def nocache(filename):
    """Content that must never be stored in any cache."""
    hit = _record_hit("nocache")
    time.sleep(LATENCY_MS / 1000.0)
    body = f"No-cache: {filename}  origin_hit=#{hit}  at={time.time():.3f}"
    resp = Response(body, content_type="text/plain")
    # no-store = never store in any cache.
    # no-cache alone would still allow caching with mandatory revalidation.
    resp.headers["Cache-Control"] = "no-cache, no-store"
    resp.headers["X-Origin-Hit"] = str(hit)
    return resp


@app.route("/short-ttl/<path:filename>")
def short_ttl(filename):
    """Content with a very short TTL (5s) to demonstrate expiry."""
    hit = _record_hit("short-ttl")
    time.sleep(LATENCY_MS / 1000.0)
    body = f"Short-TTL: {filename}  origin_hit=#{hit}  at={time.time():.3f}"
    resp = Response(body, content_type="text/plain")
    resp.headers["Cache-Control"] = "public, max-age=5"
    resp.headers["X-Origin-Hit"] = str(hit)
    return resp


@app.route("/swr/<path:filename>")
def stale_while_revalidate(filename):
    """
    stale-while-revalidate demo.

    Cache-Control: max-age=5, stale-while-revalidate=10

    Behaviour:
      0-5s   → serve fresh from cache (HIT, no origin contact)
      5-15s  → serve stale immediately, revalidate in background (STALE)
      >15s   → must revalidate synchronously (MISS / EXPIRED)

    Nginx models this with proxy_cache_use_stale + proxy_cache_background_update.
    The client never blocks waiting for the origin during the stale window.
    """
    hit = _record_hit("swr")
    time.sleep(LATENCY_MS / 1000.0)
    body = f"SWR content: {filename}  origin_hit=#{hit}  at={time.time():.3f}"
    resp = Response(body, content_type="text/plain")
    # s-maxage is the CDN-specific TTL; stale-while-revalidate is advisory
    # for the CDN to continue serving stale while fetching fresh in background.
    resp.headers["Cache-Control"] = "public, s-maxage=5, stale-while-revalidate=10"
    resp.headers["X-Origin-Hit"] = str(hit)
    return resp


@app.route("/vary/<path:filename>")
def vary_demo(filename):
    """
    Vary header cache fragmentation demo.

    The origin echoes back the Accept-Language header and sets Vary: Accept-Language.
    This instructs the CDN to create SEPARATE cache entries per language — a single
    URL becomes N cache entries (one per distinct language seen).  At scale this
    dramatically reduces the effective cache hit rate.

    Senior interview point: Vary: * effectively disables caching; Vary: Accept-Encoding
    is safe (only gzip/br); Vary: Cookie/Authorization creates per-user entries and
    should almost never be set on CDN-cached responses.
    """
    hit = _record_hit("vary")
    time.sleep(LATENCY_MS / 1000.0)
    lang = request.headers.get("Accept-Language", "en")
    body = (
        f"Vary demo: {filename}  lang={lang}  "
        f"origin_hit=#{hit}  at={time.time():.3f}"
    )
    resp = Response(body, content_type="text/plain")
    resp.headers["Cache-Control"] = "public, max-age=60"
    resp.headers["Vary"] = "Accept-Language"
    resp.headers["X-Origin-Hit"] = str(hit)
    resp.headers["Content-Language"] = lang
    return resp


@app.route("/private/<path:filename>")
def private_content(filename):
    """
    Private / authenticated content — must NEVER be stored at a CDN.

    Demonstrates the critical mistake of forgetting Cache-Control: private
    on auth-gated responses.  The experiment shows what happens when you
    accidentally omit it vs. correctly set it.
    """
    hit = _record_hit("private")
    time.sleep(LATENCY_MS / 1000.0)
    user = request.headers.get("X-User-Id", "anonymous")
    body = (
        f"PRIVATE DATA for user={user}  file={filename}  "
        f"origin_hit=#{hit}  at={time.time():.3f}"
    )
    resp = Response(body, content_type="text/plain")
    # Correct: browser may cache locally but CDN must not store it.
    resp.headers["Cache-Control"] = "private, no-store"
    resp.headers["X-Origin-Hit"] = str(hit)
    return resp


if __name__ == "__main__":
    print(f"Origin server starting with {LATENCY_MS}ms artificial latency")
    app.run(host="0.0.0.0", port=8000)
