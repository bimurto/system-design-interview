# Case Study: YouTube

**Prerequisites:** `../../01-foundations/06-caching/`, `../../01-foundations/10-networking-basics/`,
`../../01-foundations/12-blob-object-storage/`, `../../02-advanced/10-cdn-and-edge/`

---

## The Problem at Scale

YouTube is the world's largest video platform. The upload-to-playback pipeline must handle both massive write
throughput (uploads) and enormous read throughput (views), with global distribution and sub-second seek latency.

| Metric                             | Value                                   |
|------------------------------------|-----------------------------------------|
| Video uploaded per minute          | 500 hours                               |
| Hours watched per day              | 1 billion                               |
| Peak concurrent viewers            | ~100 million                            |
| Average video size (1080p, 10 min) | ~1.5 GB                                 |
| Daily upload storage               | 500h × 60min × 1.5 GB ≈ 45 TB/day (raw) |
| CDN bandwidth (peak)               | ~500 Tbps globally                      |

The core engineering challenge: a video uploaded in São Paulo must be transcoded into 5+ quality levels and distributed
to CDN edge servers in Tokyo, London, and New York — all within minutes of upload, serving millions of concurrent
viewers with sub-second seek latency.

---

## 1. Clarify Requirements

Before designing anything, drive toward a shared scope. These are the questions to ask (and the answers that bound the
design):

**Functional scope:**

- Upload: what file formats/sizes? (Any codec, up to 256 GB)
- Playback: adaptive quality? Seek? Live streaming? (ABR yes, seek yes, live out of scope here)
- Social features: likes, comments, subscriptions? (Yes, but not the hard part — treat as standard CRUD)
- Search: by title, tags, transcript? (Yes — separate search service, not the focus)
- Monetisation / ads insertion? (Out of scope for this design)

**Scale signals to ask for:**

- How many uploads per day? (500 hours/minute → ~720,000 hours/day)
- Read/write ratio? (~10,000:1 — YouTube is read-heavy)
- Geographic distribution? (Global — needs CDN)
- Consistency requirements for view counts? (Eventually consistent OK; counts lag by ~60s)

**Non-functional:**

- Video available for playback within 5 minutes of upload (360p first)
- Playback start latency < 3 seconds globally
- Seek latency < 500ms (byte-range request to CDN edge)
- 99.99% playback availability (CDN redundancy)
- Storage durability: 11 nines — video deletion should be rare/intentional

---

## 2. Capacity Estimation

| Metric                              | Calculation                                        | Result                       |
|-------------------------------------|----------------------------------------------------|------------------------------|
| Upload ingest rate                  | 500 h/min × 1.5 GB/h                               | ~12.5 GB/min ≈ 208 MB/s raw  |
| Daily raw upload storage            | 500 h/min × 60 min × 1.5 GB                        | 45 TB/day                    |
| Transcoded storage multiplier       | 5 quality levels, avg ~2× raw total                | ~4× raw                      |
| Daily storage added (all qualities) | 45 TB × 4                                          | ~180 TB/day                  |
| Storage over 10 years               | 180 TB/day × 3,650 days                            | ~657 PB                      |
| Daily view bandwidth                | 1B hours × avg 2 Mbps / 8 bits                     | ~900 PB/day ≈ 10.4 TB/s avg  |
| CDN hit ratio                       | 95% of views served from edge                      | —                            |
| Origin bandwidth                    | 5% × 10.4 TB/s                                     | ~520 GB/s to origin          |
| View rate                           | 1B views/day                                       | ~11,600 views/s peak         |
| Transcoding workers (rough)         | 500 h/min × 30 CPU-min/h per quality × 5 qualities | ~75,000 CPU cores continuous |

**Key insight for interviewers:** transcoding is the dominant cost. At 500 hours of video per minute across 5 quality
levels, you need tens of thousands of CPU cores just to keep up with real-time uploads, before accounting for
re-transcoding backlog (e.g., upgrading old 360p videos to AV1).

---

## 3. High-Level Architecture

