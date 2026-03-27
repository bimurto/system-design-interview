#!/usr/bin/env python3
"""
Search Systems Lab — Inverted Index + Elasticsearch

Prerequisites: docker compose up -d (wait ~40s for Elasticsearch to start)

What this demonstrates:
  1. Build a manual inverted index with BM25 scoring in Python (algorithm clarity)
  2. Phrase queries — why position data matters beyond posting lists
  3. Index 5000 product documents into Elasticsearch with a custom analyzer
  4. Full-text search: multi-match, field boosting, BM25 relevance scores
  5. Faceted search: filter context vs query context, aggregations
  6. Analyzer pipeline: tokenization, stop words, stemming and their trade-offs
  7. Near-real-time (NRT) indexing delay — the 1-second refresh window
  8. Deep pagination gotcha: from+size vs search_after
  9. Fuzzy search and prefix/autocomplete queries
  10. Index vs search latency benchmark
"""

import json
import time
import random
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


# ── Manual Inverted Index with BM25 ──────────────────────────────────────────

class InvertedIndex:
    """
    Inverted index with BM25 scoring and phrase query support.

    Index structure:
      self.index[term][doc_id] = [position0, position1, ...]

    This stores positions so we can support:
      - Standard term queries (any position)
      - Phrase queries ("wireless headphones" must be adjacent)
    """

    def __init__(self, k1=1.2, b=0.75):
        # term -> {doc_id -> [positions]}
        self.index       = defaultdict(lambda: defaultdict(list))
        self.docs        = {}      # doc_id -> original text
        self.doc_lengths = {}      # doc_id -> number of tokens
        self.num_docs    = 0
        self.k1          = k1     # TF saturation parameter
        self.b           = b      # length normalization parameter

    @property
    def avg_doc_length(self):
        if not self.doc_lengths:
            return 1
        return sum(self.doc_lengths.values()) / len(self.doc_lengths)

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

    def idf(self, term):
        """
        BM25 IDF:  log((N - df + 0.5) / (df + 0.5) + 1)

        The +1 outside ensures IDF is always positive even when the
        term appears in every document. Standard TF-IDF uses log(N/df)
        which can be 0 or negative for very common terms.
        """
        df = len(self.index.get(term, {}))
        if df == 0:
            return 0.0
        N = self.num_docs
        return math.log((N - df + 0.5) / (df + 0.5) + 1)

    def bm25_score(self, term, doc_id):
        """
        BM25 term score for a single (term, document) pair.

        score = IDF × (tf × (k1 + 1)) / (tf + k1 × (1 - b + b × |D|/avgdl))

        Two improvements over classic TF-IDF:
          - Saturation: as tf → ∞, the fraction → (k1+1), not ∞
          - Length normalization: |D|/avgdl shrinks the denominator for
            short documents, boosting them relative to long ones
        """
        positions = self.index.get(term, {}).get(doc_id, [])
        tf = len(positions)
        if tf == 0:
            return 0.0
        doc_len = self.doc_lengths[doc_id]
        avgdl   = self.avg_doc_length
        numerator   = tf * (self.k1 + 1)
        denominator = tf + self.k1 * (1 - self.b + self.b * doc_len / avgdl)
        return self.idf(term) * (numerator / denominator)

    def search(self, query, top_k=5):
        """Term-based BM25 search (union of posting lists, scored by BM25)."""
        query_terms = self.tokenize(query)
        scores = defaultdict(float)
        for term in query_terms:
            if term in self.index:
                for doc_id in self.index[term]:
                    scores[doc_id] += self.bm25_score(term, doc_id)
        ranked = sorted(scores.items(), key=lambda x: -x[1])
        return ranked[:top_k]

    def phrase_search(self, phrase, top_k=5):
        """
        Exact phrase query using position data.

        For the phrase "wireless headphones":
          1. Look up posting list for "wireless"   → {doc_id → [positions]}
          2. Look up posting list for "headphones" → {doc_id → [positions]}
          3. For each doc in both lists, check if any position of "headphones"
             equals any position of "wireless" + 1 (consecutive)
          4. Only matching docs are returned; scored by BM25
        """
        terms = self.tokenize(phrase)
        if not terms:
            return []

        # Start with docs that contain the first term
        candidates = set(self.index.get(terms[0], {}).keys())
        for term in terms[1:]:
            candidates &= set(self.index.get(term, {}).keys())

        results = []
        for doc_id in candidates:
            # Check if terms appear consecutively at any position
            first_positions = set(self.index[terms[0]][doc_id])
            match = False
            for start_pos in first_positions:
                if all(
                    (start_pos + offset) in self.index[terms[offset]][doc_id]
                    for offset in range(1, len(terms))
                ):
                    match = True
                    break
            if match:
                score = sum(self.bm25_score(t, doc_id) for t in terms)
                results.append((doc_id, score))

        return sorted(results, key=lambda x: -x[1])[:top_k]


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
    • Score and rank by BM25
    • Return top-K

  Position data in the posting list enables phrase queries:
    "wireless headphones" (as phrase) ≠ "wireless" AND "headphones"
    The former requires the words to appear consecutively.
