#!/usr/bin/env python3
"""
Blob/Object Storage Lab — MinIO (S3-compatible)

Prerequisites:
  pip install minio requests
  docker compose up -d  (wait ~10s for MinIO to initialize)

Usage:
  python experiment.py                  # run all phases
  python experiment.py --show-incomplete  # inspect zombie multipart upload parts

Demonstrates:
  1. Create a bucket
  2. Upload a small text file (flat namespace, ETag, metadata)
  3. Multipart upload: sequential vs parallel timing comparison
  4. Presigned URL generation and credential-free download
  5. List objects with metadata
  6. ETag / content-addressable storage: same content -> same ETag
  7. Key prefix partitioning: hotspot vs distributed write pattern
  8. (--show-incomplete) Zombie multipart upload parts and lifecycle rule motivation
"""

import argparse
import concurrent.futures
import io
import hashlib
import sys
import time
import uuid

try:
    from minio import Minio
    from minio.error import S3Error
except ImportError:
    print("ERROR: minio package not found. Run: pip install minio requests")
    raise SystemExit(1)

try:
    import requests
except ImportError:
    print("ERROR: requests package not found. Run: pip install requests")
    raise SystemExit(1)

from datetime import timedelta

MINIO_ENDPOINT = "localhost:9000"
MINIO_ACCESS   = "minioadmin"
MINIO_SECRET   = "minioadmin"
BUCKET_NAME    = "test-bucket"

client = Minio(MINIO_ENDPOINT, access_key=MINIO_ACCESS, secret_key=MINIO_SECRET, secure=False)


def section(title):
    print(f"\n{'=' * 64}")
    print(f"  {title}")
    print("=" * 64)


def wait_for_minio():
    print("  Waiting for MinIO to be ready...")
    for attempt in range(20):
        try:
            client.list_buckets()
            print("  MinIO is ready.\n")
            return
        except Exception:
            pass
        print(f"    Attempt {attempt + 1}/20, retrying in 3s...")
        time.sleep(3)
    print("  ERROR: MinIO never became ready. Run: docker compose up -d")
    raise SystemExit(1)


# ── Phase 1: Create Bucket ─────────────────────────────────────────────────────

def phase_create_bucket():
    section("Phase 1: Create Bucket")
    print("""
  S3 data model:
    Bucket  = top-level container (globally unique name in a region)
    Key     = full "path" of the object (e.g. "2024/01/photo.jpg")
    Object  = key + data + metadata + ETag + optional version ID

  Namespace is FLAT — "directories" are key prefixes using "/" as a convention.
  The S3 API has no mkdir, no rename, no append. A "rename" is CopyObject +
  DeleteObject, which is non-atomic and costs double storage transiently.

  Bucket names must be DNS-compatible (lowercase, no underscores, 3-63 chars).
""")

    if client.bucket_exists(BUCKET_NAME):
        print(f"  Bucket '{BUCKET_NAME}' already exists, reusing.")
    else:
        client.make_bucket(BUCKET_NAME)
        print(f"  Created bucket: {BUCKET_NAME}")

    buckets = client.list_buckets()
    print(f"  All buckets: {[b.name for b in buckets]}")


# ── Phase 2: Upload Small File ─────────────────────────────────────────────────

def phase_upload_small():
    section("Phase 2: Upload Small Text File")

    content = b"Hello, object storage! This is a small text file.\n"
    key = "small/hello.txt"

    result = client.put_object(
        BUCKET_NAME,
        key,
        io.BytesIO(content),
        length=len(content),
        content_type="text/plain",
    )

    # Download and verify
    response = client.get_object(BUCKET_NAME, key)
    downloaded = response.read()
    response.close()
    response.release_conn()

    local_md5 = hashlib.md5(content).hexdigest()

    print(f"  Uploaded key:      {key}")
    print(f"  Object size:       {len(content)} bytes")
    print(f"  ETag (server):     {result.etag.strip(chr(34))}")
    print(f"  Local MD5:         {local_md5}")
    print(f"  ETag == MD5:       {result.etag.strip(chr(34)) == local_md5}")
    print(f"  Content verified:  {downloaded == content}")
    print(f"""
  Key observations:
    - "small/hello.txt" is just a string key — "/" is part of the key, not a
      real directory separator. There is no directory node in the metadata store.
    - ETag for single-part uploads = MD5 of the raw content bytes.
    - S3 provides strong read-after-write consistency (since December 2020):
      the GET above always returns the bytes we just PUT.
    - Content-Type is stored as object metadata, returned in GET response headers.
""")
    return result.etag


