#!/usr/bin/env python3
"""
Search Engine Lab — experiment.py

What this demonstrates:
  Phase 1: Build inverted index manually in Python (illustrates the algorithm)
  Phase 2: Index 5,000 products in Elasticsearch
  Phase 3: Full-text search with BM25 scores
  Phase 4: Multi-field boosting (title^3, description^1)
  Phase 5: Spell correction via suggest API
  Phase 6: Faceted search with aggregations

Run:
  docker compose up -d elasticsearch
  # Wait ~60s for ES to be healthy
  docker compose run --rm indexer
  # Or locally:
  pip install elasticsearch
  ES_URL=http://localhost:9200 python experiment.py
"""

import math
import os
import random
import time
from collections import defaultdict

from elasticsearch import Elasticsearch, helpers

ES_URL = os.environ.get("ES_URL", "http://localhost:9200")
INDEX_NAME = "products"
NUM_DOCS = 5000

random.seed(42)


# ── Helpers ───────────────────────────────────────────────────────────────────

def section(title: str):
    print(f"\n{'=' * 62}")
    print(f"  {title}")
    print("=" * 62)


def wait_for_es(es: Elasticsearch, max_wait: int = 120):
    print(f"  Waiting for Elasticsearch at {ES_URL} ...")
    for i in range(max_wait):
        try:
            health = es.cluster.health(wait_for_status="yellow", timeout="5s")
            print(f"  Cluster status: {health['status']}  (ready after {i+1}s)")
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError("Elasticsearch not ready")


# ── Data generation ───────────────────────────────────────────────────────────

CATEGORIES = ["Electronics", "Books", "Clothing", "Sports", "Home & Garden", "Toys", "Automotive"]
BRANDS = ["TechCorp", "BookWorld", "StyleCo", "SportsPro", "HomeBase", "ToyLand", "AutoParts"]
ADJECTIVES = ["Premium", "Deluxe", "Standard", "Professional", "Budget", "Ultra", "Classic"]
NOUNS = ["Widget", "Gadget", "Device", "Tool", "System", "Kit", "Set", "Pack"]

PRODUCT_TEMPLATES = [
    "{adj} {noun} for {category}",
    "{brand} {adj} {noun}",
    "{category} {noun} - {adj} Edition",
    "The {adj} {noun} by {brand}",
]

DESCRIPTIONS = [
    "High quality product with excellent performance and durability.",
    "Perfect for everyday use. Easy to set up and maintain.",
    "Industry-leading design with advanced features.",
    "Budget-friendly option without compromising quality.",
    "Professional grade equipment trusted by experts.",
    "Compact and lightweight. Ideal for travel and storage.",
]


def make_product(doc_id: int) -> dict:
    category = random.choice(CATEGORIES)
    brand = random.choice(BRANDS)
    adj = random.choice(ADJECTIVES)
    noun = random.choice(NOUNS)
    template = random.choice(PRODUCT_TEMPLATES)
    title = template.format(adj=adj, noun=noun, category=category, brand=brand)
    description = random.choice(DESCRIPTIONS) + " " + random.choice(DESCRIPTIONS)
    return {
        "id": doc_id,
        "title": title,
        "description": description,
        "category": category,
        "brand": brand,
        "price": round(random.uniform(5.0, 500.0), 2),
        "rating": round(random.uniform(1.0, 5.0), 1),
        "in_stock": random.choice([True, True, True, False]),
    }


PRODUCTS = [make_product(i) for i in range(NUM_DOCS)]

# Add some deliberate near-duplicates for spell correction demo
PRODUCTS.append({
    "id": NUM_DOCS,
    "title": "Premium Widget for Electronics",
    "description": "Top of the line widget with premium build quality.",
    "category": "Electronics",
    "brand": "TechCorp",
    "price": 99.99,
    "rating": 4.8,
    "in_stock": True,
})


# ── Phase 1: Manual Inverted Index ────────────────────────────────────────────

