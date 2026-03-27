# Case Study: Uber

**Prerequisites:** `../../01-foundations/06-caching/`, `../../01-foundations/08-databases-sql-vs-nosql/`, `../../02-advanced/07-distributed-caching/`

---

## The Problem at Scale

Uber's core technical challenge is real-time matching: given a rider's location, find the nearest available driver in under 1 second. At peak, 4 million drivers are sending GPS updates every 4 seconds while hundreds of thousands of riders are requesting matches simultaneously.

| Metric | Value |
|---|---|
| Active drivers (peak) | 4 million |
| Location updates per second | 1 million (4M drivers / 4s interval) |
| Rides per day | 25 million |
| Cities served | 10,000+ |
| Match request latency target | < 1 second end-to-end |
| Location update latency target | < 100ms to index |

A naive SQL `SELECT ... ORDER BY distance LIMIT 5` against a 4-million-row table with a geospatial index would take 10-50ms per query. At 100K simultaneous match requests, this requires 100K queries/second — saturating a Postgres cluster. The solution is a purpose-built real-time geospatial index in Redis.

---

## Requirements

### Functional
- Driver app: send GPS location every 4 seconds
- Rider app: find N nearest available drivers within radius R
- Match: assign nearest driver to rider, accounting for ETA not just distance
- Driver state: track availability (available, busy, offline)
- Surge pricing: dynamically price based on supply/demand ratio per zone
- Geofencing: validate pickups/dropoffs are within service boundaries

### Non-Functional
- Match latency P99 < 1 second (includes driver ETA calculation)
- Location index update latency < 100ms (driver location visible to riders quickly)
- Location ingest throughput: 1M updates/second sustained
- Geo query throughput: 100K queries/second
- Location data TTL: evict drivers not seen in 60 seconds (went offline)
- Availability: 99.99% (< 52 min downtime/year) — riders and drivers depend on real-time

### Capacity Estimation

| Metric | Calculation | Result |
|---|---|---|
| Location updates/s | 4M drivers × (1 update / 4s) | 1M/s |
| Location update payload | driver_id (16B) + lat (8B) + lng (8B) + timestamp (8B) | ~40 bytes |
| Ingest bandwidth | 1M updates/s × 40 bytes | ~40 MB/s inbound |
| Redis geo memory/driver | 16 bytes (geohash score) + ~50 bytes (sorted set overhead) | ~66 bytes |
| Total Redis for 4M drivers | 4M × 66B | ~264 MB (fits on a single node) |
| Driver status store | 4M drivers × 32 bytes (status hash) | ~128 MB |
| Match queries/s (average) | 25M rides/day ÷ 86,400s | ~290 queries/s |
| Match queries/s (peak) | 10× average | ~3,000 queries/s |
| ETA cache entries | 3,000 active routes × 30s TTL | ~90,000 entries |
| Trip record size | pickup, dropoff, route polyline, timestamps, fare | ~2 KB |
| Trip history storage/day | 25M trips × 2 KB | 50 GB/day |
| Trip history 5-year retention | 50 GB/day × 365 × 5 | ~91 TB (compressed: ~20 TB) |
| Location history/day | 1M updates/s × 86,400s × 40 bytes | ~3.5 TB/day |

**Scale inflection points:**
- Single Redis node handles ~4M drivers in ~264 MB RAM — sharding needed for write throughput (1M GEOADD/s exceeds ~500K ops/s per Redis node), not memory.
- Kafka at 1M updates/s with 40-byte messages = ~40 MB/s write throughput per topic; achievable with 6-10 partitions on a 3-broker cluster.
- Cassandra location history at 1M writes/s requires a cluster of ~20-30 nodes (each handling ~30-50K writes/s with replication factor 3).

---

## High-Level Architecture

