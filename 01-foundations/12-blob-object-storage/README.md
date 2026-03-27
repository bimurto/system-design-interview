# 12 — Blob / Object Storage

**Prerequisites:** [11 — API Design](../11-api-design/)
**Next:** [02-advanced — Consistent Hashing](../../02-advanced/01-consistent-hashing/)

---

## Concept

Object storage exists because databases and block storage are the wrong tool for large binary content at scale. Storing a 10MB image in a relational database bloats your tables, saturates replication bandwidth, and can't be served through a CDN efficiently. Block storage (like AWS EBS) is fast and low-latency but attaches to a single instance, can't scale beyond a few TB economically, and costs roughly 4–10x more per GB than object storage. When you need to store petabytes of images, videos, log files, model weights, or backup archives — things you write once and read many times — object storage is the correct abstraction. It exposes a simple HTTP API (PUT to write, GET to read, DELETE to remove) and scales to exabytes without any schema or partitioning concerns on the client side.

The S3 revolution was not just cheaper storage — it was the architectural insight of a flat namespace. Traditional filesystems use directory trees, and those trees have metadata bottlenecks: listing a directory, creating a file, or moving a file requires locking or coordinating the parent directory's metadata. S3 eliminated this by treating every object as a key in a flat keyspace — `images/2024/01/photo.jpg` is just a string key, not a real directory path. This means metadata operations (listing, creating) scale horizontally across a hash-partitioned metadata service rather than serializing through a tree. The result is a storage system that handles millions of PUT requests per second with no hierarchical bottleneck. The tradeoff is that rename and atomic move operations don't exist — "rename" requires a copy followed by a delete, which is both non-atomic and costs double the storage during the operation.

Durability and availability are distinct properties that S3 addresses separately. Durability (99.999999999% — eleven nines) means the probability of losing your data is vanishingly small. This is achieved through erasure coding: a file is split into data shards and parity shards (e.g., Reed-Solomon 6+3), distributed across multiple physical failure domains (racks, AZs), and can be reconstructed from any sufficient subset of shards. This is fundamentally more space-efficient and resilient than simple replication for large objects. Availability (99.99% for S3 Standard) is a separate SLA describing whether the service responds to requests — and is intentionally lower than durability, because temporary unavailability doesn't mean data loss.

Object storage has become the foundational layer of modern data infrastructure. Data lakes (raw data in S3, queried by Athena/Spark) replace expensive enterprise data warehouses. ML training pipelines store datasets and model checkpoints in object storage and read them in parallel across hundreds of training nodes. Video platforms store every video ever uploaded, serving them through CDN edge caches. The economics — $0.023/GB/month for S3 Standard vs $0.10/GB for block storage — make it the only economically viable option at petabyte scale. Understanding object storage internals is prerequisite knowledge for designing any system that handles user-generated content, media, or large-scale analytics.

---

## How It Works

### Object vs Block vs File Storage

| Aspect      | Object Storage (S3)              | Block Storage (EBS)              | File Storage (EFS/NFS)          |
|-------------|----------------------------------|----------------------------------|---------------------------------|
| Access      | HTTP API (GET/PUT/DELETE)        | Raw block device (OS-level)      | POSIX filesystem (mount)        |
| Unit        | Object (key + data + metadata)   | Fixed-size block (512B–4KB)      | File in directory hierarchy     |
| Mutability  | Immutable (overwrite = new obj)  | In-place read/write              | In-place read/write             |
| Scalability | Infinite (petabytes+)            | Limited (attach to one instance) | Scales but costs more           |
| Latency     | ms (first byte)                  | sub-ms (local SSD)               | ms (network)                    |
| Cost        | Cheapest ($0.023/GB/month)       | Medium ($0.10/GB/month)          | Expensive ($0.30/GB/month)      |
| Use cases   | Images, video, backups, logs     | Database volumes, OS disk        | Shared filesystem, CMS          |
| Versioning  | Built-in optional                | Snapshots                        | No built-in                     |