""")

    # ── Phase 1: Manual inverted index with BM25 ──────────────────
    section("Phase 1: Manual Inverted Index — BM25 Algorithm")

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

    idx = InvertedIndex(k1=1.2, b=0.75)
    for doc_id, text in CORPUS:
        idx.add_document(doc_id, text)

    print(f"\n  Posting lists for key terms (term → doc_ids with positions):\n")

    for term in ["wireless", "headphones", "professional", "noise"]:
        postings = idx.index.get(term, {})
        idf_val  = idx.idf(term)
        entries  = {d: p for d, p in postings.items()}
        print(f"  '{term}' (idf={idf_val:.2f}, df={len(postings)} docs):")
        for doc_id, positions in entries.items():
            print(f"      doc {doc_id} @ positions {positions}  → '{CORPUS[doc_id][1]}'")

    print(f"\n  BM25 search: 'wireless headphones' (union, scored)")
    results = idx.search("wireless headphones", top_k=5)
    for doc_id, score in results:
        print(f"    doc {doc_id}: score={score:.4f}  '{CORPUS[doc_id][1]}'")

    print(f"""
  BM25 formula (per term per doc):
    score = IDF × tf×(k1+1) / (tf + k1×(1 - b + b×|D|/avgdl))

    k1=1.2: saturation — once tf ≈ 5, extra occurrences add little
    b=0.75: length norm — doc with 3 tokens scores higher than doc
            with 30 tokens for the same raw term count
    IDF  = log((N - df + 0.5) / (df + 0.5) + 1)
           → rare terms have high IDF; ubiquitous terms near zero
""")

    # ── Phase 2: Phrase queries ────────────────────────────────────
    section("Phase 2: Phrase Queries — Why Position Data Matters")

    print(f"\n  Corpus docs containing BOTH 'wireless' AND 'headphones':")
    term_results = idx.search("wireless headphones", top_k=10)
    for doc_id, score in term_results:
        print(f"    doc {doc_id}: score={score:.4f}  '{CORPUS[doc_id][1]}'")

    print(f"\n  Phrase search: 'wireless headphones' (must be consecutive):")
    phrase_results = idx.phrase_search("wireless headphones", top_k=10)
    if phrase_results:
        for doc_id, score in phrase_results:
            print(f"    doc {doc_id}: score={score:.4f}  '{CORPUS[doc_id][1]}'")
    else:
        print(f"    (no results — phrase not found consecutively)")

    phrase_results2 = idx.phrase_search("portable wireless", top_k=10)
    print(f"\n  Phrase search: 'portable wireless':")
    for doc_id, score in phrase_results2:
        print(f"    doc {doc_id}: score={score:.4f}  '{CORPUS[doc_id][1]}'")

    print(f"""
  Key insight: term query vs phrase query
    Term query: "wireless" OR "headphones" can appear ANYWHERE in the doc
    Phrase query: positions must be consecutive — positions differ by 1

  Without position data, phrase queries are impossible.
  Storing positions increases index size but enables:
    • Exact phrase matching ("New York Times" ≠ "New York" AND "Times")
    • Proximity scoring (terms near each other → higher score)
    • Span queries (term A within N words of term B)

  In Elasticsearch: match_phrase query uses position-aware posting lists.
  In Lucene:        PhraseQuery and SpanQuery implement this.
