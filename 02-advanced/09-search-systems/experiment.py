#!/usr/bin/env python3
"""
Search Systems Lab — Inverted Index + Elasticsearch

Prerequisites: docker compose up -d (wait ~30s for Elasticsearch to start)

What this demonstrates:
  1. Build a simple inverted index manually in Python (shows the algorithm)
  2. Index 5000 product documents into Elasticsearch
  3. Full-text search with BM25 relevance scoring
  4. Faceted search: filter by category, price range, rating
  5. Analyzer effects: default tokenization vs custom with stemming
  6. Index time vs search latency comparison
"""

import json
import time
import random
import string
import math
import subprocess
from collections import defaultdict

try:
    from elasticsearch import Elasticsearch, helpers
except ImportError:
    print("Installing dependencies...")
    subprocess.run(["pip", "install", "elasticsearch", "-q"], check=True)
    from elasticsearch import Elasticsearch, helpers

ES_HOST = "http://localhost:9200"
INDEX   = "products"


def section(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


# ── Manual Inverted Index ─────────────────────────────────────────────────

class InvertedIndex:
    """Simple inverted index with TF-IDF-like scoring."""

    def __init__(self):
        # term -> {doc_id -> [positions]}
        self.index       = defaultdict(lambda: defaultdict(list))
        self.docs        = {}      # doc_id -> original text
        self.doc_lengths = {}      # doc_id -> number of tokens
        self.num_docs    = 0

    def tokenize(self, text):
        """Lowercase, remove punctuation, split on whitespace."""
        text = text.lower()
        text = "".join(c if c.isalnum() or c.isspace() else " " for c in text)
        return text.split()

    def add_document(self, doc_id, text):
        tokens = self.tokenize(text)
        self.docs[doc_id] = text
        self.doc_lengths[doc_id] = len(tokens)
        self.num_docs += 1
        for pos, token in enumerate(tokens):
            self.index[token][doc_id].append(pos)

    def tf(self, term, doc_id):
        """Term frequency: count of term in doc / doc length."""
        count = len(self.index[term].get(doc_id, []))
        return count / self.doc_lengths.get(doc_id, 1)

    def idf(self, term):
        """Inverse document frequency: log(N / df)."""
        df = len(self.index[term])
        if df == 0:
            return 0
        return math.log(self.num_docs / df)

    def tfidf_score(self, term, doc_id):
        return self.tf(term, doc_id) * self.idf(term)

    def search(self, query, top_k=5):
        """Return top_k docs scored by sum of TF-IDF for all query terms."""
        query_terms = self.tokenize(query)
        scores = defaultdict(float)
        for term in query_terms:
            if term in self.index:
                for doc_id in self.index[term]:
                    scores[doc_id] += self.tfidf_score(term, doc_id)
        ranked = sorted(scores.items(), key=lambda x: -x[1])
        return ranked[:top_k]


# ── Product generator ──────────────────────────────────────────────────────

CATEGORIES = ["Electronics", "Books", "Clothing", "Home", "Sports", "Toys", "Beauty"]
ADJECTIVES = ["premium", "professional", "portable", "wireless", "ultra", "smart", "compact", "durable"]
NOUNS      = ["headphones", "laptop", "camera", "keyboard", "monitor", "chair", "desk", "lamp", "bag"]

def generate_products(n=5000):
    random.seed(42)
    products = []
    for i in range(n):
        cat  = random.choice(CATEGORIES)
        adj  = random.choice(ADJECTIVES)
        noun = random.choice(NOUNS)
        products.append({
            "id":          i,
            "name":        f"{adj.title()} {noun.title()} {i}",
            "description": (
                f"A {adj} {noun} for {cat.lower()} enthusiasts. "
                f"Features include {random.choice(ADJECTIVES)} design and "
                f"{random.choice(ADJECTIVES)} performance. "
                f"Perfect for everyday use."
            ),
            "category":    cat,
            "price":       round(random.uniform(9.99, 999.99), 2),
            "rating":      round(random.uniform(1.0, 5.0), 1),
            "in_stock":    random.choice([True, True, True, False]),
        })
    return products


def main():
    section("SEARCH SYSTEMS LAB — INVERTED INDEX + ELASTICSEARCH")
    print("""
  Search is fundamentally about an inverted index:
    Forward index:  doc_id → list of terms   (doc lookup)
    Inverted index: term   → list of doc_ids (search lookup)

  The inverted index is what makes full-text search fast:
    • Query "wireless headphones" → look up "wireless" + "headphones"
    • Intersect/union the posting lists → candidate docs
    • Score and rank by TF-IDF or BM25
    • Return top-K
""")

    # ── Phase 1: Manual inverted index ────────────────────────────
    section("Phase 1: Manual Inverted Index — The Algorithm")

    CORPUS = [
        (0, "Wireless Bluetooth headphones with noise cancellation"),
        (1, "Professional noise cancelling headphones for studio use"),
        (2, "Portable wireless speaker with deep bass"),
        (3, "Smart TV 55 inch with wireless connectivity"),
        (4, "Noise cancellation software for video conferencing"),
        (5, "USB wireless keyboard and mouse combo"),
        (6, "Bluetooth wireless earbuds with charging case"),
        (7, "Professional studio monitor headphones"),
        (8, "Portable Bluetooth speaker for outdoor use"),
        (9, "Smart home wireless hub with voice control"),
    ]

    idx = InvertedIndex()
    for doc_id, text in CORPUS:
        idx.add_document(doc_id, text)

    print(f"\n  Inverted index for 10 sample documents:")
    print(f"  (showing top terms by posting list length)\n")

    sorted_terms = sorted(idx.index.items(), key=lambda x: -len(x[1]))
    for term, postings in sorted_terms[:8]:
        docs = list(postings.keys())
        idf  = idx.idf(term)
        print(f"  '{term}': {len(docs)} docs → {docs}  idf={idf:.2f}")

    print(f"\n  Search: 'wireless headphones'")
    results = idx.search("wireless headphones", top_k=5)
    for doc_id, score in results:
        print(f"    doc {doc_id}: score={score:.4f}  '{CORPUS[doc_id][1]}'")

    print(f"""
  TF-IDF intuition:
    TF (term frequency): words that appear often in a document are
        more relevant to that document
    IDF (inverse doc frequency): words that appear in many documents
        are less informative ("the", "and" → high df → low idf)
    TF-IDF = TF × IDF: penalizes common terms, rewards specific ones

  BM25 (what Elasticsearch uses by default) improves on TF-IDF:
    • Saturation: after a term appears ~5 times, extra occurrences
      add diminishing value (TF-IDF doesn't have this limit)
    • Length normalization: longer documents are penalized (they
      naturally contain more term occurrences)
    • Parameters k1=1.2 (saturation), b=0.75 (length normalization)
""")

    # ── Phase 2: Index documents into Elasticsearch ────────────────
    section("Phase 2: Indexing 5000 Products into Elasticsearch")

    try:
        es = Elasticsearch(ES_HOST)
        es.info()
        print("  Connected to Elasticsearch.")
    except Exception as e:
        print(f"  ERROR: {e}")
        print("  Run: docker compose up -d && sleep 30")
        return

    # Delete index if exists
    if es.indices.exists(index=INDEX):
        es.indices.delete(index=INDEX)

    # Create index with custom mapping
    es.indices.create(
        index=INDEX,
        settings={
            "number_of_shards":   1,
            "number_of_replicas": 0,
            "analysis": {
                "analyzer": {
                    "stemming_analyzer": {
                        "type":      "custom",
                        "tokenizer": "standard",
                        "filter":    ["lowercase", "stop", "porter_stem"],
                    }
                }
            }
        },
        mappings={
            "properties": {
                "name":        {"type": "text", "analyzer": "standard",
                                "fields": {"keyword": {"type": "keyword"}}},
                "description": {"type": "text", "analyzer": "standard",
                                "copy_to": "description_stemmed"},
                "description_stemmed": {"type": "text", "analyzer": "stemming_analyzer"},
                "category":    {"type": "keyword"},
                "price":       {"type": "float"},
                "rating":      {"type": "float"},
                "in_stock":    {"type": "boolean"},
            }
        },
    )

    products = generate_products(5000)

    print(f"\n  Indexing 5000 products...", end=" ", flush=True)
    start = time.perf_counter()

    actions = [
        {
            "_index": INDEX,
            "_id":    p["id"],
            "_source": {k: v for k, v in p.items() if k != "id"},
        }
        for p in products
    ]
    success, errors = helpers.bulk(es, actions, chunk_size=500, request_timeout=30)
    index_elapsed = time.perf_counter() - start

    print(f"done.")
    print(f"  Indexed {success} documents in {index_elapsed:.2f}s")
    print(f"  Throughput: {success / index_elapsed:.0f} docs/sec")

    # Force refresh so documents are immediately searchable
    es.indices.refresh(index=INDEX)

    # ── Phase 3: Full-text search with BM25 ───────────────────────
    section("Phase 3: Full-Text Search — BM25 Relevance Scoring")

    queries = ["wireless headphones", "portable speaker", "professional camera"]

    for query in queries:
        start = time.perf_counter()
        resp = es.search(index=INDEX, query={"multi_match": {"query": query, "fields": ["name^2", "description"]}}, size=3)
        elapsed_ms = (time.perf_counter() - start) * 1000

        hits = resp["hits"]["hits"]
        total = resp["hits"]["total"]["value"]
        print(f"\n  Query: '{query}'  ({total} matches, {elapsed_ms:.1f}ms)")
        for hit in hits:
            src = hit["_source"]
            print(f"    score={hit['_score']:.2f}  {src['name']:<40} ${src['price']:.2f}")

    print(f"""
  BM25 scoring details:
    • Fields with '^' are boosted: name^2 means name matches
      count twice as much as description matches
    • _score is the BM25 score — higher = more relevant
    • BM25 parameters in ES: k1=1.2, b=0.75 (tunable per-index)

  Near Real-Time (NRT) indexing:
    • Elasticsearch buffers new documents in an in-memory index segment
    • Every ~1 second (refresh_interval), the buffer is flushed to a
      new Lucene segment on disk (this makes it searchable)
    • Segments are periodically merged in the background (merge policy)
    • "Near" real-time: ~1s delay between index and searchability
    • Force refresh (as done above) makes documents instantly searchable
      but is expensive — don't use it in production on every index call
""")

    # ── Phase 4: Faceted search ────────────────────────────────────
    section("Phase 4: Faceted Search — Filters + Aggregations")

    start = time.perf_counter()
    resp = es.search(
        index=INDEX,
        query={
            "bool": {
                "must": [
                    {"match": {"description": "wireless"}},
                ],
                "filter": [
                    {"term":  {"in_stock": True}},
                    {"range": {"price":  {"gte": 50, "lte": 300}}},
                    {"range": {"rating": {"gte": 4.0}}},
                ],
            }
        },
        aggs={
            "by_category": {
                "terms": {"field": "category", "size": 10}
            },
            "avg_price": {
                "avg": {"field": "price"}
            },
            "price_histogram": {
                "histogram": {"field": "price", "interval": 50}
            },
        },
        size=3,
    )
    elapsed_ms = (time.perf_counter() - start) * 1000

    total = resp["hits"]["total"]["value"]
    print(f"\n  Query: 'wireless' + in_stock=true + price $50-$300 + rating ≥ 4.0")
    print(f"  Results: {total} matches ({elapsed_ms:.1f}ms)\n")

    print(f"  Top results:")
    for hit in resp["hits"]["hits"]:
        src = hit["_source"]
        print(f"    {src['name']:<40} ${src['price']:.2f}  ★{src['rating']}")

    print(f"\n  Category breakdown (facet):")
    for bucket in resp["aggregations"]["by_category"]["buckets"]:
        print(f"    {bucket['key']:<12}: {bucket['doc_count']} products")

    avg_price = resp["aggregations"]["avg_price"]["value"]
    print(f"\n  Average price in results: ${avg_price:.2f}")

    print(f"""
  Faceted search is the "filters" panel on e-commerce sites:
    • Category: Electronics (45) | Books (23) | ...
    • Price: $0–$50 (12) | $50–$100 (34) | ...
    • Rating: ★4+ (28) | ★3+ (67) | ...

  In Elasticsearch:
    • 'query' filters + scores (affects _score)
    • 'filter' context: yes/no, cached, faster (no scoring)
    • 'aggs': compute aggregations over the result set
    • keyword fields (not text) are required for exact filters
""")

    # ── Phase 5: Analyzer comparison ─────────────────────────────
    section("Phase 5: Analyzer Effects — Tokenization and Stemming")

    test_text = "Running runners run beautifully across electronic devices"

    print(f"\n  Input text: '{test_text}'\n")

    for analyzer in ["standard", "stemming_analyzer"]:
        resp = es.indices.analyze(index=INDEX, analyzer=analyzer, text=test_text)
        tokens = [t["token"] for t in resp["tokens"]]
        print(f"  {analyzer}:")
        print(f"    Tokens: {tokens}")

    print(f"""
  Standard analyzer:
    1. Unicode tokenizer (split on whitespace/punctuation)
    2. Lowercase filter
    3. Stop words NOT removed by default

  Stemming analyzer (custom, using porter_stem):
    1. Standard tokenizer
    2. Lowercase
    3. Stop word removal ("the", "a", "across", "are", ...)
    4. Porter stemmer: "running" → "run", "beautifully" → "beauti",
       "electronic" → "electron", "devices" → "devic"

  Why stemming matters for search:
    Query: "run"    → matches "running", "runner", "runs" (stemmed: "run")
    Query: "device" → matches "devices", "device" (stemmed: "devic")

  Without stemming, "running" and "run" are different terms.
  A search for "run shoes" would miss documents about "running shoes".

  Stemming trade-off:
    + Higher recall (more documents matched)
    - Lower precision (may match irrelevant documents)
    - Stemmed tokens look strange: "beautifully" → "beauti"
""")

    # ── Phase 6: Index time vs search latency ─────────────────────
    section("Phase 6: Index vs Search Latency")

    # Measure single-doc index time
    single_times = []
    for i in range(20):
        doc = {"name": f"test {i}", "description": "benchmark doc", "category": "Test",
               "price": 9.99, "rating": 4.0, "in_stock": True}
        start = time.perf_counter()
        es.index(index=INDEX, id=f"bench-{i}", document=doc)
        single_times.append((time.perf_counter() - start) * 1000)

    es.indices.refresh(index=INDEX)

    # Measure search time
    search_times = []
    for query in ["wireless", "portable speaker", "professional", "smart home", "bluetooth"]:
        start = time.perf_counter()
        es.search(index=INDEX, query={"match": {"description": query}}, size=10)
        search_times.append((time.perf_counter() - start) * 1000)

    avg_index  = sum(single_times) / len(single_times)
    avg_search = sum(search_times) / len(search_times)

    print(f"\n  Single-document index latency (20 samples):")
    print(f"    Avg: {avg_index:.1f}ms  Min: {min(single_times):.1f}ms  Max: {max(single_times):.1f}ms")

    print(f"\n  Full-text search latency (5 queries, 5000 docs):")
    print(f"    Avg: {avg_search:.1f}ms  Min: {min(search_times):.1f}ms  Max: {max(search_times):.1f}ms")

    print(f"""
  Index vs Search trade-off:
    • Indexing is expensive: tokenize, build inverted index, update
      segment files, potentially trigger segment merges
    • Searching is cheap: one index lookup per term, merge posting lists
    • Lucene segment merges happen in background but consume I/O and CPU
    • This is why ES is read-optimized (search latency << index latency)

  When NOT to use Elasticsearch for search:
    • Small datasets (< 1M rows): PostgreSQL full-text search (tsvector)
      is simpler, transactionally consistent, and fast enough
    • Simple LIKE queries: use a DB index + ILIKE (good up to millions)
    • Exact-match lookups: use a database index, not a search engine

  When to use Elasticsearch:
    • Full-text search with relevance ranking (BM25)
    • Faceted search with aggregations over millions of documents
    • Fuzzy/autocomplete/suggest features
    • Log analytics (ELK stack)
""")

    # ── Summary ───────────────────────────────────────────────────
    section("Summary — Search System Architecture")

    print("""
  ES Architecture:
  ┌─────────────────────────────────────────────────────────┐
  │  Index: "products"                                      │
  │                                                         │
  │  Shard 0      Shard 1      Shard 2                      │
  │  [Primary]    [Primary]    [Primary]                    │
  │  [Replica]    [Replica]    [Replica]                    │
  │                                                         │
  │  Each shard = one Lucene instance                        │
  │  Each Lucene instance = set of immutable segment files  │
  └─────────────────────────────────────────────────────────┘

  Routing: shard = hash(doc_id) % num_primary_shards
  Replicas: copies of shards for fault tolerance + read scaling

  ES vs Postgres full-text search:
  ┌─────────────────┬──────────────────┬───────────────────┐
  │                 │  Elasticsearch   │  Postgres FTS     │
  ├─────────────────┼──────────────────┼───────────────────┤
  │ Relevance       │ BM25 (great)     │ ts_rank (basic)   │
  │ Facets          │ Aggregations     │ GROUP BY + WHERE  │
  │ Scale           │ Horizontal       │ Vertical          │
  │ Consistency     │ Eventual (NRT)   │ ACID              │
  │ Complexity      │ High (ops-heavy) │ Low (same DB)     │
  │ Fuzzy/suggest   │ Yes (built-in)   │ Limited           │
  └─────────────────┴──────────────────┴───────────────────┘

  Rule of thumb:
    ≤ 5M rows + simple queries → Postgres with GIN index
    > 5M rows OR complex relevance → Elasticsearch

  Next: ../09-rate-limiting-algorithms/
""")


if __name__ == "__main__":
    main()
