#!/usr/bin/env python3
"""
YouTube Lab — experiment.py

What this demonstrates:
  1. Upload video to MinIO (simulates object storage)
  2. Store metadata in Postgres (title, status=processing)
  3. Simulate transcoding pipeline: processing → ready, multiple quality entries
  4. Serve video with byte-range requests via Nginx (video seeking)
  5. Increment view count in Redis (INCR), periodic flush to Postgres
  6. Show view count never writes to Postgres per-view, only batched

Run:
  docker compose up -d
  # Wait ~20s for services
  python experiment.py
"""

import hashlib
import io
import json
import os
import random
import time
import urllib.error
import urllib.request

import psycopg2
import redis

# ── Config ───────────────────────────────────────────────────────────────────

MINIO_ENDPOINT  = os.getenv("MINIO_ENDPOINT",  "http://localhost:9000")
MINIO_ACCESS    = os.getenv("MINIO_ACCESS",    "minioadmin")
MINIO_SECRET    = os.getenv("MINIO_SECRET",    "minioadmin")
MINIO_BUCKET    = "videos"
DB_URL          = os.getenv("DATABASE_URL", "postgresql://app:secret@localhost:5432/youtube")
REDIS_URL       = os.getenv("REDIS_URL",    "redis://localhost:6379")
NGINX_URL       = os.getenv("NGINX_URL",    "http://localhost:8080")

VIEW_FLUSH_INTERVAL = 30   # seconds between Redis → Postgres view count flush
VIEW_FLUSH_THRESHOLD = 10  # flush if counter exceeds this even before interval


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
    print(f"  Waiting for {label} at {url} ...")
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


# ── Schema ────────────────────────────────────────────────────────────────────

def init_db(conn):
    with conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS videos (
                    id          BIGSERIAL PRIMARY KEY,
                    title       TEXT NOT NULL,
                    user_id     BIGINT NOT NULL,
                    status      TEXT NOT NULL DEFAULT 'uploading',
                    duration_s  INT,
                    original_path TEXT,
                    created_at  TIMESTAMPTZ DEFAULT NOW(),
                    view_count  BIGINT DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS video_qualities (
                    id         BIGSERIAL PRIMARY KEY,
                    video_id   BIGINT REFERENCES videos(id),
                    quality    TEXT NOT NULL,
                    path       TEXT NOT NULL,
                    size_bytes BIGINT,
                    bitrate_kbps INT
                );
                CREATE INDEX IF NOT EXISTS idx_videos_user ON videos(user_id);
                CREATE INDEX IF NOT EXISTS idx_vq_video ON video_qualities(video_id);
            """)


def ensure_bucket(client):
    if not client.bucket_exists(MINIO_BUCKET):
        client.make_bucket(MINIO_BUCKET)
        print(f"  Created MinIO bucket: {MINIO_BUCKET}")


# ── Phase 1: Upload video to MinIO ───────────────────────────────────────────

def phase1_upload(conn, minio_client):
    section("Phase 1: Upload Video to MinIO (Object Storage)")

    # Generate a fake video file (binary blob representing raw video bytes)
    random.seed(42)
    video_size_mb = 5
    video_data = bytes(random.getrandbits(8) for _ in range(video_size_mb * 1024 * 1024))
    checksum = hashlib.sha256(video_data).hexdigest()

    print(f"\n  Simulated video: {video_size_mb}MB, SHA-256: {checksum[:16]}...")

    object_name = f"raw/{checksum[:16]}.mp4"

    start = time.perf_counter()
    minio_client.put_object(
        MINIO_BUCKET,
        object_name,
        io.BytesIO(video_data),
        length=len(video_data),
        content_type="video/mp4",
    )
    upload_ms = (time.perf_counter() - start) * 1000

    print(f"  Uploaded to MinIO in {upload_ms:.0f}ms")
    print(f"  Path: {MINIO_BUCKET}/{object_name}")

    # Store metadata in Postgres with status=uploading→processing
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO videos (title, user_id, status, original_path)
                   VALUES (%s, %s, 'processing', %s) RETURNING id""",
                ("My First Upload", 1001, object_name),
            )
            video_id = cur.fetchone()[0]

    print(f"\n  Postgres metadata: video_id={video_id}, status=processing")
    print(f"  (Transcoding pipeline will update status → ready)")
    return video_id, object_name, video_data


# ── Phase 2: Postgres metadata ───────────────────────────────────────────────

