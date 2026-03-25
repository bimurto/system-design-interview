# Case Study: WhatsApp

**Prerequisites:** `../../01-foundations/04-replication/`, `../../02-advanced/04-event-driven-architecture/`, `../../02-advanced/14-idempotency-exactly-once/`

---

## The Problem at Scale

WhatsApp serves 2 billion users sending over 100 billion messages per day. The core engineering challenge is not bulk throughput but **connection management**: maintaining persistent WebSocket connections for hundreds of millions of simultaneously online users, routing messages between them in real time, and guaranteeing delivery even when recipients are offline.

| Metric | Value |
|---|---|
| Total users | 2 billion |
| Daily active users | 500 million |
| Peak concurrent connections | 100 million |
| Messages per day | 100 billion |
| Messages per second (peak) | ~2 million |
| Media files per day | 4.5 billion |
| Servers (at 1M conn/server) | ~1,000 chat servers |

A famous WhatsApp engineering fact: in 2014, they served 450 million users with just 32 engineers. The technical foundation was Erlang/OTP, a platform designed for building massively concurrent network servers with lightweight processes.

---

## Requirements

### Functional
- Send text messages between users (1:1 and group)
- Media messages (images, video, audio, documents)
- Message delivery receipts: sent, delivered, read
- Offline delivery: messages queued when recipient is offline
- Online/offline presence status
- End-to-end encryption (Signal Protocol)

### Non-Functional
- Message delivery latency < 1 second (both users online)
- Offline queue: messages retained for 30 days
- 99.99% message delivery guarantee (no message loss)
- Supports 1M+ concurrent connections per server
- End-to-end encrypted: WhatsApp's servers never see plaintext

### Capacity Estimation

| Metric | Calculation | Result |
|---|---|---|
| Messages/second | 100B/day / 86400s | ~1.16M/s |
| Storage/message | 200B avg (text + metadata) | — |
| Daily text storage | 100B × 200B | 20 TB/day |
| Media storage | 4.5B files × avg 100KB | 450 TB/day |
| Connection memory | 100M conn × 10KB per conn | 1 TB RAM total |
| Chat servers needed | 100M conn / 1M per server | 100 servers |

---

## High-Level Architecture

```
  ┌──────────────────────────────────────────────────────────────┐
  │                    Chat Server Cluster                        │
  │                                                               │
  │  User A (online) ──WebSocket──► Server-1 (holds A's socket)  │
  │  User B (online) ──WebSocket──► Server-2 (holds B's socket)  │
  │  User C (offline)                                             │
  │                                                               │
  │  Server routing:                                              │
  │    A sends msg to B:                                          │
  │      Server-1 → Redis pub/sub: channel "user:B"               │
  │      Server-2 receives → delivers to B's socket              │
  │    A sends msg to C (offline):                               │
  │      Server-1 → Postgres/Cassandra: queue message             │
  │      When C connects → server delivers queued messages        │
  └──────────────────────────────────────────────────────────────┘

  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
  │     Redis        │  │   Postgres/      │  │  Object Storage  │
  │  online:{uid}    │  │   Cassandra      │  │  (media files)   │
  │  (presence TTL)  │  │  messages table  │  │  MinIO/S3        │
  │  pub/sub routing │  │  (queue + state) │  │                  │
  └──────────────────┘  └──────────────────┘  └──────────────────┘
```

**Chat servers** maintain WebSocket connections. Each server holds up to 1M simultaneous connections (achievable with async I/O — Erlang processes, Go goroutines, or Python asyncio). The server keeps an in-memory map of `user_id → websocket`. When a message arrives for user B, the server checks if B is local; if not, it routes via Redis pub/sub.

**Redis** serves two roles: (1) presence — `online:{user_id}` key with TTL, refreshed by heartbeats; (2) inter-server routing — each server subscribes to `user:{user_id}` channels for users it hosts. When a message arrives for an offline-on-this-server user, it's published to that channel, and the server hosting the recipient delivers it.

**Postgres/Cassandra** stores the persistent message queue. Messages are written before delivery; their status field tracks the delivery state (sent → delivered → read). On reconnect, the server queries for all `status='sent'` messages for the reconnected user.

---

## Deep Dives

### 1. Connection Management: WebSocket + Consistent Hashing

