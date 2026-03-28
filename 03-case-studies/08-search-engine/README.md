# Case Study: Search Engine

**Prerequisites:** `../../02-advanced/08-search-systems/`, `../../01-foundations/09-indexes/`

---

## The Problem at Scale

Google handles 8.5 billion queries per day across a trillion-document index. Every query must return ranked results in
under 100ms — including network latency, scoring, and rendering.

| Metric               | Value                        |
|----------------------|------------------------------|
| Index size           | ~1 trillion documents        |
| Queries/day          | 8.5 billion                  |
| Queries/second       | ~98,000 RPS                  |
| Query latency target | < 100ms P99                  |
| Index update latency | < 1 hour for important pages |
| New documents/day    | ~100 million                 |
| Index storage        | Petabytes (distributed)      |

The core challenge: you cannot scan 1 trillion documents per query. The inverted index structure allows sub-millisecond
lookup of which documents contain a given word, reducing the problem from O(N) scan to O(k) where k is the number of
matching documents.

---

## 1. Clarify Requirements (3–5 min)

Before drawing any architecture, scope the problem explicitly. Interviewers reward engineers who identify what they are
*not* building.

**Functional scope questions:**

- Are we building a general web search (like Google) or a vertical/domain search (e-commerce, enterprise)? → Different
  freshness, corpus size, ranking signals.
- Full-text search only, or also structured filtering (price range, date, category)?
- Do we need query understanding: spell correction, synonym expansion, entity recognition?
- Personalization? Same query can return different results for different users.
- Near-real-time indexing (seconds) or batch (hours)?
- Do we need to support phrase queries (`"machine learning"`) and proximity queries?
- Semantic / vector search (meaning-based) or keyword-only?

**Non-functional scope questions:**

- Latency target: P99 < 100ms? At what RPS?
- Consistency model for index updates: is stale data (1–5 min) acceptable?
- Geographic distribution: serve queries from a single region or globally?
- Index size: thousands of documents (enterprise) or trillions (web)?

**Reasonable defaults for a FAANG interview:**

- Web-scale corpus: ~1 trillion documents, ~100M new/day
- 100K RPS globally, P99 < 100ms
- Near-real-time indexing (< 5 min for important pages)
- Full-text + structured filters + spell correction
- No strong personalization (simplification)

---

## 2. Capacity Estimation (3–5 min)

### Storage

| Metric                                    | Calculation                               | Result        |
|-------------------------------------------|-------------------------------------------|---------------|
| Raw document storage                      | 1T docs × 10 KB avg                       | ~10 PB        |
| Inverted index size                       | ~30–50% of raw (compressed posting lists) | ~3–5 PB       |
| Forward index (doc values, stored fields) | ~20% of raw                               | ~2 PB         |
| Replication factor (3×)                   | (5 PB index + 2 PB fwd) × 3               | ~21 PB total  |
| New data/day                              | 100M docs × 10 KB                         | ~1 TB/day raw |

### Query Throughput

| Metric            | Calculation                             | Result                              |
|-------------------|-----------------------------------------|-------------------------------------|
| Query RPS         | 8.5B / 86,400s                          | ~98,000 RPS                         |
| Peak multiplier   | ~3× average                             | ~300,000 RPS peak                   |
| Queries per shard | ~2,000 RPS per Lucene shard (empirical) | ~50 shards for average load         |
| Shards for peak   | 300K / 2K                               | ~150 shards (or fewer larger nodes) |
| Result bandwidth  | 98K RPS × 10 KB avg response            | ~1 GB/s egress                      |

### Crawl Pipeline

| Metric                        | Calculation                 | Result                  |
|-------------------------------|-----------------------------|-------------------------|
| Re-crawl rate (high priority) | 10B pages × 1 re-crawl/week | ~16,500 pages/s         |
| Crawler bandwidth             | 16.5K pages/s × 10 KB/page  | ~165 MB/s inbound       |
| Index write throughput        | 100M new docs/day           | ~1,160 docs/s sustained |

**Key insight:** at 50 shards the coordinator receives 50 × top-K results per query. For K=100, that's 5,000 candidates
to re-rank in <5ms — manageable with in-memory sorting, but sets an upper bound on shard count before coordinator
becomes a bottleneck.

---

## 3. High-Level Architecture

