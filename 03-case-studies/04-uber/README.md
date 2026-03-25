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
- Surge pricing: dynamically price based on supply/demand ratio per zone
- Geofencing: validate pickups/dropoffs are within service boundaries

### Non-Functional
- Match latency P99 < 1 second (includes driver ETA calculation)
- Location index update latency < 100ms (driver location visible to riders quickly)
- Location ingest throughput: 1M updates/second sustained
- Geo query throughput: 100K queries/second
- Location data TTL: evict drivers not seen in 60 seconds (went offline)

### Capacity Estimation

| Metric | Calculation | Result |
|---|---|---|
| Location updates/s | 4M drivers × (1 update / 4s) | 1M/s |
| Redis geo memory/driver | 16 bytes (geohash + overhead) | — |
| Total Redis for 4M drivers | 4M × 16B | ~64 MB (trivial) |
| Match queries/s | 25M rides/day / 86400s | ~300 queries/s average |
| Peak match queries/s | 10× average | ~3,000 queries/s |
| Trip history storage | 25M trips/day × 1KB | 25 GB/day |

---

## High-Level Architecture

```
  ┌──────────────────────────────────────────────────────────────┐
  │                     Location Update Path                      │
  │                                                               │
  │  Driver App (every 4s) → Location Service API                │
  │                        → Kafka (location_updates topic)       │
  │                        → Redis GEOADD drivers:geo             │
  │                        → Cassandra (location_history)         │
  └──────────────────────────────────────────────────────────────┘

  ┌──────────────────────────────────────────────────────────────┐
  │                     Match Request Path                        │
  │                                                               │
  │  Rider App → Dispatch Service                                 │
  │    1. Redis GEORADIUS: nearest N drivers                      │
  │    2. Filter: available, correct vehicle type                 │
  │    3. ETA service: calculate real ETA via road network        │
  │    4. Rank by ETA (not distance)                              │
  │    5. Offer to #1 driver → accept/decline → offer to #2      │
  └──────────────────────────────────────────────────────────────┘

  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
  │     Redis        │  │    PostGIS       │  │    Cassandra     │
  │  drivers:geo     │  │  (geofences,     │  │  location_history│
  │  (real-time      │  │   zone polygons, │  │  (timeseries,    │
  │   geo index)     │  │   analytics)     │  │   audit log)     │
  └──────────────────┘  └──────────────────┘  └──────────────────┘
```

**Redis geo commands** are the real-time driver index. `GEOADD` stores each driver's location as a geohash-encoded integer in a sorted set. `GEORADIUS` retrieves all drivers within a radius in sub-millisecond time by translating the radius into a set of geohash cell ranges and doing a sorted set range scan. The entire 4M-driver index fits in ~64 MB of RAM.

**PostGIS** handles spatial queries that require database-level complexity: geofence polygon storage and point-in-polygon tests, demand heatmap generation, city boundary management, and historical analysis. PostGIS queries are slower (milliseconds) but benefit from full SQL expressiveness and ACID guarantees.

**Cassandra** stores the historical location stream (driver_id, timestamp, lat, lng). This time-series data is write-heavy (1M inserts/second) and queried by time range (e.g., "all locations for driver X between 2pm-3pm"). Cassandra's time-series write performance and time-ordered clustering keys make it a better fit than Postgres for this access pattern.

---

## Deep Dives

### 1. Geo-Indexing: Redis for Real-Time, PostGIS for History

Redis implements geo commands on top of its sorted set data structure. Every `GEOADD` operation converts (latitude, longitude) to a 52-bit geohash integer and stores it as a sorted set score. This encoding has ~0.6m precision at the equator — more than sufficient for driver matching.

The encoding enables a critical optimisation: nearby locations have nearby geohash values, so a radius query becomes a range scan on the sorted set. Redis translates the search circle into 8-9 geohash cells that cover the circle (plus a fringe buffer), then scans only those score ranges. This is why GEORADIUS scales sub-linearly with total driver count — it only examines drivers in nearby cells, not all 4M.

**Key Redis geo operations:**
- `GEOADD key lng lat member` — O(log N) insert/update
- `GEORADIUS key lng lat radius unit [WITHDIST] [COUNT n] [ASC]` — O(N+log(M))
- `GEODIST key member1 member2 unit` — O(log N) exact distance
- `GEOPOS key member` — O(log N) retrieve stored position

Redis does not support point-in-polygon queries directly. Geofence checks are implemented at the application layer (ray-casting algorithm) or in PostGIS (`ST_Contains`).

### 2. Matching Algorithm: ETA Minimisation, Not Distance

Matching by straight-line distance (`GEORADIUS` nearest) is a simplification. The actual Uber dispatch algorithm minimises estimated time of arrival (ETA), not distance. A driver 0.5km away on the other side of a river might have an ETA of 8 minutes, while a driver 1.5km away on the same side has an ETA of 3 minutes.

**ETA calculation pipeline:**
1. `GEORADIUS` returns 10–20 candidate drivers in the area
2. Filter by availability status and vehicle type
3. For each candidate, call the ETA service (maps API with real-time traffic)
4. Rank by ETA; offer the trip to the #1 driver
5. Driver has 15 seconds to accept; on decline, offer to #2