```
  ┌─────────────────────────────────────────────────────────────────┐
  │                      Upload Path                                 │
  │                                                                  │
  │  Client → Upload API (resumable, chunked) → Object Storage (raw)│
  │                     → Postgres (metadata: status=processing)    │
  │                     → Message Queue (transcoding job published) │
  │                                                                  │
  │  Transcoding Workers (pull from queue):                         │
  │    FFmpeg/AV1: raw → 360p/720p/1080p/4K HLS segments (.ts)     │
  │    Upload segments to Object Storage                             │
  │    Update Postgres: status=ready, insert quality entries        │
  └─────────────────────────────────────────────────────────────────┘

  ┌─────────────────────────────────────────────────────────────────┐
  │                      Playback Path                               │
  │                                                                  │
  │  Client → CDN Edge ──► (cache hit) → serve .ts segment          │
  │                    └── (miss) → Origin Nginx → Object Storage   │
  │                                                                  │
  │  Client player (adaptive bitrate, HLS/DASH):                   │
  │    1. Fetch manifest (.m3u8): segment list + quality levels     │
  │    2. Measure bandwidth, choose quality                          │
  │    3. Prefetch next 3-5 segments                                │
  │    4. Switch quality dynamically at segment boundaries          │
  └─────────────────────────────────────────────────────────────────┘

  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
  │  Object Storage  │  │    Postgres       │  │     Redis        │
  │  (MinIO/GCS/S3)  │  │  videos table     │  │  view_count:id   │
  │  raw + segments  │  │  video_qualities  │  │  (INCR, flush)   │
  └──────────────────┘  └──────────────────┘  └──────────────────┘
```

**Object storage (MinIO/GCS/S3)** stores all video bytes: the original upload and all transcoded outputs. Chosen over a
filesystem because it scales to petabytes without capacity planning, serves HTTP byte-range requests natively,
integrates directly with CDNs, and costs ~$20/TB/month vs ~$200/TB/month for block storage.

**Postgres** stores structured metadata: video title, uploader, upload time, status (`uploading` → `processing` →
`ready` / `failed`), duration, and per-quality file paths. It never stores video bytes. The `view_count` column is
updated by a batch flush process, not per-view.

**Transcoding workers** are the most compute-intensive part of the system. A 1-hour video at 4K requires ~30 minutes of
CPU time per quality level. YouTube runs thousands of GPU-accelerated workers and prioritises recently uploaded videos
to minimise time-to-availability.

**Redis** holds view count increments in memory. Each view executes `INCR view_count:{video_id}` (nanosecond latency, no
disk I/O). A background process flushes accumulated counts to Postgres every 60 seconds.

---

## 4. Deep Dives

### 4.1 Resumable Upload Protocol

Large video files (up to 256 GB) cannot be uploaded in a single HTTP POST — network interruptions would require starting
over. YouTube uses a resumable upload protocol:

1. **Initiate:** `POST /upload` returns an upload session URL and a `upload_id`
2. **Upload chunks:** `PUT /upload?upload_id=X` with `Content-Range: bytes 0-4194303/268435456`. If the connection
   drops, the client queries for the current offset and resumes from there
3. **Complete:** when the final chunk is received, the server stores the raw file in object storage atomically

The upload API is stateless between chunks — the `upload_id` maps to a temporary staging area in object storage. This
makes it horizontally scalable. The Postgres record is only created after the final chunk lands.

**Key interview point:** the raw upload path must handle 208 MB/s continuous ingest across all users. Use a dedicated
upload fleet behind a separate DNS entry (e.g., `upload.youtube.com`) to avoid mixing with API traffic.

### 4.2 Transcoding Pipeline

Raw video arrives in any codec and container format (H.264, HEVC, AV1, MKV, MOV, AVI). The transcoding pipeline:

1. **Ingest:** validate file integrity, extract metadata (duration, resolution, codec) via FFprobe
2. **Enqueue:** publish a transcoding job to a message queue (Pub/Sub or Kafka) with `video_id`, `raw_path`, and
   `priority`
3. **Decode:** workers pull the job, download the raw file from object storage, decode with FFmpeg
4. **Encode:** produce outputs at each quality level using YouTube's custom VP9/AV1 encoder
5. **Segment:** split each quality into 2-second `.ts` chunks (HLS) or `.m4s` fragments (DASH), generate manifest
   files (`.m3u8` / `.mpd`)
