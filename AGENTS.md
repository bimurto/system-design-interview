# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is an interactive, lab-driven system design interview preparation course for senior/staff engineers (3+ years experience) targeting FAANG-level interviews. It contains 39 topic folders across three sections, each pairing a concept README with a locally-runnable Docker Compose experiment.

## Running Experiments

Each topic is self-contained. The standard workflow for any topic:

```bash
cd 01-foundations/06-caching/        # navigate to a topic
docker compose up -d                  # start local infrastructure (Redis, Postgres, Kafka, etc.)
python experiment.py                  # run the demo
docker compose down -v                # teardown (remove volumes)
```

## Python Dependencies

Install globally or in a venv before running experiments:

```bash
pip install redis psycopg2-binary kafka-python-ng requests flask strawberry-graphql grpcio faust-streaming
```

## Docker Sandbox (Makefile)

The top-level `Makefile` manages a Docker sandbox for development, not for experiments:

```bash
make build    # build the claude-sandbox image
make run      # start the sandbox with workspace mounted
make shell    # open bash in the running sandbox
make stop     # stop and remove the container
make logs     # tail container logs
```

## Content Architecture

```
01-foundations/     # 12 topics: scalability, CAP, caching, load balancing, DBs, APIs, etc.
02-advanced/        # 15 topics: consistent hashing, Raft/Paxos, Kafka, rate limiting, CDN, etc.
03-case-studies/    # 12 systems: URL shortener, Twitter, YouTube, Uber, WhatsApp, etc.
```

Each topic folder contains:
- `README.md` — concept explanation, trade-offs, interview talking points
- `docker-compose.yml` — local infrastructure (no cloud required)
- `experiment.py` — runnable Python demo of the concept
- Optionally: `app.py`, `nginx.conf`, or other service files

## Adding New Topics

Follow the existing pattern: one folder per topic with `README.md`, `docker-compose.yml`, and `experiment.py`. Topics should be self-contained — no shared state between folders.

## Interview Structure Reference

Each case-study README is structured around the standard interview framework:
1. Clarify Requirements (3–5 min)
2. Capacity Estimation (3–5 min)
3. High-Level Design (10 min)
4. Deep Dive (15–20 min)
