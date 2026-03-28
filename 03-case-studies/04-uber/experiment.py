#!/usr/bin/env python3
"""
Uber Geo-Location Lab — experiment.py

What this demonstrates:
  1. Seed 1,000 driver locations using Redis GEOADD
  2. Find 5 nearest drivers using GEOSEARCH; simulate ETA ranking vs distance ranking
  3. Driver state machine: AVAILABLE → ON_TRIP → AVAILABLE; stale driver eviction
  4. Location update throughput: pipeline GEOADD, extrapolate to 4M drivers
  5. Kafka location update pipeline: produce driver GPS updates, consume into Redis geo index
  6. Geofence check (ray-casting) + H3 surge pricing simulation
  7. PostGIS ST_DWithin vs Redis GEOSEARCH latency comparison

Run:
  docker compose up -d
  # Wait ~30s for Kafka and PostGIS to initialize
  python experiment.py
"""

import hashlib
import json
import math
import os
import random
import time
import urllib.request

import psycopg2
import redis

# ── Config ───────────────────────────────────────────────────────────────────

DB_URL    = os.getenv("DATABASE_URL", "postgresql://app:secret@localhost:5432/uber")
REDIS_URL = os.getenv("REDIS_URL",    "redis://localhost:6379")
API_URL   = os.getenv("API_URL",      "http://localhost:5002")
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "localhost:9092")

# San Francisco bounding box
SF_CENTER = (37.7749, -122.4194)  # (lat, lng)
SF_RADIUS_KM = 15.0

# City geofence polygon (approximate SF boundary, simplified)
SF_POLYGON = [
    (-122.5155, 37.7079),
    (-122.3573, 37.7079),
    (-122.3573, 37.8123),
    (-122.5155, 37.8123),
    (-122.5155, 37.7079),
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def section(title):
    print(f"\n{'=' * 62}")
    print(f"  {title}")
    print("=" * 62)


def get_db():
    return psycopg2.connect(DB_URL)


def get_redis():
    return redis.from_url(REDIS_URL, decode_responses=True)


def wait_for_service(url, max_wait=60):
    print(f"  Waiting for service at {url} ...")
    for i in range(max_wait):
        try:
            urllib.request.urlopen(f"{url}/health", timeout=3)
            print(f"  Service ready after {i+1}s")
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError(f"Service at {url} did not start in time")


def random_sf_point():
    """Generate a random (lat, lng) within SF bounding box."""
    lat = random.uniform(37.7079, 37.8123)
    lng = random.uniform(-122.5155, -122.3573)
    return lat, lng


def haversine_km(lat1, lng1, lat2, lng2):
    """Great-circle distance in km."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlng / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def point_in_polygon(lat, lng, polygon):
    """Ray-casting algorithm for point-in-polygon test."""
    x, y = lng, lat
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def deterministic_road_factor(driver_id: str) -> float:
    """
    Return a stable simulated road factor for a driver (1.3 – 1.8).
    Uses a hash of the driver_id so the value is consistent across calls
    without re-seeding the global random state.
    """
    digest = int(hashlib.md5(driver_id.encode()).hexdigest(), 16)
    return 1.3 + (digest % 1000) / 2000.0  # range [1.3, 1.8)


def init_postgis(conn):
    with conn:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS postgis")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS driver_locations (
                    driver_id    TEXT PRIMARY KEY,
                    geom         GEOMETRY(Point, 4326),
                    updated_at   TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_driver_geom
                ON driver_locations USING GIST(geom)
            """)


# ── Phase 1: Seed 1,000 driver locations in Redis ────────────────────────────