The ETA service is expensive (road network graph traversal per driver). Uber caches ETA results in Redis with a 30-second TTL keyed by `(origin_geohash, destination_geohash)` to avoid recomputing the same route multiple times.

### 3. Location Update Pipeline

Driver location updates flow through Kafka before reaching Redis. This decouples the 1M/s ingest rate from the Redis write path and provides a replay buffer for catchup:

```
Driver → WebSocket/HTTP → Location Service → Kafka (location_updates)
                                                    │
                                           ┌────────┴────────┐
                                           │                 │
                                     Redis Consumers  Cassandra Consumers
                                     (real-time index) (history store)
```

Kafka partitioning: partition by `driver_id % num_partitions`. This ensures all location updates for one driver go to the same partition (and same Redis consumer), preventing out-of-order updates from overwriting a newer position with an older one.

### 4. Surge Pricing: H3 Hexagonal Grid

Surge pricing multiplier is computed per geographic cell. Uber uses Uber's own H3 library (open-sourced in 2018) which divides the Earth into a hierarchical hexagonal grid.

**Why hexagons?** In a square grid, diagonal neighbours are ~41% farther away than edge neighbours. In a hexagonal grid, all 6 neighbours are equidistant. This eliminates directional bias: a driver in the cell to the northeast is just as "close" as one directly north, which matters for demand calculation and cell aggregation.

**Surge calculation (per H3 cell, every 60 seconds):**
1. Count available drivers in cell: query Redis GEORADIUS within cell boundary
2. Count open ride requests in cell: query pending request queue
3. `surge_multiplier = max(1.0, 1.0 + (requests - drivers) / drivers)`
4. Store in Redis: `HSET surge:{h3_cell_id} multiplier 1.8 drivers 12 requests 30`
5. Client app colors map by surge multiplier, adjusts displayed price

---

## How It Actually Works

Uber's engineering blog describes their "Ringpop" and later "Schemaless" systems. Key details from public engineering posts:

**Real-time location system:** Uber uses a custom in-memory datastore called "DISCO" (Driver Index with Spatial Coordinates Operations) rather than vanilla Redis, purpose-built for geo queries at their scale. DISCO is sharded by geohash cell, so each shard only stores drivers in its geographic partition. This lets them scale write throughput by adding shards rather than scaling a monolithic Redis instance.

**Supply/demand prediction:** Uber runs an ML model that predicts ride demand in each H3 cell 15-60 minutes ahead, allowing the system to proactively incentivise drivers to reposition to high-demand zones before demand materialises.

**The "God View":** Uber's internal dashboard shows real-time locations of all drivers and riders globally. This is served from the same Redis geo index, with WebSocket streaming of position updates to the dashboard.

Source: Uber Engineering Blog, "How Uber Uses Location Data in the Real World" (2017); Uber Engineering Blog, "Spatial Indexes" (2017); H3 GitHub repository and documentation (2018).

---

## Hands-on Lab

**Time:** ~20–25 minutes
**Services:** `db` (PostGIS 15-3.3), `cache` (Redis 7), `api` (Python/Flask)

### Setup

```bash
cd system-design-interview/03-case-studies/04-uber/
docker compose up -d
# Wait ~20s for PostGIS extension to initialize
docker compose ps
```

### Experiment

```bash
python experiment.py
```

Six phases run automatically:

1. **Seed:** GEOADD 1,000 driver locations in San Francisco bounding box, measure throughput
2. **Find nearest:** GEORADIUS from rider position, show top-5 nearest drivers with distance
3. **Update throughput:** pipeline 500 GEOADD commands, measure ops/second
4. **Geofence:** ray-casting point-in-polygon test for 5 locations against SF boundary polygon
5. **PostGIS:** sync 200 drivers, run ST_DWithin query, compare with Redis GEORADIUS
6. **Latency comparison:** benchmark Redis vs PostGIS at multiple search radii

### Break It

**Simulate driver going offline (TTL expiry):**

```bash
# In production, drivers have a 60s TTL — Redis auto-removes stale entries.
# Simulate by manually removing a driver:
docker compose exec cache redis-cli ZREM drivers:geo driver_0001

# Verify they no longer appear in nearby search:
docker compose exec cache redis-cli GEORADIUS drivers:geo -122.4194 37.7749 5 km WITHDIST COUNT 10 ASC
```

**Observe the "empty area" problem:** if all drivers in a cell go offline simultaneously (shift change), `GEORADIUS` returns 0 results. The app must handle this by expanding the radius, showing "no drivers available," or routing the request to a driver pool in an adjacent cell.

### Observe

```bash
# Check Redis geo index memory usage
docker compose exec cache redis-cli MEMORY USAGE drivers:geo
# Notice: 1000 drivers ≈ only ~40KB — extremely memory-efficient
# Extrapolate: 4M drivers ≈ 160MB

# Count drivers in a specific area
docker compose exec cache redis-cli GEORADIUS drivers:geo -122.4194 37.7749 2 km COUNT 100 ASC | wc -l
```

