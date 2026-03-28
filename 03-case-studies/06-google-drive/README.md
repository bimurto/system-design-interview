# Case Study: Google Drive

**Prerequisites:** `../../01-foundations/12-blob-object-storage/`, `../../01-foundations/04-replication/`, `../../02-advanced/14-idempotency-exactly-once/`

---

## Interview Framework

| Phase | Duration | What to Cover |
|---|---|---|
| Clarify Requirements | 3–5 min | File size limits, sync latency SLA, version history retention, offline access, collaboration model |
| Capacity Estimation | 3–5 min | Storage at scale, dedup savings, delta sync bandwidth reduction |
| High-Level Design | 10 min | Upload path (chunk → hash → check → upload → commit), sync notification (WebSocket push), metadata store, object storage |
| Deep Dive | 15–20 min | Chunked upload + resumability, CDC for delta sync, cross-user deduplication, conflict resolution (OT vs conflict copy), metadata sharding |

---

## The Problem at Scale

Google Drive stores 2 trillion files for 1 billion users. The core engineering challenges are not just storage scale but sync efficiency — when a user edits a 1GB file and changes 3 words, the system should upload only the changed bytes, not re-upload the entire file. And when two users edit the same document simultaneously, the system must resolve the conflict without silently losing either person's work.

| Metric | Value |
|---|---|
| Total users | 1 billion |
| Files stored | 2 trillion |
| Free storage per user | 15 GB |
| Total storage | ~15 exabytes (15 × 10^18 bytes) |
| Daily uploads | ~2 billion files |
| Sync events per day | ~20 billion |
| Peak sync RPS | ~500,000 |

At this scale, per-byte deduplication (storing identical files once across all users) reduces actual storage by an estimated 20–30%. Efficient delta sync (uploading only changed chunks) reduces bandwidth by 60–90% for typical document edits.

---

## Requirements

### Functional
- Upload and download files of any size (up to 5 TB per file)
- Sync changes across devices in near-real-time (< 30 seconds)
- Version history: restore any previous version
- Shared folders and collaborative editing
- Offline access: sync when connection restored
- Search files by name, content, type

### Non-Functional
- Delta sync: upload only changed parts of modified files
- Content deduplication: identical files stored once across all users
- Resumable uploads: large file uploads survive network interruptions
- 99.999% file durability (never lose a file)
- Sync latency < 30 seconds for small changes
- Conflict detection and resolution for concurrent edits

### Capacity Estimation

| Metric | Calculation | Result |
|---|---|---|
| Storage (raw) | 1B users × 15 GB avg used | 15 exabytes |
| Storage after dedup | 15 EB × 0.75 dedup ratio | ~11 EB |
| Daily sync bandwidth | 2B files × 10 KB avg change | 20 TB/day |
| Without delta sync | 2B files × 1 MB avg size | 2 PB/day |
| Metadata storage | 2T files × 1 KB metadata | 2 PB |
| Version history | 10 versions × ~10% unique chunks per edit | ~2× base file size (not 10×) |

---

## High-Level Architecture

```
  ┌──────────────────────────────────────────────────────────────┐
  │                       Upload Path                             │
  │                                                               │
  │  Client sync agent:                                           │
  │    1. Split file into 4MB chunks                              │
  │    2. SHA-256 each chunk                                      │
  │    3. POST /upload/check: which hashes are new?               │
  │    4. Upload only new chunks (PUT /chunk/{hash})              │
  │    5. POST /file/commit: list of all chunk hashes             │
  │                                                               │
  │  Server:                                                      │
  │    Store new chunks in object storage (GCS/S3/MinIO)          │
  │    Update metadata in Spanner/Postgres                        │
  │    Notify other devices via WebSocket push                    │
  └──────────────────────────────────────────────────────────────┘

  ┌──────────────────────────────────────────────────────────────┐
  │                      Sync Notification                        │
  │                                                               │
  │  Server → WebSocket push to all connected devices of user     │
  │  Client: download manifest, compute local diff, fetch chunks  │
  └──────────────────────────────────────────────────────────────┘

  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
  │  Object Storage  │  │    Postgres/     │  │     Redis        │
  │  (GCS/S3/MinIO)  │  │    Spanner       │  │  upload sessions │
  │  chunks/{hash}   │  │  files, versions │  │  notification    │
  │  content-addr.   │  │  chunks (dedup)  │  │  queues          │
  └──────────────────┘  └──────────────────┘  └──────────────────┘
```

