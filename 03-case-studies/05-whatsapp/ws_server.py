#!/usr/bin/env python3
"""
WhatsApp WebSocket Server

Handles client connections, routes messages via Redis pub/sub,
tracks online/offline status, stores messages in Postgres.

Protocol (JSON messages):
  Connect:   send {"type": "auth", "user_id": "alice"}
  Send msg:  send {"type": "message", "to": "bob", "content": "hello", "msg_id": "uuid"}
  Server:    sends {"type": "message", ...} on delivery
             sends {"type": "receipt", "msg_id": "...", "status": "delivered"|"read"}
             sends {"type": "queued_messages", "messages": [...]} on connect
"""

import asyncio
import json
import os
import time
import uuid

import psycopg2
import psycopg2.extras
import redis
import websockets
import websockets.exceptions

DB_URL    = os.environ.get("DATABASE_URL", "postgresql://app:secret@localhost:5432/whatsapp")
REDIS_URL = os.environ.get("REDIS_URL",    "redis://localhost:6379")

# user_id → websocket connection
connected_users: dict[str, websockets.WebSocketServerProtocol] = {}


# ── DB helpers ───────────────────────────────────────────────────────────────

def get_db():
    return psycopg2.connect(DB_URL)


def get_redis():
    return redis.from_url(REDIS_URL, decode_responses=True)


def init_db():
    conn = get_db()
    with conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id          TEXT PRIMARY KEY,
                    sender_id   TEXT NOT NULL,
                    recipient_id TEXT NOT NULL,
                    content     TEXT NOT NULL,
                    status      TEXT NOT NULL DEFAULT 'sent',
                    created_at  TIMESTAMPTZ DEFAULT NOW(),
                    delivered_at TIMESTAMPTZ,
                    read_at     TIMESTAMPTZ
                );
                CREATE INDEX IF NOT EXISTS idx_msg_recipient
                    ON messages(recipient_id, status, created_at);
            """)
    conn.close()


def store_message(msg_id, sender_id, recipient_id, content, status="sent"):
    conn = get_db()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO messages (id, sender_id, recipient_id, content, status)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (id) DO NOTHING""",
                    (msg_id, sender_id, recipient_id, content, status),
                )
    finally:
        conn.close()


def update_message_status(msg_id, status):
    conn = get_db()
    ts_col = {"delivered": "delivered_at", "read": "read_at"}.get(status)
    try:
        with conn:
            with conn.cursor() as cur:
                if ts_col:
                    cur.execute(
                        f"UPDATE messages SET status=%s, {ts_col}=NOW() WHERE id=%s",
                        (status, msg_id),
                    )
                else:
                    cur.execute("UPDATE messages SET status=%s WHERE id=%s", (status, msg_id))
    finally:
        conn.close()


def get_queued_messages(user_id):
    """Retrieve all undelivered messages for a user."""
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT id, sender_id, content, created_at
                   FROM messages
                   WHERE recipient_id = %s AND status = 'sent'
                   ORDER BY created_at""",
                (user_id,),
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── WebSocket handler ─────────────────────────────────────────────────────────

async def handle_client(websocket):
    user_id = None
    r = get_redis()

    try:
        async for raw_msg in websocket:
            data = json.loads(raw_msg)
            msg_type = data.get("type")

            if msg_type == "auth":
                user_id = data["user_id"]
                connected_users[user_id] = websocket

                # Mark online in Redis with 90s TTL (heartbeat refreshes)
                r.setex(f"online:{user_id}", 90, "1")

                print(f"[SERVER] {user_id} connected")

                # Deliver queued messages
                queued = get_queued_messages(user_id)
                if queued:
                    for msg in queued:
                        await websocket.send(json.dumps({
                            "type":      "message",
                            "msg_id":    msg["id"],
                            "from":      msg["sender_id"],
                            "content":   msg["content"],
                            "timestamp": str(msg["created_at"]),
                        }))
                        update_message_status(msg["id"], "delivered")
                        # ACK2: notify original sender that message is now delivered
                        # This is a key part of the 3-ACK model: the sender gets ACK2
                        # when the recipient's device receives the message, even if delayed.
                        sender_ws = connected_users.get(msg["sender_id"])
                        if sender_ws:
                            try:
                                await sender_ws.send(json.dumps({
                                    "type":   "receipt",
                                    "msg_id": msg["id"],
                                    "status": "delivered",
                                }))
                            except Exception:
                                pass
                    await websocket.send(json.dumps({
                        "type":    "queued_messages_delivered",
                        "count":   len(queued),
                    }))
                    print(f"[SERVER] Delivered {len(queued)} queued messages to {user_id}")

            elif msg_type == "message":
                sender_id    = user_id
                recipient_id = data["to"]
                content      = data["content"]
                msg_id       = data.get("msg_id", str(uuid.uuid4()))

                # Store in Postgres with status=sent
                store_message(msg_id, sender_id, recipient_id, content, "sent")

                # ACK1: server received the message
                await websocket.send(json.dumps({
                    "type":   "receipt",
                    "msg_id": msg_id,
                    "status": "sent",
                }))
                print(f"[SERVER] Stored message {msg_id}: {sender_id} → {recipient_id}")

                # Try immediate delivery if recipient is online
                recipient_ws = connected_users.get(recipient_id)
                if recipient_ws:
                    try:
                        await recipient_ws.send(json.dumps({
                            "type":      "message",
                            "msg_id":    msg_id,
                            "from":      sender_id,
                            "content":   content,
                            "timestamp": str(int(time.time())),
                        }))
                        update_message_status(msg_id, "delivered")
                        # ACK2: delivered to recipient device
                        await websocket.send(json.dumps({
                            "type":   "receipt",
                            "msg_id": msg_id,
                            "status": "delivered",
                        }))
                        print(f"[SERVER] Delivered {msg_id} to {recipient_id} (online)")
                    except Exception:
                        print(f"[SERVER] Failed to deliver {msg_id} to {recipient_id}")
                else:
                    print(f"[SERVER] {recipient_id} is offline — message {msg_id} queued in Postgres")

            elif msg_type == "read":
                msg_id = data["msg_id"]
                sender_id_of_msg = data.get("sender_id")
                update_message_status(msg_id, "read")

                # ACK3: send read receipt to original sender
                sender_ws = connected_users.get(sender_id_of_msg)
                if sender_ws:
                    await sender_ws.send(json.dumps({
                        "type":   "receipt",
                        "msg_id": msg_id,
                        "status": "read",
                    }))
                print(f"[SERVER] Read receipt sent for {msg_id}")

            elif msg_type == "heartbeat":
                if user_id:
                    r.setex(f"online:{user_id}", 90, "1")

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        if user_id:
            connected_users.pop(user_id, None)
            r.delete(f"online:{user_id}")
            # Write last_seen timestamp on disconnect (matches README description)
            r.set(f"last_seen:{user_id}", str(int(time.time())))
            print(f"[SERVER] {user_id} disconnected")


async def main():
    for attempt in range(20):
        try:
            init_db()
            break
        except Exception as e:
            print(f"DB not ready ({attempt+1}): {e}")
            await asyncio.sleep(2)

    print("[SERVER] Starting WebSocket server on :8765")
    async with websockets.serve(handle_client, "0.0.0.0", 8765):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
