#!/usr/bin/env python3
"""
Google Drive Lab — experiment.py

What this demonstrates:
  1. Upload file in 4MB chunks to MinIO (multipart upload)
  2. Modify 1 byte → re-upload: show only 1 changed chunk uploaded (delta sync)
  3. Two clients upload identical file → content hash deduplication
  4. Simulate conflict: both clients modify same version → conflict copy
  5. Show version history in Postgres (file_id, version, chunk_hashes, created_at)

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
    Returns (file_id, version, uploaded_count, skipped_count).
    """
    chunks = chunk_file(file_data)
    file_hash = sha256(file_data)
    uploaded = 0
    skipped = 0
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

    return file_id, version, uploaded, skipped


# ── Phase 1: Chunked upload ───────────────────────────────────────────────────

def phase1_chunked_upload(conn, mc):
    section("Phase 1: Chunked Upload — 4MB Blocks")

    random.seed(42)
    # Create a 10MB file (2.5 chunks at 4MB each → 3 chunks)
    file_size = 10 * 1024 * 1024
    file_data = bytes(random.getrandbits(8) for _ in range(file_size))
    file_hash = sha256(file_data)

    print(f"\n  File: 10MB, SHA-256: {file_hash[:16]}...")
    print(f"  Chunk size: 4MB → {len(chunk_file(file_data))} chunks")

    start = time.perf_counter()
    file_id, version, uploaded, skipped = upload_chunks(
        mc, conn, file_data, "alice", "document.pdf"
    )
    elapsed_ms = (time.perf_counter() - start) * 1000

    print(f"\n  Upload complete: {elapsed_ms:.0f}ms")
    print(f"    File ID:    {file_id}")
    print(f"    Version:    {version}")
    print(f"    Chunks uploaded:  {uploaded}")
    print(f"    Chunks skipped:   {skipped}")

    print(f"""
  Chunked upload advantages:
    Resume on failure: if upload fails at chunk 2 of 100, restart from chunk 2
    Parallel upload: chunks can be uploaded concurrently (not shown here)
    Deduplication: skip chunks already in the store (see Phase 3)

  Resumable upload protocol:
    1. Client: POST /upload/initiate → upload_id
    2. Client: PUT /upload/{upload_id}/chunk/{n}  (can retry each chunk)
    3. Client: POST /upload/{upload_id}/complete → file_id
    Server tracks which chunks are received in Redis: SADD upload:{id} {chunk_n}
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

    print(f"\n  Original file: {len(original_data)//1024}KB")
    print(f"  Modified: changed byte at position {modify_pos:,}")
    print(f"\n  Chunk comparison:")
    print(f"  {'Chunk':>7}  {'Original hash':>16}  {'New hash':>16}  {'Changed?':>9}")
    print(f"  {'-'*7}  {'-'*16}  {'-'*16}  {'-'*9}")
    changed = []
    for (idx, _, oh), (_, _, nh) in zip(original_chunks, modified_chunks):
        changed_flag = "YES" if oh != nh else "no"
        if oh != nh:
            changed.append(idx)
        print(f"  {idx:>7}  {oh[:16]:>16}  {nh[:16]:>16}  {changed_flag:>9}")

    start = time.perf_counter()
    file_id2, version2, uploaded, skipped = upload_chunks(
        mc, conn, modified_data, "alice", "document.pdf"
    )
    elapsed_ms = (time.perf_counter() - start) * 1000

    print(f"\n  Re-upload: {elapsed_ms:.0f}ms")
    print(f"    Version:          {version2}")
    print(f"    Chunks uploaded:  {uploaded}  (only changed chunks!)")
    print(f"    Chunks skipped:   {skipped}  (unchanged, already in store)")
    print(f"    Bytes transferred:{uploaded * CHUNK_SIZE // 1024}KB vs {len(original_data)//1024}KB full re-upload")
    print(f"    Bandwidth saved:  {skipped}/{len(original_chunks)} chunks = {skipped/len(original_chunks)*100:.0f}%")

    print(f"""
  Rsync-style rolling hash (production implementation):
    Dropbox and Google Drive use a rolling hash (Rabin fingerprint or similar)
    to find changed regions at BYTE granularity, not just chunk boundaries.
    A 1-byte insertion near chunk boundary shifts all subsequent chunks —
    rolling hash detects the actual changed region.

    This lab uses fixed-size chunks for simplicity.
    Production: variable-size chunks via content-defined chunking (CDC)
    where chunk boundaries are determined by content, not position.
""")
    return modified_data


# ── Phase 3: Content deduplication ───────────────────────────────────────────

def phase3_deduplication(conn, mc, original_data: bytes):
    section("Phase 3: Content Deduplication — Two Users, One File")

    print("""
  Scenario: Alice and Bob both upload the same 10MB file.
  With content-hash deduplication, the bytes are stored only once.
""")

    # Bob uploads the same file
    start = time.perf_counter()
    file_id_bob, version_bob, uploaded_bob, skipped_bob = upload_chunks(
        mc, conn, original_data, "bob", "document.pdf"
    )
    elapsed_ms = (time.perf_counter() - start) * 1000

    print(f"  Alice uploaded 'document.pdf': {len(original_data)//1024}KB, all chunks stored")
    print(f"  Bob uploads identical file: {elapsed_ms:.0f}ms")
    print(f"    Chunks uploaded (new bytes): {uploaded_bob}")
    print(f"    Chunks skipped (already stored): {skipped_bob}")
    print(f"    Actual bytes transferred: {uploaded_bob * CHUNK_SIZE} bytes")

    # Show metadata
    with conn.cursor() as cur:
        cur.execute(
            "SELECT user_id, filename, file_hash, size_bytes FROM files ORDER BY id",
        )
        rows = cur.fetchall()
    with conn.cursor() as cur:
        cur.execute("SELECT hash, ref_count FROM chunks ORDER BY ref_count DESC LIMIT 5")
        chunk_rows = cur.fetchall()

    print(f"\n  Files table (two entries, same content):")
    print(f"  {'User':<8}  {'Filename':<20}  {'Hash prefix':>12}  {'Size':>10}")
    print(f"  {'-'*8}  {'-'*20}  {'-'*12}  {'-'*10}")
    for user, fname, fhash, size in rows:
        print(f"  {user:<8}  {fname:<20}  {fhash[:12]:>12}  {size//1024:>8}KB")

    print(f"\n  Chunks table (ref_count shows sharing):")
    print(f"  {'Hash prefix':>14}  {'Ref Count':>10}  {'Shared by'}")
    print(f"  {'-'*14}  {'-'*10}  {'-'*12}")
    for chunk_hash, ref_count in chunk_rows:
        shared = f"{ref_count} users" if ref_count > 1 else "1 user"
        print(f"  {chunk_hash[:14]:>14}  {ref_count:>10}  {shared}")

    storage_without = len(rows) * len(original_data)
    with conn.cursor() as cur:
        cur.execute("SELECT SUM(size_bytes) FROM chunks")
        actual_storage = cur.fetchone()[0] or 0

    print(f"\n  Storage savings:")
    print(f"    Without dedup: {storage_without//1024}KB")
    print(f"    With dedup:    {actual_storage//1024}KB")
    print(f"    Savings:       {(1 - actual_storage/storage_without)*100:.0f}%")

    print(f"""
  Dropbox "Magic Pocket" deduplication (2016):
    Dropbox found ~30% of files uploaded by users already existed in the store.
    Hash-based deduplication (SHA-256 per chunk) eliminates redundant storage.
    Privacy note: two users cannot access each other's chunks (access control
    is at the metadata layer — chunk hashes are never exposed to users).
