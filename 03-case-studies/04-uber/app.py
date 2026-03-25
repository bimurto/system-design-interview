#!/usr/bin/env python3
"""
Uber Driver Location API

Endpoints:
  POST /driver/location           body: {driver_id, lat, lng} → update location
  GET  /drivers/nearby?lat=&lng=&radius_km=  → list nearest drivers
  GET  /health                    → {"status": "ok"}
"""

import os
import time
import psycopg2
import redis
from flask import Flask, request, jsonify

app = Flask(__name__)


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
    data = request.get_json(force=True) or {}
    driver_id = data.get("driver_id")
    lat = float(data.get("lat", 0))
    lng = float(data.get("lng", 0))

    if not driver_id:
        return jsonify({"error": "driver_id required"}), 400

    r = get_redis()
    r.geoadd("drivers:geo", [(lng, lat, driver_id)])

    return jsonify({"status": "ok", "driver_id": driver_id})


@app.route("/drivers/nearby")
def nearby_drivers():
    lat    = float(request.args.get("lat", 37.7749))
    lng    = float(request.args.get("lng", -122.4194))
    radius = float(request.args.get("radius_km", 2.0))
    count  = int(request.args.get("count", 5))

    r = get_redis()
    results = r.georadius(
        "drivers:geo", lng, lat, radius, unit="km",
        withdist=True, count=count, sort="ASC"
    )

    drivers = [
        {"driver_id": d[0], "distance_km": round(float(d[1]), 3)}
        for d in results
    ]
    return jsonify({"drivers": drivers, "count": len(drivers)})


if __name__ == "__main__":
    for attempt in range(20):
        try:
            init_db()
            break
        except Exception as e:
            print(f"DB not ready ({attempt+1}): {e}")
            time.sleep(2)

    app.run(host="0.0.0.0", port=5000, debug=False)