""")

    # ── Phase 3: Index documents into Elasticsearch ────────────────
    section("Phase 3: Indexing 5000 Products into Elasticsearch")

    try:
        es = Elasticsearch(ES_HOST)
        es.info()
        print("  Connected to Elasticsearch.")
    except Exception as e:
        print(f"  ERROR: {e}")
        print("  Run: docker compose up -d && sleep 40")
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
    print(f"  Index created with dynamic=false for non-declared fields.")
    print(f"  Mapping uses 'keyword' for category/in_stock (exact filter),")
    print(f"  'text' for name/description (full-text search with BM25).\n")

    products = generate_products(5000)

    print(f"  Indexing 5000 products with bulk API...", end=" ", flush=True)
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
    print(f"  (Bulk API amortizes per-document HTTP + ack overhead)")

    # Force refresh so documents are immediately searchable
    es.indices.refresh(index=INDEX)

    # ── Phase 4: Full-text search with BM25 ───────────────────────
    section("Phase 4: Full-Text Search — BM25 Relevance Scoring")

    queries = ["wireless headphones", "portable speaker", "professional camera"]

    for query in queries:
        start = time.perf_counter()
        resp = es.search(
            index=INDEX,
            query={"multi_match": {"query": query, "fields": ["name^2", "description"]}},
            size=3,
        )
        elapsed_ms = (time.perf_counter() - start) * 1000

        hits = resp["hits"]["hits"]
        total = resp["hits"]["total"]["value"]
        print(f"\n  Query: '{query}'  ({total} matches, {elapsed_ms:.1f}ms)")
        for hit in hits:
            src = hit["_source"]
            print(f"    score={hit['_score']:.2f}  {src['name']:<40} ${src['price']:.2f}")

    print(f"""
  BM25 scoring details:
    • 'name^2': field boost — a match in the name counts 2× more
      than a match in the description (boosted at query time, not index time)
    • _score is the BM25 score summed over all matching terms
    • multi_match: runs the query across multiple fields, takes max or sum

  Query context vs filter context:
    • query context:  "match"  → computes _score, affects ranking
    • filter context: "filter" → yes/no, cached, NO score computation
    Use filter for structured fields (price, category, in_stock).
    Use query for free-text fields (name, description).
""")

    # ── Phase 5: Faceted search ────────────────────────────────────
    section("Phase 5: Faceted Search — Filters + Aggregations")

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

    print(f"\n  Category breakdown (aggregation / facet):")
    for bucket in resp["aggregations"]["by_category"]["buckets"]:
        print(f"    {bucket['key']:<12}: {bucket['doc_count']} products")

    avg_price = resp["aggregations"]["avg_price"]["value"]
    print(f"\n  Average price in results: ${avg_price:.2f}")

    print(f"""
  Faceted search is the "filters" sidebar on e-commerce sites:
    Category: Electronics (45) | Books (23) | ...
    Price:    $0–$50 (12)      | $50–$100 (34) | ...
    Rating:   ★4+ (28)         | ★3+ (67) | ...

  In Elasticsearch:
    • 'must' (query context): contributes to _score — use for relevance
    • 'filter' context: yes/no, does NOT affect _score, gets cached in OS
      page cache — dramatically faster for repeated structured queries
    • 'aggs': computed over the filtered result set, not the top-K hits
    • Aggregations require 'keyword' fields (not 'text') for exact grouping
""")

    # ── Phase 6: Analyzer comparison ─────────────────────────────
    section("Phase 6: Analyzer Pipeline — Tokenization and Stemming")

    test_text = "Running runners run beautifully across electronic devices"

    print(f"\n  Input text: '{test_text}'\n")

    for analyzer in ["standard", "stemming_analyzer"]:
        resp = es.indices.analyze(index=INDEX, analyzer=analyzer, text=test_text)
        tokens = [t["token"] for t in resp["tokens"]]
        print(f"  {analyzer}:")
        print(f"    Tokens ({len(tokens)}): {tokens}")

    print(f"""
  Standard analyzer pipeline:
    1. Unicode standard tokenizer  (split on whitespace/punctuation)
    2. Lowercase filter
    Result: stop words kept, no stemming — high precision, lower recall

  Stemming analyzer pipeline (custom):
    1. Standard tokenizer
    2. Lowercase
    3. Stop word filter  ("across", "for", "a", "the" → removed)
    4. Porter stemmer    "running"→"run", "beautifully"→"beauti",
                         "electronic"→"electron", "devices"→"devic"
    Result: query "run shoes" matches "running shoes" — higher recall

  Critical rule: index-time analyzer MUST match query-time analyzer.
    If you index with stemming_analyzer but search with standard,
    "run" in the query won't match "run" stems in the index.
    ES enforces this by using the field's analyzer at search time too.

  Stemming trade-off:
    + Recall: "run" matches "running", "runner", "runs"
    - Precision: "organ" matches "organized", "organic" (over-stemming)
    - Lossy: cannot reconstruct "beautifully" from "beauti"

  For FAANG interviews: mention language-specific analyzers.
    CJK (Chinese/Japanese/Korean) languages have no whitespace between
    words — you need a tokenizer that uses a dictionary or n-grams.
    Wikipedia's CirrusSearch runs a different analyzer per language.