class InvertedIndex:
    def __init__(self):
        self.index: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self.doc_count = 0
        self.doc_freq: dict[str, int] = defaultdict(int)
        self._docs: dict[int, str] = {}

    def add_document(self, doc_id: int, text: str):
        self._docs[doc_id] = text
        terms = text.lower().split()
        term_freq: dict[str, int] = defaultdict(int)
        for term in terms:
            # Simple tokenization: strip punctuation
            term = term.strip(".,!?;:'\"()")
            if term:
                term_freq[term] += 1
        for term, tf in term_freq.items():
            self.index[term].append((doc_id, tf))
            self.doc_freq[term] += 1
        self.doc_count += 1

    def search(self, query: str) -> list[tuple[int, float, str]]:
        terms = query.lower().split()
        scores: dict[int, float] = defaultdict(float)
        for term in terms:
            term = term.strip(".,!?;:'\"()")
            if term in self.index:
                idf = math.log(self.doc_count / self.doc_freq[term])
                for doc_id, tf in self.index[term]:
                    scores[doc_id] += tf * idf  # TF-IDF score
        ranked = sorted(scores.items(), key=lambda x: -x[1])[:10]
        return [(doc_id, score, self._docs[doc_id][:60]) for doc_id, score in ranked]

    def stats(self) -> dict:
        return {
            "documents": self.doc_count,
            "unique_terms": len(self.index),
            "total_postings": sum(len(v) for v in self.index.values()),
            "avg_postings_per_term": sum(len(v) for v in self.index.values()) / max(1, len(self.index)),
        }


def phase1_manual_inverted_index():
    section("Phase 1: Manual Inverted Index (TF-IDF)")

    print("""
  Building a 200-document inverted index from scratch.

  Algorithm:
    1. Tokenize text → terms
    2. For each term: record (doc_id, term_frequency)
    3. On query: compute TF-IDF score for each matching doc
       TF-IDF(t,d) = TF(t,d) × IDF(t)
       IDF(t)      = log(N / df(t))    (N=doc count, df=docs containing t)
""")

    idx = InvertedIndex()
    sample_docs = PRODUCTS[:200]
    for doc in sample_docs:
        text = doc["title"] + " " + doc["description"] + " " + doc["category"]
        idx.add_document(doc["id"], text)

    stats = idx.stats()
    print(f"  Index built over {stats['documents']} documents:")
    print(f"  {'Unique terms':<30} {stats['unique_terms']:>10,}")
    print(f"  {'Total postings':<30} {stats['total_postings']:>10,}")
    print(f"  {'Avg postings/term':<30} {stats['avg_postings_per_term']:>10.1f}")

    # Demo searches
    queries = ["premium widget electronics", "professional tool", "budget gadget"]
    for query in queries:
        results = idx.search(query)
        print(f"\n  Query: \"{query}\"")
        print(f"  {'Rank':<5} {'Score':>8}  {'Doc preview'}")
        print(f"  {'-'*5}  {'-'*8}  {'-'*40}")
        for rank, (doc_id, score, preview) in enumerate(results[:5], 1):
            print(f"  {rank:<5}  {score:>8.3f}  {preview}...")

    # Show posting list structure
    print(f"\n  Posting list for term 'premium' (first 5 entries):")
    print(f"  {'doc_id':>8}  {'TF':>5}")
    print(f"  {'-'*8}  {'-'*5}")
    for doc_id, tf in idx.index.get("premium", [])[:5]:
        print(f"  {doc_id:>8}  {tf:>5}")

    print(f"""
  TF-IDF intuition:
  - TF (term frequency):  "widget" appears 5× in doc → high TF
  - IDF (inverse doc freq): "widget" appears in 50/200 docs → IDF = log(200/50) = 1.39
  - A rare term (IDF=4.6) in a matching doc scores higher than a common term (IDF=0.1)
  - Limitation: TF-IDF ignores term position, document length, field weighting
  - BM25 (Elasticsearch default) fixes the document length problem
""")


# ── Phase 2: Index 5000 products in Elasticsearch ────────────────────────────

def phase2_index_products(es: Elasticsearch):
    section("Phase 2: Index 5,000 Products in Elasticsearch")

    # Define index with custom mapping
    mapping = {
        "mappings": {
            "properties": {
                "title": {"type": "text", "boost": 3.0, "analyzer": "english"},
                "description": {"type": "text", "analyzer": "english"},
                "category": {"type": "keyword"},
                "brand": {"type": "keyword"},
                "price": {"type": "float"},
                "rating": {"type": "float"},
                "in_stock": {"type": "boolean"},
            }
        },
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 0,
        }
    }

    if es.indices.exists(index=INDEX_NAME):
        es.indices.delete(index=INDEX_NAME)
    es.indices.create(index=INDEX_NAME, body=mapping)
    print(f"  Created index '{INDEX_NAME}' with custom mapping")

    # Bulk index
    def gen_actions():
        for doc in PRODUCTS:
            yield {"_index": INDEX_NAME, "_id": doc["id"], "_source": doc}

    t0 = time.time()
    success, errors = helpers.bulk(es, gen_actions(), chunk_size=500)
    elapsed = time.time() - t0

    es.indices.refresh(index=INDEX_NAME)
    count = es.count(index=INDEX_NAME)["count"]

    print(f"  Indexed {success} documents in {elapsed:.2f}s  ({success/elapsed:.0f} docs/s)")
    print(f"  Total documents in index: {count}")
    if errors:
        print(f"  Errors: {len(errors)}")