**Object storage** uses content-addressed storage: the key for each object is its SHA-256 hash (`chunks/{hash[:2]}/{hash}`). This means identical data always has the same key — a natural deduplication scheme. Two users uploading the same file result in one object store write, two metadata records.

**Postgres** stores structured metadata: file records, version history (as arrays of chunk hashes), and a chunks table tracking reference counts. The chunks table is the deduplication index: before uploading a chunk, the client checks if its hash exists in the chunks table. If it does, upload is skipped.

**Redis** caches upload sessions (which chunks of a multipart upload have been received) for resumable upload support. It also serves as the pub/sub layer for sync notifications: when file X is updated, all connected devices watching file X receive a push notification.

---

## Deep Dives

### 1. Chunked Upload and Resumable Transfers

Large file uploads fail due to network interruptions. A 1GB file upload that fails at 99% must not restart from the beginning. Chunked upload with server-side state enables resumption:

**Protocol:**
1. Client: `POST /upload/initiate` → server returns `upload_id`, stores `SADD upload_session:{upload_id} {total_chunks}` in Redis
2. Client: for each chunk, `PUT /upload/{upload_id}/chunk/{n}` with chunk bytes; server stores the chunk and records `SADD upload_received:{upload_id} {n}` in Redis
3. Client (or client on reconnect): `GET /upload/{upload_id}/status` → server returns list of received chunk indices; client uploads only missing chunks
4. Client: `POST /upload/{upload_id}/complete` → server assembles file record, notifies other devices

**Chunk size selection:** 4MB is the default chunk size for Google Drive and Dropbox. Smaller chunks increase overhead (more HTTP requests, more database records). Larger chunks waste bandwidth when a chunk must be retried after a partial failure. 4MB balances these concerns and aligns with typical SSD read unit sizes.

### 2. Delta Sync: Rolling Hash for Byte-Level Efficiency

Fixed-size chunking has a fundamental flaw: inserting one byte at the beginning of a file shifts all subsequent chunk boundaries, making every chunk appear "changed." A 10GB file with a 1-byte insertion near the start would require re-uploading the entire file with fixed-size chunks.

**Content-defined chunking (CDC)** solves this using a rolling hash (Rabin fingerprint):
1. Slide a rolling hash window (64 bytes) over the file
2. When the hash matches a boundary pattern (e.g., last N bits are all zero), cut a chunk there
3. Chunk boundaries are determined by content, not position

Result: inserting bytes near the start only affects nearby chunks. All downstream chunks have the same hash as before, because their content and boundaries are unchanged.

**Rsync algorithm:** Dropbox's original sync client used the rsync rolling hash algorithm. The server sends a "signature" (weak + strong checksums of existing chunks). The client computes the rolling hash of the modified file and finds matching regions. Only non-matching regions are transferred. This enables byte-level delta sync regardless of chunk boundary alignment.

### 3. Content Deduplication

Every chunk uploaded to Google Drive or Dropbox is identified by its content hash (SHA-256). Before uploading a chunk, the client sends only the hash to the server to check if it already exists:

```
Client: POST /chunk/check  body: {"hashes": ["abc123...", "def456..."]}
Server: returns {"abc123...": "exists", "def456...": "missing"}
Client: uploads only "def456..." chunk bytes
```

At Google Drive's scale, an estimated 20–30% of all uploaded data already exists in the store. Common sources of duplicates: the same photo shared across many users' shared albums; standard software installers uploaded by many users; templated documents.

**Cross-user deduplication:** Dropbox performs deduplication across all users (one copy of a common file serves everyone). This raised privacy concerns — the same hash means the same file, which reveals that two users have identical data. Google Drive stores per-user copies of chunks with shared underlying storage at the infrastructure level, which provides deduplication benefits without metadata-level cross-user linkage.

### 4. Sync Notification Architecture

After a file is uploaded, all other devices belonging to the same user (and all collaborators with access) must be notified in near-real-time. This is a fan-out problem.

