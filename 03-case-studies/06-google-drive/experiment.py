#!/usr/bin/env python3
"""
Google Drive Lab — experiment.py

What this demonstrates:
  1. Upload file in 4MB chunks to MinIO (multipart upload)
  2. Modify 1 byte → re-upload: show only 1 changed chunk uploaded (delta sync)
  3. Two clients upload identical file → content hash deduplication
  4. Resumable upload: track received chunks in Redis; resume after simulated failure
  5. Simulate conflict: both clients modify same version → conflict copy
  6. Show version history in Postgres (file_id, version, chunk_hashes, created_at)

Run:
  docker compose up -d
  # Wait ~20s for services
  python experiment.py
"""

import hashlib
import io
import os
import random
import time
import urllib.request
import uuid

import psycopg2
import redis

# ── Config ────────────────────────────────────────────────────────────────────

MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT",  "http://localhost:9002")
MINIO_ACCESS   = os.getenv("MINIO_ACCESS",    "minioadmin")
MINIO_SECRET   = os.getenv("MINIO_SECRET",    "minioadmin")
MINIO_BUCKET   = "gdrive-chunks"
DB_URL         = os.getenv("DATABASE_URL", "postgresql://app:secret@localhost:5432/gdrive")
REDIS_URL      = os.getenv("REDIS_URL",    "redis://localhost:6379")

CHUNK_SIZE = 4 * 1024 * 1024  # 4MB chunks


# ── Helpers ───────────────────────────────────────────────────────────────────

def section(title):
    print(f"\n{'=' * 62}")
    print(f"  {title}")
    print("=" * 62)


def install_packages():
    import subprocess, sys
    pkgs = ["minio", "psycopg2-binary", "redis"]
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet"] + pkgs)


def wait_for_service(url, max_wait=60, label="service"):
    print(f"  Waiting for {label} ...")
    for i in range(max_wait):
        try:
            urllib.request.urlopen(url, timeout=3)
            print(f"  {label} ready after {i+1}s")
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError(f"{label} did not start within {max_wait}s")


def wait_for_postgres(dsn, max_wait=60, label="Postgres"):
    print(f"  Waiting for {label} ...")
    for i in range(max_wait):
        try:
            conn = psycopg2.connect(dsn)
            conn.close()
            print(f"  {label} ready after {i+1}s")
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError(f"{label} did not start within {max_wait}s")


def wait_for_redis(url, max_wait=60, label="Redis"):
    print(f"  Waiting for {label} ...")
    for i in range(max_wait):
        try:
            r = redis.from_url(url, decode_responses=True, socket_connect_timeout=3)
            r.ping()
            print(f"  {label} ready after {i+1}s")
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError(f"{label} did not start within {max_wait}s")


def get_db():
    return psycopg2.connect(DB_URL)


def get_redis():
    return redis.from_url(REDIS_URL, decode_responses=True)


