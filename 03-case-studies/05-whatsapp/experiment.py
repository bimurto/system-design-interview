#!/usr/bin/env python3
"""
WhatsApp Messaging Lab — experiment.py

What this demonstrates:
  1. Connect 3 clients via WebSocket
  2. Send message A→B (B online) → immediate delivery, print receipt states
  3. Disconnect B
  4. Send message A→B (B offline) → queued in Postgres
  5. Reconnect B → queued messages delivered
  6. Show 3-receipt model: sent / delivered / read

Run:
  docker compose up -d
  # Wait ~30s for ws_server to be ready
  python experiment.py
"""

import asyncio
import datetime
import json
import os
import time
import uuid

import psycopg2
import redis
import websockets

WS_URL    = os.getenv("WS_URL",      "ws://localhost:8765")
DB_URL    = os.getenv("DATABASE_URL", "postgresql://app:secret@localhost:5432/whatsapp")
REDIS_URL = os.getenv("REDIS_URL",    "redis://localhost:6379")


# ── Helpers ───────────────────────────────────────────────────────────────────

def section(title):
    print(f"\n{'=' * 62}")
    print(f"  {title}")
    print("=" * 62)


def get_db():
    return psycopg2.connect(DB_URL)


def get_redis():
    return redis.from_url(REDIS_URL, decode_responses=True)


async def wait_for_server(max_wait=60):
    print(f"  Waiting for WebSocket server at {WS_URL} ...")
    for i in range(max_wait):
        try:
            async with websockets.connect(WS_URL, open_timeout=3):
                pass
            print(f"  Server ready after {i+1}s")
            return
        except Exception:
            await asyncio.sleep(1)
    raise RuntimeError("WebSocket server did not start in time")


def install_packages():
    import subprocess, sys
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--quiet",
         "websockets", "psycopg2-binary", "redis"]
    )


async def connect_client(user_id):
    ws = await websockets.connect(WS_URL)
    await ws.send(json.dumps({"type": "auth", "user_id": user_id}))
    return ws


async def recv_with_timeout(ws, timeout=2.0):
    """Receive a message, return None on timeout."""
    try:
        return json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
    except asyncio.TimeoutError:
        return None


async def collect_receipts(ws, count, timeout=3.0):
    """Collect up to `count` messages within timeout seconds."""
    messages = []
    deadline = time.monotonic() + timeout
    while len(messages) < count and time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        msg = await recv_with_timeout(ws, timeout=remaining)
        if msg:
            messages.append(msg)
    return messages


# ── Phase 1: Connect 3 clients ────────────────────────────────────────────────

async def phase1_connect():
    section("Phase 1: Connect 3 Clients via WebSocket")

    alice = await connect_client("alice")
    bob   = await connect_client("bob")
    carol = await connect_client("carol")

    print(f"\n  Connected: alice, bob, carol")
    print(f"\n  WebSocket connection model:")
    print(f"""
  Each user maintains a persistent WebSocket connection to a server.
  The server maps user_id → websocket connection in memory.

  At WhatsApp scale (2B users, ~100M online at peak):
    ~1M connections per server (using async I/O, e.g., Erlang/OTP)
    2000 servers needed for 2B users if each has 1M conn capacity
    Server assignment via consistent hashing on user_id
    Client reconnects to same server on disconnect (sticky routing)
""")

    r = get_redis()
    # Verify online status in Redis
    for user_id in ["alice", "bob", "carol"]:
        online = r.exists(f"online:{user_id}")
        print(f"  Redis online:{user_id} = {'1 (online)' if online else '0 (offline)'}")

    return alice, bob, carol


# ── Phase 2: Online delivery — A→B, B online ─────────────────────────────────

