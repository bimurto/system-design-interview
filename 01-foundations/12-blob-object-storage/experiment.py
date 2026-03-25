#!/usr/bin/env python3
"""
Blob/Object Storage Lab — MinIO (S3-compatible)

Prerequisites:
  pip install minio requests
  docker compose up -d  (wait ~10s)

Demonstrates:
  1. Create a bucket
  2. Upload a small text file
  3. Multipart upload of a 30MB file (5MB chunks), timed
  4. Presigned URL generation and download
  5. List objects with metadata
  6. ETag / content-addressable storage: same content → same ETag
"""

import io
import os
import time
import hashlib
import tempfile

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

MINIO_ENDPOINT  = "localhost:9000"
MINIO_ACCESS    = "minioadmin"
MINIO_SECRET    = "minioadmin"
BUCKET_NAME     = "test-bucket"

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
    Bucket  = top-level container (globally unique name in region)
    Key     = full "path" of the object (e.g. "2024/01/photo.jpg")
    Object  = key + data + metadata + ETag

  Namespace is FLAT — "directories" are just key prefixes with "/" convention.
  Bucket names must be DNS-compatible (lowercase, no underscores).
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

    print(f"  Uploaded key:  {key}")
    print(f"  Object size:   {len(content)} bytes")
    print(f"  ETag:          {result.etag}")
    print(f"""
  Key observations:
    - Object key is arbitrary string — "small/hello.txt" is just a key with "/" in it
    - No actual directory was created; it's a flat namespace
    - ETag = MD5 of content (for single-part uploads)
    - Metadata (Content-Type, custom headers) stored alongside object
""")
    return result.etag


# ── Phase 3: Multipart Upload ─────────────────────────────────────────────────