# ── Phase 3: Full-text search with BM25 scores ───────────────────────────────

def phase3_full_text_search(es: Elasticsearch):
    section("Phase 3: Full-Text Search — BM25 Scores")

    print("""
  Elasticsearch uses BM25 (Best Match 25) by default.
  BM25 improves on TF-IDF by normalizing for document length:

    BM25(t,d) = IDF(t) × (tf × (k1+1)) / (tf + k1 × (1 - b + b × dl/avgdl))

  k1=1.2 controls TF saturation (diminishing returns for repeated terms)
  b=0.75 controls length normalization (longer docs penalized slightly)
""")

    queries = [
        ("premium widget", "simple term query"),
        ("professional tool kit", "multi-term query"),
        ("budget electronics gadget", "3-term query"),
    ]

    for query, note in queries:
        result = es.search(
            index=INDEX_NAME,
            body={
                "query": {"match": {"title": query}},
                "size": 5,
                "explain": False,
            }
        )
        hits = result["hits"]["hits"]
        total = result["hits"]["total"]["value"]
        print(f"\n  Query: \"{query}\"  [{note}] — {total} total matches")
        print(f"  {'Rank':<5} {'Score':>8}  {'Title'}")
        print(f"  {'-'*5}  {'-'*8}  {'-'*40}")
        for rank, hit in enumerate(hits, 1):
            print(f"  {rank:<5}  {hit['_score']:>8.3f}  {hit['_source']['title']}")


# ── Phase 4: Multi-field boosting ────────────────────────────────────────────

def phase4_multi_field_boosting(es: Elasticsearch):
    section("Phase 4: Multi-Field Boosting (title^3, description^1)")

    print("""
  Field boosting: matches in the title field count 3× more than description.
  This reflects the intuition that if a product's title contains the search
  term, it's more relevant than if only the description mentions it.
""")

    query_text = "professional electronics"

    # Without boosting (equal weight)
    result_equal = es.search(
        index=INDEX_NAME,
        body={
            "query": {
                "multi_match": {
                    "query": query_text,
                    "fields": ["title", "description"],
                }
            },
            "size": 5,
        }
    )

    # With title boosting
    result_boosted = es.search(
        index=INDEX_NAME,
        body={
            "query": {
                "multi_match": {
                    "query": query_text,
                    "fields": ["title^3", "description^1"],
                }
            },
            "size": 5,
        }
    )

    print(f"  Query: \"{query_text}\"\n")
    print(f"  Without boosting:                    With title^3 boosting:")
    print(f"  {'Score':>8}  {'Title':<35}   {'Score':>8}  {'Title'}")
    print(f"  {'-'*8}  {'-'*35}   {'-'*8}  {'-'*35}")
    hits_eq = result_equal["hits"]["hits"]
    hits_bo = result_boosted["hits"]["hits"]
    for i in range(min(5, len(hits_eq), len(hits_bo))):
        eq_score = hits_eq[i]["_score"]
        eq_title = hits_eq[i]["_source"]["title"][:33]
        bo_score = hits_bo[i]["_score"]
        bo_title = hits_bo[i]["_source"]["title"][:33]
        print(f"  {eq_score:>8.3f}  {eq_title:<35}   {bo_score:>8.3f}  {bo_title}")

    print("""
  Boosting ensures title-matching results rank above description-only matches.
  Real search engines use learned field weights (Learning-to-Rank) rather than
  hard-coded boost values.
""")


# ── Phase 5: Spell correction ─────────────────────────────────────────────────