# ── Phase 3: Multipart Upload — Sequential vs Parallel ────────────────────────

def phase_multipart_upload():
    section("Phase 3: Multipart Upload — Sequential vs Parallel Timing")
    print("""
  Multipart upload mechanics:
    1. InitiateMultipartUpload  -> server returns upload_id (opaque token)
    2. UploadPart(upload_id, part_number, data) x N  -> ETag per part
       Parts can be sent in parallel — each is an independent HTTP PUT.
    3. CompleteMultipartUpload(upload_id, [(part_num, ETag), ...])
       Server assembles parts in part_number order into the final object.
    4. AbortMultipartUpload(upload_id) — server discards partial data.
       CRITICAL: if you never call Abort or Complete, parts remain stored
       and billed indefinitely ("zombie" uploads). Use a lifecycle rule:
         AbortIncompleteMultipartUpload: DaysAfterInitiation: 7

  Part constraints (S3/MinIO):
    - Minimum part size: 5 MiB (except the last part, which can be smaller)
    - Maximum part size: 5 GiB
    - Maximum number of parts: 10,000
    - Maximum object size: 5 TiB

  ETag for multipart objects: MD5(ETag1 + ETag2 + ...)-N
    (NOT a plain MD5 of the full file — don't compare to local md5sum)
""")

    size_mb    = 30
    chunk_mb   = 5
    size_bytes  = size_mb  * 1024 * 1024
    chunk_bytes = chunk_mb * 1024 * 1024
    num_parts   = size_mb // chunk_mb

    # Generate deterministic data
    print(f"  Generating {size_mb}MB file ({num_parts} x {chunk_mb}MB parts)...")
    chunk_pattern = bytes(range(256)) * (chunk_bytes // 256)
    data = (chunk_pattern * (size_bytes // len(chunk_pattern)))[:size_bytes]
    parts = [data[i * chunk_bytes:(i + 1) * chunk_bytes] for i in range(num_parts)]

    # --- Sequential upload (simulate no parallelism) ---
    key_seq = "large/30mb-sequential.bin"
    print(f"\n  [Sequential] Uploading {num_parts} parts one at a time...")

    t0 = time.perf_counter()
    # MinIO SDK with thread_count=1 uploads parts sequentially
    client.put_object(
        BUCKET_NAME, key_seq,
        io.BytesIO(data), length=size_bytes,
        part_size=chunk_bytes,
        num_parallel_uploads=1,
        content_type="application/octet-stream",
    )
    elapsed_seq = time.perf_counter() - t0
    print(f"  Sequential: {elapsed_seq:.2f}s  ({size_mb / elapsed_seq:.1f} MB/s)")

    # --- Parallel upload ---
    key_par = "large/30mb-parallel.bin"
    print(f"\n  [Parallel]   Uploading {num_parts} parts with thread pool...")

    t0 = time.perf_counter()
    client.put_object(
        BUCKET_NAME, key_par,
        io.BytesIO(data), length=size_bytes,
        part_size=chunk_bytes,
        num_parallel_uploads=num_parts,
        content_type="application/octet-stream",
    )
    elapsed_par = time.perf_counter() - t0
    print(f"  Parallel:   {elapsed_par:.2f}s  ({size_mb / elapsed_par:.1f} MB/s)")

    speedup = elapsed_seq / elapsed_par if elapsed_par > 0 else 1.0
    print(f"""
  Result comparison:
    Sequential: {elapsed_seq:.2f}s
    Parallel:   {elapsed_par:.2f}s
    Speedup:    {speedup:.2f}x

  On localhost, speedup is modest because network isn't the bottleneck.
  In a cloud environment (e.g., EC2 -> S3), parallel parts saturate
  available bandwidth and can yield 5-10x speedup for large objects.
  The SDK defaults to ~5 concurrent parts; tune num_parallel_uploads
  based on object size, part size, and available network bandwidth.
""")
    return elapsed_par, size_mb / elapsed_par


# ── Phase 4: Presigned URL ─────────────────────────────────────────────────────

def phase_presigned_url():
    section("Phase 4: Presigned URL — Temporary Access Without Credentials")
    print("""
  Presigned URL encodes in the query string:
    - Bucket + key
    - Expiry timestamp (X-Amz-Expires)
    - Credential scope (X-Amz-Credential)
    - HMAC-SHA256 signature (X-Amz-Signature)

  The server validates the signature on every request. No session state needed.

  Use cases:
    a) Direct browser upload: server generates presigned PUT URL -> browser
       uploads directly to S3, bypassing your app server entirely. Saves
       bandwidth and compute on your fleet for every upload.
    b) Temporary share link: customer downloads a private report for 15 minutes.
    c) CDN origin fetch: CloudFront uses Origin Access Control (OAC) to fetch
       from private S3; users never access S3 directly.

  Security gotchas:
    - Presigned URLs are capability tokens: anyone with the URL can use it.
    - No revocation mechanism. If leaked, the only remedies are:
        1. Delete the object (destroys the resource, not just the link)
        2. Rotate the IAM key used to sign (invalidates ALL outstanding URLs)
    - Use short expiries for sensitive content (minutes, not hours/days).
    - Do NOT log presigned URLs in application logs — treat them like secrets.
    - Presigned URLs can be used for PUT (upload) as well as GET (download).
      Scope the operation carefully; a presigned PUT URL allows overwriting.
""")

    key    = "small/hello.txt"
    expiry = timedelta(seconds=60)

    url = client.presigned_get_object(BUCKET_NAME, key, expires=expiry)
    print(f"  Presigned GET URL (60s expiry):")
    # Show query params to illustrate what's embedded
    base, _, qs = url.partition("?")
    print(f"    Base:   {base}")
    for param in qs.split("&"):
        print(f"    Param:  {param}")

    resp = requests.get(url, timeout=10)
    print(f"\n  HTTP GET via presigned URL (no Authorization header):")
    print(f"    Status:  {resp.status_code}")
    print(f"    Content: {resp.text.strip()!r}")
    print(f"    Size:    {len(resp.content)} bytes")

    # Confirm no auth header was sent
    auth_sent = resp.request.headers.get("Authorization", "(none)")
    print(f"    Auth header in request: {auth_sent}")

    print("""
  The authorization is entirely in the URL query string — no request headers
  needed. This is why presigned URLs work from a bare curl or browser tab.
""")


# ── Phase 5: List Objects ──────────────────────────────────────────────────────

def phase_list_objects():
    section("Phase 5: List Objects with Metadata")
    print("""
  S3 listing is prefix-based. The API returns up to 1,000 objects per call
  and supports pagination via continuation tokens (not page numbers).

  Listing with Delimiter="/" simulates directory listing by returning:
    - Objects with no "/" after the prefix directly
    - "CommonPrefixes" (virtual subdirectory entries)

  At scale (billions of objects), listing is expensive — avoid LIST in the
  hot path of your application. Use a metadata database (DynamoDB, Postgres)
  to track object keys and use S3 only for data retrieval.
""")

    objects = list(client.list_objects(BUCKET_NAME, recursive=True))
    print(f"  Objects in bucket '{BUCKET_NAME}':\n")
    print(f"  {'Key':<42} {'Size':>12}  ETag")
    print(f"  {'-'*42} {'-'*12}  {'-'*32}")
    for obj in objects:
        etag = (obj.etag or "").strip('"')[:32]
        size_str = f"{obj.size:,}" if obj.size else "0"
        print(f"  {obj.object_name:<42} {size_str:>12}  {etag}")

    print(f"\n  Total objects: {len(objects)}")

    # Demonstrate prefix-based listing (simulated directories)
    print(f"\n  Prefix listing with delimiter '/' (simulated directories):")
    prefixes_seen = set()
    for obj in client.list_objects(BUCKET_NAME, delimiter="/"):
        if obj.is_dir:
            prefixes_seen.add(obj.object_name)
            print(f"    [DIR]  {obj.object_name}")
        else:
            print(f"    [OBJ]  {obj.object_name}")
    print(f"\n  Only top-level 'directories' (common prefixes) returned.")


# ── Phase 6: ETag / Content-Addressable Storage ────────────────────────────────

def phase_etag():
    section("Phase 6: ETag — Content-Addressable Storage")
    print("""
  ETag (Entity Tag) for single-part uploads = MD5 hash of the object content.
  Upload the same bytes to two different keys -> identical ETags.

  S3 itself does NOT deduplicate storage — you pay for both objects.
  But applications can use ETags to implement deduplication:
    1. Hash the file locally (MD5 or SHA-256)
    2. Query your metadata DB: does a key with this hash already exist?
    3. If yes, store a reference; skip the upload.
  This is how Dropbox's client-side deduplication works.

  ETag use cases:
    - HTTP conditional GET:  If-None-Match: "etag" -> 304 Not Modified (CDN/browser caching)
    - Integrity check after download: compare ETag to local MD5
    - Optimistic concurrency: PUT with If-Match header (only overwrite if ETag matches)

  ETag for multipart uploads: MD5(part_ETag_1 + part_ETag_2 + ...)-<num_parts>
    This is NOT the MD5 of the full object. Don't compare to local md5sum.
    Always use stat_object / HeadObject to get authoritative metadata.
""")

    content = b"Identical content — upload twice, get same ETag."
    key1    = "dedup/upload-a.txt"
    key2    = "dedup/upload-b.txt"

    r1 = client.put_object(BUCKET_NAME, key1, io.BytesIO(content), length=len(content))
    r2 = client.put_object(BUCKET_NAME, key2, io.BytesIO(content), length=len(content))

    local_md5 = hashlib.md5(content).hexdigest()

    print(f"  Content:              {content.decode()!r}")
    print(f"  Local MD5:            {local_md5}")
    print(f"  ETag ({key1}): {r1.etag.strip(chr(34))}")
    print(f"  ETag ({key2}): {r2.etag.strip(chr(34))}")
    print(f"  ETags match:          {r1.etag == r2.etag}")
    print(f"  ETag == local MD5:    {r1.etag.strip(chr(34)) == local_md5}")

    # Show what happens with different content
    content2 = b"Different content — ETag will differ."
    r3 = client.put_object(BUCKET_NAME, "dedup/upload-c.txt",
                           io.BytesIO(content2), length=len(content2))
    print(f"\n  Different content:")
    print(f"  ETag (upload-c.txt):  {r3.etag.strip(chr(34))}")
    print(f"  ETags differ:         {r1.etag != r3.etag}")


# ── Phase 7: Key Prefix Partitioning ──────────────────────────────────────────

def phase_prefix_partitioning():
    section("Phase 7: Key Prefix Partitioning — Hotspot vs Distributed Writes")
    print("""
  S3 (and MinIO) partition their internal metadata index by key prefix.
  Default throughput per prefix partition:
    - 3,500 PUT/COPY/POST/DELETE requests per second
    - 5,500 GET/HEAD requests per second

  If all keys share the same prefix, all writes hit one partition -> throttling
  (HTTP 503 Slow Down). Classic hotspot pattern: timestamp-prefixed keys.

  Fix: add a random hash prefix to distribute keys across many partitions.

  Pattern A (BAD at high write rate):
    uploads/2024-01-15/img001.jpg   <- all writes today hit same partition
    uploads/2024-01-15/img002.jpg
    uploads/2024-01-15/img003.jpg

  Pattern B (GOOD):
    a3f2/uploads/2024-01-15/img001.jpg   <- distributed across 16^4 partitions
    b8c1/uploads/2024-01-15/img002.jpg
    9d4e/uploads/2024-01-15/img003.jpg

  Note: since ~2018 S3 auto-scales partitions based on observed traffic patterns,
  but this takes 15-30 minutes to kick in. The hash-prefix approach avoids the
  initial throttle window and is still recommended for sustained high write rates.
""")

    n_objects = 20

    # Pattern A: sequential / timestamp-style keys (hotspot)
    print(f"  Writing {n_objects} objects with HOTSPOT prefix pattern...")
    t0 = time.perf_counter()
    for i in range(n_objects):
        key = f"uploads/2024-01-15/img{i:04d}.jpg"
        body = f"image-data-{i}".encode()
        client.put_object(BUCKET_NAME, key, io.BytesIO(body), length=len(body))
    elapsed_hot = time.perf_counter() - t0
    print(f"  Hotspot pattern:     {elapsed_hot:.3f}s for {n_objects} objects")

    # Pattern B: hash-prefixed keys (distributed)
    print(f"\n  Writing {n_objects} objects with DISTRIBUTED prefix pattern...")
    t0 = time.perf_counter()
    for i in range(n_objects):
        raw_key = f"uploads/2024-01-15/img{i:04d}.jpg"
        prefix  = hashlib.md5(raw_key.encode()).hexdigest()[:4]
        key     = f"{prefix}/{raw_key}"
        body    = f"image-data-{i}".encode()
        client.put_object(BUCKET_NAME, key, io.BytesIO(body), length=len(body))
    elapsed_dist = time.perf_counter() - t0
    print(f"  Distributed pattern: {elapsed_dist:.3f}s for {n_objects} objects")

    print(f"""
  On localhost (single-node MinIO), timing difference is negligible because
  MinIO doesn't shard across partitions the way S3 does at scale. The benefit
  manifests at production scale (millions of writes/day against real S3).

  Key naming strategy:
    - If keys are already random (UUIDs, content hashes): no prefix needed.
    - If keys are sequential/time-ordered: add a 4-hex-char hash prefix.
    - Never use predictable ascending prefixes at high write throughput.

  Example hash-prefix function:
    def s3_key(logical_key: str) -> str:
        prefix = hashlib.md5(logical_key.encode()).hexdigest()[:4]
        return f"{{prefix}}/{{logical_key}}"
""")


# ── Phase 8 (optional): Zombie Multipart Uploads ──────────────────────────────

def phase_show_incomplete():
    section("Phase 8: Zombie Multipart Uploads (--show-incomplete)")
    print("""
  This phase demonstrates what happens when a multipart upload is started
  but never completed or aborted — the "zombie" upload scenario.

  In production, this happens when:
    - The uploading process crashes or is killed mid-upload
    - A network timeout causes the client to give up without aborting
    - A bug in application code skips the abort-on-error path

  Incomplete parts:
    - Are NOT visible in normal object listings (list_objects)
    - DO consume storage and appear on your S3 bill
    - Can accumulate to gigabytes or terabytes over time silently

  Remedy: S3 / MinIO lifecycle rule:
    {
      "Rules": [{
        "Status": "Enabled",
        "AbortIncompleteMultipartUpload": {
          "DaysAfterInitiation": 7
        }
      }]
    }
  This rule deletes incomplete parts after 7 days automatically.
""")

    zombie_key = "zombie/incomplete-upload.bin"
    part_size  = 5 * 1024 * 1024  # 5 MiB minimum part size

    # Initiate multipart upload manually via the S3 HTTP API
    # MinIO SDK doesn't expose raw initiate/upload-part — use requests + presigned logic
    # Instead demonstrate by starting a put_object and intentionally interrupting it
    # We simulate by uploading a partial object then checking listing vs storage

    print("  Simulating an abandoned multipart upload...")
    print("  (Starting a multipart upload and never completing it)")

    # Use the low-level S3 API via the minio client's _url_open to show the upload_id
    # The simplest demonstration: initiate via put_object in a thread and kill it
    # For clarity, show what a zombie looks like through MinIO's admin API

    # Start a large upload that we'll interrupt
    size_bytes = 15 * 1024 * 1024  # 15 MiB — requires multipart (3 parts)
    data = bytes(range(256)) * (size_bytes // 256)

    class UploadInterrupted(Exception):
        pass

    bytes_uploaded = [0]

    class InterruptedStream(io.RawIOBase):
        """Stream that deliberately fails after the first part."""
        def __init__(self, data):
            self._data = data
            self._pos  = 0

        def readinto(self, b):
            if self._pos >= part_size + 1024:  # fail mid-second-part
                raise UploadInterrupted("Simulated crash during upload")
            n = min(len(b), len(self._data) - self._pos)
            b[:n] = self._data[self._pos:self._pos + n]
            self._pos += n
            bytes_uploaded[0] = self._pos
            return n

        def readable(self):
            return True

    stream = io.BufferedReader(InterruptedStream(data), buffer_size=8192)

    try:
        client.put_object(
            BUCKET_NAME, zombie_key,
            stream, length=size_bytes,
            part_size=part_size,
            content_type="application/octet-stream",
        )
        print("  (Upload completed unexpectedly — MinIO buffered the stream)")
    except Exception as e:
        print(f"  Upload failed as expected: {type(e).__name__}")

    # Check whether the key appears in normal listing
    visible_objects = [
        obj.object_name
        for obj in client.list_objects(BUCKET_NAME, recursive=True)
        if "zombie" in obj.object_name
    ]
    print(f"\n  Objects visible under 'zombie/' prefix: {visible_objects}")
    print(f"  (Incomplete multipart parts are NOT shown in normal listing)")

    print(f"""
  In a real S3 environment you would use:
    aws s3api list-multipart-uploads --bucket <bucket>
  to see all in-progress (including abandoned) multipart uploads.

  Each incomplete upload shows its upload_id, key, and when it was initiated.
  The storage consumed by uploaded parts does appear on your bill immediately.

  Add this lifecycle rule to any bucket that accepts multipart uploads:
    aws s3api put-bucket-lifecycle-configuration \\
      --bucket <bucket> \\
      --lifecycle-configuration '{{
        "Rules": [{{
          "Status": "Enabled",
          "Filter": {{}},
          "AbortIncompleteMultipartUpload": {{"DaysAfterInitiation": 7}}
        }}]
      }}'
""")


# ── Summary ────────────────────────────────────────────────────────────────────

def phase_summary(upload_elapsed, upload_throughput):
    section("Summary: Object Storage Concepts")
    print(f"""
  Lab results:
    30MB parallel multipart upload: {upload_elapsed:.2f}s ({upload_throughput:.1f} MB/s on localhost)

  Storage class comparison (AWS S3):
  ┌──────────────────┬──────────┬──────────────────────────────────────────┐
  │ Class            │ Retrieval│ Use case                                 │
  ├──────────────────┼──────────┼──────────────────────────────────────────┤
  │ Standard         │ ms       │ Frequently accessed (images, API assets) │
  │ Standard-IA      │ ms       │ Infrequent access; >30 day min storage   │
  │ One Zone-IA      │ ms       │ Non-critical, recreatable data           │
  │ Glacier Instant  │ ms       │ Archive accessed occasionally            │
  │ Glacier Flexible │ min-hrs  │ Long-term archive, rare retrieval        │
  │ Glacier Deep     │ hrs      │ Compliance, 7+ year retention            │
  └──────────────────┴──────────┴──────────────────────────────────────────┘

  Key design decisions for blob storage at scale:
    1. Private bucket + presigned URLs or CDN (never public bucket)
    2. Multipart threshold: use for files >100 MiB; required >5 GiB
    3. Hash-prefix key naming to avoid partition hotspots
    4. Lifecycle policies: abort incomplete multipart after 7 days,
       transition to IA/Glacier based on access patterns
    5. Versioning: enable for user-generated content / critical data;
       protects against accidental deletion and enables point-in-time restore
    6. CDN in front: CloudFront/CloudFlare absorbs read traffic; S3 serves
       only cache misses, reducing latency from ~50ms to ~5ms at edge

  S3 strong consistency (since December 2020):
    Previously: eventual consistency for overwrite PUTs and DELETEs
    Now: strong read-after-write consistency for all operations
    Impact: no more stale-read bugs after upload; no retry loops needed
    Caveat: "strongly consistent" within a region; cross-region replication
    (S3 CRR) is still asynchronous with minutes-scale replication lag.

  Failure modes to address in interviews:
    - Zombie multipart uploads   -> lifecycle AbortIncompleteMultipartUpload
    - Prefix hotspot throttling  -> hash-prefix key naming
    - Presigned URL leaks        -> short expiry + per-user URLs
    - Non-atomic rename          -> write to final key directly; avoid move
    - Cross-region replication lag -> use S3 CRR with replication metrics

  Next: ../../02-advanced/  (advanced topics)
""")


def main():
    parser = argparse.ArgumentParser(
        description="Blob/Object Storage Lab — MinIO (S3-compatible)"
    )
    parser.add_argument(
        "--show-incomplete",
        action="store_true",
        help="Run Phase 8: demonstrate zombie multipart upload parts",
    )
    args = parser.parse_args()

    section("BLOB/OBJECT STORAGE LAB — MinIO (S3-compatible)")
    print("""
  Services:
    MinIO API     -> http://localhost:9000
    MinIO Console -> http://localhost:9001  (minioadmin / minioadmin)

  Run: docker compose up -d && pip install minio requests
""")

    wait_for_minio()
    phase_create_bucket()
    phase_upload_small()
    elapsed, throughput = phase_multipart_upload()
    phase_presigned_url()
    phase_list_objects()
    phase_etag()
    phase_prefix_partitioning()

    if args.show_incomplete:
        phase_show_incomplete()

    phase_summary(elapsed, throughput)


if __name__ == "__main__":
    main()