```
  ┌──────────────────────────────────────────────────────────────┐
  │                     Location Update Path                      │
  │                                                               │
  │  Driver App (every 4s)                                        │
  │    → Location Service (WebSocket or HTTP/2)                   │
  │    → Kafka (location_updates topic, partitioned by driver_id) │
  │         ├─→ Redis Consumers: GEOADD drivers:geo              │
  │         └─→ Cassandra Consumers: location_history (append)   │
  └──────────────────────────────────────────────────────────────┘

  ┌──────────────────────────────────────────────────────────────┐
  │                     Match Request Path                        │
  │                                                               │
  │  Rider App → Dispatch Service                                 │
  │    1. Redis GEOSEARCH: nearest N drivers (radius expanding)   │
  │    2. Filter: available status (Redis HGET driver:status:{id})│
  │    3. Filter: vehicle type, rating, preferences               │
  │    4. ETA service: real ETA via road network (cached 30s)     │
  │    5. Rank by ETA ascending                                   │
  │    6. Offer to #1 driver → 15s accept window                 │
  │    7. On decline/timeout → offer to #2, repeat               │
  └──────────────────────────────────────────────────────────────┘

  ┌──────────────────────────────────────────────────────────────┐
  │                     Driver State Machine                      │
  │                                                               │
  │  OFFLINE ←→ AVAILABLE ←→ ON_TRIP                             │
  │                                                               │
  │  State stored in: Redis HSET driver:status:{driver_id}       │
  │  TTL tracking: Redis HSET driver:heartbeat:{driver_id}       │
  │  Stale detection: background job ZRANGEBYSCORE by last_seen  │
  └──────────────────────────────────────────────────────────────┘

  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
  │     Redis        │  │    PostGIS       │  │    Cassandra     │
  │  drivers:geo     │  │  (geofences,     │  │  location_history│
  │  driver:status:* │  │   zone polygons, │  │  (timeseries,    │
  │  surge:h3:*      │  │   analytics)     │  │   audit log)     │
  │  eta:cache:*     │  │                  │  │                  │
  └──────────────────┘  └──────────────────┘  └──────────────────┘
```

**Redis geo commands** are the real-time driver index. `GEOADD` stores each driver's location as a geohash-encoded integer in a sorted set. `GEOSEARCH` retrieves all drivers within a radius in sub-millisecond time by translating the radius into a set of geohash cell ranges and doing a sorted set range scan. The entire 4M-driver index fits in ~264 MB of RAM.

**PostGIS** handles spatial queries that require database-level complexity: geofence polygon storage and point-in-polygon tests, demand heatmap generation, city boundary management, and historical analysis. PostGIS queries are slower (milliseconds) but benefit from full SQL expressiveness and ACID guarantees.

**Cassandra** stores the historical location stream (driver_id, timestamp, lat, lng). This time-series data is write-heavy (1M inserts/second) and queried by time range (e.g., "all locations for driver X between 2pm-3pm"). Cassandra's time-series write performance and time-ordered clustering keys make it a better fit than Postgres for this access pattern.

**Kafka** decouples the 1M/s location ingest from downstream consumers. Partitioning by `driver_id % num_partitions` ensures all updates for a given driver are processed in order by the same consumer, preventing an old location from overwriting a newer one.

---

## Deep Dives

### 1. Geo-Indexing: Redis for Real-Time, PostGIS for History

Redis implements geo commands on top of its sorted set data structure. Every `GEOADD` operation converts (latitude, longitude) to a 52-bit geohash integer and stores it as a sorted set score. This encoding has ~0.6m precision at the equator — more than sufficient for driver matching.

The encoding enables a critical optimisation: nearby locations have nearby geohash values, so a radius query becomes a range scan on the sorted set. Redis translates the search circle into 8-9 geohash cells that cover the circle (plus a fringe buffer), then scans only those score ranges. This is why `GEOSEARCH` scales sub-linearly with total driver count — it only examines drivers in nearby cells, not all 4M.

**Key Redis geo operations:**
- `GEOADD key lng lat member` — O(log N) insert/update
- `GEOSEARCH key FROMLONLAT lng lat BYRADIUS radius unit [WITHDIST] [COUNT n] [ASC]` — O(N+log(M)); replaces deprecated `GEORADIUS`
- `GEODIST key member1 member2 unit` — O(log N) exact distance
- `GEOPOS key member` — O(log N) retrieve stored position