**Rule of thumb:** Use object storage for anything you write once and read many times (media, backups, ML datasets, static websites). Use block storage for databases and anything needing in-place updates.

### S3 Data Model

```
Account
 └─ Bucket  (globally unique name, tied to a region)
      └─ Object
           ├─ Key:      "images/2024/01/photo.jpg"   (arbitrary string)
           ├─ Data:     binary content
           ├─ Metadata: Content-Type, custom headers, ETag, Last-Modified
           └─ Version:  (if versioning enabled)
```

**Flat namespace:** S3 has no real directories. `"images/2024/01/photo.jpg"` is a key where `/` is part of the string. The console and SDKs simulate folders by splitting on `/`. Listing with `Delimiter="/"` returns "common prefixes" (virtual folders).

**Key design:** S3 partitions by key prefix. If all your keys share the same prefix (e.g., timestamp-based keys like `2024-01-15-...`), all writes hit the same partition. Randomize the prefix (hash prefix, UUID prefix) to distribute load across partitions at high write throughput.

### 11 Nines Durability

AWS S3 advertises **99.999999999% (11 nines) durability** — expected loss of 0.000000001% of objects per year.

How it's achieved: **Erasure coding**, not RAID.

```
Erasure coding (e.g., Reed-Solomon 6+3):
  File split into 6 data shards + 3 parity shards = 9 total
  Stored across 9 different AZs/racks
  Can reconstruct from any 6 of 9 shards
  → Tolerate loss of any 3 shards simultaneously

vs RAID-5:
  1 parity drive; tolerate 1 drive failure only
  Not designed for distributed geo-redundant systems
```

S3 Standard replicates across at least 3 Availability Zones within the region.

### Multipart Upload

**Required** for objects > 5GB. Recommended for objects > 100MB.

```
1. CreateMultipartUpload(bucket, key)
   → server returns upload_id

2. UploadPart(upload_id, part_number=1, data=5MB_chunk)  → ETag1
   UploadPart(upload_id, part_number=2, data=5MB_chunk)  → ETag2
   ... (parts can be uploaded in parallel)

3. CompleteMultipartUpload(upload_id, [(1, ETag1), (2, ETag2), ...])
   → server concatenates parts, object becomes visible

On failure: AbortMultipartUpload(upload_id) — server cleans up partial data
```

**Part constraints:**
- Minimum part size: 5MB (except the last part, which can be smaller)
- Maximum parts: 10,000
- Maximum object size: 5TB

**ETag for multipart:** `MD5(ETag1 + ETag2 + ...)-N` where N = number of parts. Not directly comparable to a local MD5 of the whole file.

### Presigned URLs

A presigned URL encodes the request parameters + expiry + HMAC signature. Anyone holding the URL can perform the operation until expiry — no AWS credentials needed.

```python
# Server generates (has credentials):
url = s3.generate_presigned_url(
    "get_object",
    Params={"Bucket": "my-bucket", "Key": "private/report.pdf"},
    ExpiresIn=3600  # seconds
)

# Client uses (no credentials):
requests.get(url)  # works for 1 hour
```

**Common patterns:**
- **Direct browser upload:** server generates presigned PUT URL → browser uploads directly to S3 (bypasses your server, no data proxy needed)
- **Temporary share link:** email a presigned GET URL to a customer
- **CDN origin:** CloudFront + S3 (signed requests so only CloudFront can fetch from S3)

### Storage Classes

| Class               | Retrieval  | Min Storage | Use Case                         |
|---------------------|------------|-------------|----------------------------------|
| S3 Standard         | ms         | None        | Hot data, frequent access        |
| S3 Standard-IA      | ms         | 30 days     | Infrequent access (monthly)      |
| S3 One Zone-IA      | ms         | 30 days     | Non-critical, recreatable data   |
| S3 Glacier Instant  | ms         | 90 days     | Archive with occasional access   |
| S3 Glacier Flexible | 1min–12hrs | 90 days     | Long-term archive                |
| S3 Glacier Deep     | 12–48hrs   | 180 days    | Compliance, regulatory retention |