def get_minio():
    from minio import Minio
    return Minio(
        MINIO_ENDPOINT.replace("http://", ""),
        access_key=MINIO_ACCESS,
        secret_key=MINIO_SECRET,
        secure=False,
    )


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def chunk_file(data: bytes, chunk_size: int = CHUNK_SIZE) -> list[tuple[int, bytes, str]]:
    """Split data into (chunk_index, chunk_bytes, sha256_hash) tuples."""
    chunks = []
    for i in range(0, len(data), chunk_size):
        chunk = data[i:i + chunk_size]
        chunks.append((i // chunk_size, chunk, sha256(chunk)))
    return chunks


def total_bytes(chunks: list[tuple[int, bytes, str]]) -> int:
    """Sum of actual chunk byte lengths (last chunk may be smaller than CHUNK_SIZE)."""
    return sum(len(c) for _, c, _ in chunks)


# ── Schema ────────────────────────────────────────────────────────────────────

def init_db(conn):
    with conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS files (
                    id          BIGSERIAL PRIMARY KEY,
                    user_id     TEXT NOT NULL,
                    filename    TEXT NOT NULL,
                    file_hash   TEXT NOT NULL,
                    size_bytes  BIGINT NOT NULL,
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS file_versions (
                    id          BIGSERIAL PRIMARY KEY,
                    file_id     BIGINT REFERENCES files(id),
                    version     INT NOT NULL,
                    chunk_hashes TEXT[] NOT NULL,
                    created_at  TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE (file_id, version)
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    hash        TEXT PRIMARY KEY,
                    object_key  TEXT NOT NULL,
                    size_bytes  BIGINT NOT NULL,
                    ref_count   INT DEFAULT 1,
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_files_user ON files(user_id);
                CREATE INDEX IF NOT EXISTS idx_fv_file ON file_versions(file_id, version);
            """)


def ensure_bucket(mc):
    if not mc.bucket_exists(MINIO_BUCKET):
        mc.make_bucket(MINIO_BUCKET)
        print(f"  Created MinIO bucket: {MINIO_BUCKET}")


# ── Upload helpers ────────────────────────────────────────────────────────────

def upload_chunks(mc, conn, file_data: bytes, user_id: str, filename: str):
    """
    Upload file in chunks. Skip chunks already in the store (deduplication).
    Returns (file_id, version, uploaded_count, skipped_count, uploaded_bytes).
    """
    chunks = chunk_file(file_data)
    file_hash = sha256(file_data)
    uploaded = 0
    skipped = 0
    uploaded_bytes = 0
    chunk_hashes = []

    for idx, chunk_bytes, chunk_hash in chunks:
        chunk_hashes.append(chunk_hash)
        object_key = f"chunks/{chunk_hash[:2]}/{chunk_hash}"

        with conn.cursor() as cur:
            cur.execute("SELECT hash FROM chunks WHERE hash = %s", (chunk_hash,))
            exists = cur.fetchone()

        if exists:
            # Chunk already in object store — increment ref count
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE chunks SET ref_count = ref_count + 1 WHERE hash = %s",
                        (chunk_hash,),
                    )
            skipped += 1
        else:
            # New chunk — upload to MinIO
            mc.put_object(
                MINIO_BUCKET,
                object_key,
                io.BytesIO(chunk_bytes),
                length=len(chunk_bytes),
                content_type="application/octet-stream",
            )
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO chunks (hash, object_key, size_bytes)
                           VALUES (%s, %s, %s) ON CONFLICT (hash) DO UPDATE
                           SET ref_count = chunks.ref_count + 1""",
                        (chunk_hash, object_key, len(chunk_bytes)),
                    )
            uploaded += 1
            uploaded_bytes += len(chunk_bytes)

    # Store file metadata + version
    with conn:
        with conn.cursor() as cur:
            # Check if file already exists for this user+filename
            cur.execute(
                "SELECT id FROM files WHERE user_id=%s AND filename=%s",
                (user_id, filename),
            )
            row = cur.fetchone()
            if row:
                file_id = row[0]
                cur.execute(
                    "SELECT MAX(version) FROM file_versions WHERE file_id=%s",
                    (file_id,),
                )
                max_v = cur.fetchone()[0] or 0
                version = max_v + 1
                cur.execute(
                    "UPDATE files SET file_hash=%s, size_bytes=%s WHERE id=%s",
                    (file_hash, len(file_data), file_id),
                )
            else:
                cur.execute(
                    "INSERT INTO files (user_id, filename, file_hash, size_bytes) VALUES (%s,%s,%s,%s) RETURNING id",
                    (user_id, filename, file_hash, len(file_data)),
                )
                file_id = cur.fetchone()[0]
                version = 1

            cur.execute(
                "INSERT INTO file_versions (file_id, version, chunk_hashes) VALUES (%s,%s,%s)",
                (file_id, version, chunk_hashes),
            )

    return file_id, version, uploaded, skipped, uploaded_bytes


# ── Phase 1: Chunked upload ───────────────────────────────────────────────────

def phase1_chunked_upload(conn, mc):
    section("Phase 1: Chunked Upload — 4MB Blocks")

    random.seed(42)
    # Create a 10MB file (2.5 chunks at 4MB each → 3 chunks)
    file_size = 10 * 1024 * 1024
    file_data = bytes(random.getrandbits(8) for _ in range(file_size))
    file_hash = sha256(file_data)

    print(f"\n  File: 10MB, SHA-256: {file_hash[:16]}...")
    chunks = chunk_file(file_data)
    print(f"  Chunk size: 4MB → {len(chunks)} chunks")
    for idx, chunk_bytes, chunk_hash in chunks:
        print(f"    chunk[{idx}]: {len(chunk_bytes)//1024}KB  hash: {chunk_hash[:16]}...")

    start = time.perf_counter()
    file_id, version, uploaded, skipped, uploaded_bytes = upload_chunks(
        mc, conn, file_data, "alice", "document.pdf"
    )
    elapsed_ms = (time.perf_counter() - start) * 1000

    print(f"\n  Upload complete: {elapsed_ms:.0f}ms")
    print(f"    File ID:          {file_id}")
    print(f"    Version:          {version}")
    print(f"    Chunks uploaded:  {uploaded}")
    print(f"    Chunks skipped:   {skipped}")
    print(f"    Bytes transferred:{uploaded_bytes // 1024}KB")

    print(f"""
  Chunked upload advantages:
    Resume on failure: if upload fails at chunk 2 of 100, restart from chunk 2
    Parallel upload: chunks can be uploaded concurrently (not shown here)
    Deduplication: skip chunks already in the store (see Phase 3)

  Resumable upload protocol:
    1. Client: POST /upload/initiate → upload_id  (stored in Redis)
    2. Client: PUT /upload/{{upload_id}}/chunk/{{n}}  (can retry each chunk independently)
    3. Client: POST /upload/{{upload_id}}/complete → file_id
    Server tracks which chunks are received in Redis: SADD upload:{{id}} {{chunk_n}}
    On reconnect: SMEMBERS upload:{{id}} tells client which chunks to skip
""")
    return file_data, file_id


# ── Phase 2: Delta sync — modify 1 byte ──────────────────────────────────────

def phase2_delta_sync(conn, mc, original_data: bytes, file_id: int):
    section("Phase 2: Delta Sync — Modify 1 Byte, Re-upload")

    # Modify 1 byte in the middle of chunk 1 (so only chunk 1 changes)
    modified_data = bytearray(original_data)
    modify_pos = CHUNK_SIZE + 100  # inside chunk 1
    modified_data[modify_pos] = (modified_data[modify_pos] + 1) % 256
    modified_data = bytes(modified_data)

    original_chunks  = chunk_file(original_data)
    modified_chunks  = chunk_file(modified_data)
    total_original_bytes = total_bytes(original_chunks)

    print(f"\n  Original file: {len(original_data)//1024}KB")
    print(f"  Modified: changed 1 byte at position {modify_pos:,} (inside chunk 1)")
    print(f"\n  Chunk comparison:")
    print(f"  {'Chunk':>7}  {'Size':>7}  {'Original hash':>16}  {'New hash':>16}  {'Changed?':>9}")
    print(f"  {'-'*7}  {'-'*7}  {'-'*16}  {'-'*16}  {'-'*9}")
    changed = []
    for (idx, orig_bytes, oh), (_, mod_bytes, nh) in zip(original_chunks, modified_chunks):
        changed_flag = "YES <<<" if oh != nh else "no"
        if oh != nh:
            changed.append(idx)
        print(f"  {idx:>7}  {len(orig_bytes)//1024:>5}KB  {oh[:16]:>16}  {nh[:16]:>16}  {changed_flag:>9}")

    start = time.perf_counter()
    file_id2, version2, uploaded, skipped, uploaded_bytes = upload_chunks(
        mc, conn, modified_data, "alice", "document.pdf"
    )
    elapsed_ms = (time.perf_counter() - start) * 1000

    print(f"\n  Re-upload: {elapsed_ms:.0f}ms")
    print(f"    Version:          {version2}")
    print(f"    Chunks uploaded:  {uploaded}  (only changed chunks!)")
    print(f"    Chunks skipped:   {skipped}  (unchanged, already in store)")
    print(f"    Bytes transferred:{uploaded_bytes // 1024}KB vs {total_original_bytes//1024}KB full re-upload")
    if total_original_bytes > 0:
        saved_pct = (1 - uploaded_bytes / total_original_bytes) * 100
        print(f"    Bandwidth saved:  {saved_pct:.0f}%")

    print(f"""
  Rsync-style rolling hash (production implementation):
    Dropbox and Google Drive use a rolling hash (Rabin fingerprint or similar)
    to find changed regions at BYTE granularity, not just chunk boundaries.
    A 1-byte INSERTION near a chunk boundary shifts all subsequent fixed-size
    chunk boundaries, making every downstream chunk appear "changed" even if
    the content is identical.

    Content-defined chunking (CDC) solves this:
      - Roll a 64-byte Rabin hash window across the file
      - Cut a chunk boundary when hash & MASK == PATTERN
      - Chunk boundaries are content-driven, not position-driven
      - An insertion only creates new boundaries near the insertion site;
        downstream content retains the same boundaries and hashes

    This lab uses fixed-size chunks for clarity.
    Production: variable-size CDC chunks (avg 4MB, min 512KB, max 16MB)
""")
    return modified_data


# ── Phase 3: Content deduplication ───────────────────────────────────────────

def phase3_deduplication(conn, mc, original_data: bytes):
    section("Phase 3: Content Deduplication — Two Users, One File")

    print("""
  Scenario: Alice and Bob both upload the same 10MB file.
  With content-hash deduplication, the bytes are stored only once.
  Bob's upload transfers 0 bytes to object storage — only metadata is written.
""")

    # Bob uploads the same file Alice already uploaded (original_data)
    start = time.perf_counter()
    file_id_bob, version_bob, uploaded_bob, skipped_bob, uploaded_bytes_bob = upload_chunks(
        mc, conn, original_data, "bob", "document.pdf"
    )
    elapsed_ms = (time.perf_counter() - start) * 1000

    print(f"  Alice already uploaded 'document.pdf': {len(original_data)//1024}KB stored")
    print(f"  Bob uploads identical file: {elapsed_ms:.0f}ms")
    print(f"    Chunks uploaded (new bytes to object store): {uploaded_bob}")
    print(f"    Chunks skipped (already in store):           {skipped_bob}")
    print(f"    Actual bytes transferred to MinIO:           {uploaded_bytes_bob} bytes")

    # Show files metadata
    with conn.cursor() as cur:
        cur.execute(
            "SELECT user_id, filename, file_hash, size_bytes FROM files ORDER BY id",
        )
        rows = cur.fetchall()
    with conn.cursor() as cur:
        cur.execute("SELECT hash, ref_count, size_bytes FROM chunks ORDER BY ref_count DESC LIMIT 5")
        chunk_rows = cur.fetchall()

    print(f"\n  Files table (two separate metadata rows, same underlying chunks):")
    print(f"  {'User':<8}  {'Filename':<20}  {'Hash prefix':>12}  {'Size':>10}")
    print(f"  {'-'*8}  {'-'*20}  {'-'*12}  {'-'*10}")
    for user, fname, fhash, size in rows:
        print(f"  {user:<8}  {fname:<20}  {fhash[:12]:>12}  {size//1024:>8}KB")

    print(f"\n  Chunks table (ref_count > 1 means shared across users/versions):")
    print(f"  {'Hash prefix':>14}  {'Ref Count':>10}  {'Size':>8}  {'Shared by'}")
    print(f"  {'-'*14}  {'-'*10}  {'-'*8}  {'-'*12}")
    for chunk_hash, ref_count, size_bytes in chunk_rows:
        shared = f"{ref_count} refs" if ref_count > 1 else "1 ref"
        print(f"  {chunk_hash[:14]:>14}  {ref_count:>10}  {size_bytes//1024:>6}KB  {shared}")

    # Storage savings based on Alice's file only (original_data)
    # "without dedup" = Alice + Bob storing separate copies
    files_count = sum(1 for u, _, _, _ in rows if u in ("alice", "bob") and _ == "document.pdf")
    storage_without = max(files_count, 2) * len(original_data)
    with conn.cursor() as cur:
        cur.execute("SELECT SUM(size_bytes) FROM chunks")
        actual_storage = cur.fetchone()[0] or 0

    print(f"\n  Storage savings (Alice + Bob, same file):")
    print(f"    Without dedup: {storage_without//1024}KB (two separate copies)")
    print(f"    With dedup:    {actual_storage//1024}KB  (one copy, two metadata pointers)")
    if storage_without > 0:
        print(f"    Savings:       {(1 - actual_storage/storage_without)*100:.0f}%")

    print(f"""
  Dropbox "Magic Pocket" deduplication (2016):
    Dropbox found ~30% of all uploaded data already existed in the store.
    Hash-based dedup (SHA-256 per chunk) eliminates redundant storage.

  Privacy trade-off — cross-user dedup leaks membership:
    Knowing the hash of a file lets you check if it exists in the store
    (hash-proof-of-work attack, demonstrated in Dropbox research, 2011).
    Mitigations:
      - Convergent encryption: encrypt chunk with key = HMAC(hash, user_secret)
        Two users with identical content get the same ciphertext → still dedup.
        A user without the file cannot decrypt even if they know the hash.
      - Per-user dedup only: no cross-user sharing. Simpler, more private,
        but forfeits the 20-30% cross-user savings.
    Google Drive stores per-user metadata references; the underlying GCS
    object storage deduplicates at the infrastructure layer (opaque to the app).
""")
    return file_id_bob


# ── Phase 4: Resumable Upload via Redis ──────────────────────────────────────

def phase4_resumable_upload(conn, mc, r):
    section("Phase 4: Resumable Upload — Redis Session Tracking")

    print("""
  Scenario: A 3-chunk file upload fails after chunk 0 is sent.
  The client reconnects and resumes from chunk 1, not the beginning.

  Protocol:
    POST /upload/initiate       → server: HSET upload:{id} total_chunks 3
    PUT  /upload/{id}/chunk/{n} → server: SADD upload:{id}:received {n}
    GET  /upload/{id}/status    → server: SMEMBERS upload:{id}:received
    PUT  /upload/{id}/chunk/{n} → client retries only missing chunks
    POST /upload/{id}/complete  → server: assemble file_id, clear session keys
""")

    random.seed(77)
    # 3-chunk file (~12MB)
    file_data = bytes(random.getrandbits(8) for _ in range(3 * CHUNK_SIZE))
    chunks = chunk_file(file_data)
    upload_id = str(uuid.uuid4())[:8]
    total_chunks = len(chunks)

    # Server-side: initiate upload session in Redis
    session_key    = f"upload:{upload_id}"
    received_key   = f"upload:{upload_id}:received"
    r.hset(session_key, mapping={
        "total_chunks": total_chunks,
        "filename":     "large_video.mp4",
        "user_id":      "carol",
        "started_at":   int(time.time()),
    })
    r.expire(session_key, 86400)   # session lives 24h
    r.expire(received_key, 86400)

    print(f"  Upload session created: upload_id={upload_id}, total_chunks={total_chunks}")
    print(f"  Redis key: {session_key}")
    print(f"    {r.hgetall(session_key)}\n")

    # Simulate: upload chunk 0 only, then "connection drops"
    idx, chunk_bytes, chunk_hash = chunks[0]
    object_key = f"chunks/{chunk_hash[:2]}/{chunk_hash}"
    mc.put_object(MINIO_BUCKET, object_key, io.BytesIO(chunk_bytes),
                  length=len(chunk_bytes), content_type="application/octet-stream")
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO chunks (hash, object_key, size_bytes)
                   VALUES (%s, %s, %s) ON CONFLICT (hash) DO UPDATE
                   SET ref_count = chunks.ref_count + 1""",
                (chunk_hash, object_key, len(chunk_bytes)),
            )
    r.sadd(received_key, 0)
    print(f"  Uploaded chunk 0 ({len(chunk_bytes)//1024}KB) → hash: {chunk_hash[:16]}...")
    print(f"  [Connection drops after chunk 0]\n")

    # Client reconnects — queries which chunks are already received
    received = {int(x) for x in r.smembers(received_key)}
    missing  = [i for i in range(total_chunks) if i not in received]
    print(f"  Client reconnects. Queries GET /upload/{upload_id}/status")
    print(f"    Redis SMEMBERS {received_key}: {sorted(received)}")
    print(f"    Received chunks: {sorted(received)}")
    print(f"    Missing chunks:  {missing}  ← only these will be re-uploaded\n")

    # Resume: upload only missing chunks
    resumed_bytes = 0
    for i in missing:
        _, chunk_bytes_i, chunk_hash_i = chunks[i]
        object_key_i = f"chunks/{chunk_hash_i[:2]}/{chunk_hash_i}"
        mc.put_object(MINIO_BUCKET, object_key_i, io.BytesIO(chunk_bytes_i),
                      length=len(chunk_bytes_i), content_type="application/octet-stream")
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO chunks (hash, object_key, size_bytes)
                       VALUES (%s, %s, %s) ON CONFLICT (hash) DO UPDATE
                       SET ref_count = chunks.ref_count + 1""",
                    (chunk_hash_i, object_key_i, len(chunk_bytes_i)),
                )
        r.sadd(received_key, i)
        resumed_bytes += len(chunk_bytes_i)
        print(f"  Resumed: uploaded chunk {i} ({len(chunk_bytes_i)//1024}KB)")

    # Complete: assemble file record
    all_hashes = [c[2] for c in chunks]
    file_hash  = sha256(file_data)
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO files (user_id, filename, file_hash, size_bytes) VALUES (%s,%s,%s,%s) RETURNING id",
                ("carol", "large_video.mp4", file_hash, len(file_data)),
            )
            file_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO file_versions (file_id, version, chunk_hashes) VALUES (%s,%s,%s)",
                (file_id, 1, all_hashes),
            )

    # Clean up Redis session
    r.delete(session_key, received_key)
    pre_failure_bytes = len(chunks[0][1])
    # On a full restart, the client would re-upload ALL chunks (including chunk 0 again).
    # With resumable upload, chunk 0 is NOT re-uploaded on reconnect — it was already received.
    saved_bytes = pre_failure_bytes  # chunk 0 was not re-sent on resume
    print(f"\n  Upload complete. File ID: {file_id}, Version: 1")
    print(f"  Uploaded before failure: {pre_failure_bytes//1024}KB  (chunk 0)")
    print(f"  Uploaded on resume:      {resumed_bytes//1024}KB  (chunks 1 and 2)")
    print(f"  Total transferred:       {(pre_failure_bytes + resumed_bytes)//1024}KB  (== file size)")
    print(f"  Saved vs full restart:   {saved_bytes//1024}KB  (chunk 0 not re-sent on reconnect)")
    print(f"  Redis session keys deleted (upload complete)\n")

    print(f"""
  Why Redis for session state?
    Upload sessions are ephemeral (hours, not years) → no need for durable SQL.
    SADD/SMEMBERS are O(1) per chunk — cheap at scale.
    Redis TTL auto-expires abandoned sessions (24h) — no cleanup job needed.
    If the API server crashes mid-upload, the session survives in Redis; any
    server in the cluster can resume serving the client on reconnect.
""")


