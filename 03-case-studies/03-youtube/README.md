# Case Study: YouTube

**Prerequisites:** `../../01-foundations/06-caching/`, `../../01-foundations/10-networking-basics/`, `../../01-foundations/12-blob-object-storage/`, `../../02-advanced/10-cdn-and-edge/`

---

## The Problem at Scale

YouTube is the world's largest video platform. The upload-to-playback pipeline must handle both massive write throughput (uploads) and enormous read throughput (views), with global distribution and sub-second seek latency.

| Metric | Value |
|---|---|
| Video uploaded per minute | 500 hours |
| Hours watched per day | 1 billion |
| Peak concurrent viewers | ~100 million |
| Average video size (1080p, 10 min) | ~1.5 GB |
| Daily upload storage | 500h × 60min × 1.5GB ≈ 45 TB/day |
| CDN bandwidth (peak) | ~500 Tbps globally |

The core engineering challenge: a video uploaded in São Paulo must be transcoded into 5 quality levels and distributed to CDN edge servers in Tokyo, London, and New York — all within minutes of upload, serving millions of concurrent viewers with sub-second seek latency.

---

## Requirements

### Functional
- Upload video (any format, up to 256 GB)
- Transcode to multiple resolutions: 360p, 480p, 720p, 1080p, 4K
- Stream video with adaptive quality (auto-adjusts based on bandwidth)
- Seek to any point in the video without re-downloading
- Track view counts, likes, comments
- Search videos by title, tag, transcript

### Non-Functional
- Video available for playback within 5 minutes of upload (for most resolutions)
- Playback start latency < 3 seconds globally
- Seek latency < 500ms (byte-range request to CDN)
- 99.99% playback availability (CDN redundancy)
- Storage durability: 11 nines (same as S3) — a video deleted from YouTube should be rare

### Capacity Estimation

| Metric | Calculation | Result |
|---|---|---|
| Upload throughput | 500h/min = 8.3h/s of video | ~500 MB/s raw ingest |
| Storage (10 years) | 45 TB/day × 365 × 10 | ~164 PB |
| Transcoded storage multiplier | ~4× for all quality levels | ~650 PB total |
| View bandwidth | 1B hours × avg 2 Mbps | ~694 Tbps/day average |
| CDN hit ratio | 95% of views served from CDN | — |
| Origin bandwidth | 5% × 694 Tbps | ~35 Tbps to origin |

---

## High-Level Architecture

```
  ┌─────────────────────────────────────────────────────────────────┐
  │                      Upload Path                                 │
  │                                                                  │
  │  Client → Upload API → Object Storage (raw .mp4)                │
  │                     → Postgres (metadata: status=processing)    │
  │                     → Message Queue (transcoding job)           │
  │                                                                  │
  │  Transcoding Workers (pull from queue):                         │
  │    FFmpeg: raw → 360p/720p/1080p/4K HLS segments (.ts files)   │
  │    Upload segments to Object Storage                             │
  │    Update Postgres: status=ready, add quality entries           │
  └─────────────────────────────────────────────────────────────────┘

  ┌─────────────────────────────────────────────────────────────────┐
  │                      Playback Path                               │
  │                                                                  │
  │  Client → CDN Edge ──► (cache hit) → serve .ts segment          │
  │                    └── (miss) → Origin Nginx → Object Storage   │
  │                                                                  │
  │  Client player (adaptive bitrate):                              │
  │    1. Fetch manifest (.m3u8): list of segments + quality levels │
  │    2. Measure bandwidth, choose quality                          │
  │    3. Prefetch next 3-5 segments                                │
  │    4. Switch quality dynamically based on throughput             │
  └─────────────────────────────────────────────────────────────────┘

  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
  │  Object Storage  │  │    Postgres       │  │     Redis        │
  │  (MinIO/GCS/S3)  │  │  videos table     │  │  view_count:id   │
  │  raw + segments  │  │  video_qualities  │  │  (INCR, flush)   │
  └──────────────────┘  └──────────────────┘  └──────────────────┘
```

**Object storage (MinIO/GCS/S3)** stores all video bytes: the original upload and all transcoded outputs. Object storage is chosen over a filesystem because it scales to petabytes without capacity planning, serves HTTP byte-range requests natively, integrates directly with CDNs, and costs ~$20/TB/month vs ~$200/TB/month for block storage.

**Postgres** stores structured metadata: video title, uploader, upload time, status (uploading/processing/ready/failed), duration, and per-quality file paths. It never stores video bytes. The `view_count` column is updated by a batch flush process, not per-view.

**Transcoding workers** are the most compute-intensive part of the system. A 1-hour video at 4K requires ~30 minutes of CPU time per quality level. YouTube runs thousands of these workers (GPU-accelerated in production) and prioritises recently uploaded videos for faster availability.

**Redis** holds view count increments in memory. Each view executes `INCR view_count:{video_id}` (nanosecond latency, no disk I/O). A background process flushes accumulated counts to Postgres every 60 seconds.

---

## Deep Dives

### 1. Transcoding Pipeline