**WebSocket per connected device:**
Each sync client maintains a persistent WebSocket connection to a notification server. When a file is committed, the upload service publishes an event to a message bus (Kafka or Pub/Sub). Notification servers subscribed to the user's topic push the event to all of that user's open WebSocket connections.

```
Upload Service → Kafka topic: user.{user_id}.changes
                         ↓
           Notification Server (fan-out per user)
                         ↓
    WebSocket push → Device A, Device B, Device C
```

**Why not polling?** A 30-second sync SLA with 500M active users polling every 30 seconds = ~17M requests/second to the sync check endpoint. WebSocket push inverts this: the server pushes only when there is a real change, cutting unnecessary traffic by orders of magnitude.

**Notification server scalability:**
- Each notification server holds WebSocket connections for a subset of users. A user's devices always connect to the same notification server (sticky routing by `user_id` hash).
- If a notification server crashes, clients reconnect and re-establish WebSocket connections within seconds. No in-flight events are lost because Kafka retains them; the notification server replays from the last committed offset on restart.

**Long-polling as fallback:** Clients that cannot maintain a WebSocket (corporate firewalls, certain mobile networks) fall back to long-polling: `GET /sync/poll?since={last_event_id}` blocks up to 30 seconds and returns immediately if a new event arrives. This degrades gracefully to the 30-second SLA.

### 5. Conflict Resolution

When two users edit the same file version concurrently, the server detects a conflict on save:

**Detection:** each file version has a monotonically increasing version number. When user B attempts to save, they include the base version number they edited from. If `base_version < current_version`, a concurrent edit has occurred.

**Google Drive's resolution:** create a "conflict copy" — a new file named `filename (User's conflicted copy 2024-01-15)`. Both versions are preserved. The user must manually review and merge. This approach never loses data but requires user action.

**Google Docs' resolution:** real-time collaborative editing uses Operational Transformation (OT) — the same algorithm as Google Wave and most online code editors. Each edit is a transform (insert char at position N, delete char at position M). When two edits conflict, the server transforms both operations so they can be applied in any order and produce the same result. Users see each other's cursors in real time; the document converges to the same state for all viewers. This is the gold standard for collaborative text editing but is complex to implement correctly.

---

## How It Actually Works

**Dropbox "Magic Pocket" (2016):** Dropbox published a detailed blog post describing their migration from AWS S3 to their own distributed object storage system ("Magic Pocket"). Key details: chunks are stored content-addressed (hash as key), with a global deduplication index. Dropbox found that 30% of all uploaded data already existed in their store, and that 70% of sync events transferred zero bytes (only metadata changed). The chunk size is 4MB by default; Dropbox's rsync-based client uses variable-size chunks.

**Google Drive and Spanner:** Google uses Cloud Spanner for Drive metadata, providing global strong consistency with automatic sharding. This ensures that file version numbers are globally consistent — no two conflicting saves can both claim to be "version N."

**iCloud Drive** uses a similar chunked architecture, but with per-user deduplication only (not cross-user) due to privacy constraints in Apple's design philosophy.

Source: Dropbox Engineering Blog, "Inside the Magic Pocket" (2016); Dropbox Engineering Blog, "Streaming File Synchronization" (2014); Google Cloud Next talk on Google Drive architecture (2019).

---

## Hands-on Lab

**Time:** ~20–25 minutes
**Services:** `minio` (object storage), `db` (Postgres 15), `cache` (Redis 7)

### Setup

```bash
cd system-design-interview/03-case-studies/06-google-drive/
docker compose up -d
# Wait ~15s for services
docker compose ps
```

### Experiment

```bash
python experiment.py
```

Five phases run automatically:

1. **Chunked upload:** generate a 10MB file, split into 4MB chunks, upload to MinIO, record metadata in Postgres
2. **Delta sync:** modify 1 byte in chunk 1, re-upload — observe only the changed chunk is transferred
3. **Deduplication:** Bob uploads identical file — observe 0 bytes transferred, chunks ref_count incremented
4. **Conflict:** Alice and Bob both edit version 1 — Alice saves first; Bob's save creates a conflict copy
5. **Version history:** show full version history in Postgres, demonstrate per-version chunk diff

### Break It

**Simulate upload failure mid-way:**

```bash
python -c "
import io, random, hashlib
from minio import Minio