Redis does not support point-in-polygon queries directly. Geofence checks are implemented at the application layer (ray-casting algorithm) or in PostGIS (`ST_Contains`).

### 2. Driver State Machine and Availability Tracking

The geo index alone is insufficient — a driver visible in `GEOSEARCH` may already be on a trip. Availability state is a separate concern from location:

```
OFFLINE  ──[app opens]──▶  AVAILABLE  ──[trip assigned]──▶  ON_TRIP
                              ◀──[trip ends]──
    ◀──[app closes or 60s heartbeat timeout]──
```

**Implementation in Redis:**
```
# Driver goes online:
HSET driver:status:{driver_id} status AVAILABLE vehicle_type UberX last_seen 1710000000

# Driver accepts trip:
HSET driver:status:{driver_id} status ON_TRIP trip_id trip_abc

# Stale driver eviction (background job every 10s):
ZRANGEBYSCORE driver:heartbeats 0 {now - 60}  → list of stale drivers
  → ZREM driver:heartbeats {driver_id}
  → ZREM drivers:geo {driver_id}
  → HSET driver:status:{driver_id} status OFFLINE
```

Redis does not support per-member TTL on sorted sets (only per-key TTL). Uber tracks last-seen timestamps in a separate sorted set (`driver:heartbeats`, scored by last_seen epoch) and runs a cleanup process to evict stale entries. This avoids keeping a polling loop per driver and scales to millions of drivers with a single range scan.

**Why not per-key TTL?** If each driver were a separate Redis key, 4M keys with individual TTLs would create 1M TTL-expiry events per 4-second window, overwhelming Redis's active expiry mechanism. The heartbeat sorted set approach does one range scan.

### 3. Matching Algorithm: ETA Minimisation, Not Distance

Matching by straight-line distance (`GEOSEARCH` nearest) is a simplification. The actual Uber dispatch algorithm minimises estimated time of arrival (ETA), not distance. A driver 0.5km away on the other side of a river might have an ETA of 8 minutes, while a driver 1.5km away on the same side has an ETA of 3 minutes.

**ETA calculation pipeline:**
1. `GEOSEARCH` returns 10–20 candidate drivers in the area
2. Filter by `driver:status:{id}` — keep only AVAILABLE with matching vehicle type
3. For each candidate, call the ETA service (maps API with real-time traffic)
4. Rank by ETA ascending; offer the trip to the #1 driver
5. Driver has 15 seconds to accept; on decline, offer to #2

The ETA service is expensive (road network graph traversal per driver). Uber caches ETA results in Redis with a 30-second TTL keyed by `eta:{origin_h3_cell}:{destination_h3_cell}` to avoid recomputing the same route multiple times. Most offers within a cell pair share a cached ETA, reducing ETA service load by ~80%.

**Radius expansion:** If `GEOSEARCH` returns fewer than 3 available candidates (drivers online but all on trips), the dispatch service expands the radius: 1km → 2km → 5km → fallback to "no drivers available." This cascading search prevents bad matches from nearby-but-unavailable drivers.

### 4. Location Update Pipeline

Driver location updates flow through Kafka before reaching Redis. This decouples the 1M/s ingest rate from the Redis write path and provides a replay buffer for catchup:

```
Driver → WebSocket/HTTP/2 → Location Service → Kafka (location_updates)
                                                      │
                                             ┌────────┴────────┐
                                             │                 │
                                       Redis Consumers  Cassandra Consumers
                                       (real-time index) (history store)
```

Kafka partitioning: partition by `driver_id % num_partitions`. This ensures all location updates for one driver go to the same partition (and same Redis consumer), preventing out-of-order updates from overwriting a newer position with an older one.