""")
    return file_id_bob


# ── Phase 4: Conflict resolution ─────────────────────────────────────────────

def phase4_conflict(conn, mc, original_data: bytes, file_id: int):
    section("Phase 4: Conflict Resolution — Simultaneous Edits")

    print("""
  Scenario: Alice and Bob both download version 1 of a shared document.
  Both make different edits offline. Both try to save simultaneously.

  Google Drive's approach: create a "conflict copy" rather than last-write-wins.
  The user must manually merge the conflicting versions.
""")

    # Alice's edit: modify bytes in chunk 0
    alice_data = bytearray(original_data)
    alice_data[100] = 0xFF
    alice_data = bytes(alice_data)

    # Bob's edit: modify bytes in chunk 2
    bob_data = bytearray(original_data)
    if len(bob_data) > 2 * CHUNK_SIZE + 200:
        bob_data[2 * CHUNK_SIZE + 200] = 0xAA
    bob_data = bytes(bob_data)

    # Alice saves first (gets version 2)
    alice_id, alice_v, _, _ = upload_chunks(mc, conn, alice_data, "alice", "shared_doc.pdf")
    print(f"  Alice saves edit → version {alice_v} of shared_doc.pdf")

    # Bob tries to save the same base version (version 1 → version 2 conflict)
    # Detect: Bob's base version != current version → conflict
    with conn.cursor() as cur:
        cur.execute(
            "SELECT MAX(version) FROM file_versions WHERE file_id = %s",
            (alice_id,),
        )
        current_version = cur.fetchone()[0] or 1

    bob_base_version = 1  # Bob downloaded version 1
    has_conflict = current_version > bob_base_version

    print(f"  Bob tries to save (based on version {bob_base_version})")
    print(f"  Current version: {current_version}")
    print(f"  Conflict detected: {has_conflict}")

    if has_conflict:
        # Create a conflict copy under a different filename
        conflict_filename = f"shared_doc (Bob's conflicted copy {time.strftime('%Y-%m-%d')}).pdf"
        bob_id, bob_v, _, _ = upload_chunks(mc, conn, bob_data, "bob", conflict_filename)
        print(f"\n  Google Drive creates conflict copy: '{conflict_filename}'")
        print(f"    Bob's version saved as version {bob_v}")

    # Show version history
    with conn.cursor() as cur:
        cur.execute("""
            SELECT fv.version, fv.created_at, f.user_id, f.filename, fv.chunk_hashes
            FROM file_versions fv
            JOIN files f ON f.id = fv.file_id
            ORDER BY fv.created_at
        """)
        rows = cur.fetchall()

    print(f"\n  Version history:")
    print(f"  {'v':>3}  {'User':<8}  {'Filename':<40}  {'Chunks'}")
    print(f"  {'-'*3}  {'-'*8}  {'-'*40}  {'-'*10}")
    for version, created_at, user, fname, chunk_hashes in rows:
        print(f"  v{version:<2}  {user:<8}  {fname[:40]:<40}  {len(chunk_hashes)} chunks")

    print(f"""
  Conflict resolution strategies:

  ┌─────────────────────┬──────────────────────────────────────────┐
  │ Strategy            │ Behaviour                                │
  ├─────────────────────┼──────────────────────────────────────────┤
  │ Last-write-wins     │ Bob's save overwrites Alice's silently   │
  │ First-write-wins    │ Bob's save rejected; he must re-merge    │
  │ Conflict copy (GDrive)│ Both versions kept; user merges       │
  │ Operational Transform│ Google Docs: merge at character level   │
  │ CRDT               │ Notion/Figma: conflict-free data types   │
  └─────────────────────┴──────────────────────────────────────────┘

  Google Drive uses conflict copy for binary files (PDFs, Excel).
  Google Docs uses Operational Transform for real-time collaboration
  on text documents (same approach as Google Wave, adopted widely).