def phase1_seed_drivers(r):
    section("Phase 1: Seed 1,000 Driver Locations — Redis GEOADD")

    print("""
  Redis geo commands use a geohash-encoded sorted set internally.
  Each member (driver_id) is stored with a score = geohash integer.
  This enables O(log N) proximity queries via sorted set range scans.
""")

    random.seed(42)
    driver_locations = {}
    drivers = []
    for i in range(1000):
        driver_id = f"driver_{i:04d}"
        lat, lng  = random_sf_point()
        driver_locations[driver_id] = (lat, lng)
        drivers.append((lng, lat, driver_id))  # Redis GEOADD expects: lng, lat, name

    # Batch insert with pipeline
    start = time.perf_counter()
    BATCH_SIZE = 100
    for i in range(0, len(drivers), BATCH_SIZE):
        batch = drivers[i:i + BATCH_SIZE]
        r.geoadd("drivers:geo", batch)
    elapsed_ms = (time.perf_counter() - start) * 1000

    count = r.zcard("drivers:geo")
    print(f"  Seeded {count} drivers in {elapsed_ms:.1f}ms  ({count / (elapsed_ms / 1000):.0f} inserts/s)")
    print(f"\n  Sample drivers near SF center ({SF_CENTER[0]:.4f}, {SF_CENTER[1]:.4f}):")

    nearby = r.geosearch(
        "drivers:geo",
        longitude=SF_CENTER[1],
        latitude=SF_CENTER[0],
        radius=1.0,
        unit='km',
        sort='ASC',
        count=5,
        withcoord=True,
        withdist=True,
    )
    print(f"  {'Driver ID':<15}  {'Distance km':>12}")
    print(f"  {'-'*15}  {'-'*12}")
    for driver_id, dist, coord in nearby:
        print(f"  {driver_id:<15}  {float(dist):>12.3f}")

    return driver_locations


# ── Phase 2: Find nearest drivers + ETA simulation ───────────────────────────

def phase2_find_nearest(r):
    section("Phase 2: Find 5 Nearest Drivers — GEOSEARCH + ETA Simulation")

    rider_lat, rider_lng = 37.7751, -122.4180  # near Union Square, SF

    print(f"\n  Rider at ({rider_lat}, {rider_lng}) — Union Square, SF")

    start = time.perf_counter()
    results = r.geosearch(
        "drivers:geo",
        longitude=rider_lng,
        latitude=rider_lat,
        radius=5.0,
        unit='km',
        sort='ASC',
        count=10,
        withcoord=True,
        withdist=True,
    )
    elapsed_ms = (time.perf_counter() - start) * 1000

    print(f"\n  GEOSEARCH (5km, top 10): {elapsed_ms:.2f}ms\n")

    # Simulate ETA for each candidate using a deterministic road factor
    candidates = []
    for driver_id, dist, coord in results:
        road_factor = deterministic_road_factor(driver_id)
        eta_min = (float(dist) / 30.0) * 60 * road_factor
        candidates.append((driver_id, float(dist), eta_min))

    # Show both distance-ranked and ETA-ranked top-5 to illustrate the difference
    by_distance = sorted(candidates, key=lambda x: x[1])[:5]
    by_eta      = sorted(candidates, key=lambda x: x[2])[:5]

    print(f"  {'Rank':>4}  {'Driver (distance order)':<24}  {'dist km':>8}  {'ETA min':>8}")
    print(f"  {'-'*4}  {'-'*24}  {'-'*8}  {'-'*8}")
    for rank, (did, dist, eta) in enumerate(by_distance, 1):
        print(f"  {rank:>4}  {did:<24}  {dist:>8.3f}  {eta:>8.1f}")

    print()
    print(f"  {'Rank':>4}  {'Driver (ETA order)':<24}  {'dist km':>8}  {'ETA min':>8}")
    print(f"  {'-'*4}  {'-'*24}  {'-'*8}  {'-'*8}")
    for rank, (did, dist, eta) in enumerate(by_eta, 1):
        print(f"  {rank:>4}  {did:<24}  {dist:>8.3f}  {eta:>8.1f}")

    if by_distance[0][0] != by_eta[0][0]:
        print(f"\n  NOTE: distance-nearest ({by_distance[0][0]}) != ETA-nearest ({by_eta[0][0]})")
        print(f"        Urban road geometry means the closest driver isn't always fastest.")

    print(f"""
  GEOSEARCH internals:
    1. Convert center (lat, lng) to geohash integer
    2. Determine the 8-9 geohash cells covering the search circle
    3. Scan sorted set score ranges for those cells (ZRANGEBYSCORE)
    4. Post-filter by exact Haversine distance to discard cell corners
    5. Return sorted by distance
  Time complexity: O(N+log(M)) — N results returned, M total drivers.
  At 4M drivers this remains sub-millisecond for typical radius queries.
""")
    return results


# ── Phase 3: Driver state machine + stale eviction ───────────────────────────