WebSocket connections are stateful: a specific server holds user A's socket. When user B sends a message to A, the request must reach A's server. Two approaches:

**Centralized routing:** a lookup service maps `user_id → server_id`. Every message does a lookup, then routes to the correct server. Simple but adds a network hop and creates a bottleneck.

**Consistent hashing:** assign users to servers deterministically by `hash(user_id) % ring`. Any server can route a message to user A's server without a directory lookup. When a server is added/removed, only a fraction of users are remapped (graceful via consistent hashing properties — see `../../02-advanced/01-consistent-hashing/`).

**Redis pub/sub as the routing layer (this lab):** each chat server subscribes to Redis channels for the users it currently hosts. Simpler to implement but adds latency (Redis hop) for cross-server messages. Works well at moderate scale; at WhatsApp's actual scale, direct inter-server communication is more efficient.

### 2. Message Delivery Guarantee: The 3-ACK Model

WhatsApp's receipt system tracks three distinct states, each providing a delivery guarantee:

**ACK1 — Sent (single grey tick ✓):** the WhatsApp server has received the message and durably stored it. Even if the recipient is offline for 30 days, the message will be delivered when they reconnect. This ACK is sent immediately by the server to the sender.

**ACK2 — Delivered (double grey tick ✓✓):** the message has been transmitted to the recipient's device and stored in the device's local database. This does not mean the user has opened the chat. The recipient's app sends a delivery ACK to the server, which forwards it to the sender.

**ACK3 — Read (double blue tick ✓✓ blue):** the user has opened the chat and seen the message. The recipient's app sends a read ACK when the chat is foregrounded. Note: WhatsApp allows users to disable read receipts (blue ticks), in which case ACK3 is never sent to the sender.

This model requires the server to be the intermediary for all receipts, not just the initial delivery. The database tracks all three timestamps (`sent_at`, `delivered_at`, `read_at`).

### 3. Presence System

WhatsApp presence ("last seen" and online indicator) works as follows:

**Online:** user is connected via WebSocket. Server sets `online:{user_id}` in Redis with a 90-second TTL. Client sends a heartbeat every 30 seconds, refreshing the TTL. If the client disconnects ungracefully (app crash, network drop), the key expires after 90 seconds and the user is considered offline.

**Last seen:** when a user disconnects, the server writes `last_seen:{user_id} = timestamp` to Redis. Contacts can query this to show "last seen today at 3:47 PM." This is a privacy-sensitive feature — WhatsApp allows users to hide their last-seen from non-contacts or everyone.

**Presence fan-out:** when Alice comes online, should her 200 contacts see her go online? At 2B users × 200 contacts × arrival rate, naively fanning out all presence updates would generate billions of events/second. WhatsApp's solution: only fan out to contacts who are currently online and have recently interacted with Alice. This reduces fan-out by ~99%.

### 4. Offline Delivery and Queue

When a message is sent to an offline user:

1. Message is stored in Postgres/Cassandra with `status='sent'`
2. Server records that delivery failed (recipient not connected)
3. When recipient reconnects, auth handler queries all `status='sent'` messages for that user
4. Messages are delivered in creation order, marking each `delivered` and sending ACK2 to original sender
5. Messages are retained for 30 days before being dropped

**Queue depth limit:** if a user accumulates too many queued messages (WhatsApp internal limit), older messages may be dropped with a "X older messages" notification. This prevents the queue from growing unboundedly for inactive users.

---

## How It Actually Works

WhatsApp's architecture is described in Rick Reed's Erlang Factory talk (2014) "WhatsApp: 1+ Million Connections Per Server":

**Erlang/OTP:** WhatsApp uses Erlang for its chat servers. The Erlang BEAM VM is specifically designed for concurrent network servers: lightweight processes (2KB heap each, vs 1MB for OS threads), message-passing concurrency, and the OTP supervisor/worker pattern for fault tolerance. One Erlang node can handle 2M+ concurrent connections, more than achieved with typical async Python or Node.js.

**XMPP heritage:** WhatsApp originally used the XMPP protocol (Extensible Messaging and Presence Protocol), an XML-based chat protocol. Over time they replaced XML with a more efficient binary protocol (based on Protocol Buffers), but retained XMPP's delivery guarantee semantics (stream resumption, stanza ACKs).