mc = Minio('localhost:9002', access_key='minioadmin', secret_key='minioadmin', secure=False)

# Upload only the first chunk of a 3-chunk file (simulate interrupted upload)
random.seed(99)
file_data = bytes(random.getrandbits(8) for _ in range(10*1024*1024))
chunk = file_data[:4*1024*1024]
chunk_hash = hashlib.sha256(chunk).hexdigest()
mc.put_object('gdrive-chunks', f'chunks/{chunk_hash[:2]}/{chunk_hash}',
              io.BytesIO(chunk), len(chunk))
print(f'Uploaded chunk 0 hash: {chunk_hash[:16]}...')
print('Chunks 1 and 2 are missing — resume by uploading only them')
"
```

**Demonstrate deduplication savings at scale:**

```bash
python -c "
# Show how many unique vs total chunks exist after the lab
import psycopg2
conn = psycopg2.connect('postgresql://app:secret@localhost:5432/gdrive')
with conn.cursor() as cur:
    cur.execute('SELECT COUNT(*), SUM(ref_count), SUM(size_bytes) FROM chunks')
    unique_chunks, total_refs, unique_bytes = cur.fetchone()
    print(f'Unique chunks stored: {unique_chunks}')
    print(f'Total chunk references: {total_refs}')
    print(f'Dedup ratio: {total_refs/unique_chunks:.2f}x' if unique_chunks else '')
    print(f'Bytes stored (unique): {unique_bytes/1024:.0f}KB')
"
```

### Observe

```bash
# List all objects in MinIO (content-addressed storage)
docker compose exec minio mc ls local/gdrive-chunks/chunks/ --recursive

# Show version history in Postgres
docker compose exec db psql -U app gdrive -c \
  "SELECT f.filename, fv.version, array_length(fv.chunk_hashes,1) chunks, fv.created_at
   FROM file_versions fv JOIN files f ON f.id=fv.file_id ORDER BY fv.created_at;"

# Show deduplication stats
docker compose exec db psql -U app gdrive -c \
  "SELECT hash[:16] as hash_prefix, ref_count, size_bytes FROM chunks ORDER BY ref_count DESC;"