```
  Web Crawlers
       │
       ▼
  ┌─────────────────────────────────────────────────────────────┐
  │                   Crawl Frontier                            │
  │  URL scheduler (priority queue by importance + freshness)   │
  │  Deduplication via URL fingerprint + content hash (SimHash) │
  └──────────────────────┬──────────────────────────────────────┘
                         │  raw HTML / document bytes
                         ▼
  ┌─────────────────────────────────────────────────────────────┐
  │               Indexing Pipeline  (Kafka-based)              │
  │  Parse → Tokenize → Stem → TF count → PageRank lookup       │
  │  Emit (term, doc_id, tf, static_score) tuples to Kafka      │
  └──────────────────────┬──────────────────────────────────────┘
                         │  posting list segments
                         ▼
  ┌─────────────────────────────────────────────────────────────┐
  │                   Index Cluster                             │
  │   Shard 0       Shard 1      ...      Shard N               │
  │  (immutable    (immutable            (immutable             │
  │  Lucene segs)  Lucene segs)          Lucene segs)           │
  │                                                             │
  │  Each shard: primary + 2 replicas for fault tolerance       │
  └──────────────────────┬──────────────────────────────────────┘
                         │
  ┌──────────────────────▼─────────────────────────────────────┐
  │               Query Coordinator (scatter-gather)            │
  │  1. Parse query → spell correct → expand synonyms           │
  │  2. Fan out to all N shards in parallel                     │
  │  3. Each shard returns top-K (BM25-ranked) results          │
  │  4. Merge K×N candidates, apply LTR model to top-1000       │
  │  5. Snippet generation → return top-10                      │
  └──────────────────────┬─────────────────────────────────────┘
                         │
  ┌──────────────────────▼─────────────────────────────────────┐
  │                 Cache Layer                                 │
  │  Query result cache (Redis):  popular queries cached 60s    │
  │  Filter cache (in-process):   bitsets for common filters    │
  │  Fielddata cache:             aggregation field data        │
  └─────────────────────────────────────────────────────────────┘
```

**Inverted index:** each shard holds a subset of documents. The index maps term → posting list (sorted array of doc
IDs + term frequencies). Lucene stores posting lists as compressed, sorted byte arrays on disk and mmaps them into
memory for fast access.

**Query execution (scatter-gather):** the coordinator broadcasts the query to all shards in parallel. Each shard returns
its top-K results. The coordinator merges and re-ranks the K×N candidates to produce the final top-10. This is
embarrassingly parallel — latency is bounded by the *slowest* shard (the straggler problem).

**Re-ranking:** BM25 gives a first-pass relevance score. A Learning-to-Rank (LTR) model (LambdaMART/XGBoost) takes the
top-1000 BM25 results plus additional signals (click-through rate, freshness, PageRank, user history) and produces the
final ranked list.

---

## 4. Deep Dives

### 4.1 Inverted Index at Scale: Posting List Compression

A posting list for a common word like "the" might contain hundreds of millions of doc IDs. Storing them as 4-byte
integers would require ~400 MB for one term. Compression is essential.

**Delta encoding:** instead of storing `[1001, 1005, 1020, 1021]`, store `[1001, 4, 15, 1]`. The differences (deltas)
between consecutive sorted IDs are small integers, compressible with variable-byte encoding.

**Variable-byte (VByte) encoding:** uses 7 bits per byte for the value, 1 bit as continuation flag. Numbers < 128 take 1
byte; numbers < 16,384 take 2 bytes. Typical compression ratio: 4× over fixed-width integers.

**FOR (Frame of Reference):** divide posting list into 128-element blocks. Each block stores a base value + small deltas
that fit in fewer bits (e.g., 4 bits if all deltas < 16). Used by Lucene's `FOR` codec.

**PForDelta:** similar to FOR but patches outliers separately. Used in many production systems for 5–10× compression
over raw integers.

**Effect on query latency:** compressed posting lists fit in CPU cache. Decompression with SIMD instructions takes ~1
clock cycle per integer. An intersection of two 10M-entry posting lists takes ~10ms on modern hardware.

### 4.2 Ranking: TF-IDF → BM25 → Learning-to-Rank

**TF-IDF** (1972): `score(t,d) = TF(t,d) × log(N/df(t))`

- Problem: a 1000-word document mentioning "cat" 10 times scores the same as a 10-word document mentioning "cat" 10
  times.

**BM25** (1994, "Best Match 25"): adds document-length normalization:

```
BM25(t,d) = IDF(t) × (tf × (k1+1)) / (tf + k1 × (1 - b + b × |d|/avgdl))
```