6. **Upload:** push all segments + manifests to object storage
7. **Notify:** update Postgres status to `ready`, emit a notification event to the uploader

Key design decisions:

- **Parallel transcoding:** all quality levels are transcoded simultaneously across multiple workers. A 10-minute video
  can be fully processed in ~3 minutes wall-clock time
- **Progressive availability:** 360p is available within ~2 minutes (fast to transcode), while 4K may take 15+ minutes.
  The video is marked `ready` when at least 360p is complete
- **Priority queuing:** recently uploaded videos get higher priority. A backlog of old videos being re-transcoded to AV1
  runs at lower priority
- **GPU acceleration:** NVENC / VAAPI reduces per-video CPU time by 10–20× for H.264/H.265 output
- **Thumbnail extraction:** a separate lightweight worker extracts thumbnail candidates at 10%, 50%, and 90% of video
  duration

### 4.3 Adaptive Bitrate Streaming (HLS/DASH)

Traditional progressive download serves one large MP4. A viewer seeking to minute 45 of a 1-hour video must first
download 45 minutes of data. This is wasteful and breaks on slow connections.

**HLS (HTTP Live Streaming)** solves this by splitting each quality level into small segments (2–6 seconds each). A
master manifest lists all quality variants:

```
#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=640x360
360p/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720
720p/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=8000000,RESOLUTION=1920x1080
1080p/index.m3u8
```

Each quality-level manifest lists segments:

```
#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:4
#EXTINF:4.0,
segment_0001.ts
#EXTINF:4.0,
segment_0002.ts
...
```

The client player implements an adaptive algorithm:

1. Measure throughput while downloading the current segment
2. If throughput > 1.5× required bitrate: upgrade quality next segment
3. If throughput < 0.8× required bitrate: downgrade quality next segment
4. Keep a 10–30 second buffer of pre-fetched segments

Quality switches happen between segment boundaries (every 4 seconds), making them nearly invisible to viewers.

**Seeking:** the player calculates which segment contains the target timestamp from the manifest, then issues a
byte-range HTTP request for just that segment. A seek to any position requires downloading at most one 4-second
segment (~2 MB at 1080p), not the entire video.

### 4.4 View Count at Scale

At YouTube's scale (1B views/day = ~11,600 views/second), per-view database writes are completely infeasible — a single
Postgres row update acquires a row lock and performs a disk write on every call.

**Per-view:** `INCR view_count:{video_id}` in Redis. Redis handles millions of operations per second on a single node.
This is atomic (no race conditions), takes ~0.1 ms, and requires no disk I/O.

**Batch flush (every 60 seconds):** a background worker scans all `view_count:*` keys, reads and atomically deletes each
counter (`GETDEL`), and executes a single `UPDATE videos SET view_count = view_count + $delta WHERE id = $video_id` per
video. For 10,000 videos with active views, this is 10,000 Postgres writes per minute — completely manageable.

**Accuracy trade-off:** counts shown to users lag by up to 60 seconds. YouTube's public view counts have historically
had additional delays (hours to days) because their actual pipeline includes fraud detection and deduplication before
crediting views — why new video counts sometimes "stall" at a few hundred before jumping.

**Durability risk:** if Redis crashes before a flush, in-flight view counts are lost. Mitigations:

- Redis AOF persistence (flush to disk every second) — you lose at most 1 second of counts
- Dual-write to Kafka for a durable event log; the flush worker reads from Kafka instead
- For billing/monetisation, a separate Kafka-based pipeline is used (zero loss acceptable there)

### 4.5 CDN Strategy and Hot vs. Cold Videos

YouTube uses Google Global Cache (GGC) — private CDN PoPs installed inside ISP data centers worldwide. Two distinct
access patterns require different strategies:

**Hot videos (top 0.1%, viral content):**

- Pre-positioned to all relevant edge PoPs immediately after transcoding completes
- CDN hit rate approaches 100% — origin servers never see individual viewer requests
- Cache TTL: hours to days

**Cold videos (long tail, 99.9% of content):**

- Not pre-positioned — would waste CDN storage
- First viewer in a region triggers a cache miss → origin fetch → segment cached at edge
- Subsequent viewers in the same region hit the CDN
- Cache TTL: 7–30 days (videos rarely change after upload)

