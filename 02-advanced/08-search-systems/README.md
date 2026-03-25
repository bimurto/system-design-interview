# Search Systems

**Prerequisites:** `../07-distributed-caching/`
**Next:** `../09-rate-limiting-algorithms/`

---

## Concept

Full-text search is the ability to find documents that contain a given set of words, ranked by relevance. A relational database can answer "which rows contain the word 'wireless'?" with a `LIKE '%wireless%'` query, but this requires a full sequential scan — unusable for millions of rows. A search engine inverts this data structure: instead of scanning every document for the query term, it pre-builds an **inverted index** that maps each term to the list of documents containing it. Looking up "wireless" becomes a constant-time index lookup, returning a posting list of matching document IDs in microseconds.

The inverted index is the core data structure of every search engine, from Apache Lucene to Google. At its simplest, it is a hash map: `{term → [doc_id1, doc_id2, ...]}`. A more complete implementation stores position information: `{term → {doc_id → [position1, position2, ...]}}`. Positions enable phrase queries ("wireless headphones" must appear as an exact phrase, not just both words anywhere in the document) and proximity scoring (documents where the query terms appear close together are ranked higher).

Ranking is the other half of search. Early search engines used pure Boolean matching (a document either contains all query terms or doesn't), giving no way to distinguish a document where "wireless headphones" appears in the title from one where it appears once in a 10,000-word review. **TF-IDF** (Term Frequency-Inverse Document Frequency) introduced statistical relevance: a term's relevance to a document is proportional to how often it appears in that document (TF) and inversely proportional to how many documents contain it (IDF). Common words ("the", "and") have high document frequency and low IDF, so they contribute little to scores. Rare, specific terms have high IDF and are very informative.

**BM25** (Best Match 25) is the modern successor to TF-IDF, introduced in the 1990s and still the default in Elasticsearch and Solr. BM25 addresses two weaknesses of TF-IDF: **TF saturation** (a term appearing 100 times in a document shouldn't score 100x better than one appearing once — returns diminish after ~5 occurrences) and **document length normalization** (a longer document naturally has more term occurrences; BM25 normalizes scores by document length). The two tuning parameters, `k1` (TF saturation, default 1.2) and `b` (length normalization, default 0.75), can be adjusted per-index for different corpus characteristics.

Elasticsearch packages Lucene (a Java search library) into a distributed system with horizontal scaling via sharding, replication for fault tolerance, a REST API, and powerful aggregation capabilities for faceted search. A single Elasticsearch index is divided into N primary shards; each shard is an independent Lucene instance. Documents are routed to shards via `shard = hash(doc_id) % num_primary_shards`. Read requests (searches) are distributed across primary and replica shards. The number of primary shards is fixed at index creation time — changing it requires reindexing.

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

  Step 2: Build posting lists
    wireless:      {0: [0], 2: [1]}        ← doc 0 pos 0, doc 2 pos 1
    bluetooth:     {0: [1]}
    headphones:    {0: [2], 1: [1]}
    professional:  {1: [0]}
    for:           {1: [2]}
    studio:        {1: [3]}
    portable:      {2: [0]}
    speaker:       {2: [2]}

  Query "wireless headphones":
    Step 1: Look up "wireless" → {0, 2}
    Step 2: Look up "headphones" → {0, 1}
    Step 3: Union → {0, 1, 2}, score by TF-IDF/BM25
    Step 4: doc 0 scores highest (contains both terms)
```

**BM25 formula:**
```
  score(D, Q) = Σ IDF(qi) × (TF(qi,D) × (k1+1)) / (TF(qi,D) + k1 × (1-b + b×|D|/avgdl))

  Where:
    qi      = query term i
    IDF(qi) = log((N - df(qi) + 0.5) / (df(qi) + 0.5) + 1)
    TF(qi,D) = count of qi in document D
    |D|     = document length (tokens)
    avgdl   = average document length in corpus
    k1=1.2  = TF saturation parameter
    b=0.75  = length normalization parameter
```

**Elasticsearch near-real-time indexing:**
```
  1. Client sends index request → ES in-memory write buffer
  2. Every refresh_interval (default 1s): buffer → new Lucene segment
     (segment = immutable set of index files on disk)
     → document becomes SEARCHABLE
  3. Every few minutes: multiple small segments → merged into one large segment
     (merge = background process, I/O intensive)
  4. ES acknowledges the write after the write buffer is flushed to
     the transaction log (translog) — not after it becomes searchable

  This is "near" real-time: ~1s between index and search visibility.
  Force refresh (/_refresh) makes it immediate but is expensive.
```

**Elasticsearch architecture:**
```
  Cluster: 3 nodes

  Index "products" (2 primary shards, 1 replica each):
    Node 1: Shard 0 Primary, Shard 1 Replica
    Node 2: Shard 1 Primary, Shard 0 Replica
    Node 3: (coordinator node, routes requests)

  Search request:
    1. Client → any node (coordinator)
    2. Coordinator computes which shards are needed
    3. Scatter: send query to one copy of each shard (round-robin between primary/replica)
    4. Each shard returns top-K local results with scores
    5. Gather: coordinator merges all results, re-ranks globally, returns top-K
```

### Trade-offs

| Feature | Elasticsearch | Postgres FTS (tsvector) | Solr | Algolia |
|---|---|---|---|---|
| Relevance | BM25 (excellent) | ts_rank (basic) | BM25/TF-IDF | Proprietary (excellent) |
| Scale | Horizontal sharding | Vertical | Horizontal | Managed (unlimited) |
| Facets | Aggregations (fast) | GROUP BY (slower) | Facets | Built-in |
| Consistency | Eventual (NRT ~1s) | ACID (strong) | Eventual | Eventual |
| Ops complexity | High | Low (same DB) | High | None (SaaS) |
| Fuzzy/autocomplete | Built-in | Limited | Good | Excellent |
| Cost | Infrastructure | Included with DB | Infrastructure | Per-record + per-query |
| Best for | Large-scale search, logs | Simple search on existing data | Traditional enterprise search | Best UX, managed, fast |

### Failure Modes

**Split-brain in Elasticsearch cluster:** if the master node fails and two groups of nodes each elect a new master, you get two clusters with divergent state — documents indexed to one cluster don't exist in the other. Elasticsearch prevents this with `minimum_master_nodes = (N/2)+1` (or in newer versions, automatic quorum-based election). With 3 nodes, 2 must agree to elect a master, preventing split-brain.

**Mapping explosion:** if a document with a dynamic field (e.g., a JSON payload where field names are user-controlled) is indexed, Elasticsearch creates a new mapping entry for each field. An attacker or bug that sends thousands of unique field names can create millions of mapping entries, exhausting heap space. Mitigation: use `dynamic: false` or `dynamic: strict` to prevent automatic field discovery, and define all fields explicitly.

**Search-time fan-out overhead:** a query hitting an index with 1000 shards sends 1000 parallel sub-queries. Coordinator node must wait for all responses, hold 1000 partial result sets in memory, and merge. This is the "over-sharding" problem — more shards doesn't mean faster search if the data volume per shard is already tiny. Optimal shard size is 10-50GB per primary shard.

## Interview Talking Points

- "An inverted index maps each term to a posting list of document IDs. Search is: look up each query term in the index, intersect/union the posting lists, score with BM25. This is O(1) index lookup + O(posting_list_size) scoring — far faster than scanning every document."
- "BM25 improves TF-IDF with TF saturation (term appearing 100 times isn't 100x more relevant than once) and document length normalization (long documents aren't penalized for naturally having more term occurrences)."
- "Elasticsearch uses 'near real-time' indexing: documents are buffered in memory, flushed to a searchable Lucene segment every ~1 second (refresh interval). Between the write and the refresh, the document isn't searchable — plan for this in your application."
- "The number of primary shards is fixed at index creation time. Over-sharding (many small shards) causes expensive fan-out and coordinator memory pressure. Under-sharding (too few shards) limits parallelism for large indices. Aim for 10-50GB per primary shard."
- "For simple search on existing relational data, Postgres full-text search (tsvector/GIN index) is often sufficient — same transaction scope, no extra infrastructure, good enough for millions of rows. Only graduate to Elasticsearch when you need relevance tuning, complex facets, or hundreds of millions of records."
- "ES analyzers control how text is tokenized and normalized. The same analyzer must be used at index time and query time for consistent results. Stemming ('running' → 'run') increases recall but can reduce precision. Custom analyzers let you tune this per-field."

## Hands-on Lab

**Time:** ~25-35 minutes
**Services:** Elasticsearch 8.8 (port 9200, security disabled)

### Setup

```bash
cd system-design-interview/02-advanced/08-search-systems/
docker compose up -d
# Wait ~30 seconds for Elasticsearch to be healthy
```

### Experiment

```bash
pip install elasticsearch
python experiment.py
```

The script: (1) builds a manual Python inverted index for 10 documents showing TF-IDF scoring, (2) indexes 5000 generated product documents into Elasticsearch with a custom mapping and stemming analyzer, (3) runs full-text BM25 searches showing relevance scores, (4) runs a faceted query filtering by category/price/rating with aggregations showing category breakdown, (5) compares standard vs stemming analyzer tokenization, and (6) benchmarks single-document index latency vs search latency.

### Break It

```bash
# Demonstrate mapping explosion (safe version)
python -c "
from elasticsearch import Elasticsearch
es = Elasticsearch('http://localhost:9200')

# Create index with dynamic mapping enabled (default)
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

The manual inverted index phase shows how "wireless headphones" query scores doc 0 highest (contains both terms, both with high TF in a short document). The BM25 search in Elasticsearch shows very fast response times (< 10ms) even for 5000 documents. The analyzer comparison shows that the stemming analyzer reduces "running runners run beautifully" to just "run run run beauti" — significantly increasing recall but losing the distinction between forms.

### Teardown

```bash
docker compose down -v
```

## Real-World Examples

- **Elasticsearch at Uber:** Uber uses Elasticsearch to power geospatial search for driver-rider matching and trip history search. They index billions of trip records, using custom geographic queries to find nearby drivers. Their challenges include mapping explosion from dynamic fields in trip metadata and managing index shard count as data grew to petabytes. Source: Uber Engineering Blog, "Uber's Search Platform," 2019.
- **Wikipedia search (CirrusSearch):** Wikipedia uses Elasticsearch (via the CirrusSearch extension) to power full-text search across 60+ million articles in 300+ languages. They run custom analyzers for each language (different stemmers, stop word lists, tokenizers for CJK languages). Their relevance tuning considers article quality scores, links, and view counts alongside BM25. Source: Wikimedia Foundation Technical blog, "Improving Wikipedia search," 2016.
- **GitHub Code Search:** GitHub rebuilt their code search engine in 2022 to index 200 million repositories. They use a custom Elasticsearch deployment with specialized analyzers for code (splitting on camelCase, underscores, parentheses). Their "Blackbird" index pre-computes repository rankings for better relevance. Source: GitHub Engineering Blog, "The technology behind GitHub's new code search," 2023.

## Common Mistakes

- **Using Elasticsearch for transactional data.** ES is not ACID-compliant. A document indexed at 9:00:00.000 may not be searchable until 9:00:01.000 (NRT refresh). Documents can be lost if the write is acknowledged before the translog is flushed to disk. Never use ES as the primary store for data — use a database as source of truth and ES as a search index.
- **Not setting `number_of_replicas=0` during bulk index.** When bulk-loading data, having replicas doubles the indexing work. Set `number_of_replicas=0` during load, then increase to the desired value after load completes. This can make bulk indexing 2-5x faster.
- **Querying with `_source` includes of large fields.** Fetching `_source` for 10,000 results returns all fields by default. If `description` is 5KB, that's 50MB of data per query. Use `_source_includes` to return only needed fields, or use `stored_fields` for frequently queried fields.
- **Using wildcard queries on high-cardinality fields.** `LIKE '%wireless%'` in SQL is slow because it can't use an index. In ES, `wildcard: {name: '*wireless*'}` (leading wildcard) is similarly expensive — it must iterate over all terms in the index. Use `match` queries (which use the inverted index) for full-text, and `prefix` queries (which can use the index) for autocomplete.