- `k1=1.2`: controls TF saturation. At k1=1.2, TF=10 gives only ~9× the score of TF=1.
- `b=0.75`: length normalization. b=1.0 = full normalization; b=0 = no normalization.
- BM25 is the default in Elasticsearch, Lucene, and Solr.

**The distributed BM25 IDF problem (critical at scale):** IDF is computed using global document frequency (`df`) and
total document count (`N`). In a sharded cluster, each shard only sees its local subset — its local IDF estimate can
deviate significantly from the true global IDF, especially for rare terms. Elasticsearch addresses this with:

1. **Global IDF mode:** coordinator collects per-term `df` from all shards before executing the query (extra round-trip,
   adds ~5ms).
2. **DFS (Distributed Frequency Search):** `search_type=dfs_query_then_fetch` collects global stats first.
3. **Large corpus assumption:** at millions of docs per shard, local IDF converges to global IDF statistically —
   production systems typically accept the approximation.

**Learning-to-Rank (LTR):** a gradient-boosted tree (LambdaMART/XGBoost) trained on click data:

- Features: BM25 score, PageRank, document freshness, click-through rate, user query history, anchor text signals
- Training signal: which result did users click? (implicit relevance judgement via pairwise NDCG optimization)
- Applied to top-1000 BM25 results (re-ranking, not replacing first-pass retrieval)
- 10–30% improvement in NDCG@10 over BM25 alone in production systems

### 4.3 Indexing Pipeline: Crawl → Parse → Merge

1. **Crawl:** distributed crawlers fetch URLs from the frontier. DNS caching, robots.txt compliance, politeness delays.
   SimHash deduplication prevents near-duplicate pages from inflating index size.
2. **Parse:** extract text from HTML, PDF, Word. Strip tags, normalize whitespace. Extract structured metadata (title,
   author, date).
3. **Tokenize:** split text into terms. Handle hyphenation, contractions, punctuation, Unicode.
4. **Normalize:** lowercase, remove stop words, stem (running → run) or lemmatize. Language detection per document.
5. **Score pre-computation:** compute static scores (PageRank, domain authority) offline via iterative graph
   computation. These are merged with query-time BM25 at ranking time.
6. **Sort:** emit `(term, doc_id, tf)` tuples. Sort by term (MapReduce shuffle or Kafka partitioning by term hash).
7. **Merge:** for each term, merge all `(doc_id, tf)` pairs into a posting list.
8. **Compress:** delta-encode + VByte compress posting lists.
9. **Write segment:** Lucene writes immutable segment files. Background merges compact small segments into larger ones.

**Segment merging (LSM-tree analogy):** new documents go into a small in-memory buffer (IndexWriter). When full, flush
to a new immutable segment file on disk. Background threads merge small segments into larger ones. During merge, deleted
documents are physically removed (tombstone compaction). Query time degrades with many small segments (must search all),
so merging is critical for query performance.

**Near-real-time indexing:** Lucene's `IndexWriter` flushes in-memory buffers to a new searchable segment every 1 second
by default (configurable via `refresh_interval`). The segment is searchable immediately after flush — before `fsync`.
Full durability uses fsync on a longer interval (5s default in Elasticsearch). This gives ~1s indexing latency with
minimal I/O overhead.

**refresh_interval trade-off for bulk loads:** setting `refresh_interval=-1` disables automatic refresh during a bulk
load. Elasticsearch no longer creates many small segments per second, yielding 3–10× higher ingest throughput. The
production pattern: set `-1` before loading, restore to `1s` and call `_forcemerge` when done.

### 4.4 Query Understanding: Spell Correction and Query Expansion

**Spell correction:**

- Vocabulary-based: suggest the closest term in the index vocabulary by edit distance (Levenshtein ≤ 2)
- Noisy channel model: `P(correction | misspelling) ∝ P(misspelling | correction) × P(correction)`. The prior
  `P(correction)` is the term's document frequency — rare index terms are poor suggestions.
- N-gram language model trained on query logs scores candidate corrected phrases holistically.
- Implementation: Elasticsearch `suggest` API uses a finite-state automaton (Levenshtein automaton) built from the
  vocabulary for O(1) lookup per edit distance threshold.

**Query expansion:**

- Synonyms: "tablet" → "tablet OR iPad OR slate" (manual synonym dictionary or Word2Vec/BERT embeddings for semantic
  expansion)
