#!/usr/bin/env python3
"""
YouTube Lab — experiment.py

What this demonstrates:
  1. Upload video to MinIO (simulates object storage)
  2. Store metadata in Postgres (title, status=processing)
  3. Simulate transcoding pipeline: processing → ready, multiple quality entries
  4. Generate HLS master manifest + per-quality segment manifests
  5. Serve video with byte-range requests via Nginx (video seeking / HTTP 206)
  6. Increment view count in Redis (INCR), observe Postgres stays at 0
  7. Batch flush Redis counters to Postgres in a single pass
  8. Benchmark Redis pipeline throughput vs naive per-call throughput

Run:
  docker compose up -d
  # Wait ~20s for services
  python experiment.py
"""

import hashlib
import io
import os
import time
import urllib.error
import urllib.request

# ── Config ───────────────────────────────────────────────────────────────────

MINIO_ENDPOINT  = os.getenv("MINIO_ENDPOINT",  "http://localhost:9000")
MINIO_ACCESS    = os.getenv("MINIO_ACCESS",    "minioadmin")
MINIO_SECRET    = os.getenv("MINIO_SECRET",    "minioadmin")
MINIO_BUCKET    = "videos"
DB_URL          = os.getenv("DATABASE_URL", "postgresql://app:secret@localhost:5432/youtube")
REDIS_URL       = os.getenv("REDIS_URL",    "redis://localhost:6379")
NGINX_URL       = os.getenv("NGINX_URL",    "http://localhost:8080")


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


def wait_for_postgres(dsn, max_wait=60):
    """Poll until Postgres accepts connections."""
    import psycopg2
    print(f"  Waiting for Postgres ...")
    for i in range(max_wait):
        try:
            conn = psycopg2.connect(dsn)
            conn.close()
            print(f"  Postgres ready after {i+1}s")
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError("Postgres did not start within 60s")


def wait_for_redis(url, max_wait=60):
    """Poll until Redis accepts connections."""
    import redis as redis_lib
    print(f"  Waiting for Redis ...")
    r = redis_lib.from_url(url)
    for i in range(max_wait):
        try:
            r.ping()
            print(f"  Redis ready after {i+1}s")
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError("Redis did not start within 60s")


def get_db():
    import psycopg2
    return psycopg2.connect(DB_URL)


def get_redis():
    import redis
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
                    id           BIGSERIAL PRIMARY KEY,
                    video_id     BIGINT REFERENCES videos(id),
                    quality      TEXT NOT NULL,
                    path         TEXT NOT NULL,
                    size_bytes   BIGINT,
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

    # Use os.urandom — fast C-level random bytes, not a Python-level loop.
    # A Python loop over getrandbits(8) for 5MB takes ~3s; os.urandom takes <1ms.
    video_size_mb = 5
    video_data = os.urandom(video_size_mb * 1024 * 1024)
    checksum = hashlib.sha256(video_data).hexdigest()

    print(f"\n  Simulated video: {video_size_mb}MB, SHA-256: {checksum[:16]}...")
    print(f"  (SHA-256 enables exact deduplication: same bytes → skip re-transcoding)")

    # Deduplication check: if a video with this hash already exists, skip upload
    object_name = f"raw/{checksum[:16]}.mp4"
    try:
        minio_client.stat_object(MINIO_BUCKET, object_name)
        print(f"  Dedup hit: object already in MinIO — skipping upload, reusing segments")
    except Exception:
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

    # Store metadata in Postgres with status=processing
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO videos (title, user_id, status, original_path)
                   VALUES (%s, %s, 'processing', %s) RETURNING id""",
                ("My First Upload", 1001, object_name),
            )
            video_id = cur.fetchone()[0]

    print(f"\n  Postgres metadata: video_id={video_id}, status=processing")
    print(f"  Note: video bytes live in MinIO, NOT Postgres — Postgres stores only paths.")
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

  At production scale YouTube uses Bigtable (video_id as row key) not Postgres —
  same logical model, but horizontal scale without sharding complexity.
""")


# ── Phase 3: Transcoding pipeline ────────────────────────────────────────────

