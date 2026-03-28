# Search Systems

**Prerequisites:** `../07-distributed-caching/`
**Next:** `../10-rate-limiting-algorithms/`

---

## Concept

Full-text search is the ability to find documents that contain a given set of words, ranked by relevance. A relational database can answer "which rows contain the word 'wireless'?" with a `LIKE '%wireless%'` query, but this requires a full sequential scan — unusable for millions of rows. A search engine inverts this data structure: instead of scanning every document for the query term, it pre-builds an **inverted index** that maps each term to the list of documents containing it. Looking up "wireless" becomes a constant-time index lookup, returning a posting list of matching document IDs in microseconds.

The inverted index is the core data structure of every search engine, from Apache Lucene to Google. At its simplest, it is a hash map: `{term → [doc_id1, doc_id2, ...]}`. A more complete implementation stores position information: `{term → {doc_id → [position1, position2, ...]}}`. Positions enable **phrase queries** ("wireless headphones" must appear as an exact phrase, not just both words anywhere in the document) and proximity scoring (documents where the query terms appear close together are ranked higher). Without position data, you cannot implement phrase search — this is why Lucene's posting list format is significantly larger than a pure term-to-docid map.

Ranking is the other half of search. Early search engines used pure Boolean matching (a document either contains all query terms or doesn't), giving no way to distinguish a document where "wireless headphones" appears in the title from one where it appears once in a 10,000-word review. **TF-IDF** (Term Frequency-Inverse Document Frequency) introduced statistical relevance: a term's relevance to a document is proportional to how often it appears in that document (TF) and inversely proportional to how many documents contain it (IDF). Common words ("the", "and") have high document frequency and low IDF, so they contribute little to scores. Rare, specific terms have high IDF and are very informative.

**BM25** (Best Match 25) is the modern successor to TF-IDF, introduced in the 1990s and still the default in Elasticsearch and Solr. BM25 addresses two weaknesses of TF-IDF: **TF saturation** (a term appearing 100 times in a document shouldn't score 100x better than one appearing once — returns diminish after ~5 occurrences) and **document length normalization** (a longer document naturally has more term occurrences; BM25 normalizes scores by document length). The two tuning parameters, `k1` (TF saturation, default 1.2) and `b` (length normalization, default 0.75), can be adjusted per-index for different corpus characteristics.

Elasticsearch packages Lucene (a Java search library) into a distributed system with horizontal scaling via sharding, replication for fault tolerance, a REST API, and powerful aggregation capabilities for faceted search. A single Elasticsearch index is divided into N primary shards; each shard is an independent Lucene instance. Documents are routed to shards via `shard = hash(doc_id) % num_primary_shards`. Read requests (searches) are distributed across primary and replica shards. The number of primary shards is fixed at index creation time — changing it requires full reindexing, which is why getting this decision right upfront matters.

## How It Works

**Inverted index construction:**
```
  Document 0: "Wireless Bluetooth headphones"
  Document 1: "Professional headphones for studio"
  Document 2: "Portable wireless speaker"

  Step 1: Tokenize (lowercase, split, remove punctuation)
    Doc 0: [wireless, bluetooth, headphones]
    Doc 1: [professional, headphones, for, studio]
    Doc 2: [portable, wireless, speaker]

  Step 2: Build posting lists WITH positions
    wireless:      {0: [0], 2: [1]}        ← doc 0 pos 0, doc 2 pos 1
    bluetooth:     {0: [1]}
    headphones:    {0: [2], 1: [1]}
    professional:  {1: [0]}
    for:           {1: [2]}
    studio:        {1: [3]}
    portable:      {2: [0]}
    speaker:       {2: [2]}

  Term query "wireless headphones":
    Step 1: Look up "wireless" → {0, 2}
    Step 2: Look up "headphones" → {0, 1}
    Step 3: Union → {0, 1, 2}, score each by BM25
    Step 4: doc 0 scores highest (contains both terms)

  Phrase query "wireless headphones":
    Step 1-2: same as above → candidates = {0} (intersection)
    Step 3: for doc 0, check: position("headphones") == position("wireless") + 1
            → "wireless" at pos 0, "headphones" at pos 2 → NOT adjacent
            → no match for exact phrase "wireless headphones"
    Phrase query "bluetooth headphones":
            → "bluetooth" at pos 1, "headphones" at pos 2 → adjacent → MATCH
```

**BM25 formula:**
```
  score(D, Q) = Σ IDF(qi) × (TF(qi,D) × (k1+1)) / (TF(qi,D) + k1 × (1-b + b×|D|/avgdl))

  Where:
    qi       = query term i
    IDF(qi)  = log((N - df(qi) + 0.5) / (df(qi) + 0.5) + 1)
               [+1 outside ensures IDF > 0 even when term is in every doc]
    TF(qi,D) = count of qi in document D
    |D|      = document length (tokens)
    avgdl    = average document length in corpus
    k1=1.2   = TF saturation (as TF→∞, numerator approaches k1+1)
    b=0.75   = length normalization (0=disabled, 1=fully normalized)
```

**Lucene segment-based write model:**
```
  1. Client sends index request → ES in-memory write buffer
                                → translog (WAL, fsync every 5s by default)
     → ES ACKs the write here (write is durable in translog)

  2. Every refresh_interval (default 1s): buffer → new Lucene segment
     (segment = immutable set of files: term dict, posting lists, doc values)
     → document becomes SEARCHABLE

  3. Every ~30s or when segments accumulate:
     multiple small segments → merged into one large segment
     (merge is background; I/O intensive; can throttle write throughput)

  Key distinction:
    • Translog durability: controls data loss on crash (fsync=request → no loss)
    • Refresh interval:    controls search visibility delay (~1s by default)
    These are orthogonal — you can have durable writes that are slow to appear.
```

**Elasticsearch architecture:**
```
  Cluster: 3 nodes

  Index "products" (2 primary shards, 1 replica each):
    Node 1: Shard 0 Primary, Shard 1 Replica
    Node 2: Shard 1 Primary, Shard 0 Replica
    Node 3: (coordinator node, routes requests)

  Search scatter-gather:
    1. Client → any node (becomes coordinator for this request)
    2. Coordinator maps query to relevant shards
    3. Scatter: send query to one copy of each shard (round-robin primary/replica)
    4. Each shard executes BM25 locally, returns top-K local results with scores
    5. Gather: coordinator merges all shard results, re-ranks globally, returns top-K

  Deep pagination cost:
    • from=10000, size=10 → each shard returns 10010 results
    • Coordinator receives 10010 × num_shards results, sorts, discards all but 10
    • Memory and CPU cost is O(from × num_shards) on the coordinator
    • Default max_result_window=10000; use search_after for deep pagination
```

### Analyzer Pipeline

The analyzer converts raw text into indexed tokens. The same analyzer must be used at index time and query time — a mismatch means queries won't match indexed terms.

```
  Standard analyzer (default):
    Input:  "Running Runners across devices."
    Steps:  Unicode tokenizer → lowercase
    Output: [running, runners, across, devices]

  Custom stemming analyzer:
    Input:  "Running Runners across devices."
    Steps:  Standard tokenizer → lowercase → stop words → porter_stem
    Output: [run, run, devic]
            ("across" removed as stop word; all forms of "run" collapse to "run")

  Analyzers per use case:
    Standard:   high precision, lower recall (exact form matching)
    Stemming:   higher recall, lower precision (form-agnostic matching)
    Language:   language-specific stemmers + stop words (English, German, etc.)
    CJK:        n-gram or dictionary-based (no whitespace between words in Chinese)
    Code:       split on camelCase, underscores, dots (GitHub code search)
```

### Hybrid Search

Pure BM25 excels at exact keyword matching but fails on vocabulary mismatch ("earphones" vs. "headphones") and semantic intent ("noise isolation" vs. "active noise cancellation"). Dense vector search (kNN) captures semantic similarity but requires every document to have an embedding and can surface plausible-but-wrong matches that lack the exact terms. Hybrid search combines both signals.

**Architecture:**
```
  User query: "wireless earphones for commuting"
               │
               ├─► BM25 branch: tokenize → posting list lookup → ranked list R_bm25
               │
               └─► Dense vector branch:
                     embed query → kNN HNSW lookup → ranked list R_knn
                                                          │
                                       RRF merge ◄────────┘
                                          │
                              final ranked result list
```

**Reciprocal Rank Fusion (RRF):**
```
  For each document d across all result lists:
    rrf_score(d) = Σ_retriever  1 / (k + rank_in_retriever(d))

  k = 60  (smoothing constant, default in ES 8.8+)

  Example — document "Studio Monitor Headphones":
    BM25 rank:  #3  →  1/(60 + 3) = 0.0159
    kNN rank:   #1  →  1/(60 + 1) = 0.0164
    RRF score:          0.0323

  Why RRF over score normalisation:
    • BM25 scores and cosine similarity are dimensionally incompatible.
      Normalisation requires global min/max — impossible in a live sharded cluster
      without an expensive aggregation pass.
    • RRF uses only ordinal rank — insensitive to score magnitude or distribution.
    • Adding a third retriever (cross-encoder re-ranker, ColBERT, etc.) is additive:
      rrf_score += 1/(k + rank_in_reranker(d))
```

**HNSW index for dense vectors:**
```
  ES stores dense_vector fields in a per-shard HNSW graph.
  At query time, graph traversal finds approximate nearest neighbours
  in O(log N) vs O(N×dims) for brute-force.

  Key parameters:
    m (max edges per node):   16–64   higher → better recall, larger index
    ef_construction:          100–200 higher → better recall, slower indexing
    num_candidates (ef_search): 10–100 higher → better recall, slower query

  Recall@10 benchmark (typical):
    ef_construction=128, m=16 → ~96% recall vs brute-force
    ef_construction=64,  m=8  → ~90% recall
```

**When to use each retrieval strategy:**
| Scenario | Best approach |
|---|---|
| Product code / SKU / exact name | BM25 only |
| Conversational Q&A, intent-heavy | Dense kNN only |
| Mixed keyword + semantic (most e-commerce) | Hybrid RRF |
| Real-time with no embedding infra | BM25 + synonyms |
| Re-ranking top-20 for highest quality | BM25 + kNN + cross-encoder |

### Trade-offs

| Feature | Elasticsearch | Postgres FTS (tsvector) | Solr | Algolia |
|---|---|---|---|---|
| Relevance | BM25 (excellent) | ts_rank (basic) | BM25/TF-IDF | Proprietary (excellent) |
| Scale | Horizontal sharding | Vertical | Horizontal | Managed (unlimited) |
| Facets | Aggregations (fast) | GROUP BY (slower) | Facets | Built-in |
| Consistency | Eventual (NRT ~1s) | ACID (strong) | Eventual | Eventual |
| Ops complexity | High | Low (same DB) | High | None (SaaS) |
| Fuzzy/autocomplete | Built-in | Limited | Good | Excellent |
| Vector/semantic | kNN (dense_vector) | pgvector extension | Yes | Yes |
| Cost | Infrastructure | Included with DB | Infrastructure | Per-record + per-query |
| Best for | Large-scale, logs, analytics | Simple search on existing data | Traditional enterprise | Best UX, managed, fast |

### Failure Modes

**Split-brain in Elasticsearch cluster:** if the master node fails and two groups of nodes each elect a new master, you get two clusters with divergent state — documents indexed to one cluster don't exist in the other. Elasticsearch prevents this with `minimum_master_nodes = (N/2)+1` (or in newer versions, automatic quorum-based election). With 3 nodes, 2 must agree to elect a master, preventing split-brain.

**Mapping explosion:** if a document with a dynamic field (e.g., a JSON payload where field names are user-controlled) is indexed, Elasticsearch creates a new mapping entry for each field. An attacker or bug that sends thousands of unique field names can create millions of mapping entries, exhausting heap space. Mitigation: use `dynamic: false` or `dynamic: strict` to prevent automatic field discovery, and define all fields explicitly.

**Search-time fan-out overhead (over-sharding):** a query hitting an index with 1000 shards sends 1000 parallel sub-queries. Coordinator node must wait for all responses, hold 1000 partial result sets in memory, and merge. More shards does not mean faster search if data volume per shard is already tiny — the coordination overhead dominates. Optimal shard size is 10–50GB per primary shard. Use the `_shrink` API to reduce shard count without full reindexing.

**Translog data loss:** by default, Elasticsearch fsyncs the translog every 5 seconds (`index.translog.sync_interval=5s`). A hard crash can lose up to 5 seconds of acknowledged writes. If your application cannot tolerate any data loss, set `index.translog.durability=request` to fsync on every operation — but expect 5–10× higher write latency.

**Hot shard problem:** if documents are routed with a skewed key (e.g., routing by `user_id` where one user has 80% of the documents), one shard gets most of the indexing and query load. Symptom: one node is CPU-saturated while others are idle. Fix: use a custom routing hash or the `_routing` field with multiple shards per logical partition.

**Deep pagination memory blow-up:** `from+size` pagination fetches `from+size` documents per shard and merges them at the coordinator. At `from=50000, size=10` with 5 shards, the coordinator processes 250050 documents to return 10 results. ES enforces `max_result_window=10000` by default. Use `search_after` with a sort tiebreaker, or the Point-in-Time (PIT) API for stable cursor-based pagination.

**Synonym explosion:** a synonym token filter configured at index time inserts extra tokens into posting lists for every synonym of every indexed term. A synonym ring of 5 words (e.g., "tv, television, telly, set, box") multiplies posting list entries 5× for every matching document. In a 50M-document medical corpus with 100-way ICD code synonym sets, index size can grow 10–20× and segment merges become extremely I/O intensive, causing indexing to fall behind ingestion. Mitigation: prefer query-time synonym expansion (synonym filter applied at search time, not index time), keep synonym rings small and focused (≤ 5 terms per concept), and monitor `_cat/indices?v` store.size after each synonym list update.

**Index corruption and translog loss:** Lucene segments are immutable and self-describing, but the translog — the WAL bridging in-memory writes to durable segments — can become corrupt if the underlying storage fails mid-write (e.g., EC2 instance store with a sudden power cut, or a Kubernetes pod eviction during an fsync). A corrupt translog prevents the shard from opening on restart. Recovery path: delete the corrupt translog shard copy (`elasticsearch-translog truncate --index products --shard-id 0`), allow ES to promote a replica to primary or restore from snapshot. Prevention: use `index.translog.durability=request` for data-critical indices; replicate across AZs so a storage failure on one node does not take out all copies simultaneously; maintain regular Snapshot Lifecycle Management (SLM) policies to S3 or GCS as the final safety net.

## Interview Talking Points

- "An inverted index maps each term to a posting list of document IDs. Search is: look up each query term in the index, intersect/union the posting lists, score with BM25. This is O(1) index lookup + O(posting_list_size) scoring — far faster than scanning every document."
- "BM25 improves TF-IDF with TF saturation (term appearing 100 times isn't 100x more relevant than once) and document length normalization (long documents aren't unfairly penalized). The formula's k1 and b parameters are tunable per-index — e-commerce titles need different tuning than long news articles."
- "Position data in posting lists enables phrase queries. Without positions, you can only answer 'does doc contain term?' but not 'do term A and term B appear consecutively?' Storing positions roughly doubles index size but is essential for phrase search and proximity scoring."
- "Elasticsearch uses 'near real-time' indexing: documents are buffered in memory, flushed to a searchable Lucene segment every ~1 second (refresh interval). Between the write ACK and the refresh, the document is in the translog but not searchable. Plan for this: don't query ES immediately after a write and expect to see the result."
- "The number of primary shards is fixed at index creation time. Over-sharding (many small shards) causes expensive fan-out and coordinator memory pressure. Under-sharding limits write parallelism for large indices. Target 10–50GB per primary shard; use the _shrink API to consolidate shards as data stabilizes."
- "For simple search on existing relational data, Postgres full-text search (tsvector/GIN index) is often sufficient — same transaction scope, no extra infrastructure, good enough for tens of millions of rows. Graduate to Elasticsearch when you need relevance tuning, complex aggregations, or horizontal scale."
- "ES analyzers control how text is tokenized and normalized. The same analyzer must be used at index time and query time. Stemming ('running' → 'run') increases recall but can reduce precision (e.g., 'organ' matching 'organized'). For multilingual corpora like Wikipedia, each language needs its own analyzer configuration."
- "Deep pagination with `from+size` is O(from × num_shards) memory on the coordinator — at from=50000 with 5 shards, you're sorting 250,000 docs to return 10. Use `search_after` with a sort tiebreaker for cursor-based pagination that is O(page_size) regardless of depth."
- "For modern AI-augmented search, dense vector similarity (kNN) is increasingly important. Elasticsearch's `dense_vector` field with HNSW indexing enables approximate nearest-neighbor search over embedding vectors, used for semantic search, recommendation, and image similarity — often combined with BM25 via 'hybrid search' (RRF or linear combination of scores)."
- "Keeping Elasticsearch in sync with the primary database is a classic dual-write vs CDC debate. Dual-write is operationally simple but creates a partial-failure window: if the application crashes after the DB commit but before the ES index call, the index is silently stale. CDC (Change Data Capture) via Debezium tailing the Postgres WAL into Kafka and an ES connector eliminates this gap — ES sees every committed change exactly once, in order, with at-least-once delivery. The trade-off is added infra complexity and ~seconds of replication lag. For FAANG-scale systems with strict auditability requirements, CDC is the safer choice."
- "Hybrid BM25 + kNN search with RRF is now the industry standard for production search. BM25 handles keyword precision; dense vector kNN handles semantic intent; RRF fuses the rank lists without requiring score normalisation — which is impossible to do correctly in a sharded cluster without a prohibitively expensive global aggregation. Shopify, Airbnb, and Uber have all published case studies showing 10–30% relevance improvements from hybrid over BM25 alone. The main cost is the embedding pipeline at both index time and query time."

## Hands-on Lab

**Time:** ~50-60 minutes
**Services:** Elasticsearch 8.8 (port 9200, security disabled)

### Setup

```bash
cd system-design-interview/02-advanced/09-search-systems/
docker compose up -d
# Wait ~40 seconds for Elasticsearch to be healthy
```

### Experiment

```bash
pip install elasticsearch
python experiment.py
```

The script demonstrates: (1) a manual Python BM25 inverted index with position-aware posting lists, (2) phrase queries using position data vs term queries, (3) bulk indexing 5000 documents with a custom stemming analyzer, (4) full-text BM25 search with field boosting, (5) faceted search with filter context and aggregations, (6) analyzer pipeline comparison (standard vs stemming), (7) the NRT 1-second refresh delay demonstrated live, (8) deep pagination `from+size` failure vs `search_after` cursor pagination, (9) fuzzy/typo-tolerant search and prefix autocomplete, (10) index vs search latency benchmark, (11) score explanation with the `_explain` API (reading the BM25 score tree to debug relevance), (12) synonym search with a custom synonym token filter (vocabulary expansion, synonym explosion risk), and (13) hybrid BM25 + dense vector kNN search with an HNSW index and Reciprocal Rank Fusion (RRF) score combination.

### Break It

```bash
# Demonstrate mapping explosion (safe version)
python -c "
from elasticsearch import Elasticsearch
es = Elasticsearch('http://localhost:9200')

# Create index with dynamic mapping enabled (default — dangerous)
if es.indices.exists(index='dynamic-test'):
    es.indices.delete(index='dynamic-test')

# Index a document with 100 unique field names
doc = {f'field_{i}': f'value_{i}' for i in range(100)}
es.index(index='dynamic-test', id='1', document=doc)
es.indices.refresh(index='dynamic-test')

# Check how many mappings were created
mapping = es.indices.get_mapping(index='dynamic-test')
fields = mapping['dynamic-test']['mappings']['properties']
print(f'Indexed 1 document with 100 unique fields')
print(f'ES created {len(fields)} mapping entries automatically')
print(f'At scale: millions of unique fields → heap exhaustion → cluster crash')
print(f'Fix: set dynamic: false or dynamic: strict in the mapping')
"
```

### Observe

The manual inverted index phase shows the BM25 formula in action: term saturation and length normalization visible in the scores. The phrase query phase shows that "wireless headphones" as an exact phrase matches fewer documents than a term query — because position data constrains the match. The NRT phase shows a document being invisible immediately after indexing and appearing after ~1.5 seconds without a manual refresh. The pagination phase demonstrates the `max_result_window` error and how `search_after` avoids it. The `_explain` phase shows the full BM25 score tree — IDF leaf, TF leaf, field boost — for a specific (query, document) pair, demonstrating how to audit relevance. The synonym phase shows "earphones" matching "headphones" documents via the synonym token filter, and the `_analyze` API output confirms the token expansion. The hybrid phase shows BM25-only, kNN-only, and RRF-combined results side by side — note that RRF typically promotes documents that appear in both result lists, which are usually the highest-quality matches.

### Teardown

```bash
docker compose down -v
```

## Real-World Examples

- **Elasticsearch at Uber:** Uber uses Elasticsearch to power geospatial search for driver-rider matching and trip history search. They index billions of trip records, using custom geographic queries to find nearby drivers. Their challenges include mapping explosion from dynamic fields in trip metadata and managing index shard count as data grew to petabytes. Source: Uber Engineering Blog, "Uber's Search Platform," 2019.
- **Wikipedia search (CirrusSearch):** Wikipedia uses Elasticsearch (via the CirrusSearch extension) to power full-text search across 60+ million articles in 300+ languages. They run custom analyzers for each language (different stemmers, stop word lists, tokenizers for CJK languages). Their relevance tuning considers article quality scores, links, and view counts alongside BM25. Source: Wikimedia Foundation Technical blog, "Improving Wikipedia search," 2016.
- **GitHub Code Search:** GitHub rebuilt their code search engine in 2022 to index 200 million repositories. They use a custom Elasticsearch deployment with specialized analyzers for code (splitting on camelCase, underscores, parentheses). Their "Blackbird" index pre-computes repository rankings for better relevance. Source: GitHub Engineering Blog, "The technology behind GitHub's new code search," 2023.
- **Shopify Product Search:** Shopify migrated product search from Elasticsearch to a hybrid BM25 + vector search architecture to improve semantic matching ("summer dress" matching "floral sundress"). They combine sparse BM25 scores with dense embedding similarity (HNSW index) using reciprocal rank fusion (RRF). Source: Shopify Engineering Blog, "Improving Product Search with AI," 2023.

## Common Mistakes

- **Using Elasticsearch as the primary datastore.** ES is not ACID-compliant. A document indexed at 9:00:00.000 may not be searchable until 9:00:01.000 (NRT refresh). By default, up to 5 seconds of acknowledged writes can be lost on hard crash (translog fsync interval). Never use ES as the source of truth — use a database as the primary store and ES as the search index, kept in sync via CDC (Change Data Capture) or dual-write with idempotent operations.
- **Not setting `number_of_replicas=0` during bulk index.** When bulk-loading data, having replicas doubles the indexing work. Set `number_of_replicas=0` during load, then increase to the desired value after load completes. This can make bulk indexing 2–5x faster.
- **Querying with large `_source` fetches.** Fetching `_source` for 10,000 results returns all fields by default. If `description` is 5KB, that's 50MB of data per query. Use `_source_includes` to return only needed fields, or store frequently accessed fields as `stored_fields` separately from `_source`.
- **Using leading wildcards on high-cardinality fields.** In ES, `wildcard: {name: '*wireless*'}` (leading wildcard) is expensive — it must iterate over all terms in the index dictionary. Use `match` queries (which use the inverted index) for full-text search, and `prefix` queries or the `completion` suggester for autocomplete.
- **Wrong analyzer at query time.** If you index with a `stemming_analyzer` but query the field with `standard`, the query term "running" won't match the indexed stem "run". ES automatically uses the field's index-time analyzer at query time for `match` queries — but if you use `term` queries (which bypass analysis), you must pass the already-analyzed form. Use `match` (analyzed) for text fields, `term` (exact) for keyword fields.
- **Ignoring shard count at index creation.** The number of primary shards cannot be changed after index creation without full reindexing. Start with a shard count based on expected data volume (target 10–50GB per shard). If you under-shard a fast-growing index, you will be forced into an expensive reindex operation under load. Use index aliases + reindex to migrate without downtime.