""")

    # ── Phase 7: Near-real-time indexing delay ────────────────────
    section("Phase 7: Near-Real-Time (NRT) Indexing Delay")

    print(f"""
  Elasticsearch uses Lucene's segment-based write model:
    1. New document → in-memory write buffer (NOT yet searchable)
    2. Every refresh_interval (default: 1s) → buffer flushed to a
       new immutable Lucene segment on disk → NOW searchable
    3. Every ~30s (or when segments accumulate) → background merge
       combines small segments into larger ones (I/O intensive)
    4. Write is ACK'd after the translog flush, not after refresh

  The translog is Elasticsearch's WAL (write-ahead log).
  By default, translog is fsynced every 5s or 512MB —
  up to 5s of acknowledged writes can be lost on hard crash.
  Set index.translog.durability=request for per-operation fsync
  (safe but ~5–10x slower writes).
""")

    # Index a document WITHOUT refreshing, then show it's not searchable
    probe_doc = {
        "name": "NRT probe document unique token xyzzy99",
        "description": "This document tests the NRT refresh window.",
        "category": "Test",
        "price": 1.00,
        "rating": 5.0,
        "in_stock": True,
    }
    probe_id = "nrt-probe-1"
    es.index(index=INDEX, id=probe_id, document=probe_doc)
    # Deliberately do NOT refresh here

    resp_before = es.search(
        index=INDEX,
        query={"match": {"name": "xyzzy99"}},
        size=1,
    )
    hits_before = resp_before["hits"]["total"]["value"]
    print(f"  Indexed doc with unique token 'xyzzy99'.")
    print(f"  Search immediately after index (no refresh): {hits_before} hits")
    print(f"  → Document is in the translog but NOT in a searchable segment yet.")

    time.sleep(1.5)  # Wait for the default refresh_interval
    resp_after = es.search(
        index=INDEX,
        query={"match": {"name": "xyzzy99"}},
        size=1,
    )
    hits_after = resp_after["hits"]["total"]["value"]
    print(f"  Search after ~1.5s (refresh_interval elapsed): {hits_after} hits")
    print(f"  → Document is now in a Lucene segment and searchable.")
    print(f"""
  Implication for applications:
    • Never rely on a document being immediately searchable after index
    • If you need immediate searchability (e.g., confirm your own post),
      call /_refresh explicitly — but only in tests, not hot paths
    • Design UIs to show optimistic results from the write side
      rather than re-querying ES immediately after a write
""")

    # ── Phase 8: Deep pagination gotcha ──────────────────────────
    section("Phase 8: Deep Pagination — from+size vs search_after")

    print(f"""
  Naive pagination with from+size:
    GET /products/_search?from=9000&size=10
    → ES must fetch 9010 docs from every shard, send all to coordinator,
      coordinator sorts 9010 × num_shards docs, discards all but 10
    → Memory: O(from + size) per shard × num_shards on coordinator
    → At from=10000 ES raises an error by default (index.max_result_window=10000)

  This is called the "deep pagination" problem.
  It is fundamentally a scatter-gather problem: you cannot skip ahead
  without materializing all preceding results.
