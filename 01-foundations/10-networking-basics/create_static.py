#!/usr/bin/env python3
"""Generate static payload files for the networking lab.
Run once before docker compose up, or the experiment.py will call it automatically.
"""
import os

static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(static_dir, exist_ok=True)

sizes = {
    "1kb.bin": 1024,
    "100kb.bin": 100 * 1024,
    "1mb.bin": 1024 * 1024,
    "10mb.bin": 10 * 1024 * 1024,
}

for filename, size in sizes.items():
    path = os.path.join(static_dir, filename)
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(b"x" * size)
        print(f"  Created {filename} ({size:,} bytes)")
    else:
        print(f"  {filename} already exists")

print("Static files ready.")