# ── Phase 5: Conflict resolution ─────────────────────────────────────────────

def phase5_conflict(conn, mc, original_data: bytes):
    section("Phase 5: Conflict Resolution — Simultaneous Edits")

    print("""
  Scenario: Alice and Bob both download version 1 of a shared document.
  Both make different edits offline. Both try to save simultaneously.

  Optimistic locking: each save includes the base_version the client edited from.
  Server rejects saves where base_version < current_version.
  Google Drive's approach: create a "conflict copy" rather than last-write-wins.
  The user must manually merge the conflicting versions.
""")

    # Both Alice and Bob start from the same shared file (version 1)
    # First: create a shared baseline file under a shared filename
    _, _, _, _, _ = upload_chunks(mc, conn, original_data, "alice", "shared_doc.pdf")

    # Fetch the file_id for the shared doc (Alice owns it)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM files WHERE user_id='alice' AND filename='shared_doc.pdf'",
        )
        shared_file_id = cur.fetchone()[0]

    # Get current version (should be 1)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT MAX(version) FROM file_versions WHERE file_id=%s",
            (shared_file_id,),
        )
        base_version = cur.fetchone()[0] or 1

    print(f"  Shared file created: shared_doc.pdf (file_id={shared_file_id}, version={base_version})")
    print(f"  Alice and Bob both download version {base_version} and go offline to edit.\n")

    # Alice's edit: modify bytes in chunk 0
    alice_data = bytearray(original_data)
    alice_data[100] = 0xFF
    alice_data = bytes(alice_data)

    # Bob's edit: modify bytes in chunk 2
    bob_data = bytearray(original_data)
    if len(bob_data) > 2 * CHUNK_SIZE + 200:
        bob_data[2 * CHUNK_SIZE + 200] = 0xAA
    bob_data = bytes(bob_data)

    # Alice saves first — succeeds because base_version == current_version
    with conn.cursor() as cur:
        cur.execute(
            "SELECT MAX(version) FROM file_versions WHERE file_id=%s",
            (shared_file_id,),
        )
        current_version = cur.fetchone()[0] or 1

    alice_base = base_version
    if alice_base == current_version:
        # Alice's save accepted — write new version
        chunks_a = chunk_file(alice_data)
        hashes_a = [h for _, _, h in chunks_a]
        for _, chunk_bytes, chunk_hash in chunks_a:
            object_key = f"chunks/{chunk_hash[:2]}/{chunk_hash}"
            with conn.cursor() as cur:
                cur.execute("SELECT hash FROM chunks WHERE hash=%s", (chunk_hash,))
                if not cur.fetchone():
                    mc.put_object(MINIO_BUCKET, object_key, io.BytesIO(chunk_bytes),
                                  length=len(chunk_bytes), content_type="application/octet-stream")
                    with conn:
                        with conn.cursor() as cur2:
                            cur2.execute(
                                """INSERT INTO chunks (hash, object_key, size_bytes)
                                   VALUES (%s,%s,%s) ON CONFLICT (hash) DO UPDATE
                                   SET ref_count = chunks.ref_count + 1""",
                                (chunk_hash, object_key, len(chunk_bytes)),
                            )
        alice_version = current_version + 1
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE files SET file_hash=%s, size_bytes=%s WHERE id=%s",
                    (sha256(alice_data), len(alice_data), shared_file_id),
                )
                cur.execute(
                    "INSERT INTO file_versions (file_id, version, chunk_hashes) VALUES (%s,%s,%s)",
                    (shared_file_id, alice_version, hashes_a),
                )
        print(f"  Alice saves (base_version={alice_base}, current={current_version}) → ACCEPTED as version {alice_version}")
    else:
        print(f"  Alice's save would have been REJECTED (concurrent edit detected)")

    # Bob tries to save — conflict: base_version (1) < current_version (2)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT MAX(version) FROM file_versions WHERE file_id=%s",
            (shared_file_id,),
        )
        current_version_now = cur.fetchone()[0] or 1

    bob_base = base_version   # Bob still thinks version 1 is latest
    has_conflict = bob_base < current_version_now

    print(f"  Bob tries to save  (base_version={bob_base}, current={current_version_now}) → CONFLICT DETECTED={has_conflict}")

    if has_conflict:
        # Create a conflict copy under a different filename
        conflict_filename = f"shared_doc (Bob's conflicted copy {time.strftime('%Y-%m-%d')}).pdf"
        _, bob_v, _, _, _ = upload_chunks(mc, conn, bob_data, "bob", conflict_filename)
        print(f"\n  Google Drive creates conflict copy: '{conflict_filename}'")
        print(f"    Bob's edits preserved as a separate file (version {bob_v})")
        print(f"    Alice's version {alice_version} remains the canonical version")

    # Show version history across all affected files
    with conn.cursor() as cur:
        cur.execute("""
            SELECT fv.version, fv.created_at, f.user_id, f.filename, fv.chunk_hashes
            FROM file_versions fv
            JOIN files f ON f.id = fv.file_id
            WHERE f.filename LIKE 'shared_doc%'
            ORDER BY fv.created_at
        """)
        rows = cur.fetchall()

    print(f"\n  Version history (shared_doc family):")
    print(f"  {'v':>3}  {'User':<8}  {'Filename':<46}  {'Chunks'}")
    print(f"  {'-'*3}  {'-'*8}  {'-'*46}  {'-'*10}")
    for version, created_at, user, fname, chunk_hashes in rows:
        print(f"  v{version:<2}  {user:<8}  {fname[:46]:<46}  {len(chunk_hashes)} chunks")

    print(f"""
  Conflict resolution strategies compared:

  ┌──────────────────────────┬───────────────────────────────────────────┐
  │ Strategy                 │ Behaviour                                 │
  ├──────────────────────────┼───────────────────────────────────────────┤
  │ Last-write-wins          │ Bob's save overwrites Alice's silently    │
  │ First-write-wins (OCC)   │ Bob's save rejected; he must re-merge    │
  │ Conflict copy (GDrive)   │ Both versions kept; user merges manually  │
  │ Operational Transform    │ Google Docs: merge at character level     │
  │ CRDT                     │ Notion/Figma: conflict-free data types    │
  └──────────────────────────┴───────────────────────────────────────────┘

  Why conflict copy for binary files (PDFs, Excel, images)?
    Byte-level merge is undefined for binary formats — you cannot OT-merge
    two JPEG edits. Creating a conflict copy is the safest option.
    Google Docs uses OT because the data model (text operations) is mergeable.
    Figma/Notion use CRDTs for structured collaborative data without a central
    OT server, at the cost of higher storage overhead and eventual consistency.
""")