""")

    # Demonstrate from+size reaching the default limit
    try:
        resp = es.search(
            index=INDEX,
            query={"match_all": {}},
            from_=9990,
            size=10,
        )
        print(f"  from=9990, size=10: returned {len(resp['hits']['hits'])} hits (near limit)")
    except Exception as e:
        print(f"  from=9990 raised: {e}")

    try:
        resp_over = es.search(
            index=INDEX,
            query={"match_all": {}},
            from_=10001,
            size=10,
        )
        print(f"  from=10001: returned {len(resp_over['hits']['hits'])} hits")
    except Exception as e:
        err_msg = str(e)[:120]
        print(f"  from=10001 raised exception (max_result_window exceeded):")
        print(f"    {err_msg}")

    print(f"""
  Solution: search_after (cursor-based pagination)
    1. Run first page: sort=[rating:desc, _id:asc], get last sort values
    2. Next page: add search_after=[last_rating, last_id]
    → ES only fetches page_size docs per shard; no coordinator accumulation
    → O(page_size) memory regardless of how deep you paginate
    → Trade-off: no random-access (can't jump to page 500), cursor only
""")

    # Demonstrate search_after
    resp_page1 = es.search(
        index=INDEX,
        query={"match": {"description": "wireless"}},
        sort=[{"rating": "desc"}, {"_id": "asc"}],
        size=3,
    )
    page1_hits = resp_page1["hits"]["hits"]
    print(f"  Page 1 (search_after not used):")
    for hit in page1_hits:
        src = hit["_source"]
        print(f"    _id={hit['_id']:<6} rating={src['rating']}  sort_values={hit['sort']}")

    last_sort = page1_hits[-1]["sort"]
    resp_page2 = es.search(
        index=INDEX,
        query={"match": {"description": "wireless"}},
        sort=[{"rating": "desc"}, {"_id": "asc"}],
        size=3,
        search_after=last_sort,
    )
    page2_hits = resp_page2["hits"]["hits"]
    print(f"\n  Page 2 (search_after={last_sort}):")
    for hit in page2_hits:
        src = hit["_source"]
        print(f"    _id={hit['_id']:<6} rating={src['rating']}  sort_values={hit['sort']}")

    print(f"""
  search_after requires a consistent sort with a tiebreaker (_id).
  Without a tiebreaker, duplicate sort values can cause docs to be
  skipped or repeated across pages.

  For export-all use cases (all matching docs, no UI):
    Use the Scroll API or Point-in-Time (PIT) API — both take a snapshot
    of the index state so concurrent indexing doesn't shift results.
""")

    # ── Phase 9: Fuzzy and autocomplete ──────────────────────────
    section("Phase 9: Fuzzy Search and Prefix Autocomplete")

    print(f"\n  Fuzzy search handles typos by allowing edit distance:")

    fuzzy_tests = [
        ("headphons",  "match with fuzziness=AUTO"),  # 1 deletion
        ("wireles",    "match with fuzziness=AUTO"),  # 1 deletion
        ("proffesional", "match with fuzziness=AUTO"), # 1 substitution
    ]

    for misspelled, label in fuzzy_tests:
        resp = es.search(
            index=INDEX,
            query={"match": {"name": {"query": misspelled, "fuzziness": "AUTO"}}},
            size=2,
        )
        total = resp["hits"]["total"]["value"]
        print(f"\n  Query: '{misspelled}' ({label})")
        print(f"  Matches: {total}")
        for hit in resp["hits"]["hits"][:2]:
            print(f"    score={hit['_score']:.2f}  {hit['_source']['name']}")

    print(f"""
  Fuzziness=AUTO uses edit distance based on term length:
    Length 1-2:  no fuzziness (exact)
    Length 3-5:  fuzziness=1 (1 insert/delete/substitute)
    Length 6+:   fuzziness=2

  Fuzzy queries use a BK-tree (Burkhard-Keller tree) over the term
  dictionary to find all terms within the edit distance bound.
  This is more expensive than exact match but far cheaper than a
  sequential scan — it's still an index operation.

  Autocomplete / prefix search:
    match_phrase_prefix: "wirele" matches "wireless", "wireless headphones"
    completion suggester: pre-built FST (finite state transducer) for
    sub-millisecond prefix lookups — used for search-as-you-type UIs.
""")

    resp_prefix = es.search(
        index=INDEX,
        query={"match_phrase_prefix": {"name": {"query": "wirele", "max_expansions": 5}}},
        size=3,
    )
    print(f"  Prefix query 'wirele' matches: {resp_prefix['hits']['total']['value']} docs")
    for hit in resp_prefix["hits"]["hits"]:
        print(f"    {hit['_source']['name']}")

    # ── Phase 10: Index vs search latency ─────────────────────────
    section("Phase 10: Index vs Search Latency Benchmark")

    # Single-doc index latency
    single_times = []
    for i in range(20):
        doc = {
            "name": f"bench {i}", "description": "benchmark document",
            "category": "Test", "price": 9.99, "rating": 4.0, "in_stock": True,
        }
        start = time.perf_counter()
        es.index(index=INDEX, id=f"bench-{i}", document=doc)
        single_times.append((time.perf_counter() - start) * 1000)

    es.indices.refresh(index=INDEX)

    # Search latency
    search_times = []
    for query in ["wireless", "portable speaker", "professional", "smart home", "bluetooth"]:
        start = time.perf_counter()
        es.search(index=INDEX, query={"match": {"description": query}}, size=10)
        search_times.append((time.perf_counter() - start) * 1000)

    avg_index  = sum(single_times)  / len(single_times)
    avg_search = sum(search_times)  / len(search_times)

    print(f"\n  Single-document index latency (20 samples):")
    print(f"    Avg: {avg_index:.1f}ms  Min: {min(single_times):.1f}ms  Max: {max(single_times):.1f}ms")

    print(f"\n  Full-text BM25 search latency (5 queries, ~5000 docs):")
    print(f"    Avg: {avg_search:.1f}ms  Min: {min(search_times):.1f}ms  Max: {max(search_times):.1f}ms")

    print(f"""
  Index >> Search latency because:
    Indexing:   tokenize → update inverted index → write translog → ack
                background: segment creation, potential segment merge (I/O)
    Searching:  index lookup per term → merge posting lists → BM25 score
                → all in-memory, no writes, OS page cache friendly

  ES is read-optimized (Lucene's immutable segment model).
  Segments are written once, never modified — only merged or deleted.
  This makes segments ideal for OS page cache (no cache invalidation).

  Bulk indexing tip: set number_of_replicas=0 during load,
  restore to desired value after. Replicas double indexing I/O.
  This alone can make bulk load 2–5x faster.
""")

    # ── Summary ───────────────────────────────────────────────────
    section("Summary — Search System Architecture Decisions")

    print("""
  Lucene / Elasticsearch segment model:
  ┌─────────────────────────────────────────────────────────────┐
  │  Write path:                                                │
  │    doc → in-memory buffer → [refresh ~1s] → segment        │
  │                          → translog (WAL, fsync ~5s)        │
  │                                                             │
  │  Segment = immutable set of files:                         │
  │    .fst   : term dictionary (finite state transducer)       │
  │    .doc   : posting lists (doc IDs)                        │
  │    .pos   : position data (for phrase queries)              │
  │    .dvd/m : doc values (for sorting/aggregations)          │
  │                                                             │
  │  Merge: N small segments → 1 large segment (background)    │
  └─────────────────────────────────────────────────────────────┘

  ES cluster search scatter-gather:
  ┌───────────────────────────────────────────────────────────┐
  │  Client → Coordinator node                                │
  │    ↓ scatter: send query to one copy of each shard        │
  │  Shard 0    Shard 1    Shard 2   (each: top-K + scores)  │
  │    ↓ gather: coordinator merges, re-ranks, returns top-K  │
  │  Client ← top-K global results                            │
  └───────────────────────────────────────────────────────────┘

  ES vs Postgres FTS vs Algolia:
  ┌──────────────────┬─────────────────┬────────────┬──────────┐
  │                  │  Elasticsearch  │ Postgres   │ Algolia  │
  ├──────────────────┼─────────────────┼────────────┼──────────┤
  │ Relevance        │ BM25 (great)    │ ts_rank    │ Propr.   │
  │ Facets/Aggs      │ Yes (fast)      │ GROUP BY   │ Built-in │
  │ Scale            │ Horizontal      │ Vertical   │ Managed  │
  │ Consistency      │ Eventual (NRT)  │ ACID       │ Eventual │
  │ Fuzzy/Autocmpl   │ Yes             │ Limited    │ Excell.  │
  │ Ops burden       │ High            │ None extra │ None     │
  │ Cost             │ Infra           │ Infra      │ Per-call │
  └──────────────────┴─────────────────┴────────────┴──────────┘

  Decision framework:
    ≤ 5M rows, simple queries              → Postgres tsvector + GIN index
    > 5M rows, relevance tuning, facets    → Elasticsearch
    Managed, best UX, latency-sensitive    → Algolia (SaaS cost)
    Semantic / vector similarity search    → pgvector or Elasticsearch kNN

  Next: ../10-rate-limiting-algorithms/
""")


if __name__ == "__main__":
    main()
