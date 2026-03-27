#!/usr/bin/env python3
"""
Uber Driver Location API

Endpoints:
  POST /driver/location           body: {driver_id, lat, lng} → update location + heartbeat
  POST /driver/status             body: {driver_id, status}   → AVAILABLE | ON_TRIP | OFFLINE
  GET  /drivers/nearby?lat=&lng=&radius_km=  → list nearest AVAILABLE drivers
  GET  /match?lat=&lng=&vehicle_type=        → dispatch: nearest AVAILABLE driver ranked by simulated ETA
  GET  /health                    → {"status": "ok"}
"""

import os
import time
import psycopg2
import redis
from flask import Flask, request, jsonify

app = Flask(__name__)

VALID_STATUSES = {"AVAILABLE", "ON_TRIP", "OFFLINE"}


def get_db():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def get_redis():
    return redis.from_url(os.environ["REDIS_URL"], decode_responses=True)


def init_db():
    conn = get_db()
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
    conn.close()


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/driver/location", methods=["POST"])
def update_location():
    """
    Update driver GPS location. Also refreshes the heartbeat timestamp.
    In production this would write to Kafka; here we write directly to Redis.
    """
    data = request.get_json(force=True) or {}
    driver_id = data.get("driver_id")
    lat = float(data.get("lat", 0))
    lng = float(data.get("lng", 0))

    if not driver_id:
        return jsonify({"error": "driver_id required"}), 400

    r = get_redis()
    pipe = r.pipeline()
    # Update geo index
    pipe.geoadd("drivers:geo", [(lng, lat, driver_id)])
    # Update heartbeat (scored sorted set for stale eviction)
    pipe.zadd("driver:heartbeats", {driver_id: time.time()})
    # Set default status if not yet present
    pipe.hsetnx(f"driver:status:{driver_id}", "status", "AVAILABLE")
    pipe.execute()

    return jsonify({"status": "ok", "driver_id": driver_id})


@app.route("/driver/status", methods=["POST"])
def update_status():
    """
    Transition driver availability state: AVAILABLE | ON_TRIP | OFFLINE.
    Offline drivers are removed from the geo index.
    """
    data = request.get_json(force=True) or {}
    driver_id = data.get("driver_id")
    new_status = (data.get("status") or "").upper()

    if not driver_id:
        return jsonify({"error": "driver_id required"}), 400
    if new_status not in VALID_STATUSES:
        return jsonify({"error": f"status must be one of {VALID_STATUSES}"}), 400

    r = get_redis()
    pipe = r.pipeline()
    pipe.hset(f"driver:status:{driver_id}", "status", new_status)
    if new_status == "OFFLINE":
        # Remove from geo index and heartbeat tracker
        pipe.zrem("drivers:geo", driver_id)
        pipe.zrem("driver:heartbeats", driver_id)
    pipe.execute()

    return jsonify({"status": "ok", "driver_id": driver_id, "new_status": new_status})


@app.route("/drivers/nearby")
def nearby_drivers():
    """
    Return nearest drivers sorted by distance. Includes availability status.
    Uses GEOSEARCH (replaces deprecated GEORADIUS).
    """
    lat = float(request.args.get("lat", 37.7749))
    lng = float(request.args.get("lng", -122.4194))
    radius = float(request.args.get("radius_km", 2.0))
    count = int(request.args.get("count", 5))

    r = get_redis()
    results = r.geosearch(
        "drivers:geo",
        longitude=lng,
        latitude=lat,
        radius=radius,
        unit="km",
        withdist=True,
        count=count,
        sort="ASC",
    )

    # Batch-fetch status for all candidates
    pipe = r.pipeline()
    for driver_id, _dist in results:
        pipe.hget(f"driver:status:{driver_id}", "status")
    statuses = pipe.execute()

    drivers = [
        {
            "driver_id": driver_id,
            "distance_km": round(float(dist), 3),
            "status": statuses[i] or "UNKNOWN",
        }
        for i, (driver_id, dist) in enumerate(results)
    ]
    return jsonify({"drivers": drivers, "count": len(drivers)})


@app.route("/match")
def match_driver():
    """
    Dispatch endpoint: find the best AVAILABLE driver for a rider.
    Filters by availability and simulates ETA ranking (ETA = distance * road_factor).
    Demonstrates the radius-expansion fallback when fewer than 3 candidates exist.
    """
    lat = float(request.args.get("lat", 37.7749))
    lng = float(request.args.get("lng", -122.4194))
    vehicle_type = request.args.get("vehicle_type", "UberX")

    r = get_redis()

    # Radius expansion loop: 1km → 2km → 5km
    for radius_km in [1.0, 2.0, 5.0]:
        candidates = r.geosearch(
            "drivers:geo",
            longitude=lng,
            latitude=lat,
            radius=radius_km,
            unit="km",
            withdist=True,
            count=20,
            sort="ASC",
        )

        if not candidates:
            continue

        # Batch-fetch status
        pipe = r.pipeline()
        for driver_id, _dist in candidates:
            pipe.hget(f"driver:status:{driver_id}", "status")
            pipe.hget(f"driver:status:{driver_id}", "vehicle_type")
        raw = pipe.execute()

        available = []
        for i, (driver_id, dist_km) in enumerate(candidates):
            status = raw[i * 2] or "UNKNOWN"
            vtype = raw[i * 2 + 1] or "UberX"
            if status == "AVAILABLE":
                # Simulate ETA: straight-line distance * road factor (1.3-1.8)
                import random
                random.seed(hash(driver_id) % 1000)
                road_factor = 1.3 + random.random() * 0.5
                eta_minutes = (float(dist_km) / 30.0) * 60 * road_factor
                available.append({
                    "driver_id": driver_id,
                    "distance_km": round(float(dist_km), 3),
                    "eta_minutes": round(eta_minutes, 1),
                    "road_factor": round(road_factor, 2),
                })

        if len(available) >= 1:
            # Rank by ETA (not distance)
            available.sort(key=lambda d: d["eta_minutes"])
            return jsonify({
                "match": available[0],
                "candidates_evaluated": len(available),
                "search_radius_km": radius_km,
                "ranked_by": "eta",
            })

    return jsonify({"error": "no drivers available", "searched_radius_km": 5.0}), 503


if __name__ == "__main__":
    for attempt in range(20):
        try:
            init_db()
            break
        except Exception as e:
            print(f"DB not ready ({attempt+1}): {e}")
            time.sleep(2)

    app.run(host="0.0.0.0", port=5000, debug=False)
