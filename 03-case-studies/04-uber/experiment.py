#!/usr/bin/env python3
"""
Uber Geo-Location Lab — experiment.py

What this demonstrates:
  1. Seed 1000 driver locations using Redis GEOADD
  2. Find 5 nearest drivers using GEORADIUS
  3. Simulate 100 location updates/second (batch GEOADD)
  4. Implement geofence check: is a coordinate inside a city boundary?
  5. PostGIS query: find all drivers in a polygon
  6. Compare Redis GEORADIUS vs PostGIS ST_DWithin latency

Run:
  docker compose up -d
  # Wait ~15s for services
  python experiment.py
"""

import math
import os
import random
import time
import urllib.request
import urllib.parse
import json

import psycopg2
import redis

# ── Config ───────────────────────────────────────────────────────────────────

DB_URL    = os.getenv("DATABASE_URL", "postgresql://app:secret@localhost:5432/uber")
REDIS_URL = os.getenv("REDIS_URL",    "redis://localhost:6379")
API_URL   = os.getenv("API_URL",      "http://localhost:5002")

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
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng/2)**2
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


# ── Phase 1: Seed 1000 driver locations in Redis ─────────────────────────────

def phase1_seed_drivers(r):
    section("Phase 1: Seed 1,000 Driver Locations — Redis GEOADD")

    print("""
  Redis geo commands use a geohash-encoded sorted set internally.
  Each member (driver_id) is stored with a score = geohash integer.
  This enables O(log N) proximity queries using sorted set range scans.
""")

    random.seed(42)
    driver_locations = {}
    drivers = []
    for i in range(1000):
        driver_id = f"driver_{i:04d}"
        lat, lng  = random_sf_point()
        driver_locations[driver_id] = (lat, lng)
        drivers.append((lng, lat, driver_id))  # MinIO-style: lng, lat, name

    # Batch insert with pipeline
    start = time.perf_counter()
    BATCH_SIZE = 100
    for i in range(0, len(drivers), BATCH_SIZE):
        batch = drivers[i:i+BATCH_SIZE]
        r.geoadd("drivers:geo", batch)
    elapsed_ms = (time.perf_counter() - start) * 1000

    count = r.zcard("drivers:geo")
    print(f"  Seeded {count} drivers in {elapsed_ms:.1f}ms  ({count/(elapsed_ms/1000):.0f} inserts/s)")
    print(f"\n  Sample drivers near SF center ({SF_CENTER[0]:.4f}, {SF_CENTER[1]:.4f}):")

    # Show a few near the center
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


# ── Phase 2: Find nearest drivers (GEORADIUS) ─────────────────────────────────

def phase2_find_nearest(r):
    section("Phase 2: Find 5 Nearest Drivers — Redis GEORADIUS")

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
        count=5,
        withcoord=True,
        withdist=True,
    )
    elapsed_ms = (time.perf_counter() - start) * 1000

    print(f"\n  GEOSEARCH (5km, top 5): {elapsed_ms:.2f}ms\n")
    print(f"  {'Rank':>6}  {'Driver ID':<15}  {'Distance km':>12}")
    print(f"  {'-'*6}  {'-'*15}  {'-'*12}")
    for rank, (driver_id, dist, coord) in enumerate(results, 1):
        print(f"  {rank:>6}  {driver_id:<15}  {float(dist):>12.3f}")

    print(f"""
  Redis GEORADIUS implementation:
    1. Convert center (lat, lng) to geohash integer
    2. Determine the geohash cells covering the search radius
    3. Scan the sorted set for members in those cell ranges (ZRANGEBYSCORE)
    4. Filter by exact distance (great-circle formula)
    5. Return sorted by distance

  Time complexity: O(N+log(M)) where N=results in radius, M=total drivers
  At 4M drivers: still sub-millisecond for typical radius queries
""")
    return results


# ── Phase 3: Simulate 100 location updates/second ────────────────────────────

def phase3_location_updates(r):
    section("Phase 3: Location Update Throughput — 100 Updates/Second")

    print("""
  Drivers send GPS updates every 4 seconds (Uber's actual interval).
  4M active drivers / 4s interval = 1M updates/second to ingest.
  Redis GEOADD is O(log N) and handles 100K+ ops/s per node.
""")

    random.seed(99)
    n_updates = 500

    # Batch updates (simulates multiple drivers reporting at once)
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
    print(f"  {'Drivers':>12}  {'Update interval':>17}  {'Required ops/s':>16}")
    print(f"  {'-'*12}  {'-'*17}  {'-'*16}")
    for n_drivers, interval_s in [(100_000, 4), (1_000_000, 4), (4_000_000, 4)]:
        req_ops = n_drivers // interval_s
        feasible = "YES" if req_ops < 500_000 else "needs sharding"
        print(f"  {n_drivers:>12,}  {interval_s:>16}s  {req_ops:>14,}  {feasible}")


# ── Phase 4: Geofence check ───────────────────────────────────────────────────