- Entity recognition: "apple" in a phones query → Apple Inc. (disambiguation via query context)
- Acronym expansion: "ML" → "machine learning"
- Query relaxation: if the AND of all terms returns zero results, progressively relax to OR.

**Query classification:**

- Navigational: user wants a specific website ("youtube login") → return direct link, de-rank other results
- Informational: user wants knowledge ("how does BM25 work") → return articles, knowledge panels
- Transactional: user wants to buy/do something ("buy iphone 15") → return shopping results, ads
- Classification changes ranking strategy; modern systems use a fine-tuned BERT classifier on query text.

### 4.5 Caching Strategy

**Query result cache (Redis/Memcached):**

- Cache top-1000 most frequent queries with 60s TTL.
- Hit rate: ~40–60% of queries are popular/repeated. Covers majority of traffic with small cache.
- Invalidation: TTL-based (acceptable for search — slightly stale results are fine).

**Filter/bitset cache (in-process, per shard):**

- Common filters (`in_stock=true`, `category=Electronics`) produce bitsets over all doc IDs.
- Bitset cached in JVM heap. A filter match becomes a bitset AND with the BM25 result set.
- LRU eviction. Invalidated when segment is merged (doc ID space changes).

**Shard request cache:**

- Elasticsearch caches aggregation results (facet counts) per shard for repeated identical queries.
- Invalidated on any index refresh. Useful for dashboard-style queries that repeat frequently.

**The cache-freshness tension:** a 60s query cache means index updates take up to 60s longer to appear in results. For
breaking news or price changes, this is unacceptable — use shorter TTLs or cache bypass for freshness-sensitive query
classes.

### 4.6 Failure Modes and Scale Challenges

**Shard failure:**

- Each primary shard has N replicas (typically 2). On primary failure, Elasticsearch promotes a replica to primary
  within seconds.
- During promotion, new writes are buffered; existing queries continue against remaining replicas.
- If all replicas of a shard fail simultaneously, that shard's documents are unavailable (partial result degradation vs.
  full query failure — configurable via `allow_partial_search_results`).

**Coordinator bottleneck (the fan-out problem):**

- At 1,000 shards, every query fans out to 1,000 shards. Coordinator must open 1,000 connections, await 1,000 responses,
  and merge 1,000 × top-K results.
- Mitigation: route queries to a subset of shards based on query type or user segment; use a two-tier coordinator (
  regional sub-coordinators aggregate before the global coordinator).
- Straggler mitigation: issue speculative duplicate requests to slow shards; use the first response.

**Hot term problem:**

- A posting list for "javascript" might be 50M entries. Deserializing and scoring 50M docs per query on one shard is a
  bottleneck.
- Mitigation: skip lists inside posting lists allow jumping past doc IDs below a minimum BM25 threshold (`min_score`).
  Lucene uses `Block WAND` algorithm — skips low-scoring candidates without fully evaluating them.

**Index skew (uneven shard sizing):**

- If one shard has 10× the documents of others (due to uneven hashing or manual routing), it becomes the tail latency
  bottleneck for every query.
- Detection: monitor per-shard document count and query latency via `_cat/shards` API.
- Mitigation: custom routing keys, periodic shard rebalancing.

**Split-brain and replica divergence:**

- Network partition can cause two nodes to both believe they are primary for the same shard.
- Elasticsearch uses a distributed consensus mechanism (similar to Raft via Zen Discovery / Cluster State Manager) to
  elect a single primary per shard. A quorum of master-eligible nodes must agree.
- Write fencing: Elasticsearch uses `sequence numbers` (per-shard monotonic counters) and `primary terms` to detect and
  reject writes from stale primaries after a partition heals.

### 4.7 Vector Search and Semantic Retrieval (Modern FAANG Requirement)

Keyword search fails on semantic intent: a query for "fast car" misses documents about "high-speed vehicles." Vector
search addresses this by encoding both queries and documents as dense vectors and retrieving by similarity.

**How it works:**

1. **Embedding model** (e.g., BERT, sentence-transformers, OpenAI `text-embedding-ada-002`) encodes each document chunk
   into a dense vector of 768–1536 dimensions.
2. At query time, the query is encoded into the same vector space.
3. **Approximate Nearest Neighbor (ANN)** search finds the top-K most similar document vectors by cosine similarity or
   dot product.

**ANN algorithms:**