def phase3_transcoding(conn, minio_client, video_id, video_data):
    section("Phase 3: Transcoding Pipeline — Multi-Resolution Outputs")

    print("""
  Real transcoding pipeline:
    Upload → SQS/Kafka → Transcoding workers → Multiple resolutions → Object storage
    Workers use FFmpeg/AV1 to produce HLS .ts segments at 360p/720p/1080p/4K

  Key design decisions:
    • All quality levels transcoded in parallel across separate workers
    • 360p available in ~2 min (fast encode) → video marked 'ready' immediately
    • 4K may take 30+ min — progressive availability, not all-or-nothing
    • GPU acceleration (NVENC/VAAPI): 10–20× faster than CPU encoding
    • Spot/preemptible VMs: jobs are idempotent, safe to retry on preemption

  Simulated here: create proportionally-sized quality variants in MinIO
""")

    qualities = [
        ("360p",   500_000,  0.10),
        ("720p",  2_500_000, 0.40),
        ("1080p", 8_000_000, 0.90),
    ]

    checksum = hashlib.sha256(video_data).hexdigest()[:16]
    quality_records = []

    for quality_label, bitrate_bps, scale in qualities:
        transcoded_size = int(len(video_data) * scale)
        # Simulate distinct transcoded bytes (different slice per quality)
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

    print(f"\n  Quality variants stored in Postgres:")
    print(f"  {'Quality':<10}  {'Bitrate kbps':>14}  {'Size':>10}")
    print(f"  {'-'*10}  {'-'*14}  {'-'*10}")
    for quality, bitrate, size in rows:
        print(f"  {quality:<10}  {bitrate:>14,}  {size//1024:>8}KB")

    return quality_records, checksum


# ── Phase 4: HLS Manifest ─────────────────────────────────────────────────────

def phase4_hls_manifest(minio_client, video_id, quality_records, checksum):
    section("Phase 4: HLS Master Manifest — Adaptive Bitrate Streaming")

    print("""
  HLS (HTTP Live Streaming) splits each quality level into small .ts segments
  (2–6 seconds each). A master manifest (.m3u8) lists all quality variants.
  The player picks the highest quality that fits in available bandwidth and
  switches quality dynamically at segment boundaries — invisible to the viewer.

  Master manifest format:
""")

    # Build a realistic HLS master manifest
    master_lines = ["#EXTM3U", "#EXT-X-VERSION:3", ""]
    for q in sorted(quality_records, key=lambda x: x["bitrate_kbps"]):
        resolution_map = {"360p": "640x360", "720p": "1280x720", "1080p": "1920x1080"}
        resolution = resolution_map.get(q["quality"], "unknown")
        bandwidth = q["bitrate_kbps"] * 1000
        master_lines.append(f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},RESOLUTION={resolution},CODECS="avc1.4d401f,mp4a.40.2"')
        master_lines.append(f'{q["quality"]}/index.m3u8')
        master_lines.append("")
    master_manifest = "\n".join(master_lines)

    print("  --- master.m3u8 ---")
    print(master_manifest)

    # Upload master manifest to MinIO
    manifest_path = f"transcoded/{checksum}/master.m3u8"
    manifest_bytes = master_manifest.encode()
    minio_client.put_object(
        MINIO_BUCKET,
        manifest_path,
        io.BytesIO(manifest_bytes),
        length=len(manifest_bytes),
        content_type="application/vnd.apple.mpegurl",
    )
    print(f"  Uploaded to MinIO: {MINIO_BUCKET}/{manifest_path}")

    # Build a per-quality segment manifest (360p example, 3-minute video at 4s segments)
    duration_s = 180
    segment_duration = 4
    num_segments = duration_s // segment_duration

    segment_lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{segment_duration}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "",
    ]
    for i in range(num_segments):
        segment_lines.append(f"#EXTINF:{segment_duration}.0,")
        segment_lines.append(f"segment_{i:04d}.ts")
    segment_lines.append("#EXT-X-ENDLIST")
    segment_manifest = "\n".join(segment_lines)

    # Show first few segments
    preview_lines = segment_lines[:14]
    preview_lines.append(f"  ... ({num_segments} segments total for {duration_s}s video)")
    print(f"\n  --- 360p/index.m3u8 (first {len(preview_lines)} lines) ---")
    for line in preview_lines:
        print(f"  {line}")

    segment_manifest_path = f"transcoded/{checksum}/360p/index.m3u8"
    manifest_bytes = segment_manifest.encode()
    minio_client.put_object(
        MINIO_BUCKET,
        segment_manifest_path,
        io.BytesIO(manifest_bytes),
        length=len(manifest_bytes),
        content_type="application/vnd.apple.mpegurl",
    )

    print(f"""
  Seeking with HLS:
    Player reads manifest → knows each segment covers exactly {segment_duration}s.
    Seek to t=90s → request segment_{90//segment_duration:04d}.ts  (segment index 22).
    Only that one segment is fetched — not the preceding 90 seconds of video.
    At 1080p (~8 Mbps), one 4s segment ≈ 4MB. Sub-second seek from any CDN edge.
""")

    return manifest_path