async def phase2_online_delivery(alice, bob):
    section("Phase 2: Message Delivery — B Online (Immediate)")

    msg_id  = str(uuid.uuid4())[:8]
    content = "Hey Bob, are you there?"

    print(f"\n  Alice → Bob: '{content}'")
    print(f"  Message ID: {msg_id}\n")

    await alice.send(json.dumps({
        "type":   "message",
        "to":     "bob",
        "content": content,
        "msg_id": msg_id,
    }))

    # Collect receipts on Alice's side
    print(f"  Receipts received by Alice:")
    receipts_seen = set()
    for _ in range(3):
        msg = await recv_with_timeout(alice, timeout=2.0)
        if msg and msg.get("type") == "receipt":
            status = msg["status"]
            if status not in receipts_seen:
                receipts_seen.add(status)
                symbol = {"sent": "✓", "delivered": "✓✓", "read": "✓✓ (blue)"}
                icon = symbol.get(status, "?")
                print(f"    {icon}  status={status}")

    # Bob receives the message
    bob_msg = await recv_with_timeout(bob, timeout=2.0)
    if bob_msg:
        print(f"\n  Bob received: '{bob_msg.get('content')}'")
        print(f"    from: {bob_msg.get('from')}")
        print(f"    msg_id: {bob_msg.get('msg_id')}")

    # Bob sends read receipt
    await bob.send(json.dumps({
        "type":      "read",
        "msg_id":    msg_id,
        "sender_id": "alice",
    }))

    read_receipt = await recv_with_timeout(alice, timeout=2.0)
    if read_receipt and read_receipt.get("status") == "read":
        print(f"\n  Alice received read receipt: ✓✓ (blue)")

    print(f"""
  3-receipt model timeline:
    T+0ms:  Alice sends message
    T+5ms:  Server receives → ACK1 (sent ✓)
    T+8ms:  Server delivers to Bob's device → ACK2 (delivered ✓✓)
    T+?:    Bob opens chat → client sends read event → ACK3 (read ✓✓ blue)
""")
    return msg_id


# ── Phase 3: Offline delivery — B disconnects, messages queue ────────────────

async def phase3_offline_delivery(alice, bob):
    section("Phase 3: Offline Message Queuing — B Disconnects")

    print(f"\n  Disconnecting Bob...")
    await bob.close()
    await asyncio.sleep(0.5)

    r = get_redis()
    bob_online = r.exists("online:bob")
    print(f"  Redis online:bob = {'1' if bob_online else '0 (offline)'}")

    # Alice sends messages while Bob is offline
    offline_msgs = [
        ("Hey, you went offline!", str(uuid.uuid4())[:8]),
        ("Are you coming to the meeting?", str(uuid.uuid4())[:8]),
        ("Never mind, just ping me later", str(uuid.uuid4())[:8]),
    ]

    print(f"\n  Alice sends 3 messages while Bob is offline:")
    for content, msg_id in offline_msgs:
        await alice.send(json.dumps({
            "type":    "message",
            "to":      "bob",
            "content": content,
            "msg_id":  msg_id,
        }))
        receipt = await recv_with_timeout(alice, timeout=2.0)
        status = receipt.get("status") if receipt else "unknown"
        preview = content[:40] + ("..." if len(content) > 40 else "")
        print(f"    '{preview}' → ACK1 ({status})")
        await asyncio.sleep(0.1)

    # Verify messages are in Postgres
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, content, status FROM messages WHERE recipient_id='bob' AND status='sent' ORDER BY created_at",
        )
        queued = cur.fetchall()
    conn.close()

    print(f"\n  Postgres message queue for Bob:")
    print(f"  {'msg_id':>10}  {'status':>10}  {'content'}")
    print(f"  {'-'*10}  {'-'*10}  {'-'*35}")
    for msg_id, content, status in queued:
        print(f"  {msg_id[:10]:>10}  {status:>10}  {content[:35]}")

    print(f"\n  {len(queued)} messages waiting for Bob to reconnect")
    return offline_msgs


# ── Phase 4: Reconnect and deliver queued messages ────────────────────────────