def phase5_spell_correction(es: Elasticsearch):
    section("Phase 5: Query Understanding — Spell Correction")

    print("""
  The Elasticsearch Suggest API uses the indexed terms to suggest corrections.
  'term' suggester: suggest corrections for individual mis-spelled terms.
  'phrase' suggester: suggest corrections for the whole phrase using n-gram model.
""")

    misspelled_queries = [
        ("premim widget", "premium widget"),
        ("profesional tool", "professional tool"),
        ("eleectronics gadget", "electronics gadget"),
    ]

    for misspelled, expected in misspelled_queries:
        result = es.search(
            index=INDEX_NAME,
            body={
                "suggest": {
                    "title-suggest": {
                        "text": misspelled,
                        "term": {
                            "field": "title",
                            "suggest_mode": "always",
                            "sort": "score",
                            "max_edits": 2,
                        }
                    }
                },
                "size": 0,
            }
        )
        suggestions = result.get("suggest", {}).get("title-suggest", [])
        corrected_terms = []
        for term_suggestion in suggestions:
            opts = term_suggestion.get("options", [])
            if opts:
                corrected_terms.append(opts[0]["text"])
            else:
                corrected_terms.append(term_suggestion["text"])
        corrected = " ".join(corrected_terms)

        print(f"  Misspelled:  \"{misspelled}\"")
        print(f"  Suggested:   \"{corrected}\"")
        print(f"  Expected:    \"{expected}\"")
        match = "CORRECT" if corrected.lower() == expected.lower() else "DIFFERENT"
        print(f"  Result:      {match}\n")


# ── Phase 6: Faceted search ───────────────────────────────────────────────────

def phase6_faceted_search(es: Elasticsearch):
    section("Phase 6: Faceted Search with Aggregations")

    print("""
  Faceted search (facets / filters) lets users drill down by category, brand,
  price range, etc. Elasticsearch aggregations compute these counts efficiently
  during the same query — no second round-trip needed.
""")

    result = es.search(
        index=INDEX_NAME,
        body={
            "query": {"match": {"title": "widget"}},
            "size": 0,  # we only want aggregations, not documents
            "aggs": {
                "categories": {
                    "terms": {"field": "category", "size": 10}
                },
                "brands": {
                    "terms": {"field": "brand", "size": 5}
                },
                "price_ranges": {
                    "range": {
                        "field": "price",
                        "ranges": [
                            {"to": 50, "key": "Under $50"},
                            {"from": 50, "to": 150, "key": "$50-$150"},
                            {"from": 150, "to": 300, "key": "$150-$300"},
                            {"from": 300, "key": "Over $300"},
                        ]
                    }
                },
                "avg_rating": {"avg": {"field": "rating"}},
                "in_stock_count": {
                    "filter": {"term": {"in_stock": True}}
                },
            }
        }
    )

    total_matches = result["hits"]["total"]["value"]
    aggs = result["aggregations"]

    print(f"  Query: \"widget\" — {total_matches} matching products\n")

    print(f"  By Category:")
    for bucket in aggs["categories"]["buckets"]:
        bar = "#" * (bucket["doc_count"] // 2)
        print(f"    {bucket['key']:<20} {bucket['doc_count']:>5}  {bar}")

    print(f"\n  By Brand (top 5):")
    for bucket in aggs["brands"]["buckets"]:
        print(f"    {bucket['key']:<20} {bucket['doc_count']:>5}")

    print(f"\n  By Price Range:")
    for bucket in aggs["price_ranges"]["buckets"]:
        print(f"    {bucket['key']:<20} {bucket['doc_count']:>5}")

    print(f"\n  Average rating:  {aggs['avg_rating']['value']:.2f}")
    print(f"  In stock:        {aggs['in_stock_count']['doc_count']}")

    print("""
  How facets work internally:
  - keyword fields use doc_values (columnar storage on disk)
  - term aggregation = group-by on doc_values with a fixed-size priority queue
  - All aggs run in one pass over the matching posting lists → O(matches) time
  - No separate "count" query needed — aggregations are computed alongside search
""")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    section("SEARCH ENGINE LAB")
    print("""
  Architecture:
    Documents → Indexing pipeline → Inverted index → Query engine → Results

  Elasticsearch stack:
    Lucene segments → immutable sorted posting lists
    BM25 scoring → field boosting → aggregations

  This lab builds a product search engine over 5,000 documents.
""")

    es = Elasticsearch(ES_URL, request_timeout=30)
    wait_for_es(es)

    phase1_manual_inverted_index()
    phase2_index_products(es)
    phase3_full_text_search(es)
    phase4_multi_field_boosting(es)
    phase5_spell_correction(es)
    phase6_faceted_search(es)

    section("Lab Complete")
    print("""
  Summary:
  - Inverted index: term → posting list of (doc_id, tf) pairs
  - TF-IDF scores rare terms in matching docs higher (IDF boost)
  - BM25 adds document-length normalization over TF-IDF
  - Field boosting: title^3 makes title matches rank above description matches
  - Suggest API: term-level spell correction using edit distance on index vocab
  - Aggregations: facets (category, price range) computed in one query pass

  Next: 09-notification-system/ — fan-out delivery at 1B notifications/day
""")


if __name__ == "__main__":
    main()