**WebSocket vs polling:** Uber's driver app maintains a persistent WebSocket connection to the Location Service. This is ~10× more efficient than HTTP polling at 4-second intervals:
- HTTP polling: 1M drivers × 1 HTTP request/4s = 250K TCP connections/s (new connection overhead)
- WebSocket: 1M drivers × 1 persistent connection = 1M concurrent sockets; each update is a small frame on an existing connection
- Trade-off: WebSocket requires stateful connection management; HTTP is easier to load-balance

**Consumer lag:** If Redis consumers fall behind, the geo index has stale data — drivers appear at their old positions. The acceptable lag is one update interval (4 seconds). Uber monitors consumer lag with Kafka consumer group offsets; lag > 4 seconds triggers an alert.

### 5. Surge Pricing: H3 Hexagonal Grid

Surge pricing multiplier is computed per geographic cell. Uber uses their H3 library (open-sourced in 2018) which divides the Earth into a hierarchical hexagonal grid.

**Why hexagons?** In a square grid, diagonal neighbours are ~41% farther away than edge neighbours. In a hexagonal grid, all 6 neighbours are equidistant. This eliminates directional bias: a driver in the cell to the northeast is just as "close" as one directly north, which matters for demand calculation and cell aggregation.

**Surge calculation (per H3 cell, every 60 seconds):**
1. Count available drivers in cell: query Redis GEOSEARCH within cell boundary
2. Count open ride requests in cell: query pending request queue
3. `surge_multiplier = max(1.0, 1.0 + (requests - drivers) / max(drivers, 1))`
4. Store in Redis: `HSET surge:{h3_cell_id} multiplier 1.8 drivers 12 requests 30 ts {now}`
5. Client app colors map by surge multiplier, adjusts displayed price

**H3 hierarchy:** Resolution 8 cells are ~460m across (used for surge pricing). Resolution 9 cells are ~175m (used for precise pickup zones). Resolution 6 cells (~36km) are used for city-level demand forecasting. H3's parent-child relationships allow rolling up fine-grained data to coarser cells efficiently.

### 6. Redis Sharding Strategy

A single Redis node handles ~500K write ops/second. At 1M location updates/second, sharding is required:

**Geographic sharding (Uber's DISCO approach):**
- Divide the world into N geographic shards (e.g., by H3 cell prefix or city)
- Each shard owns drivers in its geographic partition
- A request routes to the shard(s) covering the query area
- Edge cases: drivers near shard boundaries appear in multiple shards

**Hash-slot sharding (Redis Cluster):**
- Redis Cluster uses 16,384 hash slots; key `drivers:geo:{region}` maps to a slot
- Queries spanning multiple regions require scatter-gather across shards
- Simpler operationally than geographic sharding but less locality-aware

**Cross-shard queries:** A match request near a city boundary may need to query two adjacent shards. Dispatch service sends parallel queries and merges results, adding ~1ms of fan-out latency.

---

## Failure Modes and Scale Challenges

| Failure | Impact | Mitigation |
|---|---|---|
| Redis primary failover | 10-30s where `GEOSEARCH` fails; drivers invisible | Redis Sentinel/Cluster with replica promotion; circuit breaker returns cached results |
| Kafka consumer lag | Geo index shows stale driver positions (up to minutes old) | Consumer group lag alerting; auto-scale consumer group; reduce batch sizes |
| Location Service overload | Location updates dropped | Rate-limit at 1 update/driver/2s; Kafka acts as buffer, absorbs bursts |
| ETA service down | Matching falls back to distance-only ranking | Circuit breaker; cache last-known ETAs; distance as fallback metric |
| PostGIS failover | Historical queries fail; real-time matching unaffected | Read replicas; Redis is source of truth for real-time, PostGIS is read-heavy |
| Driver GPS spoofing | Driver appears at fake location to cherry-pick rides | Server-side velocity check; cross-validate with cell tower data; ML anomaly detection |
| "Cold start" in a new city | No drivers → no rides → no drivers (chicken-and-egg) | Seed market with incentivized supply; pre-compute ETAs for common routes |
| Surge pricing race condition | Two workers compute surge simultaneously; last-write-wins | Redis Lua script for atomic read-modify-write; partition surge computation by cell |
| Split-brain: Redis replica divergence | Two dispatch nodes see different driver positions | Quorum reads via Redis Cluster; accept eventual consistency within one update interval |

---

## How It Actually Works

Uber's engineering blog describes their "Ringpop" and later "Schemaless" systems. Key details from public engineering posts:

**Real-time location system:** Uber uses a custom in-memory datastore called "DISCO" (Driver Index with Spatial Coordinates Operations) rather than vanilla Redis, purpose-built for geo queries at their scale. DISCO is sharded by geohash cell, so each shard only stores drivers in its geographic partition. This lets them scale write throughput by adding shards rather than scaling a monolithic Redis instance.

**Dispatch system:** The dispatch service is itself sharded — each instance owns a subset of geographic cells. When a rider requests a trip, the request routes to the dispatch instance owning the rider's cell. This instance queries its local DISCO shard plus adjacent shards for border cases. This avoids cross-data-center coordination for the common case.

**Supply/demand prediction:** Uber runs an ML model that predicts ride demand in each H3 cell 15-60 minutes ahead, allowing the system to proactively incentivise drivers to reposition to high-demand zones before demand materialises. Predictions are based on historical patterns, events, weather, and time of day.

**The "God View":** Uber's internal dashboard shows real-time locations of all drivers and riders globally. This is served from the same geo index, with WebSocket streaming of position updates to the dashboard. At 1M updates/second, the dashboard samples at 1/10 rate to avoid overwhelming browser rendering.

Source: Uber Engineering Blog, "How Uber Uses Location Data in the Real World" (2017); Uber Engineering Blog, "Spatial Indexes" (2017); H3 GitHub repository and documentation (2018); Uber Engineering Blog, "Streamline: Uber's Dispatch Platform" (2022).

---

## Hands-on Lab

**Time:** ~20–25 minutes
**Services:** `db` (PostGIS 15-3.3), `cache` (Redis 7), `kafka` + `zookeeper` (Confluent 7.5), `api` (Python/Flask)

### Setup

```bash
cd system-design-interview/03-case-studies/04-uber/
docker compose up -d
# Wait ~30s for Kafka and PostGIS to initialize
docker compose ps
```

### Experiment

```bash
python experiment.py
```

Seven phases run automatically:

1. **Seed:** GEOADD 1,000 driver locations in San Francisco bounding box, measure throughput
2. **Find nearest + ETA simulation:** GEOSEARCH from rider position, simulate ETA ranking vs distance ranking
3. **Driver state machine:** demonstrate AVAILABLE → ON_TRIP → AVAILABLE transitions with stale driver eviction
4. **Update throughput:** pipeline 500 GEOADD commands, measure ops/second, extrapolate to 4M drivers
5. **Geofence:** ray-casting point-in-polygon test for 5 locations against SF boundary polygon + surge pricing simulation
6. **PostGIS:** sync 200 drivers, run ST_DWithin query, compare with Redis GEOSEARCH
7. **Latency comparison:** benchmark Redis vs PostGIS at multiple search radii

### Break It

**Simulate driver going offline (TTL expiry):**

```bash
# Remove a specific driver from the geo index and heartbeat tracker
docker compose exec cache redis-cli ZREM drivers:geo driver_0001
docker compose exec cache redis-cli HDEL driver:status:driver_0001 status

# Verify they no longer appear in nearby search:
docker compose exec cache redis-cli GEOSEARCH drivers:geo FROMLONLAT -122.4194 37.7749 BYRADIUS 5 km ASC WITHDIST COUNT 10
```

**Observe the "empty area" problem:** if all drivers in a cell go offline simultaneously (shift change), `GEOSEARCH` returns 0 results. The app must handle this by expanding the radius, showing "no drivers available," or routing the request to a driver pool in an adjacent cell.

**Simulate Kafka consumer lag:**

```bash
# Pause the Redis consumer group to simulate lag
docker compose exec kafka kafka-consumer-groups.sh \
  --bootstrap-server localhost:9092 \
  --group location-redis-consumer \
  --describe
# Check LAG column — should be 0 normally
```

### Observe

```bash
# Check Redis geo index memory usage
docker compose exec cache redis-cli MEMORY USAGE drivers:geo
# Notice: 1000 drivers ≈ only ~60KB — extremely memory-efficient
# Extrapolate: 4M drivers ≈ 240MB — fits on a single Redis node

# Count drivers in a specific area
docker compose exec cache redis-cli GEOSEARCH drivers:geo FROMLONLAT -122.4194 37.7749 BYRADIUS 2 km ASC COUNT 100 | wc -l

# Check driver status entries
docker compose exec cache redis-cli KEYS "driver:status:*" | wc -l

# View surge pricing state
docker compose exec cache redis-cli HGETALL surge:h3:8928308280fffff
```

### Teardown

```bash
docker compose down -v
```

---

## Interview Checklist

1. **Q: Why use Redis GEOSEARCH instead of PostGIS for real-time driver matching?**
   A: Redis GEOSEARCH returns results in sub-millisecond time from memory. PostGIS with a GIST index takes 5-50ms from disk even with good indexes, and doesn't scale to 100K queries/second without massive infrastructure. Redis stores 4M driver locations in ~264 MB of RAM. The real-time matching use case prioritises latency over query flexibility — PostGIS is used where complex spatial SQL is needed (geofence management, analytics).

2. **Q: How does Redis implement GEOSEARCH internally?**
   A: Redis converts (lat, lng) to a 52-bit geohash integer using a space-filling curve (interleaving latitude and longitude bits). This integer is stored as the score in a Redis sorted set. A radius query translates the circle into 8-9 geohash cells covering the area, then scans only those score ranges in the sorted set. Nearby points have nearby geohash scores, so the scan is bounded. Finally, each candidate is filtered by exact Haversine distance to remove points at the corners of the covering cells. Time complexity: O(N+log(M)) where N = results returned and M = total drivers.

3. **Q: How do you handle 1M location updates per second without dropping updates?**
   A: Kafka absorbs the 1M/s ingest spike: drivers write to Kafka (durable, high-throughput), and Redis consumers read from Kafka and update the geo index. Kafka partitioned by `driver_id` ensures ordered processing per driver — prevents an old location from overwriting a newer one. If Redis consumers lag, Kafka retains messages; no updates are lost. The acceptable lag is ~4 seconds (one update interval), after which stale locations affect match quality but don't cause data loss. Consumer lag is monitored via Kafka consumer group metrics; lag > 4s triggers auto-scaling.

4. **Q: Why is the matching algorithm based on ETA rather than distance?**
   A: Urban geography creates large discrepancies between straight-line distance and drive time. A driver across a river may be 0.3km away but 8 minutes ETA. A driver 1.5km away on the same street may be 2 minutes ETA. Minimising ETA produces better rider experience and higher driver utilisation. The ETA calculation uses real-time traffic data (cached per route cell in Redis with 30s TTL). ETA is computed only for filtered AVAILABLE candidates (10-20), not all 4M drivers.

5. **Q: How does Uber implement surge pricing at geographic scale?**
   A: The city is divided into H3 hexagonal cells (~460m diameter at resolution 8). Every 60 seconds, a worker counts available drivers and open requests in each cell, computes `surge = max(1.0, 1.0 + (requests - drivers) / max(drivers, 1))`, and stores the result in Redis (`HSET surge:{h3_cell_id} multiplier 1.8 drivers 12 requests 30`). The rider app fetches surge multipliers for visible H3 cells and colors the map accordingly. H3 hexagons are preferred over squares because all 6 neighbors are equidistant, eliminating directional bias. Surge computation is partitioned by cell prefix so multiple workers operate without contention.

6. **Q: What happens when a driver's GPS signal is lost for 60 seconds?**
   A: Drivers are evicted after 60 seconds without a heartbeat. Since Redis geo doesn't support per-member TTL (only per-key TTL), last-seen timestamps are tracked in a separate sorted set (`driver:heartbeats`, scored by epoch seconds). A background job runs every 10 seconds: `ZRANGEBYSCORE driver:heartbeats 0 {now-60}` returns stale drivers; for each, `ZREM drivers:geo`, update status to OFFLINE. This approach scales to millions of drivers with a single range scan rather than millions of individual TTL timers. When GPS resumes, the next GEOADD restores their position and a heartbeat update moves their status back to AVAILABLE.

7. **Q: How do you shard the Redis geo index for 4M drivers?**
   A: The bottleneck is write throughput (1M GEOADD/s), not memory (264 MB fits on one node). Shard by geographic region using H3 cell prefixes. Each Redis shard owns drivers in its geographic partition. A match request near shard boundary queries two adjacent shards in parallel and merges results (scatter-gather, adds ~1ms). Uber's DISCO system takes this further: each DISCO instance owns a geohash cell range, so location updates route to exactly one instance — no cross-shard coordination for writes. Edge case: drivers at shard boundaries must be registered in both adjacent shards.

8. **Q: How would you build the "ETA service" that estimates driver arrival time?**
   A: The ETA service uses a road network graph (OpenStreetMap or proprietary data) with real-time traffic weights. It runs Dijkstra's or A* shortest-path algorithm from driver location to rider location. At scale: pre-compute ETAs for common `(source_h3_cell, destination_h3_cell)` pairs and cache them in Redis (30-second TTL). This converts most ETA lookups to O(1) cache hits (~80% hit rate in dense areas). The road network graph is updated with traffic data every 60 seconds from GPS traces of all moving vehicles — Uber uses its own fleet as a massive traffic sensor, giving real-time traffic that commercial APIs can't match.

9. **Q: How do you ensure GPS location data is accurate and not spoofed?**
   A: Server-side velocity check: if a new location is > N km from the previous location in < T seconds (exceeding maximum possible vehicle speed), reject as spoofed. Cross-validate GPS with cell tower triangulation and WiFi positioning (available via the phone's Location Manager). Flag suspicious patterns: driver appears in two cities simultaneously, location jumps > 200 km/h, location trajectory inconsistent with road network. ML models trained on legitimate driver trajectories detect anomalies. Severe violations trigger fraud investigation and account suspension.

10. **Q: How does availability state interact with the geo index? Why track them separately?**
    A: The geo index (`drivers:geo`) tracks location only — it cannot distinguish between an available driver and one mid-trip. Storing availability in the geo index would require removing drivers on trip and re-adding them when free, causing ~25M additional writes/day (one remove + one add per trip). Instead, availability is a separate Redis hash: `driver:status:{driver_id}`. During dispatch, `GEOSEARCH` returns the N nearest by location, then a pipeline of `HGET driver:status:{id}` filters to AVAILABLE only. This separation also allows updating location and status independently at different rates (location every 4s, status only on state transitions).

11. **Q: Walk me through matching a rider to a driver, end to end.**
    A: (1) Rider opens app → client shows nearby drivers (GEOSEARCH, displayed on map as dots). (2) Rider requests trip → Dispatch Service receives request with pickup coordinates, vehicle type, time. (3) Dispatch queries Redis GEOSEARCH: top 20 nearest drivers within 5km, expand to 10km if < 3 results. (4) Pipeline HGET for each candidate's status; keep only AVAILABLE + matching vehicle type — typically reduces to 5-8 candidates. (5) ETA Service computes drive time from each candidate to pickup (cache hit ~80% of the time). (6) Rank by ETA ascending; offer trip to #1 driver via push notification. (7) Driver has 15s to accept; on accept: HSET driver:status TRIP, create trip record in Postgres, notify rider. (8) On decline/timeout: offer to #2. (9) Fallback: if all candidates decline, expand radius to 10km and retry once. (10) If still no acceptance: return "no drivers available" after ~90 seconds total.