**Cache stampede problem:** a viral video published simultaneously to millions of subscribers causes a thundering herd
at the CDN edge — all cache miss requests hit origin in parallel before the first response caches. Mitigations:

- **Request coalescing (collapsed forwarding):** the CDN holds duplicate in-flight requests and serves them all from the
  first response
- **Probabilistic early expiration:** refresh cache slightly before TTL expires, not exactly at expiry
- **Staggered subscriber notification:** fan-out notifications to subscribers in waves, not simultaneously

### 4.6 Content Deduplication

The same video is frequently re-uploaded (pirated content, news clips shared by multiple channels). Without
deduplication, YouTube would store and transcode the same bytes millions of times.

**Exact deduplication:** compute SHA-256 of the raw upload before storing. If a video with that hash already exists,
store only a metadata pointer to the existing raw object. The transcoded segments are already available — the new video
can be marked `ready` immediately, skipping transcoding entirely.

**Near-duplicate detection (ContentID):** re-encoded, cropped, or colour-adjusted copies have different SHA-256 hashes.
YouTube's ContentID system uses perceptual hashing (audio fingerprinting + visual feature extraction) to detect these.
This is a separate ML pipeline, not part of the core upload flow.

---

## Failure Modes and Scale Challenges

These are the failure scenarios that separate a good answer from a great one in a FAANG interview:

| Failure                          | Cause                                              | Mitigation                                                                                                                        |
|----------------------------------|----------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------|
| Transcoding worker crash mid-job | OOM, preemption, hardware fault                    | Message queue visibility timeout (30 min); job re-queued automatically. Transcoding is idempotent.                                |
| Transcoding queue backlog        | Viral upload spike (major event), slow workers     | Auto-scale worker pool (spot VMs). Priority queue: new uploads > re-transcoding backlog. Alert on queue depth.                    |
| Redis view count loss            | Redis crash before flush                           | AOF persistence (1-second fsync). For monetisation: Kafka-backed durable event log.                                               |
| Origin thundering herd           | Viral video, cache miss at CDN                     | Request coalescing at CDN edge. Pre-warm CDN for anticipated high-traffic events.                                                 |
| Hot shard in metadata DB         | One video_id queried millions of times/sec         | Read replicas for metadata. Cache video metadata in Redis/Memcached (TTL: 60s). Video metadata rarely changes post-upload.        |
| Upload ingest node failure       | Node crash during chunked upload                   | Resumable upload: client queries current offset and retries. Raw bytes in staging object storage are durable.                     |
| Storage cost explosion           | Storing 4K for every video forever                 | Tier storage: hot (SSD-backed), warm (HDD), cold (glacier). Most videos get <1000 views — archive 4K after 30 days of inactivity. |
| Geographic replication lag       | New video not yet propagated to distant CDN region | Multi-region object storage replication (GCS multi-region). CDN miss falls back to nearest region with data, not origin.          |

---

## How It Actually Works

YouTube's architecture is described in several public sources:

**Storage:** YouTube uses Google Cloud Storage (GCS) for video bytes, backed by Google's Colossus distributed file
system. The sheer scale required building custom object storage rather than using off-the-shelf solutions.

**Transcoding:** YouTube processes video using custom codecs (VP9, and increasingly AV1) rather than standard
H.264/H.265. AV1 provides ~30% better compression at the same quality, saving significant CDN bandwidth at scale. The
encoder is highly parallelised across Google's fleet.

**Metadata:** YouTube uses Bigtable (not Postgres) for video metadata at 2+ billion videos. The `video_id` is the row
key. Secondary indexes (by user, by tag) are maintained separately. The lab uses Postgres to demonstrate the same
logical model at small scale.

**CDN:** GGC PoPs are installed inside ISP data centers worldwide. Most YouTube traffic is served directly from ISP
caches, never reaching Google's origin servers. This is why YouTube loads fast even in regions without nearby Google
data centers.

**View counting:** YouTube's view count system uses an architecture similar to what's described here: in-memory counting
followed by periodic aggregation, with a separate fraud-detection pipeline that validates views before crediting them (
bots, repeated views from same IP, etc.).

Source: Google I/O "YouTube Architecture" (2012); YouTube Engineering Blog "AV1 compression" (2018); IMC 2012 paper on
YouTube's CDN; Vitess blog on YouTube's MySQL sharding history.