def phase3_driver_state_machine(r):
    section("Phase 3: Driver State Machine — AVAILABLE → ON_TRIP → Stale Eviction")

    print("""
  Driver state is tracked separately from location.
  The geo index cannot distinguish available from on-trip drivers;
  availability is stored in a Redis hash: driver:status:{driver_id}.

  States: OFFLINE ←→ AVAILABLE ←→ ON_TRIP
""")

    test_driver = "driver_state_test"
    now = time.time()

    # --- Driver comes online ---
    pipe = r.pipeline()
    pipe.geoadd("drivers:geo", [(-122.4183, 37.7753, test_driver)])
    pipe.zadd("driver:heartbeats", {test_driver: now})
    pipe.hset(f"driver:status:{test_driver}", mapping={"status": "AVAILABLE", "vehicle_type": "UberX"})
    pipe.execute()

    status = r.hget(f"driver:status:{test_driver}", "status")
    print(f"  [{test_driver}] after coming online:  status = {status}")

    # --- Trip assigned ---
    r.hset(f"driver:status:{test_driver}", mapping={"status": "ON_TRIP", "trip_id": "trip_abc123"})
    status = r.hget(f"driver:status:{test_driver}", "status")
    print(f"  [{test_driver}] after trip assigned:  status = {status}")

    # --- Trip ends ---
    pipe = r.pipeline()
    pipe.hset(f"driver:status:{test_driver}", "status", "AVAILABLE")
    pipe.hdel(f"driver:status:{test_driver}", "trip_id")
    pipe.execute()
    status = r.hget(f"driver:status:{test_driver}", "status")
    print(f"  [{test_driver}] after trip completes: status = {status}")

    # --- Stale eviction: simulate heartbeat that is 90s old ---
    stale_driver = "driver_stale_test"
    stale_ts = now - 90  # 90 seconds ago — exceeds 60s eviction threshold
    pipe = r.pipeline()
    pipe.geoadd("drivers:geo", [(-122.4190, 37.7760, stale_driver)])
    pipe.zadd("driver:heartbeats", {stale_driver: stale_ts})
    pipe.hset(f"driver:status:{stale_driver}", "status", "AVAILABLE")
    pipe.execute()

    print(f"\n  Simulating background eviction job (runs every 10s):")
    print(f"  ZRANGEBYSCORE driver:heartbeats 0 {{now-60}} → stale drivers")

    eviction_cutoff = now - 60
    stale = r.zrangebyscore("driver:heartbeats", 0, eviction_cutoff)
    print(f"  Found {len(stale)} stale driver(s): {stale}")

    evicted = 0
    pipe = r.pipeline()
    for driver_id in stale:
        pipe.zrem("driver:heartbeats", driver_id)
        pipe.zrem("drivers:geo", driver_id)
        pipe.hset(f"driver:status:{driver_id}", "status", "OFFLINE")
        evicted += 1
    pipe.execute()
    print(f"  Evicted {evicted} driver(s) — removed from geo index, set OFFLINE")

    # Confirm stale_driver is gone from geo index
    pos = r.geopos("drivers:geo", stale_driver)
    print(f"  GEOPOS for {stale_driver} after eviction: {pos[0]} (None = removed)")

    print(f"""
  Why not per-key TTL?
    Storing each driver as an individual Redis key with TTL would fire
    1M TTL-expiry events every 4-second window, overwhelming Redis's
    active expiry thread.  A single ZRANGEBYSCORE on the heartbeat sorted
    set evicts all stale drivers in one O(log N + K) scan.
""")

    # Clean up test drivers
    pipe = r.pipeline()
    for d in [test_driver, stale_driver]:
        pipe.zrem("drivers:geo", d)
        pipe.zrem("driver:heartbeats", d)
        pipe.delete(f"driver:status:{d}")
    pipe.execute()


# ── Phase 4: Location update throughput ──────────────────────────────────────