- **HNSW (Hierarchical Navigable Small World):** graph-based index. Each node connects to M neighbors at multiple
  layers. Query traverses the graph greedily. O(log N) expected query time; build time is O(N log N). Used by
  Elasticsearch `dense_vector` field, pgvector, Milvus, Weaviate.
- **IVF-PQ (Inverted File + Product Quantization):** clusters vectors into Voronoi cells (IVF), then compresses
  residuals with product quantization. Lower memory than HNSW; slightly lower recall. Used by FAISS.
- **Recall vs. latency trade-off:** ANN algorithms trade recall for speed. HNSW at `ef_search=100` achieves ~95% recall
  at ~5ms for 10M vectors; increasing `ef_search` raises recall toward 100% at higher latency cost.

**Hybrid search (keyword + vector):**

- BM25 and ANN retrieval have complementary failure modes: BM25 misses semantic matches; ANN misses exact keyword
  matches.
- **Reciprocal Rank Fusion (RRF):** `score(d) = Σ 1/(k + rank_i(d))` across result lists from each retriever. Simple,
  parameter-free, and empirically strong.
- Production systems (Google, Bing, Amazon) use hybrid retrieval as the standard approach. BM25 handles
  navigational/exact queries; vector retrieval handles conversational/semantic queries.

**Retrieval-Augmented Generation (RAG):**

- Modern search-adjacent systems retrieve top-K chunks via hybrid search, then pass them as context to an LLM to
  synthesize an answer.
- Vector store is the retrieval backbone: chunk documents → embed → store in HNSW index → query at read time.
- Relevant at FAANG interviews when the question involves "AI-powered search" or "question answering over documents."

**Scale challenges for vector search:**

- A 1T-document corpus with 768-dimensional float32 vectors = ~3 PB of raw embedding storage (768 × 4 bytes × 1T).
  Product quantization reduces this 8–32×.
- HNSW graphs are memory-resident for fast traversal; IVF-PQ is more disk-friendly.
- **Sharding strategy:** unlike inverted indexes (shard by document hash), vector indexes benefit from clustering-aware
  sharding: documents with similar embeddings on the same shard → higher intra-shard recall → fewer shards to query per
  request.

---

## How It Actually Works

**Elasticsearch** stores documents as Lucene segments. Each segment is an immutable inverted index. Queries run against
all segments and results are merged. Background segment merging reduces the number of segments over time (the
`_forcemerge` API manually triggers this).

From the Elasticsearch "Inside the Architecture" series:

- Inverted index + doc values (columnar) stored per-segment
- BM25 computed entirely within each shard; coordinator does not re-score — it re-ranks by the shard-local BM25 score
- Aggregations use doc values (columnar storage) not the inverted index — this is why `keyword` fields are faster for
  aggregations than `text` fields

**Google's PageRank** (Page et al., 1998): the foundational insight that a page's importance is proportional to the
importance of pages that link to it. PageRank is computed offline via iterative power iteration on the web graph (
converges in ~50–100 iterations). It serves as a static prior in the BM25+LTR ranking model.

**Lucene** (Apache): the open-source library underlying Elasticsearch, Solr, and many commercial systems. Key
abstractions: `IndexWriter` (build), `IndexSearcher` (query), `Analyzer` (tokenize/normalize), `Similarity` (BM25
scoring), `Codec` (compressed segment format). 25+ years old and still the dominant full-text search library.

Source: Dean & Ghemawat "MapReduce: Simplified Data Processing on Large Clusters" (OSDI 2004); Page et al. "The PageRank
Citation Ranking" (1998); Elasticsearch "How Full-Text Search Works" (2024 docs); Lucene in Action (2nd ed.);
Elasticsearch distributed IDF blog (2014); Malkov & Yashunin "Efficient and robust approximate nearest neighbor search
using HNSW" (2018).

---

## Hands-on Lab

**Time:** ~30 minutes
**Services:** `elasticsearch` (8.13.4), `indexer` (Python)

### Setup

```bash
cd system-design-interview/03-case-studies/08-search-engine/
docker compose up -d elasticsearch
# Wait ~60s for Elasticsearch to be healthy (JVM startup takes time)
docker compose ps
```

### Experiment

```bash
# Run all phases via Docker (indexer service has no profile — just run it):
docker compose run --rm indexer

# Or locally:
pip install elasticsearch
ES_URL=http://localhost:9200 python experiment.py
```

The script runs 7 phases automatically:

1. **Manual inverted index:** build TF-IDF index over 200 docs in pure Python — shows the algorithm
2. **Index 5,000 products:** bulk index into Elasticsearch with custom mapping
3. **Full-text search:** query with BM25 scores — show top results and scores
4. **Multi-field boosting:** compare `title^3` vs equal-weight field search
5. **Spell correction:** misspelled query → Suggest API → corrected terms
6. **Faceted search:** category + brand + price range aggregations in one query
7. **BM25 explain + refresh trade-off:** show exact BM25 computation breakdown and `refresh_interval` impact on indexing
   throughput

### Break It

**Observe segment merges:**

```bash
# Watch segment count before/after indexing
curl -s http://localhost:9200/products/_stats/segments | python3 -m json.tool | grep -E '"count"|"size"'

# Force a merge to a single segment
curl -X POST "http://localhost:9200/products/_forcemerge?max_num_segments=1"

# Check again — one large segment now
curl -s http://localhost:9200/products/_stats/segments | python3 -m json.tool | grep -E '"count"|"size"'
```

**Show BM25 explanation (the exact IDF/TF/length-norm math):**

```bash
curl -X GET "http://localhost:9200/products/_explain/1" -H 'Content-Type: application/json' -d '{
  "query": {"match": {"title": "premium widget"}}
}'
```

**See the distributed IDF problem:**

```bash
# dfs_query_then_fetch collects global term stats before scoring — eliminates shard-local IDF skew
curl -X GET "http://localhost:9200/products/_search?search_type=dfs_query_then_fetch" \
  -H 'Content-Type: application/json' -d '{"query": {"match": {"title": "premium"}}}'
```

**Measure refresh_interval impact manually:**

```bash
# Check current segment count
curl -s "http://localhost:9200/products/_stats/segments" | python3 -m json.tool | grep '"count"'

# Disable refresh and bulk index 1000 more docs, then re-enable
curl -X PUT "http://localhost:9200/products/_settings" \
  -H 'Content-Type: application/json' -d '{"index": {"refresh_interval": "-1"}}'
# ... index docs ...
curl -X PUT "http://localhost:9200/products/_settings" \
  -H 'Content-Type: application/json' -d '{"index": {"refresh_interval": "1s"}}'
curl -X POST "http://localhost:9200/products/_refresh"
```

### Observe

```bash
# Cluster health and shard info
curl -s http://localhost:9200/_cat/shards?v

# Index stats
curl -s http://localhost:9200/products/_stats | python3 -m json.tool | grep -A2 '"docs"'

# See per-term posting list statistics
curl -s "http://localhost:9200/products/_termvectors/1?fields=title" | python3 -m json.tool
```

### Teardown

```bash
docker compose down -v
```

---

## Interview Checklist

1. **Q: What is an inverted index and why is it needed for search?**
   A: An inverted index maps each term to the list of document IDs containing that term (posting list). Without it, a
   query would require scanning all N documents — O(N) per query. With it, we look up each query term in the index — O(
   k) where k is the number of matching docs. For 1T documents, this is the difference between 1ms and hours per query.

2. **Q: What is the difference between TF-IDF and BM25?**
   A: Both weight terms by frequency (TF) and rarity (IDF). BM25 adds document-length normalization: a 1000-word doc
   with "cat" 10 times shouldn't outrank a 10-word doc with "cat" 3 times. The `k1` parameter controls TF saturation (
   more occurrences give diminishing returns) and `b` controls length normalization strength. BM25 consistently
   outperforms TF-IDF on standard IR benchmarks (TREC).

3. **Q: How does Elasticsearch scale to handle 100K RPS?**
   A: Horizontal sharding. An index is split into N primary shards, each holding a subset of documents. Each shard is an
   independent Lucene instance. A query coordinator (scatter-gather) fans out the query to all shards in parallel, then
   merges the top-K results from each. Adding shards adds throughput linearly — until the coordinator becomes the
   bottleneck at very high shard counts.

4. **Q: What is the distributed IDF problem and how do you solve it?**
   A: BM25 IDF requires knowing the global document frequency of a term across all shards. Each shard only knows its
   local `df`. For rare terms (low `df`), local IDF estimates can deviate significantly across shards, causing ranking
   inconsistencies. Solutions: (1) `dfs_query_then_fetch` — coordinator collects per-term `df` from all shards first (
   extra round-trip, ~5ms overhead); (2) accept the approximation for large corpora where local IDF converges to global
   IDF statistically.

