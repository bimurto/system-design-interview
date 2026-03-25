# Case Study: Search Engine

**Prerequisites:** `../../02-advanced/08-search-systems/`, `../../01-foundations/09-indexes/`

---

## The Problem at Scale

Google handles 8.5 billion queries per day across a trillion-document index. Every query must return ranked results in under 100ms — including network latency, scoring, and rendering.

| Metric | Value |
|---|---|
| Index size | ~1 trillion documents |
| Queries/day | 8.5 billion |
| Queries/second | ~98,000 RPS |
| Query latency target | < 100ms P99 |
| Index update latency | < 1 hour for important pages |
| New documents/day | ~100 million |
| Index storage | Petabytes (distributed) |

The core challenge: you cannot scan 1 trillion documents per query. The inverted index structure allows sub-millisecond lookup of which documents contain a given word, reducing the problem from O(N) scan to O(k) where k is the number of matching documents.

---

## Requirements

### Functional
- Index any text document with structured metadata fields
- Full-text search across indexed documents, ranked by relevance
- Multi-field search with per-field importance weighting
- Faceted filtering (by category, date, price range, etc.)
- Query understanding: spell correction, query expansion
- Near-real-time indexing (new documents searchable within seconds)

### Non-Functional
- Sub-100ms query latency P99 at 100K RPS
- Horizontal scalability: add shards to increase throughput
- Fault tolerance: no single point of failure for queries
- Index freshness: new documents visible within minutes

### Capacity Estimation

| Metric | Calculation | Result |
|---|---|---|
| Query RPS | 8.5B / 86,400s | ~98,000 RPS |
| Index shards needed | 98K RPS / 2K RPS per shard | ~50 shards |
| Storage per document | title + body + metadata ~10KB | — |
| Total raw storage | 1T × 10KB | ~10 PB |
| Index overhead | ~50% of raw (posting lists + metadata) | ~5 PB |
| Bandwidth (query results) | 98K × 10KB avg response | ~1 GB/s |

---

## High-Level Architecture

```
  Documents (crawled HTML, PDFs, etc.)
       │
       ▼
  ┌────────────────────────────────────────────────────────┐
  │              Indexing Pipeline                          │
  │  Parse → Tokenize → Stem → Score → Merge segments      │
  └──────────────────────┬─────────────────────────────────┘
                         │  inverted index shards
       ┌─────────────────▼──────────────────────┐
       │            Index Cluster                │
       │  Shard 0   Shard 1  ...  Shard N        │
       │  (sorted   (sorted       (sorted         │
       │  postings) postings)     postings)       │
       └─────────────────┬──────────────────────┘
                         │
  ┌──────────────────────▼──────────────────────┐
  │          Query Processing Layer              │
  │  Parse query → Lookup posting lists          │
  │  Score (BM25) → Merge results → Rank         │
  └──────────────────────┬──────────────────────┘
                         │
  ┌──────────────────────▼──────────────────────┐
  │          Result Serving / Ranking            │
  │  Re-ranking (Learning-to-Rank)               │
  │  Personalization, ads mixing                 │
  │  Snippet generation                          │
  └─────────────────────────────────────────────┘
```