def phase2_metadata(conn, video_id):
    section("Phase 2: Video Metadata in Postgres")

    with conn.cursor() as cur:
        cur.execute("SELECT id, title, status, created_at FROM videos WHERE id = %s", (video_id,))
        row = cur.fetchone()

    print(f"\n  Video record:")
    print(f"    id:         {row[0]}")
    print(f"    title:      {row[1]}")
    print(f"    status:     {row[2]}  ← starts as 'processing'")
    print(f"    created_at: {row[3]}")

    print(f"""
  Metadata vs. Video Data:
    Metadata (title, user, status, view_count) → Postgres
    Video bytes (raw, transcoded segments)     → Object Storage (MinIO/S3/GCS)

  Why separate?
    Postgres is optimised for structured queries (search by user, filter by status).
    Object storage is optimised for large binary blobs (cheap, durable, CDN-friendly).
    Never store video bytes in Postgres (BLOBs kill performance at scale).
""")


# ── Phase 3: Transcoding pipeline ────────────────────────────────────────────

def phase3_transcoding(conn, minio_client, video_id, video_data):
    section("Phase 3: Transcoding Pipeline — Multi-Resolution Outputs")

    print("""
  Real transcoding pipeline:
    Upload → SQS/Kafka → Transcoding workers → Multiple resolutions → Object storage
    Workers use FFmpeg to produce HLS segments at 360p/720p/1080p/4K

  Simulated here: create fake quality variants in MinIO + update Postgres
""")

    qualities = [
        ("360p",  500_000,  "low"),
        ("720p",  2_500_000, "medium"),
        ("1080p", 8_000_000, "high"),
    ]

    checksum = hashlib.sha256(video_data).hexdigest()[:16]
    quality_records = []

    for quality_label, bitrate_bps, size_suffix in qualities:
        # Create a smaller "transcoded" version (simulated)
        scale = {"360p": 0.1, "720p": 0.4, "1080p": 0.9}[quality_label]
        transcoded_size = int(len(video_data) * scale)
        # Use a slice of the original + quality marker to simulate different outputs
        transcoded_data = video_data[:transcoded_size]

        object_name = f"transcoded/{checksum}/{quality_label}/video.mp4"

        start = time.perf_counter()
        minio_client.put_object(
            MINIO_BUCKET,
            object_name,
            io.BytesIO(transcoded_data),
            length=len(transcoded_data),
            content_type="video/mp4",
        )
        elapsed_ms = (time.perf_counter() - start) * 1000

        quality_records.append({
            "quality": quality_label,
            "path": object_name,
            "size_bytes": len(transcoded_data),
            "bitrate_kbps": bitrate_bps // 1000,
        })
        print(f"  Transcoded {quality_label}: {len(transcoded_data)//1024}KB uploaded in {elapsed_ms:.0f}ms")

    # Update Postgres: insert quality entries, mark video ready
    with conn:
        with conn.cursor() as cur:
            for q in quality_records:
                cur.execute(
                    """INSERT INTO video_qualities (video_id, quality, path, size_bytes, bitrate_kbps)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (video_id, q["quality"], q["path"], q["size_bytes"], q["bitrate_kbps"]),
                )
            cur.execute(
                "UPDATE videos SET status='ready', duration_s=180 WHERE id=%s",
                (video_id,),
            )

    print(f"\n  Video {video_id} status updated: processing → ready")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT quality, bitrate_kbps, size_bytes FROM video_qualities WHERE video_id=%s ORDER BY bitrate_kbps",
            (video_id,),
        )
        rows = cur.fetchall()

    print(f"\n  Quality variants stored:")
    print(f"  {'Quality':<10}  {'Bitrate kbps':>14}  {'Size':>10}")
    print(f"  {'-'*10}  {'-'*14}  {'-'*10}")
    for quality, bitrate, size in rows:
        print(f"  {quality:<10}  {bitrate:>14}  {size//1024:>8}KB")

    return quality_records


# ── Phase 4: Byte-range requests (video seeking) ─────────────────────────────

def phase4_byte_range(minio_client, quality_records):
    section("Phase 4: Byte-Range Requests — Video Seeking via Nginx")

    print("""
  HTTP byte-range requests let clients seek to any position in a video
  without downloading the entire file.

  Request:  GET /video/videos/path HTTP/1.1
            Range: bytes=1048576-2097151

  Response: HTTP/1.1 206 Partial Content
            Content-Range: bytes 1048576-2097151/5242880
            Content-Length: 1048576
""")

    # Use the 720p variant (medium size)
    medium = next(q for q in quality_records if q["quality"] == "720p")
    object_path = medium["path"]
    total_size = medium["size_bytes"]

    nginx_url = f"{NGINX_URL}/video/{MINIO_BUCKET}/{object_path}"

    # First: full download (simulates initial buffering)
    start = time.perf_counter()
    req = urllib.request.Request(nginx_url)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            full_data = resp.read()
        full_ms = (time.perf_counter() - start) * 1000
        full_size = len(full_data)
        print(f"  Full download ({full_size//1024}KB): {full_ms:.0f}ms")
    except Exception as e:
        print(f"  Full download: {e}")
        full_ms = 0
        full_size = total_size

    # Byte-range: seek to 50% into the video
    seek_start = total_size // 2
    seek_end   = seek_start + min(524288, total_size // 4)  # 512KB chunk

    start = time.perf_counter()
    req = urllib.request.Request(
        nginx_url,
        headers={"Range": f"bytes={seek_start}-{seek_end}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            chunk = resp.read()
            status = resp.getcode()
            content_range = resp.headers.get("Content-Range", "n/a")
        seek_ms = (time.perf_counter() - start) * 1000
        print(f"\n  Byte-range seek to 50% (bytes={seek_start}-{seek_end}):")
        print(f"    HTTP status:   {status} (206 = Partial Content)")
        print(f"    Content-Range: {content_range}")
        print(f"    Chunk size:    {len(chunk)//1024}KB fetched in {seek_ms:.0f}ms")
        print(f"    Speedup:       {full_ms/seek_ms:.1f}x vs full download" if full_ms > 0 else "")
    except urllib.error.HTTPError as e:
        print(f"  Byte-range request: HTTP {e.code} — {e.reason}")
        print(f"  (Nginx byte-range proxy may need configuration; see nginx.conf)")

    print(f"""
  Adaptive Bitrate Streaming (HLS/DASH):
    Instead of one large MP4, YouTube serves thousands of small .ts segments
    (2-6 seconds each) at multiple quality levels.

    Client player logic:
      1. Download manifest (.m3u8 / .mpd): list of all segments + quality levels
      2. Measure current download bandwidth
      3. Select highest quality that fits in available bandwidth
      4. Download next segment (prefetch 3-5 segments ahead)
      5. If bandwidth drops: switch to lower quality mid-stream

    Benefit: seamless quality switching without buffering, works on flaky networks.
    Segment size (2-6s): small enough to switch quality quickly, large enough to
    amortise HTTP request overhead.
""")


# ── Phase 5: View count via Redis ─────────────────────────────────────────────

def phase5_view_count(conn, r, video_id):
    section("Phase 5: View Count at Scale — Redis INCR + Batch Flush")

    print("""
  Naive approach: UPDATE videos SET view_count = view_count + 1 WHERE id = ?
  At 1M views/minute this saturates Postgres with row-locking UPDATEs.

  Production approach:
    1. On each view: Redis INCR view_count:{video_id}  (atomic, in-memory, fast)
    2. Background worker: every 60s, flush Redis counters to Postgres in bulk
    3. Result: 0 Postgres writes per view, 1 batch write per minute
""")

    # Simulate 1000 views arriving quickly
    n_views = 1000
    redis_key = f"view_count:{video_id}"

    start = time.perf_counter()
    pipe = r.pipeline()
    for _ in range(n_views):
        pipe.incr(redis_key)
    pipe.execute()
    redis_ms = (time.perf_counter() - start) * 1000

    redis_count = int(r.get(redis_key) or 0)
    print(f"  Simulated {n_views} views:")
    print(f"    Redis INCR ×{n_views}: {redis_ms:.1f}ms total ({redis_ms/n_views:.3f}ms/view)")
    print(f"    Redis counter: {redis_count}")

    # Check current Postgres count
    with conn.cursor() as cur:
        cur.execute("SELECT view_count FROM videos WHERE id=%s", (video_id,))
        db_count = cur.fetchone()[0]
    print(f"    Postgres view_count: {db_count}  ← NOT updated yet")

    # Flush to Postgres
    print(f"\n  Flushing Redis counter to Postgres...")
    delta = int(r.getdel(redis_key) or 0)
    if delta > 0:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE videos SET view_count = view_count + %s WHERE id = %s",
                    (delta, video_id),
                )

    with conn.cursor() as cur:
        cur.execute("SELECT view_count FROM videos WHERE id=%s", (video_id,))
        db_count_after = cur.fetchone()[0]

    print(f"    Flushed delta: +{delta} views")
    print(f"    Postgres view_count: {db_count_after}  ← now updated")

    # Estimate savings
    db_writes_naive = n_views
    db_writes_batched = 1
    print(f"""
  Cost comparison for {n_views} views:
    Naive (per-view UPDATE):  {db_writes_naive} Postgres writes
    Batched (Redis + flush):  {db_writes_batched} Postgres write
    Reduction:                {db_writes_naive}x fewer DB writes

  At YouTube scale (1B views/day = 11,574 views/s):
    Naive:   11,574 UPDATE transactions/second → Postgres overwhelmed
    Batched: 1 bulk UPDATE per minute per video → trivial load
""")


# ── Phase 6: Batch flush demonstration ───────────────────────────────────────

def phase6_batch_flush(conn, r):
    section("Phase 6: Batch Flush — Multiple Videos at Once")

    # Create several video records and simulate concurrent views
    video_ids = []
    with conn:
        with conn.cursor() as cur:
            for i in range(5):
                cur.execute(
                    "INSERT INTO videos (title, user_id, status) VALUES (%s, %s, 'ready') RETURNING id",
                    (f"Video #{i+1}", 2000 + i),
                )
                video_ids.append(cur.fetchone()[0])

    # Simulate views on each video (different popularity)
    view_counts = [50000, 1200, 300, 15000, 750]
    print(f"\n  Simulating views on 5 videos (representing 60 seconds of traffic):\n")
    print(f"  {'Video ID':>10}  {'Views (in Redis)':>18}")
    print(f"  {'-'*10}  {'-'*18}")

    for vid, views in zip(video_ids, view_counts):
        r.incrby(f"view_count:{vid}", views)
        print(f"  {vid:>10}  {views:>18,}")

    # Batch flush: scan all view_count:* keys and flush
    print(f"\n  Batch flush (background worker tick)...")
    start = time.perf_counter()
    flushed = 0
    for key in r.scan_iter("view_count:*"):
        vid_id = int(key.split(":")[1])
        delta = int(r.getdel(key) or 0)
        if delta > 0:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE videos SET view_count = view_count + %s WHERE id = %s",
                        (delta, vid_id),
                    )
            flushed += 1
    flush_ms = (time.perf_counter() - start) * 1000

    print(f"  Flushed {flushed} counters to Postgres in {flush_ms:.1f}ms")

    # Verify
    with conn.cursor() as cur:
        placeholders = ",".join(["%s"] * len(video_ids))
        cur.execute(
            f"SELECT id, title, view_count FROM videos WHERE id IN ({placeholders}) ORDER BY view_count DESC",
            video_ids,
        )
        rows = cur.fetchall()

    print(f"\n  Postgres after flush:")
    print(f"  {'Video ID':>10}  {'Title':<15}  {'View Count':>12}")
    print(f"  {'-'*10}  {'-'*15}  {'-'*12}")
    for vid_id, title, vc in rows:
        print(f"  {vid_id:>10}  {title:<15}  {vc:>12,}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    section("YOUTUBE LAB")
    print("""
  Architecture:
    Upload → MinIO (raw video) → Postgres (metadata: status=processing)
    Transcoding workers → MinIO (360p/720p/1080p) → Postgres (status=ready)
    View → Redis INCR → Background flush → Postgres (view_count)
    Playback → Nginx → MinIO (byte-range requests, CDN simulation)
""")

    install_packages()

    wait_for_service(f"{MINIO_ENDPOINT}/minio/health/live", label="MinIO")
    wait_for_service(f"{NGINX_URL}/health", label="Nginx")

    conn = get_db()
    r    = get_redis()
    mc   = get_minio()

    init_db(conn)
    ensure_bucket(mc)

    video_id, object_name, video_data = phase1_upload(conn, mc)
    phase2_metadata(conn, video_id)
    quality_records = phase3_transcoding(conn, mc, video_id, video_data)
    phase4_byte_range(mc, quality_records)
    phase5_view_count(conn, r, video_id)
    phase6_batch_flush(conn, r)

    conn.close()

    section("Lab Complete")
    print("""
  Summary:
  • Videos stored in object storage (MinIO/S3): cheap, scalable, CDN-friendly
  • Metadata in Postgres: structured queries, status tracking, view counts
  • Transcoding pipeline: upload triggers async workers → multi-resolution outputs
  • Byte-range requests: HTTP 206 Partial Content enables video seeking without full download
  • View counts: Redis INCR avoids per-view DB write; batch flush every 60s
  • Adaptive bitrate: HLS/DASH serves 2-6s segments at multiple qualities

  Next: 04-uber/ — real-time geospatial indexing with Redis GEORADIUS + PostGIS
""")


if __name__ == "__main__":
    main()