5. **Q: How do you handle near-real-time indexing?**
   A: Lucene's IndexWriter flushes to a new in-memory segment periodically (default: every 1 second in Elasticsearch).
   The segment is searchable immediately after flush — before fsync. Full disk durability uses fsync on a longer
   interval (5s default). The `refresh_interval` setting controls this trade-off: setting it to `-1` during bulk
   indexing improves throughput 3–10× at the cost of searchability during the load.

6. **Q: What is field boosting and when do you use it?**
   A: Field boosting multiplies the BM25 score for matches in that field. `title^3` means a title match scores 3× as
   high as a body match. Used when some fields are inherently more important (product title vs description, article
   headline vs body). In production, boost values are often learned via LTR rather than set by hand, because hand-tuned
   boosts rarely generalize across query types.

7. **Q: How does spell correction work in a search engine?**
   A: The index vocabulary is itself the dictionary. For a misspelled term, a Levenshtein automaton efficiently
   enumerates all vocabulary terms within edit distance 2. Candidates are scored by a noisy-channel model: edit
   distance (likelihood of the typo) × term document frequency (prior probability the term is correct). Elasticsearch's
   term suggester does this via a finite-state automaton built from the vocabulary at index time.

8. **Q: What are facets and how are they computed efficiently?**
   A: Facets are counts of how many matching documents fall in each category (e.g., "Electronics: 45, Clothing: 23").
   They're computed via aggregations over `doc_values` — a columnar forward index stored alongside the inverted index.
   Aggregations run in one pass over the posting list of matching docs, reading the category field from doc_values. All
   facets are computed in a single query — no separate requests needed.

9. **Q: How do you handle a shard failure in production?**
   A: Each shard has replica copies on different nodes. On primary failure, Elasticsearch promotes a replica to primary
   within seconds (consensus-based election). During promotion, writes buffer and queries continue against remaining
   replicas. If all copies of a shard are unavailable, you get a partial result (controllable via
   `allow_partial_search_results`). The system never returns incorrect results — only potentially incomplete ones.

10. **Q: How do you keep a trillion-document index up to date?**
    A: Continuous re-crawl pipeline: high-priority pages (news, trending) re-crawled every few hours; long-tail pages
    monthly. New documents flow through the indexing pipeline (parse → tokenize → sort → merge) and land in new Lucene
    segments. Segment merges happen in the background — large segments are never rebuilt from scratch. The net effect:
    new documents are searchable within 1–5 minutes of crawling.

11. **Q: Walk me through a search query from user keystroke to ranked results.**
    A: (1) User types query → query understanding: spell correct (Levenshtein automaton on index vocab), expand
    synonyms, classify intent (navigational/informational/transactional). (2) Parse into structured query: BooleanQuery
    with term + phrase clauses. (3) Scatter to all N shards in parallel. (4) Each shard: look up posting lists for each
    term, intersect/union (Block WAND for early termination), score with BM25, return top-K. (5) Coordinator: merge K×N
    candidates, apply LTR model to top-1000. (6) Generate snippets (highlighted matching sentences). (7) Check query
    result cache on the way out. Total: 50–100ms end-to-end.

12. **Q: What happens when a very popular term (like "javascript") is queried?**
    A: Its posting list may have tens of millions of entries. Lucene uses the **Block WAND** (Weak AND) algorithm:
    posting lists are traversed in sorted order and candidates below a dynamic minimum score threshold are skipped using
    skip lists embedded in the posting list structure. This reduces the number of docs fully scored from O(posting list
    size) to O(results needed), making popular-term queries viable at low latency.

13. **Q: What is vector search and when would you use it instead of BM25?**
    A: Vector search encodes documents and queries as dense embeddings (768–1536 float32 dimensions) using a neural
    model (BERT, sentence-transformers). At query time, Approximate Nearest Neighbor (ANN) search — typically HNSW —
    retrieves the K documents with highest cosine similarity. Use vector search when semantic intent matters: "how to
    reduce latency" should match documents about "performance optimization" even if those exact words don't appear. BM25
    handles exact keyword matches better; vector search handles paraphrase and intent. Production systems use **hybrid
    retrieval**: run both BM25 and ANN independently, then fuse results with Reciprocal Rank Fusion (RRF):
    `score(d) = Σ 1/(k + rank_i(d))`. HNSW recall at 95% requires ~5ms for 10M vectors — feasible within a 100ms budget
    alongside BM25 retrieval.