**Inverted Index:** Each shard stores a subset of documents. The index maps term → posting list (sorted list of doc IDs containing that term). Lucene (Elasticsearch's core) stores posting lists as compressed, sorted arrays on disk.

**Query execution (scatter-gather):** The query coordinator broadcasts the query to all shards. Each shard returns its top-K results. The coordinator merges and re-ranks the K×N candidates to produce the final top-10.

**Re-ranking:** BM25 gives a first-pass relevance score. A Learning-to-Rank (LTR) model takes the top-1000 BM25 results plus additional signals (click-through rate, freshness, PageRank, user history) and produces the final ranked list.

---

## Deep Dives

### 1. Inverted Index at Scale: Posting List Compression

A posting list for a common word like "the" might contain hundreds of millions of doc IDs. Storing them as 4-byte integers would require ~400MB for one term. Compression is essential.

**Delta encoding:** instead of storing [1001, 1005, 1020, 1021], store [1001, 4, 15, 1]. The differences (deltas) between consecutive sorted IDs are small integers, compressible with variable-byte encoding.

**Variable-byte (VByte) encoding:** uses 7 bits per byte for the value, 1 bit as continuation flag. Numbers < 128 take 1 byte; numbers < 16,384 take 2 bytes. Typical compression ratio: 4x over fixed-width integers.

**FOR (Frame of Reference):** divide posting list into 128-element blocks. Each block stores a base value + small deltas. Deltas fit in fewer bits (e.g., 4 bits if all deltas < 16). Used by Lucene's `FOR` codec.

**PForDelta:** similar to FOR but patches outliers separately. Used in many production systems for 5-10x compression over raw integers.

**Effect on query latency:** compressed posting lists fit in CPU cache. Decompression with SIMD instructions takes ~1 clock cycle per integer. An intersection of two 10M-entry posting lists takes ~10ms on modern hardware.

### 2. Ranking: TF-IDF → BM25 → Learning-to-Rank

**TF-IDF** (1972): score(t,d) = TF(t,d) × log(N/df(t))
- Problem: a 1000-word document mentioning "cat" 10 times scores the same as a 10-word document mentioning "cat" 10 times.

**BM25** (1994, "Best Match 25"): adds document-length normalization:
```
BM25(t,d) = IDF(t) × (tf × (k1+1)) / (tf + k1 × (1 - b + b × |d|/avgdl))
```
- k1=1.2: controls TF saturation. At k1=1.2, TF=10 gives only ~9× the score of TF=1.
- b=0.75: length normalization. b=1.0 = full normalization; b=0 = no normalization.
- BM25 is the default in Elasticsearch, Lucene, and Solr.

**Learning-to-Rank (LTR):** a gradient-boosted tree (LambdaMART/XGBoost) trained on click data:
- Features: BM25 score, PageRank, document freshness, click-through rate, user query history
- Training signal: which result did users click? (implicit relevance judgement)
- Applied to top-1000 BM25 results (re-ranking, not replacing first-pass retrieval)
- 10-30% improvement in NDCG@10 over BM25 alone in production systems

### 3. Indexing Pipeline: Crawl → Parse → Merge

Google's original MapReduce paper (Dean & Ghemawat, 2004) describes the indexing pipeline:

1. **Parse:** extract text from HTML, PDF, Word. Strip tags, normalize whitespace.
2. **Tokenize:** split text into terms. Handle hyphenation, contractions, punctuation.
3. **Normalize:** lowercase, remove stop words, stem (running → run) or lemmatize.
4. **Score pre-computation:** compute static scores (PageRank, domain authority) offline.
5. **Sort:** emit (term, doc_id, tf) tuples. Sort by term (MapReduce shuffle).
6. **Merge:** for each term, merge all (doc_id, tf) pairs into a posting list.
7. **Compress:** delta-encode + VByte compress posting lists.
8. **Write segment:** Lucene writes immutable segment files. Background merges compact small segments into larger ones.

**Segment merging:** Lucene uses an LSM-tree-like approach. New documents go into a small in-memory buffer (IndexWriter). When full, flush to a new immutable segment file. Background threads merge small segments into larger ones (log-structured merge). During merge, deleted documents are physically removed.

### 4. Query Understanding: Spell Correction and Query Expansion

**Spell correction:**
- Vocabulary-based: suggest the closest term in the index vocabulary by edit distance (Levenshtein ≤ 2)
- N-gram model: train a language model on query logs to score candidate corrections
- Noisy channel model: P(correction | misspelling) ∝ P(misspelling | correction) × P(correction)
- Implementation: Elasticsearch `suggest` API uses the index vocabulary + edit distance

**Query expansion:**
- Synonyms: "tablet" → "tablet OR iPad OR slate" (manual synonym dictionary or Word2Vec)
- Entity recognition: "apple" in query about phones → Apple Inc. (entity disambiguation)
- Acronym expansion: "ML" → "machine learning"

**Query classification:**
- Navigational: user wants a specific website ("youtube login")
- Informational: user wants knowledge ("how does BM25 work")
- Transactional: user wants to buy/do something ("buy iphone 15")
- Classification changes ranking strategy (navigational → direct link; transactional → shopping results)

---

## How It Actually Works

**Elasticsearch** stores documents as Lucene segments. Each segment is an immutable inverted index. Queries run against all segments and results are merged. Background segment merging reduces the number of segments over time.

From the Elasticsearch "Inside the Architecture" series:
- Inverted index + doc values (columnar) stored per-segment
- BM25 computed entirely within each shard, no cross-shard coordination for scoring
- Aggregations use doc values (columnar storage) not the inverted index

**Google's PageRank** (Page et al., 1998): the foundational insight that a page's importance is proportional to the importance of pages that link to it. PageRank is computed offline via iterative power iteration on the web graph. It serves as a prior in the BM25+LTR ranking model.

**Lucene** (Apache): the open-source library underlying Elasticsearch, Solr, and many commercial systems. Key abstractions: IndexWriter (build), IndexSearcher (query), Analyzer (tokenize/normalize), Similarity (BM25 scoring). 25+ years old and still the dominant full-text search library.

Source: Dean & Ghemawat "MapReduce: Simplified Data Processing on Large Clusters" (OSDI 2004); Page et al. "The PageRank Citation Ranking" (1998); Elasticsearch "How Full-Text Search Works" (2024 docs); Lucene in Action (2nd ed.).

---

## Hands-on Lab

**Time:** ~20 minutes
**Services:** `elasticsearch` (8.8.0), `indexer` (Python)

### Setup

```bash
cd system-design-interview/03-case-studies/08-search-engine/
docker compose up -d elasticsearch
# Wait ~60s for Elasticsearch to be healthy (JVM startup takes time)
docker compose ps
```

### Experiment

```bash
# Run all phases
docker compose run --rm indexer

# Or locally:
pip install elasticsearch
ES_URL=http://localhost:9200 python experiment.py
```

The script runs 6 phases automatically:

1. **Manual inverted index:** build TF-IDF index over 200 docs in pure Python — shows the algorithm
2. **Index 5,000 products:** bulk index into Elasticsearch with custom mapping
3. **Full-text search:** query with BM25 scores — show top results and scores
4. **Multi-field boosting:** compare title^3 vs equal-weight field search
5. **Spell correction:** misspelled query → Suggest API → corrected terms
6. **Faceted search:** category + brand + price range aggregations in one query

### Break It

**Observe segment merges:**

```bash
# Watch segment count before/after indexing
curl -s http://localhost:9200/products/_stats/segments | python3 -m json.tool | grep -E '"count"|"size"'

# Force a merge
curl -X POST http://localhost:9200/products/_forcemerge?max_num_segments=1

# Check again — one large segment now
curl -s http://localhost:9200/products/_stats/segments | python3 -m json.tool | grep -E '"count"|"size"'
```

**Show BM25 explanation:**

```bash
curl -X GET "http://localhost:9200/products/_explain/1" -H 'Content-Type: application/json' -d '{
  "query": {"match": {"title": "premium widget"}}
}'
```

The `_explain` API shows the exact BM25 computation: TF, IDF, length normalization.

### Observe

```bash
# Cluster health and shard info
curl -s http://localhost:9200/_cat/shards?v

# Index stats
curl -s http://localhost:9200/products/_stats | python3 -m json.tool | grep -A2 '"docs"'
```

### Teardown

```bash
docker compose down -v
```

---

## Interview Checklist

1. **Q: What is an inverted index and why is it needed for search?**
   A: An inverted index maps each term to the list of document IDs containing that term (posting list). Without it, a query would require scanning all N documents — O(N) per query. With it, we look up each query term in the index — O(k) where k is the number of matching docs. For 1T documents, this is the difference between 1ms and hours per query.

2. **Q: What is the difference between TF-IDF and BM25?**
   A: Both weight terms by frequency (TF) and rarity (IDF). BM25 adds document-length normalization: a 1000-word doc with "cat" 10 times shouldn't outrank a 10-word doc with "cat" 3 times. The k1 parameter controls TF saturation (more occurrences give diminishing returns) and b controls length normalization strength. BM25 consistently outperforms TF-IDF on standard IR benchmarks.

3. **Q: How does Elasticsearch scale to handle 100K RPS?**
   A: Horizontal sharding. An index is split into N primary shards, each holding a subset of documents. Each shard is an independent Lucene instance. A query coordinator (the "scatter-gather" pattern) fans out the query to all shards in parallel, then merges the top-K results from each. Adding shards adds throughput linearly until the coordinator becomes the bottleneck.

4. **Q: How do you handle near-real-time indexing?**
   A: Lucene's IndexWriter flushes to a new in-memory segment periodically (default: every 1 second in Elasticsearch). The segment is available for search immediately after flush — before it's persisted to disk. Full disk durability uses fsync on a longer interval (5s default). This gives ~1s indexing latency with minimal I/O overhead.

5. **Q: What is field boosting and when do you use it?**
   A: Field boosting multiplies the BM25 score for matches in that field. `title^3` means a title match scores 3× as high as a body match. Used when some fields are inherently more important (product title vs description, article headline vs body). In production, boost values are often learned via LTR rather than set by hand.

6. **Q: How does spell correction work in a search engine?**
   A: The index vocabulary is itself the dictionary. For a misspelled term, compute edit distance to all vocabulary terms (Levenshtein ≤ 2). Score candidates by edit distance + term frequency (rare words shouldn't dominate suggestions). Elasticsearch's term suggester does this efficiently using a finite-state automaton built from the vocabulary.

7. **Q: What are facets and how are they computed efficiently?**
   A: Facets are counts of how many matching documents fall in each category (e.g., "Electronics: 45, Clothing: 23"). They're computed via aggregations over doc_values — a columnar forward index stored alongside the inverted index. Aggregations run in one pass over the posting list of matching docs, reading the category field from doc_values. No separate query needed.

8. **Q: How does Google's PageRank factor into search ranking?**
   A: PageRank is a static score computed offline from the web link graph. A page's rank is proportional to the sum of PageRank of all pages linking to it (recursively). It captures "how important is this page across all queries?" — independent of the query. In the final ranking model, PageRank is one of hundreds of features alongside BM25, CTR, freshness, and user signals.

9. **Q: How do you keep a trillion-document index up to date?**
   A: Continuous re-crawl pipeline: high-priority pages (news, trending) recrawled every few hours; long-tail pages monthly. New documents go through the indexing pipeline and are merged into existing Lucene segments. Segment merges happen in the background — large segments are never rebuilt from scratch, only new segments are created and merged incrementally.

10. **Q: Walk me through a search query from user keystroke to ranked results.**
    A: (1) User types query → query understanding: spell correct, expand synonyms, classify intent. (2) Parse into structured query: BooleanQuery with term + phrase clauses. (3) Scatter to all N shards in parallel. (4) Each shard: look up posting lists for each term, intersect/union, score with BM25, return top-K. (5) Coordinator: merge K×N candidates, apply LTR model to top-1000. (6) Generate snippets (highlighted matching sentences). (7) Return top-10 with titles, URLs, snippets. Total: 50-100ms end-to-end.