**Lifecycle policies** automate transitions:
```
Day 0   → Standard
Day 30  → Standard-IA        (save ~50%)
Day 90  → Glacier Instant    (save ~75%)
Day 365 → Glacier Deep       (save ~95%)
```

### Strong Consistency (December 2020 Change)

Before December 2020, S3 had **eventual consistency** for overwrite PUTs and DELETEs:
- Upload new version → might read old version for a short time
- Delete object → might still appear in listing briefly

Since December 2020: **strong read-after-write consistency** for all S3 operations:
- PUT then GET → always returns the new version
- DELETE then LIST → object no longer appears
- No extra configuration; applies to all regions and all object types

This eliminated a whole class of bugs in distributed systems built on S3.

**Important scoping:** Strong consistency applies within a single AWS region. Cross-Region Replication (S3 CRR) is still asynchronous — objects replicated to a secondary region can lag by minutes. Do not read from a replica region and expect to see a PUT made to the primary region moments ago.

### Object Immutability and Its Implications

Every S3 PUT replaces the object atomically — there is no append, no partial update, and no in-place modification. This immutability has architectural consequences:

**Implications for data pipelines:**
- Write to the final key name directly. "Renaming" after the fact is a copy + delete, which is non-atomic, costs double storage transiently, and is expensive for large objects.
- For ML training datasets: store snapshots as immutable prefixes (e.g., `datasets/v3/`). Never overwrite a dataset version in-place; new versions get new prefixes.
- For log aggregation (e.g., Kinesis Firehose → S3): small files accumulate. Use S3 Object Lambda or periodic compaction jobs to merge them, since you cannot append to existing objects.

**Implications for data lakes:**
- Table formats like Apache Iceberg and Delta Lake work by writing new Parquet files and updating a metadata manifest atomically. The immutability of S3 objects is a feature, not a limitation — it enables snapshot isolation and time-travel queries.

### Byte-Range Fetches

S3 supports `Range: bytes=start-end` HTTP headers for partial object retrieval:

```python
# Fetch only bytes 0-4095 of a large object (first 4KB)
response = client.get_object(bucket, key, offset=0, length=4096)
```

**Why this matters for system design:**
- **Video streaming:** serve only the requested byte range (HTTP 206 Partial Content) rather than downloading the full video file. A 4GB video file can be seeked into without downloading from the beginning.
- **Parquet/ORC footer reading:** analytics engines (Spark, Athena) read only the file footer (last N bytes) to get the schema and row group metadata, then fetch only the relevant row groups. This turns petabyte-scale queries into small targeted reads.
- **Distributed ML training:** each training worker fetches a disjoint byte range of a dataset shard, enabling parallel data loading without coordination.

### Cross-Region Replication (CRR)

S3 CRR asynchronously copies objects from a source bucket to one or more destination buckets in different regions:

```
us-east-1 (primary)  --[async replication, ~minutes lag]--> eu-west-1 (replica)
```

**Use cases:**
- Disaster recovery: survive a full region outage with RPO of minutes
- Latency optimization: serve reads from the nearest replica region
- Compliance: data residency requirements across jurisdictions

**Trade-offs:**
- Replication is eventually consistent — reads from the replica may be stale
- Cost doubles (storage + replication data transfer fees)
- Only replicates new objects after CRR is enabled; existing objects require a one-time Batch Operations job
- Deletes are not replicated by default (configurable)

### CDN Integration Pattern