**Signal Protocol for E2E encryption:** WhatsApp adopted the Signal Protocol in 2016 for end-to-end encryption. The server never sees plaintext. Key exchange uses the Double Ratchet algorithm, which provides forward secrecy: compromising a key only affects future messages, not past ones. The server stores encrypted message blobs without being able to read them.

**Cassandra for message storage:** at 100B messages/day, WhatsApp uses Cassandra partitioned by `(chat_id, date)`. Cassandra's append-only write path (LSM tree) handles the write-heavy message ingest efficiently. Reads are by `(chat_id, date, message_id)` — a Cassandra primary key lookup. Postgres is used here for simplicity.

Source: Rick Reed, "WhatsApp: 1+ Million Connections Per Server," Erlang Factory 2014; WhatsApp Engineering Blog, "WhatsApp Statistics 2022"; High Scalability Blog, "WhatsApp Architecture" (2014).

---

## Hands-on Lab

**Time:** ~20–25 minutes
**Services:** `db` (Postgres 15), `cache` (Redis 7), `ws_server` (Python asyncio WebSocket server)

### Setup

```bash
cd system-design-interview/03-case-studies/05-whatsapp/
docker compose up -d
# Wait ~30s for ws_server to be healthy
docker compose ps
docker compose logs ws_server  # should show "Starting WebSocket server on :8765"
```

### Experiment

```bash
python experiment.py
```

Six phases run automatically:

1. **Connect:** Alice, Bob, and Carol connect via WebSocket; verify Redis presence keys
2. **Online delivery:** Alice sends a message to Bob (online); observe all 3 ACK states
3. **Offline queuing:** Bob disconnects; Alice sends 3 messages; verify they queue in Postgres
4. **Reconnect:** Bob reconnects; queued messages are delivered immediately
5. **Presence:** demonstrate heartbeat TTL refresh, observe online/offline transitions
6. **Delivery guarantees:** inspect Postgres message status counts, discuss at-least-once

### Break It

**Simulate server crash while delivering a message:**

```bash
# Send message from Alice to Bob, then immediately restart the ws_server
docker compose exec ws_server kill -9 1 &
# Reconnect and see that the message was NOT lost (it was stored in Postgres first)

# Watch the logs during reconnect:
docker compose logs -f ws_server
```

**Simulate duplicate delivery:**

```bash
python -c "
import asyncio, json, websockets, uuid

async def test():
    async with websockets.connect('ws://localhost:8765') as ws:
        await ws.send(json.dumps({'type': 'auth', 'user_id': 'alice'}))
        msg_id = 'dedup-test-001'
        # Send same message twice (simulates client retry after timeout)
        for i in range(3):
            await ws.send(json.dumps({
                'type': 'message', 'to': 'bob',
                'content': 'This should only appear once!',
                'msg_id': msg_id,  # same ID
            }))
        await asyncio.sleep(1)
        print('Sent 3 times with same msg_id')

asyncio.run(test())
"

# Check Postgres: should show only 1 row with that msg_id (ON CONFLICT DO NOTHING)
docker compose exec db psql -U app whatsapp -c \
  "SELECT id, status, created_at FROM messages WHERE id='dedup-test-001';"
```

### Observe

```bash
# Watch message state transitions in Postgres
docker compose exec db psql -U app whatsapp -c \
  "SELECT sender_id, recipient_id, status, content FROM messages ORDER BY created_at LIMIT 20;"

# Check Redis presence keys and their TTLs
docker compose exec cache redis-cli KEYS "online:*"
docker compose exec cache redis-cli TTL online:alice
```

### Teardown

```bash
docker compose down -v
```

---

## Interview Checklist

1. **Q: How does WhatsApp serve 100M+ concurrent connections with ~50 servers?**
   A: Each chat server handles ~1-2M connections using async I/O. Erlang's BEAM VM runs millions of lightweight processes (2KB each). Python asyncio, Go goroutines, and Node.js achieve similar numbers. The key is non-blocking I/O: the server never blocks a thread waiting for a network response. A 64-core server with 256GB RAM can sustain ~2M WebSocket connections at low message rates.