---

## Hands-on Lab

**Time:** ~20–25 minutes
**Services:** `minio` (object storage), `db` (Postgres 15), `cache` (Redis 7), `nginx` (video gateway + byte-range
proxy)

### Setup

```bash
cd system-design-interview/03-case-studies/03-youtube/
docker compose up -d
# Wait ~20s for all services to become healthy
docker compose ps
```

### Experiment

```bash
python experiment.py
```

Seven phases run automatically:

1. **Upload:** generate a 5 MB synthetic video file, upload to MinIO, store metadata in Postgres with
   `status=processing`
2. **Metadata:** inspect the Postgres record — note that video bytes are never in the DB
3. **Transcoding:** create 360p/720p/1080p variants in MinIO, update Postgres to `status=ready`
4. **HLS manifest:** generate a multi-quality HLS master manifest and a per-quality segment manifest, demonstrating the
   playlist structure clients parse
5. **Byte-range seeking:** issue HTTP range requests via Nginx, observe HTTP 206 responses, compare full-download vs.
   seek latency
6. **View counting:** simulate 10,000 views via Redis INCR, observe that Postgres count stays at 0 until explicit flush
7. **Batch flush:** demonstrate flush of multiple video counters to Postgres in a single pass

### Break It

**Simulate transcoding worker failure:**

```bash
# Show how a video stuck in 'processing' state looks:
docker compose exec db psql -U app youtube -c \
  "UPDATE videos SET status='processing' WHERE id=1;"

# Check: video is "unavailable" until status=ready
docker compose exec db psql -U app youtube -c \
  "SELECT id, title, status FROM videos;"

# Recovery: re-process (requeue job, worker updates status)
docker compose exec db psql -U app youtube -c \
  "UPDATE videos SET status='ready' WHERE id=1;"
```

**Flood view counts:**

```bash
python -c "
import redis, time
r = redis.from_url('redis://localhost:6379')
start = time.time()
pipe = r.pipeline()
for _ in range(100000):
    pipe.incr('view_count:1')
pipe.execute()
elapsed = time.time() - start
count = r.get('view_count:1')
print(f'100K INCR in {elapsed:.2f}s = {100000/elapsed:.0f} ops/s')
print(f'Counter: {count}')
"
```

### Observe

Redis sustains ~500K–1M INCR operations per second from a pipeline, while Postgres struggles beyond ~10K writes/second
for the same workload. The byte-range response (HTTP 206) demonstrates that the client never needs to download the full
file to seek — only the target segment is fetched.

```bash
# Check MinIO objects created
docker compose exec minio mc ls local/videos/ --recursive | head -20
```

### Teardown

```bash
docker compose down -v
```

---

## Interview Checklist

1. **Q: Why store videos in object storage rather than a database or filesystem?**
   A: Object storage (S3/GCS/MinIO) is designed for large binary objects: scales to exabytes without manual capacity
   planning, serves HTTP byte-range requests natively, costs ~$20/TB/month vs ~$200/TB for block storage, and integrates
   directly with CDNs. Postgres BLOBs degrade query performance for all other operations and can't be range-fetched
   efficiently. Filesystems don't scale horizontally.

2. **Q: How long does it take for an uploaded video to be available for viewing?**
   A: Progressive availability: 360p is ready within ~2 minutes (fast transcode), 720p within ~5 minutes, 1080p within ~
   15 minutes, 4K within ~30 minutes. The video is marked `ready` when the lowest quality is available. Users uploading
   long videos (1h+) see longer delays because transcoding time scales with video duration. YouTube processes all
   quality levels in parallel across multiple workers.

3. **Q: How does adaptive bitrate streaming work at a technical level?**
   A: The video is pre-split into 2–6 second `.ts` segments at each quality level. An HLS master manifest (`.m3u8`)
   lists all quality variants; each variant has its own segment manifest. The player measures download throughput,
   selects the highest quality that fits in available bandwidth, and prefetches the next few segments. Quality switches
   happen at segment boundaries, making them imperceptible. Seeking fetches only the segment containing the target
   timestamp via HTTP byte-range — at most one 4-second segment (~2 MB at 1080p).