def phase4_location_updates(r):
    section("Phase 4: Location Update Throughput — Pipeline GEOADD")

    print("""
  Drivers send GPS updates every 4 seconds (Uber's actual interval).
  4M active drivers / 4s interval = 1M updates/second to ingest.
  Redis GEOADD is O(log N) and handles ~500K ops/s per single node.
""")

    random.seed(99)
    n_updates = 500

    start = time.perf_counter()
    pipe = r.pipeline()
    for i in range(n_updates):
        driver_id = f"driver_{random.randint(0, 999):04d}"
        lat, lng  = random_sf_point()
        pipe.geoadd("drivers:geo", [(lng, lat, driver_id)])
    pipe.execute()
    elapsed_ms = (time.perf_counter() - start) * 1000

    ops_per_sec = n_updates / (elapsed_ms / 1000)
    print(f"  {n_updates} location updates via pipeline: {elapsed_ms:.1f}ms")
    print(f"  Throughput: {ops_per_sec:.0f} updates/second")
    print(f"\n  Extrapolation to production scale:")
    print(f"  {'Drivers':>12}  {'Update interval':>17}  {'Required ops/s':>16}  {'Feasible?':>12}")
    print(f"  {'-'*12}  {'-'*17}  {'-'*16}  {'-'*12}")
    for n_drivers, interval_s in [(100_000, 4), (1_000_000, 4), (4_000_000, 4)]:
        req_ops = n_drivers // interval_s
        feasible = "YES (1 node)" if req_ops < 500_000 else "needs sharding"
        print(f"  {n_drivers:>12,}  {interval_s:>16}s  {req_ops:>14,}  {feasible:>12}")

    print(f"""
  Scale inflection: a single Redis node saturates at ~500K GEOADD/s.
  At 4M drivers the bottleneck is write throughput, not memory (~264 MB).
  Solution: shard by geographic region (H3 cell prefix) so each shard
  owns a subset of drivers and receives only the writes for its area.
""")


# ── Phase 5: Kafka location pipeline ─────────────────────────────────────────

def phase5_kafka_pipeline(r):
    section("Phase 5: Kafka Location Pipeline — Produce + Consume into Redis")

    print("""
  In production, drivers write to Kafka (not Redis directly).
  This decouples 1M/s ingest from the Redis write path and provides
  a durable replay buffer.  Partition by driver_id % num_partitions
  so all updates for a driver are ordered within one partition.

  Here we produce 50 location updates to Kafka, then consume them
  and apply each as a Redis GEOADD — exactly what a production
  location-index consumer does.
""")

    try:
        from kafka import KafkaProducer, KafkaConsumer
        from kafka.errors import NoBrokersAvailable
    except ImportError:
        print("  kafka-python-ng not installed. Run: pip install kafka-python-ng")
        print("  Skipping Kafka phase.\n")
        return

    TOPIC = "location_updates"
    N = 50

    # ── Produce ──────────────────────────────────────────────────────────────
    try:
        producer = KafkaProducer(
            bootstrap_servers=KAFKA_BOOTSTRAP,
            value_serializer=lambda v: json.dumps(v).encode(),
            request_timeout_ms=5000,
            api_version=(2, 8, 0),
        )
    except NoBrokersAvailable:
        print(f"  Kafka not reachable at {KAFKA_BOOTSTRAP}. Skipping Kafka phase.\n")
        return

    random.seed(77)
    messages = []
    for i in range(N):
        driver_id = f"driver_{random.randint(0, 999):04d}"
        lat, lng  = random_sf_point()
        msg = {"driver_id": driver_id, "lat": lat, "lng": lng, "ts": time.time()}
        messages.append(msg)
        producer.send(TOPIC, value=msg)

    producer.flush()
    print(f"  Produced {N} location updates to Kafka topic '{TOPIC}'")

    # ── Consume + index ───────────────────────────────────────────────────────
    consumer = KafkaConsumer(
        TOPIC,
        bootstrap_servers=KAFKA_BOOTSTRAP,
        auto_offset_reset="earliest",
        enable_auto_commit=True,
        group_id="location-redis-consumer-lab",
        consumer_timeout_ms=4000,
        value_deserializer=lambda v: json.loads(v.decode()),
        api_version=(2, 8, 0),
    )

    consumed = 0
    start = time.perf_counter()
    pipe = r.pipeline()
    for msg in consumer:
        payload = msg.value
        pipe.geoadd("drivers:geo", [(payload["lng"], payload["lat"], payload["driver_id"])])
        pipe.zadd("driver:heartbeats", {payload["driver_id"]: payload["ts"]})
        consumed += 1
    pipe.execute()
    consumer.close()
    elapsed_ms = (time.perf_counter() - start) * 1000

    print(f"  Consumed {consumed} messages and indexed into Redis in {elapsed_ms:.0f}ms")
    print(f"""
  Key design choices:
    • Partition key = driver_id  → all updates for one driver are ordered
      within a single partition, preventing an old location overwriting
      a newer one when consumers process in parallel.
    • Consumer lag <= 4s is acceptable (one update interval); beyond that
      the geo index shows stale positions and an alert fires.
    • Kafka retains messages for 24 h — allows replay if Redis consumers
      crash and need to rebuild the index from scratch.
""")