# ── Phase 6: Version history ──────────────────────────────────────────────────

def phase6_version_history(conn):
    section("Phase 6: Version History in Postgres")

    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                f.user_id,
                f.filename,
                fv.version,
                fv.created_at,
                array_length(fv.chunk_hashes, 1) as n_chunks,
                fv.chunk_hashes[1] as first_chunk_hash
            FROM file_versions fv
            JOIN files f ON f.id = fv.file_id
            ORDER BY f.filename, fv.version
        """)
        rows = cur.fetchall()

    print(f"\n  All file versions stored:\n")
    print(f"  {'User':<8}  {'Filename':<38}  {'v':>3}  {'Chunks':>7}  {'First chunk hash'}")
    print(f"  {'-'*8}  {'-'*38}  {'-'*3}  {'-'*7}  {'-'*18}")
    for user, fname, version, created_at, n_chunks, first_hash in rows:
        print(f"  {user:<8}  {fname[:38]:<38}  {version:>3}  {n_chunks:>7}  {(first_hash or '')[:18]}")

    # Reconstruct diff between versions 1 and 2 of document.pdf
    with conn.cursor() as cur:
        cur.execute("""
            SELECT fv.version, fv.chunk_hashes
            FROM file_versions fv
            JOIN files f ON f.id = fv.file_id
            WHERE f.filename = 'document.pdf'
            ORDER BY fv.version
        """)
        versions = cur.fetchall()

    if len(versions) >= 2:
        v1_hashes = versions[0][1]
        v2_hashes = versions[1][1]
        changed_chunks   = [i for i, (h1, h2) in enumerate(zip(v1_hashes, v2_hashes)) if h1 != h2]
        unchanged_chunks = [i for i, (h1, h2) in enumerate(zip(v1_hashes, v2_hashes)) if h1 == h2]

        print(f"\n  Diff: document.pdf  v{versions[0][0]} → v{versions[1][0]}")
        print(f"    Changed chunks:   {changed_chunks}   ← must download to restore v2")
        print(f"    Unchanged chunks: {unchanged_chunks} ← already on device, skip download")
        print(f"\n  To restore v1: fetch chunks by v1's chunk_hashes from object store")
        print(f"    Only chunks NOT on the client need to be fetched — identical to delta sync")

    # Show chunk sharing across versions
    with conn.cursor() as cur:
        cur.execute("""
            SELECT hash, ref_count, size_bytes
            FROM chunks
            ORDER BY ref_count DESC
            LIMIT 10
        """)
        chunk_rows = cur.fetchall()

    print(f"\n  Chunk reference counts (dedup across versions AND users):")
    print(f"  {'Hash prefix':>16}  {'Refs':>5}  {'Size':>8}")
    print(f"  {'-'*16}  {'-'*5}  {'-'*8}")
    for h, ref_count, size in chunk_rows:
        print(f"  {h[:16]:>16}  {ref_count:>5}  {size//1024:>6}KB")

    print(f"""
  Version retention policy (Google Drive):
    Free tier:      last 100 revisions or 30 days, whichever comes first
    Workspace:      unlimited revisions for 30 days → daily snapshots for
                    another 30 days → weekly snapshots for 1 year

  Storage cost of versioning (why it's nearly free with dedup):
    If 90% of chunks are unchanged between edits (typical for text documents),
    each new version adds only 10% × file_size of unique new chunks.
    After 10 edits:  ~2× total storage (not 10×).
    After 100 edits: ~11× total storage (not 100×).
    Content-hash dedup makes version history much cheaper than naive copy-on-write.

  Garbage collection of orphaned chunks:
    When all file_versions referencing a chunk are deleted, ref_count → 0.
    A background GC job (runs nightly) queries:
      SELECT hash FROM chunks WHERE ref_count = 0;
    then deletes from object storage and the chunks table.
    Chunks are never deleted synchronously on version delete — avoids
    a race where a concurrent upload of the same chunk is in-flight.
""")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    section("GOOGLE DRIVE LAB")
    print("""
  Architecture:
    Upload → Chunk (4MB) → Hash each chunk → Upload new chunks only (MinIO)
    Metadata → Postgres (files, file_versions, chunks tables)
    Sessions → Redis (resumable upload tracking, TTL-based expiry)
    Deduplication: chunks table maps hash → object key + ref_count
    Delta sync: on re-upload, skip chunks with matching hash
""")

    install_packages()
    wait_for_service(f"{MINIO_ENDPOINT}/minio/health/live", label="MinIO")
    wait_for_postgres(DB_URL, label="Postgres")
    wait_for_redis(REDIS_URL, label="Redis")

    conn = get_db()
    r    = get_redis()
    mc   = get_minio()

    init_db(conn)
    ensure_bucket(mc)

    file_data, file_id = phase1_chunked_upload(conn, mc)
    modified_data      = phase2_delta_sync(conn, mc, file_data, file_id)
    phase3_deduplication(conn, mc, file_data)
    phase4_resumable_upload(conn, mc, r)
    phase5_conflict(conn, mc, file_data)
    phase6_version_history(conn)

    conn.close()

    section("Lab Complete")
    print("""
  Summary:
  - Chunked upload: resume on failure, parallel upload, deduplication unit
  - Delta sync: only upload changed 4MB chunks → save 90%+ bandwidth on small edits
  - Content deduplication: SHA-256 per chunk, store once, multiple metadata pointers
  - Resumable upload: Redis tracks received chunks per session; client resumes from gap
  - Conflict copy: Google Drive saves both versions when concurrent edits detected
  - Version history: chunk_hashes array per version; diff is set difference of hashes
  - GC: ref_count=0 chunks cleaned up asynchronously to avoid race conditions

  Next: explore 07-web-crawler/ or review the Interview Checklists
""")


if __name__ == "__main__":
    main()