async def phase4_reconnect(offline_msgs, alice):
    section("Phase 4: Bob Reconnects — Queued Messages Delivered")

    print(f"\n  Bob reconnecting...")
    bob = await connect_client("bob")

    # Collect messages Bob receives and ACK2 receipts Alice receives concurrently.
    # When the server delivers queued messages to Bob it also sends ACK2 to Alice —
    # this is the 3-ACK model in action: the sender learns of delivery even though
    # it happened hours after the original send.
    delivered_to_bob = []
    ack2_to_alice = []

    # Poll both connections for a short window
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        remaining = max(0.1, deadline - time.monotonic())

        # Check Bob
        bob_msg = await recv_with_timeout(bob, timeout=min(0.3, remaining))
        if bob_msg:
            if bob_msg.get("type") == "message":
                delivered_to_bob.append(bob_msg)
            elif bob_msg.get("type") == "queued_messages_delivered":
                print(f"\n  Server: delivered {bob_msg.get('count')} queued messages to Bob")

        # Check Alice for delayed ACK2 receipts
        alice_msg = await recv_with_timeout(alice, timeout=min(0.3, remaining))
        if alice_msg and alice_msg.get("type") == "receipt" and alice_msg.get("status") == "delivered":
            ack2_to_alice.append(alice_msg)

    print(f"\n  Bob received {len(delivered_to_bob)} messages on reconnect:")
    for msg in delivered_to_bob:
        preview = msg.get('content', '')[:45]
        print(f"    From {msg.get('from')}: '{preview}'")

    print(f"\n  Alice received {len(ack2_to_alice)} delayed ACK2 (delivered) receipts:")
    for receipt in ack2_to_alice:
        print(f"    msg_id={receipt.get('msg_id')} → status=delivered ✓✓")
    if not ack2_to_alice:
        print(f"    (none — Alice may have disconnected before Bob reconnected)")

    print(f"""
  Key insight: ACK2 (delivered ✓✓) is sent to Alice the moment Bob's device
  receives the message — even if that's hours after Alice originally sent it.
  The server is the intermediary for all receipt forwarding.
""")

    r = get_redis()
    bob_online = r.exists("online:bob")
    print(f"  Redis online:bob = {'1 (back online)' if bob_online else '0'}")

    # Verify Postgres statuses updated
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, COUNT(*) FROM messages WHERE recipient_id='bob' GROUP BY status",
        )
        rows = cur.fetchall()
    conn.close()

    print(f"\n  Postgres message statuses after reconnect:")
    for status, count in rows:
        print(f"    {status}: {count} messages")

    return bob


# ── Phase 5: Presence system ──────────────────────────────────────────────────

async def phase5_presence(alice, bob, carol):
    section("Phase 5: Presence System — Online/Offline Status")

    r = get_redis()

    print(f"""
  WhatsApp presence system:
    Online:    WebSocket connected + Redis key online:{{user_id}} with 90s TTL
    Offline:   Key deleted on clean disconnect; expires after 90s on crash
    Last seen: last_seen:{{user_id}} timestamp written to Redis on disconnect

  TTL-based approach:
    Client sends heartbeat every 30s → refreshes Redis TTL to 90s
    If client dies without clean disconnect, TTL expires after 90s
    No need for explicit "user went offline" messages
""")

    users = ["alice", "bob", "carol"]
    print(f"  Current presence:")
    for user in users:
        ttl = r.ttl(f"online:{user}")
        if ttl > 0:
            status = f"ONLINE (TTL: {ttl}s)"
        else:
            last_seen_ts = r.get(f"last_seen:{user}")
            if last_seen_ts:
                dt = datetime.datetime.fromtimestamp(int(last_seen_ts))
                status = f"OFFLINE (last seen: {dt.strftime('%H:%M:%S')})"
            else:
                status = "OFFLINE"
        print(f"    {user}: {status}")

    # Simulate heartbeats
    print(f"\n  Sending heartbeats...")
    for user_ws, user_id in [(alice, "alice"), (bob, "bob"), (carol, "carol")]:
        try:
            await user_ws.send(json.dumps({"type": "heartbeat"}))
        except Exception:
            pass

    await asyncio.sleep(0.5)

    print(f"\n  After heartbeat (TTL refreshed to 90s):")
    for user in users:
        ttl = r.ttl(f"online:{user}")
        status = f"ONLINE (TTL: {ttl}s)" if ttl > 0 else "OFFLINE"
        print(f"    {user}: {status}")

    print(f"""
  Presence fan-out at scale:
    When Alice comes online, notify her contacts that she's online.
    2B users, each with ~200 contacts: naive fan-out = 400B notifications/day.
    WhatsApp approach: only notify contacts who are currently online
    and who have Alice in their recent contacts (last 7 days).
    Reduces fan-out by ~99% in practice.
""")