# ── Phase 6: Geofence check + surge pricing ───────────────────────────────────

def phase6_geofence(r):
    section("Phase 6: Geofence Check + H3 Surge Pricing Simulation")

    print("""
  Geofences define city, airport, and zone boundaries.
  Use cases:
    - Only show drivers inside the service area
    - Surge pricing zones (H3 hexagonal grid cells)
    - Airport pickup/dropoff zones
    - No-go zones (stadiums, restricted areas)
""")

    test_points = [
        (37.7749, -122.4194, "SF Union Square"),
        (37.3382, -121.8863, "San Jose (outside SF)"),
        (37.8124, -122.3573, "Oakland (edge)"),
        (37.7516, -122.4477, "SF Mission District"),
        (37.6213, -122.3790, "SFO Airport"),
    ]

    print(f"  Testing points against SF city polygon:\n")
    print(f"  {'Point':<30}  {'lat':>9}  {'lng':>10}  {'In SF?':>7}")
    print(f"  {'-'*30}  {'-'*9}  {'-'*10}  {'-'*7}")
    for lat, lng, label in test_points:
        inside = point_in_polygon(lat, lng, SF_POLYGON)
        tag = "YES" if inside else "NO"
        print(f"  {label:<30}  {lat:>9.4f}  {lng:>10.4f}  {tag:>7}")

    # Surge pricing simulation via fake H3 cells stored in Redis
    print(f"""
  Surge pricing — H3 hexagonal grid cells (resolution 8, ~460m across):
""")
    sample_cells = [
        ("8928308280fffff", 12, 30),   # Union Square — high demand
        ("8928308283fffff", 20,  8),   # SoMa — plenty of supply
        ("892830828bfffff",  3, 25),   # Castro — very high demand
    ]

    pipe = r.pipeline()
    for cell_id, drivers, requests in sample_cells:
        multiplier = max(1.0, 1.0 + (requests - drivers) / max(drivers, 1))
        pipe.hset(f"surge:h3:{cell_id}", mapping={
            "multiplier": round(multiplier, 2),
            "drivers":    drivers,
            "requests":   requests,
            "ts":         int(time.time()),
        })
    pipe.execute()

    print(f"  {'H3 Cell':<20}  {'Drivers':>8}  {'Requests':>9}  {'Surge':>7}")
    print(f"  {'-'*20}  {'-'*8}  {'-'*9}  {'-'*7}")
    for cell_id, drivers, requests in sample_cells:
        multiplier = float(r.hget(f"surge:h3:{cell_id}", "multiplier"))
        print(f"  {cell_id:<20}  {drivers:>8}  {requests:>9}  {multiplier:>6.2f}x")

    print(f"""
  Why hexagonal (H3) over square grid?
    In a square grid, diagonal neighbours are ~41% farther than edge
    neighbours.  Hexagons have equal distance to all 6 neighbours,
    eliminating directional bias in proximity and demand calculations.
    Uber open-sourced H3 in 2018; Lyft uses it too.

  Surge formula: max(1.0, 1.0 + (requests - drivers) / max(drivers, 1))
  Stored in Redis HSET surge:h3:{{cell_id}} — O(1) lookup per cell.
  Recomputed every 60 seconds by a partitioned worker pool.
""")


# ── Phase 7: PostGIS latency comparison ──────────────────────────────────────