2. **Q: How do you route a message from Alice (on server-1) to Bob (on server-3)?**
   A: Three approaches: (1) Consistent hashing: all servers know the hash ring, can directly TCP-connect to the target server. (2) Redis pub/sub: server-1 publishes to `user:bob` channel; server-3 (subscribed for Bob) receives and delivers. (3) Message bus (Kafka): server-1 publishes event; a consumer on server-3 delivers. WhatsApp uses direct server-to-server messaging in production.

3. **Q: What guarantees does the "sent" tick actually provide?**
   A: The single grey tick guarantees the message is durably stored on WhatsApp's servers. Even if the recipient's phone is off for weeks, the message will be delivered when they reconnect (up to a 30-day retention limit). It does NOT mean the recipient has received it on their device — that requires the double grey tick.

4. **Q: How do you implement offline message delivery without polling?**
   A: Messages are stored in Postgres/Cassandra with `status='sent'`. When the recipient connects, the server queries all `status='sent'` messages for that user and delivers them in order. No polling needed — the delivery happens synchronously during the connection auth phase. At reconnect, the server knows exactly which messages to deliver by querying the message store.

5. **Q: How does end-to-end encryption work without WhatsApp seeing message content?**
   A: WhatsApp uses the Signal Protocol. At first message, sender fetches recipient's public key from the key server. The Double Ratchet algorithm derives a unique encryption key for each message. The encrypted ciphertext is what WhatsApp stores and forwards. WhatsApp's servers never have access to the decryption keys — they're derived from a shared secret established between the two devices.

6. **Q: How do you handle the group message fan-out problem?**
   A: A message to a 256-person group is sent once to the server. The server expands it into 256 individual deliveries (similar to Twitter fan-out, but per-message not per-connection). Each delivery is tracked independently. If one recipient is offline, only their copy is queued. At scale (256 group members × message rate), this requires careful async processing to avoid blocking the sender's acknowledgement.

7. **Q: How does WhatsApp handle message ordering across multiple devices?**
   A: WhatsApp uses monotonically increasing message IDs within each chat. The server assigns the final ID after receiving the message, preventing out-of-order ID assignment from concurrent senders. The client orders messages by server-assigned ID, not send timestamp (which can differ across devices due to clock drift). Multi-device support (WhatsApp Web, linked devices) uses the same message ID space.

8. **Q: How do you scale the message store to 100 billion messages/day?**
   A: Cassandra partitioned by `(chat_id, date)` with a clustering key of `message_id`. This gives O(1) writes (append to partition) and O(1) reads by chat + date range. At 100B messages/day and 200B average size, daily ingest is ~20TB. Cassandra nodes are added horizontally — each new node takes over token ranges from existing nodes with zero downtime. Replication factor 3 ensures durability.

9. **Q: How do you prevent spam and abuse at 1M+ messages/second?**
   A: Rate limiting at the connection layer (max N messages/second per user, tracked in Redis with token bucket). Content scanning is limited by E2E encryption — WhatsApp can only scan unencrypted metadata (sender, recipient, timestamp, message size) for patterns. Client-side machine learning models detect patterns and report to WhatsApp without breaking encryption. Flagging mechanism: users can flag a message as spam; the flagged message (plaintext) is sent to WhatsApp's moderation team.

10. **Q: Walk me through the end-to-end flow of Alice sending "Hello" to offline Bob.**
    A: (1) Alice's app encrypts "Hello" with Bob's public key (Signal Protocol). (2) Alice's WebSocket sends `{type: "message", to: "bob", content: "<ciphertext>", msg_id: "abc123"}`. (3) Server receives, writes to Cassandra: `INSERT INTO messages (id, sender, recipient, status) VALUES ('abc123', 'alice', 'bob', 'sent')`. (4) Server checks Redis: `online:bob` → key not found. (5) Server sends ACK1 to Alice: `{type: "receipt", msg_id: "abc123", status: "sent"}`. (6) Bob connects hours later → auth handler queries `SELECT * FROM messages WHERE recipient='bob' AND status='sent'`. (7) Server delivers ciphertext to Bob's device. (8) Bob's app decrypts with his private key. (9) Server updates status='delivered', sends ACK2 to Alice. (10) Bob opens chat → app sends read event → ACK3 to Alice.