# ── Phase 6: Message delivery guarantees ─────────────────────────────────────

async def phase6_delivery_guarantees():
    section("Phase 6: Delivery Guarantees — At-Least-Once with Deduplication")

    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE status='sent') as queued,
                COUNT(*) FILTER (WHERE status='delivered') as delivered,
                COUNT(*) FILTER (WHERE status='read') as read,
                COUNT(*) as total
            FROM messages
        """)
        row = cur.fetchone()
    conn.close()

    queued, delivered, read_count, total = row

    print(f"\n  Message delivery summary:")
    print(f"  {'Status':<12}  {'Count':>8}")
    print(f"  {'-'*12}  {'-'*8}")
    print(f"  {'queued':<12}  {queued:>8}")
    print(f"  {'delivered':<12}  {delivered:>8}")
    print(f"  {'read':<12}  {read_count:>8}")
    print(f"  {'total':<12}  {total:>8}")

    print(f"""
  Delivery guarantee: at-least-once
    Messages are persisted to Postgres before sending.
    If delivery fails, message stays in 'sent' state → retried on reconnect.
    Idempotency: msg_id (UUID) prevents duplicate delivery.
    If client receives same msg_id twice, it deduplicates by ID.

  WhatsApp's actual guarantee (from XMPP heritage):
    Server → client: stream resumption (XMPP SM extension)
    Client → server: ACK required within 30s or connection reset
    End-to-end: Signal Protocol ensures message confidentiality

  Message persistence:
    Sent → Postgres (status=sent) immediately on receive
    Delivered → update status=delivered, set delivered_at
    Read → update status=read, set read_at
    Postgres is the source of truth for message state

  At WhatsApp scale (100B messages/day = 1.16M messages/second):
    Messages are partitioned across Cassandra clusters by (chat_id, date)
    Postgres is used here for simplicity; production uses Cassandra
    WHY Cassandra: write-heavy (append-only messages), time-ordered reads,
    linear scalability without hot-shard problems
""")


# ── Main ─────────────────────────────────────────────────────────────────────

async def async_main():
    section("WHATSAPP MESSAGING LAB")
    print("""
  Architecture:
    Clients ─── WebSocket ──► Server (in-memory connection map)
                                │
                           Redis (online/offline TTL)
                                │
                           Postgres (message store, delivery state)

  Key insight: messages are stored BEFORE delivery.
  Delivery is best-effort; receipts confirm actual delivery.
  Offline users get messages on reconnect from Postgres queue.
""")

    await wait_for_server()

    alice, bob, carol = await phase1_connect()
    await phase2_online_delivery(alice, bob)
    offline_msgs = await phase3_offline_delivery(alice, bob)
    bob = await phase4_reconnect(offline_msgs, alice)
    await phase5_presence(alice, bob, carol)
    await phase6_delivery_guarantees()

    # Cleanup
    for ws in [alice, bob, carol]:
        try:
            await ws.close()
        except Exception:
            pass

    section("Lab Complete")
    print("""
  Summary:
  • WebSocket persistent connections: server maps user_id → socket in memory
  • 3-receipt model: sent (✓) → delivered (✓✓) → read (✓✓ blue)
  • Offline delivery: messages queued in Postgres, delivered on reconnect
  • Delayed ACK2: sender gets delivered receipt when recipient reconnects, not at send time
  • Presence: Redis TTL with heartbeat refresh; auto-expires after 90s; last_seen on disconnect
  • At-least-once delivery + UUID idempotency key (ON CONFLICT DO NOTHING) prevents duplicates

  Next: 06-google-drive/ — chunked upload, delta sync, content deduplication
""")


def main():
    install_packages()
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