""")


# ── Phase 5: Version history ──────────────────────────────────────────────────

def phase5_version_history(conn):
    section("Phase 5: Version History in Postgres")

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
    print(f"  {'User':<8}  {'Filename':<35}  {'v':>3}  {'Chunks':>7}  {'First chunk hash'}")
    print(f"  {'-'*8}  {'-'*35}  {'-'*3}  {'-'*7}  {'-'*18}")
    for user, fname, version, created_at, n_chunks, first_hash in rows:
        print(f"  {user:<8}  {fname[:35]:<35}  {version:>3}  {n_chunks:>7}  {(first_hash or '')[:18]}")

    # Reconstruct diff between two versions
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
        changed_chunks = [i for i, (h1, h2) in enumerate(zip(v1_hashes, v2_hashes)) if h1 != h2]
        unchanged_chunks = [i for i, (h1, h2) in enumerate(zip(v1_hashes, v2_hashes)) if h1 == h2]

        print(f"\n  Diff between document.pdf v1 and v2:")
        print(f"    Changed chunks:   {changed_chunks}")
        print(f"    Unchanged chunks: {unchanged_chunks}")
        print(f"\n  To restore v1: use v1's chunk_hashes to reconstruct from object store")
        print(f"  Cost of version history: only store unique chunks per version")
        print(f"  Identical chunks across versions are stored exactly once (dedup)")

    print(f"""
  Version retention policy (Google Drive):
    Free tier: 100 revision history (or 30 days, whichever is less)
    Workspace: unlimited revision history for 30 days, then pruned to
               daily snapshots for another 30 days, then weekly for 1 year

  Storage cost of versioning:
    If 90% of chunks are unchanged between versions (typical for edits),
    each new version adds only 10% × file_size of new storage.
    After 10 edits: ~2× total storage (not 10×).
    Content-hash deduplication makes version history nearly free.
""")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    section("GOOGLE DRIVE LAB")
    print("""
  Architecture:
    Upload → Chunk (4MB) → Hash each chunk → Upload new chunks only (MinIO)
    Metadata → Postgres (files, file_versions, chunks tables)
    Deduplication: chunks table maps hash → object key + ref_count
    Delta sync: on re-upload, skip chunks with matching hash
""")

    install_packages()
    wait_for_service(f"{MINIO_ENDPOINT}/minio/health/live", label="MinIO")

    conn = get_db()
    r    = get_redis()
    mc   = get_minio()

    init_db(conn)
    ensure_bucket(mc)

    file_data, file_id = phase1_chunked_upload(conn, mc)
    modified_data      = phase2_delta_sync(conn, mc, file_data, file_id)
    phase3_deduplication(conn, mc, file_data)
    phase4_conflict(conn, mc, file_data, file_id)
    phase5_version_history(conn)

    conn.close()

    section("Lab Complete")
    print("""
  Summary:
  • Chunked upload: resume on failure, parallel upload, deduplication unit
  • Delta sync: only upload changed 4MB chunks → save 90%+ bandwidth on small edits
  • Content deduplication: SHA-256 per chunk, store once, multiple metadata pointers
  • Conflict copy: Google Drive saves both versions when concurrent edits detected
  • Version history: chunk_hashes array per version; diff is set difference of hashes

  Next: explore 07-web-crawler/ or review the Interview Checklists
""")


if __name__ == "__main__":
    main()