Raw video arrives in any codec and container format (H.264, HEVC, AV1, MKV, MOV, AVI). YouTube's transcoding pipeline:

1. **Ingest:** validate file, extract metadata (duration, resolution, codec)
2. **Enqueue:** publish a transcoding job to a message queue (Pub/Sub or Kafka)
3. **Decode:** workers pull the job, download the raw file from object storage, decode with FFmpeg
4. **Encode:** produce outputs at each quality level using YouTube's custom VP9/AV1 encoder
5. **Segment:** split each quality level into 2-second `.ts` chunks (HLS) or `.m4s` fragments (DASH), generate manifest files (`.m3u8` / `.mpd`)
6. **Upload:** push all segments to object storage
7. **Notify:** update Postgres status to `ready`, send notification to uploader

Key design decisions:
- **Parallel transcoding:** all quality levels are transcoded in parallel across multiple workers, not sequentially. A 10-minute video can be fully processed in ~3 minutes.
- **Progressive availability:** the 360p version is available within ~2 minutes (fast to transcode), while 4K may take 15+ minutes. The video is marked `ready` when at least 360p is available.
- **Thumbnail extraction:** a separate worker extracts 3 thumbnail candidates at 10%, 50%, 90% of video duration.

### 2. Adaptive Bitrate Streaming (HLS/DASH)

Traditional progressive download serves one large MP4. A viewer who seeks to minute 45 of a 1-hour video must first download 45 minutes of data. This is wasteful and breaks on slow connections.

**HLS (HTTP Live Streaming)** solves this by splitting each quality level into small segments (2–6 seconds each). The manifest file (`.m3u8`) lists all segments:

```
#EXTM3U
#EXT-X-VERSION:3
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
4. Keep a 10-30 second buffer of pre-fetched segments

This means quality switches happen between segment boundaries (every 4 seconds), making them nearly invisible to viewers.

**Seeking:** the player calculates which segment contains the target timestamp from the manifest, then issues a byte-range HTTP request for just that segment. A seek to any position requires downloading at most one 4-second segment (~2MB at 1080p), not the entire video.

### 3. View Count at Scale

At YouTube's scale (1B views/day = ~11,600 views/second), per-view database writes are completely infeasible. The solution:

**Per-view:** `INCR view_count:{video_id}` in Redis. Redis handles millions of operations per second on a single node. This is atomic (no race conditions), takes ~0.1ms, and requires no disk I/O.

**Batch flush (every 60 seconds):** a background worker scans all `view_count:*` keys, reads and deletes each counter (`GETDEL`), and executes a single `UPDATE videos SET view_count = view_count + $delta WHERE id = $video_id` per video. For 1000 videos with active views, this is 1000 Postgres writes per minute — completely manageable.

**Accuracy trade-off:** view counts shown to users lag by up to 60 seconds. YouTube's public view counts have historically had additional delays (hours to days) because their actual pipeline includes fraud detection and deduplication before crediting views.

---

## How It Actually Works

YouTube's architecture is described in several public sources:

**Storage:** YouTube uses Google Cloud Storage (GCS) for video bytes, backed by Google's Colossus distributed file system. The sheer scale required Google to build custom object storage rather than use off-the-shelf solutions.

**Transcoding:** YouTube processes video using a custom codec (VP9, and increasingly AV1) rather than standard H.264/H.265. AV1 provides ~30% better compression at the same quality, saving significant CDN bandwidth at scale. The encoder is highly parallelised across Google's fleet.

**CDN:** YouTube uses Google's private CDN (Google Global Cache, or GGC), with PoPs in ISP data centers worldwide. Most YouTube traffic is served directly from ISP caches, never reaching Google's origin servers. This is why YouTube load times feel fast even in regions without Google data centers.

**View counting:** YouTube's view count system uses a pipeline similar to what's described here: in-memory counting followed by periodic aggregation. However, YouTube also runs a separate fraud-detection pipeline that validates views before crediting them (bots, repeated views from same IP, etc.). This is why view counts on a new video sometimes appear to "stall" at 300-1000 views before jumping.

Source: Google I/O talks "YouTube Architecture" (2012); YouTube Engineering Blog "AV1 compression" (2018); academic paper on YouTube's CDN architecture (2012 IMC conference).

---

## Hands-on Lab

**Time:** ~20–25 minutes
**Services:** `minio` (object storage), `db` (Postgres 15), `cache` (Redis 7), `nginx` (video gateway + byte-range proxy)

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

Six phases run automatically:

1. **Upload:** generate a 5MB synthetic video file, upload to MinIO, store metadata in Postgres with `status=processing`
2. **Metadata:** inspect the Postgres record — note that video bytes are never in the DB
3. **Transcoding:** create 360p/720p/1080p variants in MinIO, update Postgres to `status=ready`
4. **Byte-range seeking:** issue HTTP range requests via Nginx, observe HTTP 206 responses, compare full-download vs. seek latency
5. **View counting:** simulate 1000 views via Redis INCR, observe that Postgres count stays at 0 until explicit flush
6. **Batch flush:** demonstrate flush of multiple video counters to Postgres in a single pass

### Break It

**Simulate transcoding worker failure:**

```bash
# Upload a video (phase 1 from experiment.py sets status=processing)
# Then kill the "transcoding worker" before it completes:
# In a real system, jobs would be re-queued after visibility timeout

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