def phase4_geofence(r):
    section("Phase 4: Geofence Check — Is Driver Inside City Boundary?")

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

    # Simulated surge pricing via geohash cells
    print(f"""
  Surge pricing uses H3 hexagonal grid cells:
    - SF divided into ~500 H3 cells at resolution 8 (~600m diameter)
    - Each cell stores: available_drivers, pending_requests, surge_multiplier
    - Surge = 1.0 + (requests - drivers) / drivers  (simplified)
    - Redis HSET h3:{cell_id} drivers 12 requests 30 surge 1.8

  Why hexagonal (H3) over square grid?
    Hexagons have equal distance to all 6 neighbors (squares don't).
    Eliminates directional bias in proximity calculations.
    Used by: Uber (H3 library, open-sourced 2018), Lyft
""")


# ── Phase 5: PostGIS polygon query ────────────────────────────────────────────

def phase5_postgis(conn, r, driver_locations):
    section("Phase 5: PostGIS — Historical Analysis + Complex Geo Queries")

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

    # Query 1: ST_DWithin — drivers within 2km of center
    rider_lat, rider_lng = 37.7751, -122.4180

    start = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT driver_id,
                   ST_Distance(
                       geom::geography,
                       ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography
                   ) / 1000.0 AS distance_km
            FROM driver_locations
            WHERE ST_DWithin(
                geom::geography,
                ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                2000
            )
            ORDER BY distance_km
            LIMIT 5
        """, (rider_lng, rider_lat, rider_lng, rider_lat))
        postgis_results = cur.fetchall()
    postgis_ms = (time.perf_counter() - start) * 1000

    # Query 2: Redis GEOSEARCH on same data
    start = time.perf_counter()
    redis_results = r.geosearch(
        "drivers:geo",
        longitude=rider_lng,
        latitude=rider_lat,
        radius=2.0,
        unit='km',
        sort='ASC',
        count=5,
        withcoord=True,
        withdist=True,
    )
    redis_ms = (time.perf_counter() - start) * 1000

    print(f"\n  Nearest drivers within 2km of ({rider_lat}, {rider_lng}):\n")
    print(f"  {'Source':<15}  {'Latency':>10}  {'Drivers found':>14}")
    print(f"  {'-'*15}  {'-'*10}  {'-'*14}")
    print(f"  {'PostGIS':<15}  {postgis_ms:>9.2f}ms  {len(postgis_results):>14}")
    print(f"  {'Redis GEOSEARCH':<15}  {redis_ms:>9.2f}ms  {len(redis_results):>14}")

    print(f"""
  When to use each:

  Redis GEORADIUS:
    ✓ Real-time driver matching (sub-millisecond, 4M drivers)
    ✓ Location updates at 100K+ ops/s
    ✗ No complex queries (polygon intersections, joins)
    ✗ Data lost on Redis restart (unless persisted)

  PostGIS ST_DWithin / ST_Contains:
    ✓ Historical analysis (trip heatmaps, demand forecasting)
    ✓ Complex spatial queries (routes inside polygon, geofence management)
    ✓ Persistent, ACID, joins with other tables
    ✗ Too slow for real-time matching at 4M drivers

  Uber's actual architecture:
    Redis: real-time location index (updated every 4s per driver)
    PostGIS/Cassandra: historical location data, route storage
    H3 library: hexagonal grid for surge pricing and demand forecasting
""")


# ── Phase 6: Latency comparison ───────────────────────────────────────────────

def phase6_latency_comparison(conn, r):
    section("Phase 6: Latency Comparison — Redis vs PostGIS at Scale")

    rider_lat, rider_lng = 37.7751, -122.4180
    radii = [0.5, 1.0, 2.0, 5.0]

    print(f"  Latency vs search radius ({r.zcard('drivers:geo')} drivers in Redis):\n")
    print(f"  {'Radius km':>10}  {'Redis ms':>10}  {'PostGIS ms':>12}  {'Redis faster':>13}")
    print(f"  {'-'*10}  {'-'*10}  {'-'*12}  {'-'*13}")

    for radius_km in radii:
        # Redis
        trials = 10
        redis_total = 0
        for _ in range(trials):
            start = time.perf_counter()
            r.geosearch("drivers:geo", longitude=rider_lng, latitude=rider_lat, radius=radius_km, unit='km', count=10, sort='ASC')
            redis_total += (time.perf_counter() - start) * 1000
        redis_avg = redis_total / trials

        # PostGIS
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
  At full 4M drivers, PostGIS would be significantly slower.
  Redis GEORADIUS scales sub-linearly due to geohash cell pruning.
""")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    section("UBER GEO-LOCATION LAB")
    print("""
  Architecture:
    Drivers → Redis GEOADD (real-time, 100K updates/s)
    Riders → Redis GEORADIUS (nearest drivers, sub-ms)
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
    phase3_location_updates(r)
    phase4_geofence(r)
    phase5_postgis(conn, r, driver_locations)
    phase6_latency_comparison(conn, r)

    conn.close()

    section("Lab Complete")
    print("""
  Summary:
  • Redis GEOADD/GEORADIUS: real-time location index, sub-ms queries at 4M drivers
  • PostGIS ST_DWithin: complex geo queries, historical analysis, ACID
  • Geofence: ray-casting polygon test, H3 hexagonal grid for surge pricing
  • Location update pipeline: drivers push every 4s → Kafka → Redis geo index
  • Matching algorithm: minimize ETA (not just distance) — accounts for traffic

  Next: 05-whatsapp/ — WebSocket message delivery, 3-ACK receipts, offline queuing
""")


if __name__ == "__main__":
    main()