def phase7_latency_comparison(conn, r, driver_locations):
    section("Phase 7: PostGIS ST_DWithin vs Redis GEOSEARCH Latency")

    # Sync 200 drivers to PostGIS for demonstration
    drivers_subset = list(driver_locations.items())[:200]

    with conn:
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO driver_locations (driver_id, geom, updated_at)
                   VALUES (%s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), NOW())
                   ON CONFLICT (driver_id) DO UPDATE
                   SET geom=EXCLUDED.geom, updated_at=NOW()""",
                [(did, loc[1], loc[0]) for did, loc in drivers_subset],
            )

    print(f"\n  Synced {len(drivers_subset)} drivers to PostGIS")

    rider_lat, rider_lng = 37.7751, -122.4180
    radii = [0.5, 1.0, 2.0, 5.0]

    print(f"\n  Latency vs search radius ({r.zcard('drivers:geo')} drivers in Redis, "
          f"{len(drivers_subset)} in PostGIS):\n")
    print(f"  {'Radius km':>10}  {'Redis ms':>10}  {'PostGIS ms':>12}  {'Redis faster':>13}")
    print(f"  {'-'*10}  {'-'*10}  {'-'*12}  {'-'*13}")

    for radius_km in radii:
        trials = 10

        redis_total = 0
        for _ in range(trials):
            start = time.perf_counter()
            r.geosearch(
                "drivers:geo",
                longitude=rider_lng,
                latitude=rider_lat,
                radius=radius_km,
                unit='km',
                count=10,
                sort='ASC',
            )
            redis_total += (time.perf_counter() - start) * 1000
        redis_avg = redis_total / trials

        postgis_total = 0
        for _ in range(trials):
            start = time.perf_counter()
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT driver_id FROM driver_locations
                    WHERE ST_DWithin(
                        geom::geography,
                        ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                        %s
                    )
                    ORDER BY geom::geography <-> ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography
                    LIMIT 10
                """, (rider_lng, rider_lat, radius_km * 1000, rider_lng, rider_lat))
                cur.fetchall()
            postgis_total += (time.perf_counter() - start) * 1000
        postgis_avg = postgis_total / trials

        speedup = postgis_avg / redis_avg if redis_avg > 0 else 0
        print(f"  {radius_km:>10.1f}  {redis_avg:>10.2f}  {postgis_avg:>12.2f}  {speedup:>11.1f}x")

    print(f"""
  Note: PostGIS here has only 200 drivers (subset for demo).
  At 4M drivers the PostGIS queries would be significantly slower
  even with a GIST index, while Redis scales sub-linearly due to
  geohash cell pruning.

  When to use each:

  Redis GEOSEARCH:
    + Real-time driver matching (sub-ms, 4M drivers in ~264 MB RAM)
    + Location updates at 500K+ ops/s per node
    - No complex spatial queries (polygon intersections, SQL joins)
    - Volatile unless RDB/AOF persistence is enabled

  PostGIS ST_DWithin / ST_Contains:
    + Historical analysis (trip heatmaps, demand forecasting)
    + Complex spatial queries (routes inside polygon, geofence management)
    + Persistent, ACID, joins with other tables
    - Too slow for real-time matching at 4M drivers
""")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    section("UBER GEO-LOCATION LAB")
    print("""
  Architecture:
    Drivers → Kafka (1M updates/s, partitioned by driver_id)
           → Redis GEOADD (real-time index, sub-ms GEOSEARCH)
    Riders  → Redis GEOSEARCH → ETA service → match
    Analytics → PostGIS (historical, complex spatial queries)

  Key insight: two geo stores serving different access patterns.
  Redis for real-time (fast, volatile); PostGIS for analytics (slow, durable).
""")

    wait_for_service(API_URL)

    conn = get_db()
    r    = get_redis()

    init_postgis(conn)

    driver_locations = phase1_seed_drivers(r)
    phase2_find_nearest(r)
    phase3_driver_state_machine(r)
    phase4_location_updates(r)
    phase5_kafka_pipeline(r)
    phase6_geofence(r)
    phase7_latency_comparison(conn, r, driver_locations)

    conn.close()

    section("Lab Complete")
    print("""
  Summary:
  • Redis GEOADD/GEOSEARCH: real-time location index, sub-ms queries at 4M drivers
  • ETA ranking vs distance ranking: demonstrates why ETA minimisation matters
  • Driver state machine: AVAILABLE / ON_TRIP / OFFLINE tracked in Redis hashes;
    stale eviction via heartbeat sorted set (one ZRANGEBYSCORE scan for all drivers)
  • Kafka pipeline: durable 1M/s ingest with driver_id partitioning for ordering
  • Geofence: ray-casting polygon test; H3 hexagonal grid for surge pricing
  • PostGIS ST_DWithin: ACID complex geo queries; slower but richer than Redis

  Next: 05-whatsapp/ — WebSocket message delivery, 3-ACK receipts, offline queuing
""")


if __name__ == "__main__":
    main()