```
                    ┌──────────────┐
Browser ──HTTPS──►  │  CloudFront  │ ◄── Edge PoP (~10ms latency)
                    └──────┬───────┘
                           │ Cache miss only
                           ▼
                    ┌──────────────┐
                    │   S3 Bucket  │  (private, no public access)
                    │  (us-east-1) │
                    └──────────────┘
```

- S3 bucket is **private** (no public read)
- CloudFront Origin Access Control (OAC) allows only CloudFront to fetch from S3
- Static assets cached at edge for hours/days (Cache-Control: max-age=86400)
- For user-generated content: presigned URL → direct upload to S3 → CDN distributes reads
- Cache invalidation: `aws cloudfront create-invalidation --paths "/images/*"`

### Trade-offs

| Approach                        | Pros                                          | Cons                                                 |
|---------------------------------|-----------------------------------------------|------------------------------------------------------|
| Object storage (S3/MinIO)       | Infinite scale, cheap, HTTP access            | No in-place edit, no rename, ms latency              |
| Block storage (EBS)             | Sub-ms latency, in-place writes               | Attached to one instance, expensive at scale         |
| File storage (EFS/NFS)          | POSIX interface, shared access                 | Most expensive, NFS latency overhead                 |
| Direct S3 serving               | No proxy server needed                        | Exposes bucket, no access control after link shared  |
| Presigned URL serving           | Temporary access, no proxy                    | No revocation, URL can be shared beyond intended     |
| CDN + S3                        | Edge caching, low latency globally             | Cache invalidation complexity, CloudFront costs      |
| S3 Single-region                | Lower cost, simpler                           | No redundancy across region failure                  |
| S3 Cross-Region Replication     | Disaster recovery, reduced read latency       | Eventual consistency at replica, 2x storage cost     |
| Full object fetch               | Simplest client code                          | Wastes bandwidth for video seek, analytics queries   |
| Byte-range fetch                | Efficient for video streaming, Parquet reads  | More complex client; server must support Range header|

### Failure Modes

**Eventual consistency (pre-December 2020 S3):** Before S3 moved to strong consistency, a PUT followed immediately by a GET could return the old version. Systems that uploaded a new config file and immediately read it back would sometimes get the previous version, causing hard-to-reproduce bugs. Today S3 is strongly consistent, but this history explains why older codebases sometimes have defensive read-after-write retry logic.

**Multipart upload zombies:** If a multipart upload is initiated but never completed or aborted (e.g., the client crashes mid-upload), S3 stores the uploaded parts indefinitely. These "incomplete" uploads consume storage and accumulate cost invisibly — they don't appear in normal object listings. At scale, zombie multipart uploads can become a significant cost source. Fix: set a lifecycle rule to abort incomplete multipart uploads after N days (e.g., `AbortIncompleteMultipartUpload: DaysAfterInitiation: 7`).

**Presigned URL leaks:** A presigned URL is a capability token — anyone who obtains the URL can use it until expiry. If a user shares their presigned download link (in a chat, bug report, screenshot), the recipient gains access to the object. There is no revocation mechanism short of deleting the object or rotating the signing key (which invalidates all outstanding URLs). Design systems to use short expiry times for sensitive content and to generate per-user presigned URLs rather than shared links.