### Teardown

```bash
docker compose down -v
```

---

## Interview Checklist

1. **Q: Why use Redis GEORADIUS instead of PostGIS for real-time driver matching?**
   A: Redis GEORADIUS returns results in sub-millisecond time from memory. PostGIS with a GIST index takes 5-50ms from disk even with good indexes, and doesn't scale to 100K queries/second without massive infrastructure. Redis stores 4M driver locations in ~64MB of RAM. The real-time matching use case prioritises latency over query flexibility — PostGIS is used where complex spatial SQL is needed (geofence management, analytics).

2. **Q: How does Redis implement GEORADIUS internally?**
   A: Redis converts (lat, lng) to a 52-bit geohash integer using a space-filling curve (interleaving latitude and longitude bits). This integer is stored as the score in a Redis sorted set. A radius query translates the circle into 8-9 geohash cells covering the area, then scans only those score ranges in the sorted set. Nearby points have nearby geohash scores, so the scan is bounded. Finally, each candidate is filtered by exact Haversine distance to remove points at the corners of the covering cells.

3. **Q: How do you handle 1M location updates per second without dropping updates?**
   A: Kafka absorbs the 1M/s ingest spike: drivers write to Kafka (durable, high-throughput), and Redis consumers read from Kafka and update the geo index. Kafka partitioned by driver_id ensures ordered processing per driver. If Redis consumers lag, Kafka retains messages; no updates are lost. The acceptable lag is ~4 seconds (one update interval), after which stale locations affect match quality but don't cause data loss.

4. **Q: Why is the matching algorithm based on ETA rather than distance?**
   A: Urban geography creates large discrepancies between straight-line distance and drive time. A driver across a river may be 0.3km away but 8 minutes ETA. A driver 1.5km away on the same street may be 2 minutes ETA. Minimising ETA produces better rider experience and higher driver utilisation. The ETA calculation uses real-time traffic data (cached per route segment in Redis with 30s TTL).

5. **Q: How does Uber implement surge pricing at geographic scale?**
   A: The city is divided into H3 hexagonal cells (~600m diameter at resolution 8). Every 60 seconds, a worker counts available drivers and open requests in each cell, computes `surge = 1 + (requests - drivers) / max(drivers, 1)`, and stores the result in Redis. The rider app fetches surge multipliers for visible H3 cells and colors the map accordingly. H3 hexagons are preferred over squares because all 6 neighbors are equidistant, eliminating directional bias.

6. **Q: What happens when a driver's GPS signal is lost for 60 seconds?**
   A: Drivers are considered "offline" after 60 seconds without a location update. Redis ZREMRANGEBYSCORE can expire old entries (though Redis geo doesn't have per-member TTL natively — Uber tracks last-seen time in a separate Redis hash and runs a cleanup process). During the outage, the driver doesn't appear in GEORADIUS results. When GPS resumes, the next GEOADD restores their position.

7. **Q: How do you shard the Redis geo index for 4M drivers?**
   A: Shard by geographic region. Divide the service area into N shards (e.g., by city or geohash prefix). Each shard's Redis instance holds only drivers in that region. A request for drivers near latitude X, longitude Y routes to the shard(s) covering that area. This bounds each shard's key count (~400K drivers per 10-shard cluster) and allows regional scaling. Edge cases: drivers near shard boundaries must be visible in multiple shards.

8. **Q: How would you build the "ETA service" that estimates driver arrival time?**
   A: The ETA service uses a road network graph (OpenStreetMap or proprietary data) with real-time traffic weights. It runs Dijkstra's or A* shortest-path algorithm from driver location to rider location. At scale: pre-compute ETAs for common (source_cell, destination_cell) pairs and cache them (30-second TTL). This converts most ETA lookups to O(1) cache hits. The graph is updated with traffic data every 60 seconds from GPS traces of all moving vehicles (Uber uses its own fleet as a massive traffic sensor).

9. **Q: How do you ensure GPS location data is accurate and not spoofed?**
   A: Server-side validation: if a new location is > N km from the previous location in < T seconds (exceeding maximum possible speed), reject it as spoofed. Cross-validate GPS with cell tower triangulation and WiFi positioning (when available). Flag suspicious patterns (driver appears in multiple cities simultaneously). Machine learning models detect unusual location trajectories. Severe violations (GPS manipulation to appear on longer routes) trigger fraud investigation.

10. **Q: Walk me through matching a rider to a driver, end to end.**
    A: (1) Rider opens app → client shows nearby drivers (GEORADIUS, displayed on map). (2) Rider requests trip → Dispatch Service receives request with pickup/dropoff. (3) Dispatch queries Redis GEORADIUS: top 20 nearest available drivers. (4) Filter by: vehicle type, acceptance rate, driver preferences. (5) ETA Service computes drive time from each candidate to pickup (cached by route cell). (6) Rank by ETA; offer to #1 driver via push notification. (7) Driver has 15s to accept. On accept: create trip record, notify both parties, begin navigation. On decline/timeout: offer to #2. (8) Fallback: if all 20 candidates decline, expand radius and repeat.