def phase_multipart_upload():
    section("Phase 3: Multipart Upload — 30MB File in 5MB Chunks")
    print("""
  Multipart upload algorithm:
    1. InitiateMultipartUpload  → server returns upload_id
    2. UploadPart(upload_id, part_number, data) × N  → returns ETag per part
    3. CompleteMultipartUpload(upload_id, [part ETags])  → server assembles
    (AbortMultipartUpload if anything fails — cleans up partial data)

  Why multipart?
    - Retry failed parts without re-uploading the entire file
    - Upload parts in parallel (S3 SDK does this automatically)
    - Required for objects > 5GB in S3 (recommended > 100MB)
    - Minimum part size: 5MB (except the last part)
""")

    size_mb = 30
    chunk_mb = 5
    size_bytes = size_mb * 1024 * 1024
    chunk_bytes = chunk_mb * 1024 * 1024
    key = "large/30mb-file.bin"

    # Generate file in memory (repeated pattern for compressibility)
    print(f"  Generating {size_mb}MB file...")
    chunk_pattern = bytes(range(256)) * (chunk_bytes // 256)
    data = chunk_pattern * (size_bytes // len(chunk_pattern))
    data = data[:size_bytes]

    print(f"  Uploading {size_mb}MB as {size_bytes // chunk_bytes} × {chunk_mb}MB parts...")
    t0 = time.perf_counter()

    result = client.put_object(
        BUCKET_NAME,
        key,
        io.BytesIO(data),
        length=size_bytes,
        part_size=chunk_bytes,
        content_type="application/octet-stream",
    )

    elapsed = time.perf_counter() - t0
    throughput_mbps = size_mb / elapsed

    print(f"""
  Upload complete:
    Key:        {key}
    Size:       {size_mb}MB ({size_bytes:,} bytes)
    Time:       {elapsed:.2f}s
    Throughput: {throughput_mbps:.1f} MB/s
    ETag:       {result.etag}

  Note: On localhost, throughput is limited by CPU/memory, not network.
  In production (cloud → S3): parallelism across parts improves throughput
  significantly over single-threaded upload.
""")
    return elapsed, throughput_mbps


# ── Phase 4: Presigned URL ─────────────────────────────────────────────────────

def phase_presigned_url():
    section("Phase 4: Presigned URL — Temporary Access Without Credentials")
    print("""
  Presigned URLs encode:
    - Bucket + key
    - Expiry timestamp
    - HMAC signature (proves it was issued by a valid credential holder)

  Use cases:
    - Allow browser to upload directly to S3 (bypass your server)
    - Share a private file with a customer for a limited time
    - CDN origin: private S3 + CloudFront signs requests

  Security:
    - Anyone with the URL can access the object until expiry
    - Don't embed in public pages or long-lived tokens
    - Use short expiries for sensitive data (minutes, not days)
""")

    key = "small/hello.txt"
    expiry = timedelta(hours=1)

    url = client.presigned_get_object(BUCKET_NAME, key, expires=expiry)
    print(f"  Presigned URL (1 hour expiry):")
    print(f"    {url[:100]}...")

    # Download via requests (no credentials needed)
    resp = requests.get(url, timeout=10)
    print(f"\n  Downloaded via presigned URL:")
    print(f"    HTTP status: {resp.status_code}")
    print(f"    Content:     {resp.text.strip()}")
    print(f"    Size:        {len(resp.content)} bytes")

    print("""
  The presigned URL works with any HTTP client — curl, browser, wget.
  No AWS credentials needed to download. Signature validated by MinIO/S3.
""")


# ── Phase 5: List Objects ──────────────────────────────────────────────────────

def phase_list_objects():
    section("Phase 5: List Objects with Metadata")
    print("""
  S3 listing is prefix-based. Use "/" delimiter to simulate directories.
  Large buckets: use pagination (list_objects returns up to 1000 at a time).
""")

    objects = list(client.list_objects(BUCKET_NAME, recursive=True))
    print(f"  Objects in bucket '{BUCKET_NAME}':\n")
    print(f"  {'Key':<40} {'Size':>12}  {'Last Modified':<25}  ETag")
    print(f"  {'-'*40} {'-'*12}  {'-'*25}  {'-'*32}")
    for obj in objects:
        etag = (obj.etag or "").strip('"')[:32]
        print(f"  {obj.object_name:<40} {obj.size:>12,}  {str(obj.last_modified):<25}  {etag}")

    print(f"\n  Total objects: {len(objects)}")


# ── Phase 6: ETag / Content-Addressable Storage ────────────────────────────────

def phase_etag():
    section("Phase 6: ETag — Content-Addressable Storage")
    print("""
  ETag (Entity Tag) for single-part uploads = MD5 hash of the content.
  Upload the same bytes twice → same ETag. Content defines identity.

  This is the basis of deduplication:
    - S3 itself doesn't deduplicate (you pay for both uploads)
    - But your application can check ETag before uploading
    - Storage systems (Dropbox, Drive) use content hashing to deduplicate

  Also used for:
    - HTTP conditional requests: If-None-Match: "etag" → 304 Not Modified
    - Integrity verification: compare ETag to your local MD5 after download
""")

    content = b"Identical content — upload twice, get same ETag."
    key1 = "dedup/upload-a.txt"
    key2 = "dedup/upload-b.txt"

    r1 = client.put_object(BUCKET_NAME, key1, io.BytesIO(content), length=len(content))
    r2 = client.put_object(BUCKET_NAME, key2, io.BytesIO(content), length=len(content))

    local_md5 = hashlib.md5(content).hexdigest()

    print(f"  Content:  {content.decode()!r}")
    print(f"  Local MD5:         {local_md5}")
    print(f"  ETag of {key1}: {r1.etag.strip(chr(34))}")
    print(f"  ETag of {key2}: {r2.etag.strip(chr(34))}")
    print(f"  ETags match: {r1.etag == r2.etag}")
    print(f"  ETag == MD5: {r1.etag.strip(chr(34)) == local_md5}")
    print("""
  Note: For multipart uploads, ETag = MD5 of concatenated part ETags + "-N"
  (where N = number of parts). Always verify using the SDK's stat_object
  rather than computing MD5 locally for multipart objects.
""")


# ── Summary ────────────────────────────────────────────────────────────────────

def phase_summary(upload_elapsed, upload_throughput):
    section("Summary: Object Storage Concepts")
    print(f"""
  Lab results:
    30MB multipart upload: {upload_elapsed:.2f}s ({upload_throughput:.1f} MB/s on localhost)

  Storage class comparison (AWS S3):
  ┌──────────────────┬──────────┬──────────┬──────────────────────────────────┐
  │ Class            │ Retrieval│ Cost     │ Use case                         │
  ├──────────────────┼──────────┼──────────┼──────────────────────────────────┤
  │ Standard         │ ms       │ $$$      │ Frequently accessed data         │
  │ Standard-IA      │ ms       │ $$       │ Infrequent access (>30 days)     │
  │ One Zone-IA      │ ms       │ $        │ Non-critical, recreatable data   │
  │ Glacier Instant  │ ms       │ $        │ Archive, accessed occasionally   │
  │ Glacier Flexible │ min-hrs  │ ¢        │ Long-term archive                │
  │ Glacier Deep     │ hrs      │ ¢¢       │ Compliance, 7+ year retention    │
  └──────────────────┴──────────┴──────────┴──────────────────────────────────┘

  Key design decisions for blob storage:
    1. Private vs public bucket: always private; use presigned URLs or CDN
    2. Multipart threshold: use for files >100MB; required >5GB
    3. Lifecycle policies: auto-transition to cheaper storage class after N days
    4. Versioning: enable for critical data; protects against accidental deletion
    5. CDN integration: put CloudFront/CloudFlare in front for low-latency reads

  S3 strong consistency (since December 2020):
    Previously: eventual consistency for overwrite PUTs and DELETEs
    Now: strong read-after-write consistency for all operations
    Impact: no more "read your own writes" bugs after upload

  Next: ../  (foundations complete — move to advanced topics)
""")


def main():
    section("BLOB/OBJECT STORAGE LAB — MinIO (S3-compatible)")
    print("""
  Services:
    MinIO API     → http://localhost:9000
    MinIO Console → http://localhost:9001  (minioadmin / minioadmin)

  Run: docker compose up -d && pip install minio requests
""")

    wait_for_minio()
    phase_create_bucket()
    phase_upload_small()
    elapsed, throughput = phase_multipart_upload()
    phase_presigned_url()
    phase_list_objects()
    phase_etag()
    phase_summary(elapsed, throughput)


if __name__ == "__main__":
    main()