# ── Phase 5: Byte-range requests (video seeking) ─────────────────────────────

def phase5_byte_range(quality_records):
    """Demonstrate HTTP 206 Partial Content via Nginx → MinIO proxy.

    Note: minio_client is not needed here — requests go through Nginx, which
    proxies to MinIO and forwards byte-range headers. This mirrors production
    where the CDN edge fetches segments from origin via HTTP range requests.
    """
    section("Phase 5: Byte-Range Requests — HTTP 206 Partial Content")

    print("""
  HTTP byte-range requests let clients seek to any position in a video
  without downloading the entire file. This is the mechanism HLS uses
  under the hood when fetching individual .ts segments from object storage.

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

    # First: full download to establish baseline latency
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

    # Byte-range: seek to 50% into the video (simulates user clicking to midpoint)
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
        print(f"    HTTP status:   {status} (206 = Partial Content ✓)")
        print(f"    Content-Range: {content_range}")
        print(f"    Chunk size:    {len(chunk)//1024}KB fetched in {seek_ms:.0f}ms")
        if full_ms > 0 and seek_ms > 0:
            print(f"    Speedup vs full download: {full_ms/seek_ms:.1f}x faster")
    except urllib.error.HTTPError as e:
        print(f"  Byte-range request: HTTP {e.code} — {e.reason}")
        print(f"  (Nginx byte-range proxy may need configuration; see nginx.conf)")

    print(f"""
  Why this matters at scale:
    A 1-hour 1080p video = ~3.6GB. Seeking to minute 45 via byte-range
    fetches one 4s segment (~4MB) instead of 2.7GB. CDN serves the
    segment from edge cache — sub-100ms latency globally.
""")


# ── Phase 6: View count via Redis ─────────────────────────────────────────────

def phase6_view_count(conn, r, video_id):
    section("Phase 6: View Count at Scale — Redis INCR + Batch Flush")

    print("""
  Naive approach: UPDATE videos SET view_count = view_count + 1 WHERE id = ?
  At 1M views/minute this saturates Postgres with row-locking UPDATEs.
  (Postgres handles ~10K writes/s; YouTube peaks at ~11,600 views/s.)

  Production approach:
    1. On each view: Redis INCR view_count:{video_id}  (atomic, in-memory, ~0.1ms)
    2. Background worker: every 60s, flush Redis counters to Postgres in bulk
    3. Result: 0 Postgres writes per view, 1 batch write per minute per video

  Durability trade-off:
    • Redis AOF (appendonly yes): at most 1 second of counts lost on crash
    • For billing/monetisation: separate Kafka-backed pipeline (zero loss)
    • View counts lag by up to 60s — acceptable for display purposes
""")

    # Simulate 1000 views arriving quickly via pipelined INCR
    n_views = 1000
    redis_key = f"view_count:{video_id}"

    start = time.perf_counter()
    pipe = r.pipeline()
    for _ in range(n_views):
        pipe.incr(redis_key)
    pipe.execute()
    redis_ms = (time.perf_counter() - start) * 1000

    redis_count = int(r.get(redis_key) or 0)
    ops_per_sec = n_views / (redis_ms / 1000) if redis_ms > 0 else 0
    print(f"  Simulated {n_views:,} views via Redis pipeline:")
    print(f"    Time:          {redis_ms:.1f}ms total")
    print(f"    Per-view cost: {redis_ms/n_views:.3f}ms")
    print(f"    Throughput:    {ops_per_sec:,.0f} ops/s  (single-node Redis)")
    print(f"    Redis counter: {redis_count:,}")

    # Check current Postgres count (should still be 0)
    with conn.cursor() as cur:
        cur.execute("SELECT view_count FROM videos WHERE id=%s", (video_id,))
        db_count = cur.fetchone()[0]
    print(f"    Postgres view_count: {db_count}  ← NOT updated yet (flush pending)")

    # Flush to Postgres
    print(f"\n  Flushing Redis counter to Postgres (simulates 60s background tick)...")
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

    print(f"    Flushed delta:       +{delta:,} views")
    print(f"    Postgres view_count: {db_count_after:,}  ← now updated")

    print(f"""
  Cost comparison for {n_views:,} views:
    Naive (per-view UPDATE):  {n_views:,} Postgres writes
    Batched (Redis + flush):  1 Postgres write
    Reduction:                {n_views:,}x fewer DB writes

  At YouTube scale (1B views/day = 11,574 views/s):
    Naive:   11,574 UPDATE transactions/second → Postgres overwhelmed
    Batched: 1 bulk UPDATE per minute per video → trivial load
""")


# ── Phase 7: Batch flush demonstration ───────────────────────────────────────

def phase7_batch_flush(conn, r):
    section("Phase 7: Batch Flush — Multiple Videos at Once")

    print("""
  Real flush worker pattern:
    • Runs every 60 seconds (cron or event loop)
    • Scans all view_count:* keys with SCAN (non-blocking, cursor-based)
    • GETDEL each key atomically (avoids double-counting on concurrent flushes)
    • Batches all UPDATEs in one transaction for efficiency
""")

    # Create several video records simulating a mix of viral + long-tail videos
    video_ids = []
    with conn:
        with conn.cursor() as cur:
            for i in range(5):
                cur.execute(
                    "INSERT INTO videos (title, user_id, status) VALUES (%s, %s, 'ready') RETURNING id",
                    (f"Video #{i+1}", 2000 + i),
                )
                video_ids.append(cur.fetchone()[0])

    # Simulate 60 seconds of accumulated views (different popularity levels)
    view_counts = [50_000, 1_200, 300, 15_000, 750]
    labels      = ["viral", "popular", "niche", "trending", "niche"]
    print(f"  Views accumulated in Redis over 60 seconds:\n")
    print(f"  {'Video ID':>10}  {'Title':<12}  {'Views':>10}  Category")
    print(f"  {'-'*10}  {'-'*12}  {'-'*10}  --------")
    # Use enumerate to avoid O(n) list.index() and handle duplicate IDs safely
    for idx, (vid, views, label) in enumerate(zip(video_ids, view_counts, labels)):
        r.incrby(f"view_count:{vid}", views)
        print(f"  {vid:>10}  {'Video #'+str(idx+1):<12}  {views:>10,}  {label}")

    # Batch flush: scan all view_count:* keys and flush in one pass
    print(f"\n  Running batch flush worker...")
    start = time.perf_counter()
    flushed = 0
    total_views = 0
    with conn:
        with conn.cursor() as cur:
            for key in r.scan_iter("view_count:*"):
                vid_id = int(key.split(":")[1])
                delta = int(r.getdel(key) or 0)
                if delta > 0:
                    cur.execute(
                        "UPDATE videos SET view_count = view_count + %s WHERE id = %s",
                        (delta, vid_id),
                    )
                    flushed += 1
                    total_views += delta
    flush_ms = (time.perf_counter() - start) * 1000

    print(f"  Flushed {flushed} video counters ({total_views:,} total views) in {flush_ms:.1f}ms")
    print(f"  Average: {flush_ms/flushed:.2f}ms per video" if flushed > 0 else "")

    # Verify results
    with conn.cursor() as cur:
        placeholders = ",".join(["%s"] * len(video_ids))
        cur.execute(
            f"SELECT id, title, view_count FROM videos WHERE id IN ({placeholders}) ORDER BY view_count DESC",
            video_ids,
        )
        rows = cur.fetchall()

    print(f"\n  Postgres after flush (sorted by popularity):")
    print(f"  {'Video ID':>10}  {'Title':<12}  {'View Count':>12}")
    print(f"  {'-'*10}  {'-'*12}  {'-'*12}")
    for vid_id, title, vc in rows:
        print(f"  {vid_id:>10}  {title:<12}  {vc:>12,}")


# ── Phase 8: Redis throughput benchmark ──────────────────────────────────────

def phase8_redis_throughput(r):
    section("Phase 8: Redis Throughput — Pipeline vs. Single-Call")

    print("""
  Redis pipeline batches multiple commands into one TCP round-trip.
  This is the key to achieving millions of INCR ops/s from a single client.

  Comparison:
    Single-call:  each INCR pays a full network round-trip (~0.1–0.5ms each)
    Pipeline:     N INCRs sent in one batch, one round-trip for all N
""")

    bench_key = "bench:view_count"
    n = 10_000

    # Warm-up
    r.delete(bench_key)

    # Single-call (non-pipelined)
    start = time.perf_counter()
    for _ in range(n):
        r.incr(bench_key)
    single_ms = (time.perf_counter() - start) * 1000
    single_ops = n / (single_ms / 1000)

    r.delete(bench_key)

    # Pipelined
    start = time.perf_counter()
    pipe = r.pipeline()
    for _ in range(n):
        pipe.incr(bench_key)
    pipe.execute()
    pipe_ms = (time.perf_counter() - start) * 1000
    pipe_ops = n / (pipe_ms / 1000)

    r.delete(bench_key)

    speedup = single_ms / pipe_ms if pipe_ms > 0 else float("inf")

    print(f"  {n:,} INCR operations (localhost Redis):\n")
    print(f"  {'Method':<20}  {'Time':>10}  {'Ops/s':>12}")
    print(f"  {'-'*20}  {'-'*10}  {'-'*12}")
    print(f"  {'Single-call':<20}  {single_ms:>9.0f}ms  {single_ops:>11,.0f}")
    print(f"  {'Pipelined':<20}  {pipe_ms:>9.0f}ms  {pipe_ops:>11,.0f}")
    print(f"\n  Pipeline speedup: {speedup:.1f}x faster")

    print(f"""
  Interview insight:
    Even on localhost (no real network latency), pipelining is {speedup:.0f}x faster.
    On a real network with 1ms RTT, single-call INCR tops out at ~1,000 ops/s.
    Pipelined INCR saturates the Redis CPU: hundreds of thousands of ops/s.
    YouTube's view counting works because Redis pipeline + per-60s flush
    means zero Postgres writes per view regardless of traffic volume.
""")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    section("YOUTUBE LAB")
    print("""
  Architecture:
    Upload → MinIO (raw video) → Postgres (metadata: status=processing)
    Transcoding workers → MinIO (360p/720p/1080p) → Postgres (status=ready)
    HLS manifest (.m3u8) → MinIO → CDN edge (served to players)
    View → Redis INCR → Background flush → Postgres (view_count)
    Playback → Nginx → MinIO (byte-range requests, simulates CDN)
""")

    install_packages()

    wait_for_service(f"{MINIO_ENDPOINT}/minio/health/live", label="MinIO")
    wait_for_service(f"{NGINX_URL}/health", label="Nginx")
    wait_for_postgres(DB_URL)
    wait_for_redis(REDIS_URL)

    conn = get_db()
    r    = get_redis()
    mc   = get_minio()

    init_db(conn)
    ensure_bucket(mc)

    video_id, object_name, video_data = phase1_upload(conn, mc)
    phase2_metadata(conn, video_id)
    quality_records, checksum = phase3_transcoding(conn, mc, video_id, video_data)
    phase4_hls_manifest(mc, video_id, quality_records, checksum)
    phase5_byte_range(quality_records)   # HTTP calls go through Nginx, no minio_client needed
    phase6_view_count(conn, r, video_id)
    phase7_batch_flush(conn, r)
    phase8_redis_throughput(r)

    conn.close()

    section("Lab Complete")
    print("""
  Summary:
  • Videos stored in object storage (MinIO/S3): cheap, scalable, CDN-friendly
  • Metadata in Postgres: structured queries, status tracking, view counts
  • Transcoding pipeline: upload triggers async workers → multi-resolution outputs
  • HLS manifests (.m3u8): adaptive bitrate — player picks quality to match bandwidth
  • Byte-range requests: HTTP 206 Partial Content enables seeking without full download
  • View counts: Redis INCR avoids per-view DB write; batch flush every 60s
  • AOF persistence: Redis durability — lose at most 1s of counts on crash
  • Pipeline throughput: Redis pipeline is orders of magnitude faster than single-call INCR

  Next: 04-uber/ — real-time geospatial indexing with Redis GEORADIUS + PostGIS
""")


if __name__ == "__main__":
    main()