```

### Teardown

```bash
docker compose down -v
```

---

## Interview Checklist

1. **Q: Why split files into 4MB chunks rather than uploading the whole file?**
   A: Three reasons: (1) Resumability — if a chunk upload fails, retry only that chunk, not the whole file. (2) Deduplication granularity — deduplicate at chunk level, not file level. Two files sharing 90% of content get 90% deduplication benefit. (3) Delta sync — on re-upload, only changed chunks need to be transferred. For a 1GB file with 1KB of edits, only 1-2 chunks (4-8MB) are uploaded instead of the full 1GB.

2. **Q: How does delta sync work when a user inserts text at the beginning of a file?**
   A: Fixed-size chunking fails here — inserting bytes shifts all subsequent boundaries, making every chunk "new." Production systems use content-defined chunking (CDC) with a rolling hash (Rabin fingerprint). Chunk boundaries are determined by content (when the rolling hash matches a specific pattern), not position. An insertion only affects the chunks near the insertion point; all downstream chunks retain their boundaries and hashes.

3. **Q: How do you handle cross-user content deduplication? What are the privacy implications?**
   A: Hash-based deduplication can reveal that two users have the same file (same hash = same content). Dropbox performs cross-user dedup but disclosed this in their ToS. An attacker who knows the hash of a file can check if it exists in the store (hash-proof-of-work attack). Mitigations: convergent encryption (encrypt with key derived from content hash — only users with the file can decrypt), or per-user dedup only (no cross-user info leakage but less efficient).

4. **Q: How do you ensure version history doesn't 2× storage per version?**
   A: Content deduplication applies across versions. Version N and N+1 of a file that differ by 5% share 95% of their chunks. The version history stores arrays of chunk hashes, not chunk bytes. Storage overhead per version ≈ % of changed content × chunk size. For typical document edits (<5% changed), 10 versions cost ~1.5× the original file size, not 10×.

5. **Q: Walk me through the complete upload flow for a large file.**
   A: (1) Client splits file into 4MB chunks, computes SHA-256 for each. (2) Client sends POST `/upload/check` with all chunk hashes — server returns which hashes are missing from the store. (3) Client uploads only missing chunks (PUT `/chunk/{hash}`). Server stores in object storage, updates chunks table. (4) Client sends POST `/upload/complete` with full ordered list of chunk hashes. Server creates file record and version entry. (5) Server pushes sync notification to all user's connected devices via WebSocket. (6) Other devices download only missing chunks and assemble the file.

6. **Q: How does Google Docs enable simultaneous editing without conflicts?**
   A: Operational Transformation (OT). Each keystroke becomes an operation (insert char X at position N). When two users make concurrent edits, the server transforms both operations so they can be applied in sequence and produce the same result. Example: Alice inserts 'A' at position 5; Bob inserts 'B' at position 7. If Bob's edit is applied first, Alice's position 5 is still correct. If Alice's is applied first, Bob's position must shift to 8. OT handles this transformation automatically. The server is the arbiter of ordering.

7. **Q: How would you design the metadata schema for 2 trillion files?**
   A: Files table sharded by `user_id` (each user's files on one shard). At 1B users and 100 shards, each shard holds ~10M users and ~20B files. Use a Snowflake-style `file_id` with the shard number embedded, so routing requires no directory lookup. Alternatively, use Google's Spanner (globally distributed SQL) which auto-shards based on key ranges. The chunks table is sharded by `hash prefix` (first 2 bytes of SHA-256 = 256 shards).

8. **Q: How do you handle a user who is offline for 30 days and then reconnects?**
   A: On reconnect, the sync client sends its last-seen vector clock (or the timestamp of its last successful sync). The server queries for all file changes after that timestamp for the user's files. If the user was offline for months, this could be a large diff. The server provides a paginated "changes since" API. Deleted files are marked with a tombstone (not immediately deleted) for 30+ days to support offline clients receiving deletion events.

9. **Q: How would you shard the object storage layer to handle 15 exabytes?**
   A: Object storage (GCS/S3) scales to exabytes natively — this is Google Cloud Storage's core value proposition. The content-addressed key space (SHA-256) distributes uniformly across storage nodes by construction. No hot spots from sequential keys. Adding storage nodes involves consistent hashing on the hash space. At Google's scale, Colossus (successor to GFS) manages the actual block distribution across thousands of storage servers transparently.

10. **Q: What happens if two clients both think they have the "latest" version?**
    A: This is a distributed systems conflict (split-brain). Prevention: use optimistic locking — each save includes the client's known version number. Server rejects saves where `provided_version != current_version`. Resolution: the rejected client receives the current version's content and chunk hashes, diffs against their edit, and either: (a) auto-merges if changes are non-overlapping (different chunks changed), or (b) creates a conflict copy for user review. Google Drive implements option (b) for simplicity and safety.

11. **Q: How do you notify all of a user's devices when a file changes? Why not polling?**
    A: Each sync client holds a persistent WebSocket connection to a notification server. On file commit, the upload service publishes an event to Kafka (keyed by `user_id`). Notification servers consume from Kafka and push to all WebSocket connections for that user. Polling would require 500M active users × 1 poll/30 s ≈ 17M RPS just for sync checks; WebSocket push eliminates that load. Clients behind restrictive firewalls fall back to long-polling (`GET /sync/poll?since={cursor}` blocks up to 30 s). Notification servers are stateless with respect to the event log — if one crashes, clients reconnect and Kafka replays missed events from the last committed offset.

12. **Q: How do you safely garbage collect chunks when a file version is deleted?**
    A: Deleting a file version decrements `ref_count` on all its chunks. A background GC job queries `SELECT hash FROM chunks WHERE ref_count = 0` and deletes from both the chunks table and object storage. Chunks are never deleted synchronously at version-delete time, because a concurrent upload of the same chunk hash might be in flight — synchronous deletion would cause that upload to succeed at the DB level but point to a missing object. The async GC job runs after a quiescence window (e.g., 1 hour), by which time any in-flight uploads using the same hash have either completed (and incremented ref_count above 0) or failed.