4. **Q: How do you handle view count at 11,600 views per second?**
   A: Redis `INCR view_count:{video_id}` per view (nanosecond latency, no disk). Background worker flushes to Postgres
   every 60 seconds using `UPDATE videos SET view_count = view_count + $delta`. Reduces 11,600 writes/second to ~1
   write/minute per video. Trade-off: counts lag by up to 60 seconds. For billing/monetisation, a separate Kafka-backed
   pipeline provides a durable event log with zero loss.

5. **Q: How do you scale the transcoding pipeline to handle 500 hours of video per minute?**
   A: Horizontal scaling of transcoding workers pulling from a queue (Kafka/SQS). Each worker handles one video
   independently (embarrassingly parallel). Workers are preemptible spot VMs (cheap, since jobs are retryable via
   visibility timeout). Priority queuing: recently uploaded videos get higher priority than backlog re-transcoding. GPU
   acceleration (NVENC, VAAPI) reduces per-video encoding time by 10–20×.

6. **Q: What happens if a transcoding worker crashes mid-job?**
   A: Jobs have a visibility timeout in the message queue (e.g., 30 minutes). If a worker doesn't acknowledge completion
   within the timeout, the job becomes visible again and another worker picks it up. Transcoding is idempotent:
   re-running produces the same output. The video status stays `processing` until a worker successfully completes and
   writes `status=ready`. Uploaders see "Video processing — check back later."

7. **Q: How does YouTube's CDN work? Why does YouTube load fast globally?**
   A: YouTube uses Google Global Cache (GGC) — private CDN PoPs installed inside ISP data centers worldwide. Popular
   videos are pre-positioned at edge PoPs. The CDN hit rate is ~95%: most views never reach origin. For a viral video,
   all requests may be served from 5–10 CDN PoPs, with only the first request per PoP hitting origin. Cold videos (long
   tail) are cached on demand: first viewer in a region triggers a miss, subsequent viewers hit the edge cache.

8. **Q: How do you deduplicate video uploads? (Same video uploaded by multiple users)**
   A: Compute SHA-256 of the raw upload before storing. Check if a video with that hash already exists. If so, store
   only a metadata pointer to the existing video bytes — the new video can skip transcoding entirely (segments already
   exist) and be marked `ready` immediately. Near-duplicate detection (re-encoded copies) uses YouTube's ContentID
   system: perceptual audio fingerprinting + visual feature hashing, run as a separate ML pipeline post-upload.

9. **Q: How do you shard the video metadata table at 2 billion videos?**
   A: Shard by `video_id` (hash sharding). Each Postgres shard holds ~200M videos at 10 shards. Video ID is a
   Snowflake-style ID encoding the shard number in the high bits, so routing requires no directory lookup — the
   application can compute the target shard from the ID. In production, YouTube uses Bigtable (video_id as row key)
   rather than Postgres — no sharding complexity at all, just horizontal scale.

10. **Q: How do you handle the thundering herd when a viral video is published?**
    A: Multiple mitigations: (1) CDN request coalescing (collapsed forwarding) — only one cache-miss request reaches
    origin while duplicates wait for the response. (2) Pre-warm CDN for anticipated events (live premieres, scheduled
    releases). (3) Stagger subscriber notifications in waves rather than a single fan-out blast. (4) Multi-region object
    storage replication so a CDN miss in Tokyo doesn't hit origin in the US — it hits the nearest GCS region.

11. **Q: Walk me through the complete upload-to-playback flow.**
    A: (1) Client initiates resumable upload → gets `upload_id` and session URL. (2) Client POSTs chunks; server writes
    to staging object storage. (3) On final chunk, raw bytes move to permanent GCS path, metadata row inserted in
    Bigtable with `status=processing`. (4) Upload triggers Pub/Sub event → transcoding job enqueued. (5) Workers
    transcode to all quality levels in parallel, producing HLS segments + manifests. (6) Segments uploaded to GCS;
    Bigtable updated to `status=ready`. (7) Viewer requests video → CDN serves master manifest. (8) Player measures
    bandwidth, selects quality variant, fetches segment manifest. (9) Player downloads first segments, buffers, starts
    playback. (10) Seek: player computes segment index from manifest timestamp, issues byte-range GET for that segment
    only.