Redis can sustain ~500K-1M INCR operations per second from a pipeline, while Postgres would struggle beyond ~10K writes/second for the same workload. The byte-range response (HTTP 206) demonstrates that the client never needs to download the full file to seek — only the target segment.

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
   A: Object storage (S3/GCS/MinIO) is designed for large binary objects: it scales to exabytes without manual capacity planning, serves HTTP byte-range requests natively, costs ~$20/TB/month vs ~$200/TB for block storage, and integrates directly with CDNs. Postgres BLOBs degrade query performance for all other operations and can't be range-fetched efficiently. Filesystems don't scale horizontally.

2. **Q: How long does it take for an uploaded video to be available for viewing?**
   A: Progressive availability: 360p is ready within ~2 minutes (fast transcode), 720p within ~5 minutes, 1080p within ~15 minutes, 4K within ~30 minutes. The video is marked `ready` when the lowest quality is available. Users uploading long videos (1h+) see longer delays because transcoding time scales with video duration. YouTube processes uploads in parallel across multiple workers.

3. **Q: How does adaptive bitrate streaming work at a technical level?**
   A: The video is pre-split into 2-6 second `.ts` segments at each quality level. An HLS manifest (`.m3u8`) lists all segments. The player measures download throughput, selects the highest quality that fits in available bandwidth, and prefetches the next few segments. Quality switches happen at segment boundaries (every 2-6 seconds), making them imperceptible. Seeking fetches just the segment containing the target timestamp via HTTP byte-range.

4. **Q: How do you handle view count at 11,600 views per second?**
   A: Redis `INCR view_count:{video_id}` per view (nanosecond latency, no disk). Background worker flushes to Postgres every 60 seconds using `UPDATE videos SET view_count = view_count + $delta`. This reduces 11,600 writes/second to ~1 write/minute per video. Trade-off: counts lag by up to 60 seconds. For billing/monetisation, a more durable pipeline (Kafka + stream processing) is used.

5. **Q: How do you scale the transcoding pipeline to handle 500 hours of video per minute?**
   A: Horizontal scaling of transcoding workers pulling from a queue (Kafka/SQS). Each worker handles one video independently (embarrassingly parallel). Workers are preemptible spot/preemptible VMs (cheap, since jobs are retryable). Priority queuing: recently uploaded videos get higher priority than backlog re-transcoding. GPU acceleration (NVENC, VAAPI) reduces per-video CPU time by 10-20×.

6. **Q: What happens if a transcoding worker crashes mid-job?**
   A: Jobs have a visibility timeout in the message queue. If a worker doesn't acknowledge completion within the timeout (e.g., 30 minutes), the job becomes visible again and another worker picks it up. Transcoding is idempotent: re-running produces the same output. The video status stays `processing` until a worker successfully completes. Uploaders see a "Video processing — check back later" message.

7. **Q: How does YouTube's CDN work? Why does YouTube load fast globally?**
   A: YouTube uses Google Global Cache (GGC) — private CDN PoPs installed inside ISP data centers worldwide. Popular videos are pre-positioned at edge PoPs closest to viewers. The CDN hit rate is ~95%: most views never reach Google's origin servers. For a video with 1M views/day, all 1M requests might be served from 5-10 CDN PoPs close to viewers, with only the first request per PoP hitting origin.

8. **Q: How do you deduplicate video uploads? (Same video uploaded by multiple users)**
   A: Compute SHA-256 of the raw upload before storing. Check if a video with that hash already exists. If so, store only a metadata pointer to the existing video bytes (content-based deduplication). This saves significant storage for: (a) re-uploaded pirated content, (b) the same clip uploaded by multiple news channels. YouTube's ContentID system extends this to detect re-encoded and edited versions.

9. **Q: How do you shard the videos metadata table at 2 billion videos?**
   A: Shard by `video_id` (hash sharding). Each Postgres shard holds ~200M videos at 10 shards. Video ID is a Snowflake-style ID encoding the shard number in the high bits, so routing requires no directory lookup. Alternatively, use a NoSQL store (Cassandra, Bigtable) where video_id is the partition key — YouTube actually uses Bigtable for video metadata in production.

10. **Q: Walk me through the complete upload-to-playback flow.**
    A: (1) Client uploads raw video to Upload API via resumable upload (chunked, retry-safe). (2) Raw bytes stored in GCS, metadata in Bigtable with status=processing. (3) Upload triggers Pub/Sub event → transcoding job enqueued. (4) Workers transcode to all quality levels, producing HLS segments. (5) Segments uploaded to GCS, manifest updated. (6) Metadata updated to status=ready. (7) Viewer requests video → client fetches manifest from CDN. (8) CDN hit: serve segment. CDN miss: origin fetches from GCS, caches, serves. (9) Player measures bandwidth, selects quality, prefetches segments. (10) Seek: player computes segment index from manifest timestamp, fetches that segment via byte-range.