**Hotspot on single prefix:** S3 partitions its internal metadata index by key prefix. If all your writes share the same prefix — for example, `uploads/2024-01-15/` for daily uploads — all writes that day hit the same S3 partition, potentially triggering throttling (S3's default limit is 3,500 PUTs/second per prefix). At high write throughput, add a random hash prefix (`a3f2/uploads/2024-01-15/photo.jpg`) to distribute writes across partitions. S3 does auto-scale partitions after 15–30 minutes of sustained load, but hash-prefix naming avoids the initial throttle window entirely.

**Cross-region replication lag:** S3 CRR replicates objects asynchronously. An application that writes to us-east-1 and immediately reads from eu-west-1 (replica) may get a 404 or stale version. The replication lag is typically seconds to minutes but has no hard upper bound. Never use a replica region as a strongly consistent read endpoint.

**S3 Object Lock / accidental deletion:** Without versioning, a DELETE is permanent. A misconfigured IAM policy or a runaway cleanup job can silently delete terabytes of data. Enable versioning on buckets containing critical data. For compliance requirements, enable S3 Object Lock (WORM — Write Once, Read Many) which prevents deletion even by bucket owners during the retention period.

**Small-file accumulation in streaming pipelines:** Services like Kinesis Firehose write small objects (default: 128MB or 5 minutes, whichever comes first). Over time, a bucket can accumulate millions of tiny files. Querying them with Athena or Spark incurs high LIST overhead and poor compression ratios. Periodically compact small files into large Parquet files using AWS Glue or a Spark job. S3 cannot merge files in-place — compaction means reading the small files and writing new larger ones.

---

## Interview Talking Points

- "Object storage is not a filesystem — there is no rename, no append, and no locking. Rename is copy + delete: non-atomic, costs double storage transiently, and is expensive for large objects. Design pipelines to write directly to the final key."
- "S3 has had strong read-after-write consistency since December 2020 — within a single region. Cross-Region Replication (CRR) is still asynchronous with minutes-scale lag, so don't read from a replica and expect to see a PUT made to the primary moments ago."
- "For large files, use multipart upload: it enables parallelism (concurrent part uploads), resumability (retry only the failed part), and is required for objects over 5GB. Set a lifecycle rule to abort incomplete multipart uploads after 7 days to avoid zombie upload cost accumulation."
- "Presigned URLs let you serve private content without proxying through your servers — the client downloads directly from S3 using a time-limited signed URL. They are capability tokens with no revocation mechanism; a leaked URL grants access until expiry. Use short expiry times and generate per-user URLs."
- "S3 key prefix determines which partition handles the request — at >3,500 PUTs/sec, add a 4-character hash prefix to distribute writes. S3 auto-scales partitions but takes 15–30 minutes; hash-prefix naming avoids the throttle window entirely."
- "Byte-range fetches (HTTP Range header) are a critical optimization: video players seek without downloading the full file, analytics engines (Athena, Spark) read only Parquet row group footers, and ML training workers fetch disjoint dataset shards in parallel."
- "Object immutability is a feature, not a limitation: table formats like Iceberg and Delta Lake leverage it to provide snapshot isolation and time-travel queries by treating S3 as an immutable log of Parquet files managed by a metadata layer."

---

## Hands-on Lab

**Time:** ~20–30 minutes
**Services:** minio (S3-compatible object storage), minio-mc (MinIO client for setup)

### Setup

```bash
docker compose up -d
```

MinIO Console is available at http://localhost:9001 (credentials: minioadmin / minioadmin). Wait ~10 seconds for MinIO to initialize before running the experiment.

### Experiment

```bash
python experiment.py
```

The experiment runs seven phases:

1. **Bucket creation** — creates a test bucket, explains the flat namespace and DNS naming constraints
2. **Small file upload** — uploads a small text object, downloads it back, verifies content integrity via ETag and confirms ETag equals local MD5 for single-part objects
3. **Multipart upload: sequential vs parallel** — uploads a 30MB file twice (sequential parts, then parallel parts) and compares timing; explains part constraints and zombie upload risk
4. **Presigned URL** — generates a presigned GET URL with 60-second expiry, shows all embedded query parameters, downloads without credentials, confirms no Authorization header was sent
5. **List objects** — lists all objects with metadata; demonstrates prefix + delimiter listing to simulate directory listing
6. **ETag content-addressability** — uploads the same content twice under different keys; demonstrates both ETags are identical, and a third upload with different content produces a different ETag
7. **Key prefix partitioning** — writes 20 objects with a hotspot prefix pattern and 20 with a hash-distributed prefix pattern; explains S3 partition throttle limits and when hash-prefix naming is required

### Break It

Run the zombie multipart upload demonstration:

```bash
python experiment.py --show-incomplete
```

This phase simulates an upload that fails mid-stream, then shows that the partially-uploaded object does not appear in normal `list_objects` output — illustrating why zombie parts accumulate invisibly and why every bucket accepting multipart uploads should have a lifecycle rule:

```json
{
  "Rules": [{
    "Status": "Enabled",
    "Filter": {},
    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}
  }]
}
```

### Observe

- **Sequential vs parallel timing** in Phase 3: on localhost the difference is modest (CPU/memory bound), but in cloud environments with high-bandwidth links the parallel path saturates available bandwidth and achieves 5–10x speedup
- **Presigned URL query parameters** in Phase 4: note `X-Amz-Expires`, `X-Amz-Credential`, and `X-Amz-Signature` — the entire authorization is in the URL, no headers needed
- **ETag equality** in Phase 6: same bytes → same MD5 → same ETag regardless of key name; different bytes → different ETag
- **Prefix listing** in Phase 5: `list_objects` with `delimiter="/"` returns virtual directory entries (`is_dir=True`) for common prefixes rather than listing every object underneath

### Teardown

```bash
docker compose down -v
```

---

## Real-World Examples

- **Dropbox:** Migrated from Amazon S3 to their own "Magic Pocket" object storage system, built on erasure coding across custom hardware, to gain control over storage costs at their scale (hundreds of petabytes). Source: Dropbox Tech Blog, "Rewriting the heart of our sync engine" (2019)
- **Netflix:** Stores all video content (100PB+) in S3, serving through CloudFront CDN. The combination of S3's durability and CloudFront's edge caching means Netflix never streams directly from S3 to end users — CDN absorbs nearly all read traffic. Source: Netflix Tech Blog
- **Figma:** Stores all design file data (vector graphics, assets, version history) in S3, using S3's strong consistency guarantees (post-2020) to support collaborative real-time editing where multiple users may write to the same file. Source: Figma Engineering Blog

---

## Common Mistakes

- **Treating object storage like a filesystem** — there is no atomic rename in S3. "Renaming" a file requires a CopyObject followed by DeleteObject, which is non-atomic (a reader between the two operations sees both keys), costs double storage transiently, and is expensive for large objects. Design data pipelines to write to the final key name directly.
- **Not setting lifecycle rules on incomplete multipart uploads** — abandoned multipart uploads accumulate in your bucket silently. They don't appear in normal listings but do appear on your bill. Add a lifecycle rule to abort incomplete multipart uploads after 7 days.
- **Using sequential keys as S3 key names** — keys like `2024-01-15T10:00:00-image.jpg` cause all writes in a time window to hit the same S3 partition, triggering throttling (HTTP 503) at high write rates. Prepend a random hash: `a3f2/2024-01-15T10:00:00-image.jpg`.
- **Serving private S3 content by proxying through your application server** — don't pipe S3 GET responses through your app servers to add access control. Use presigned URLs for temporary access or CloudFront signed URLs for CDN-cached content. Proxying wastes your server bandwidth and compute on byte-forwarding.
- **Assuming CRR replicas are up-to-date** — Cross-Region Replication is asynchronous. A write to the primary region may not be visible in the replica region for seconds to minutes. Never use a CRR replica as a strongly consistent read endpoint for recently-written objects.
- **Not using byte-range fetches for large objects** — fetching an entire 10GB Parquet file to read 5MB of data is wasteful. Use byte-range requests to read only the footer or specific row groups. This is how Athena and Spark achieve sub-second query latency on petabyte-scale data lakes.
- **Accumulating millions of small files in streaming pipelines** — Kinesis Firehose and similar services write many small objects. Querying millions of 1MB files in Athena is orders of magnitude slower than querying thousands of 1GB files. Schedule periodic compaction jobs (Glue, Spark) to merge small files into large Parquet files.
