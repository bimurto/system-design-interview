# 10 — Networking Basics

**Prerequisites:** [09 — Indexes](../09-indexes/)
**Next:** [11 — API Design](../11-api-design/)

---

## Concept

Networking fundamentals are not optional knowledge for system designers — they are the physical and mathematical constraints within which every distributed system operates. Latency is bounded by the speed of light: a packet traveling from New York to London and back takes at minimum ~70ms simply because of distance, regardless of how fast your servers are. Bandwidth, by contrast, is a cost question: you can buy more of it, but you cannot buy lower latency between two geographically separated data centers. Understanding this distinction shapes every architectural decision from CDN placement to microservice co-location.

The TCP/IP stack is the foundation of virtually all distributed systems communication. TCP provides the reliable, ordered, connection-oriented transport that databases, HTTP servers, and message queues depend on. The three-way handshake (SYN, SYN-ACK, ACK) guarantees both sides are ready before data flows, but it costs one round-trip before the first byte is sent. TCP's congestion control (slow-start, AIMD) means new connections begin transmitting conservatively — a behavior that punishes short-lived connections and rewards connection reuse through keep-alive and connection pooling.

Latency and bandwidth are frequently confused but solve different problems. Latency is the fixed per-request overhead: propagation delay plus processing plus queuing. Bandwidth is throughput capacity. For small API payloads (a few KB), latency dominates — the network trip costs more than the transfer time. For large file transfers (video, backups), bandwidth dominates — the speed of the link determines total time. This means the solutions are different: reducing latency requires moving servers closer to users (CDN, edge computing, geographic distribution), while increasing bandwidth requires better links or parallel transfers. Throwing bandwidth at a latency problem accomplishes nothing.

HTTP has evolved significantly to address TCP's limitations in the web context. HTTP/1.1 introduced persistent connections and pipelining, but pipelining suffered from head-of-line blocking — a slow response blocked all subsequent responses on the same connection. HTTP/2 introduced multiplexed streams over a single TCP connection, solving application-layer HOL blocking and adding header compression (HPACK) and server push. However, HTTP/2 still runs over TCP: a single lost TCP segment blocks all streams. HTTP/3 solves this by running over QUIC, a UDP-based transport that implements reliability per-stream — a lost packet only blocks the stream it belongs to, not all streams. QUIC also achieves 0-RTT connection resumption for returning clients.

---

## How It Works

### OSI Model

| Layer | Name         | Protocol Examples       | Unit    |
|-------|--------------|-------------------------|---------|
| 7     | Application  | HTTP, DNS, SMTP, gRPC   | Message |
| 6     | Presentation | TLS/SSL, compression    | —       |
| 5     | Session      | (mostly in application) | —       |
| 4     | Transport    | TCP, UDP                | Segment |
| 3     | Network      | IP, ICMP                | Packet  |
| 2     | Data Link    | Ethernet, Wi-Fi         | Frame   |
| 1     | Physical     | Cables, radio, fiber    | Bit     |

In practice, engineers work with L3 (IP), L4 (TCP/UDP), and L7 (HTTP/gRPC) daily. Layers 5–6 are largely folded into TLS and the application layer.

### TCP vs UDP

| Property        | TCP                         | UDP                           |
|-----------------|-----------------------------|-------------------------------|
| Connection      | 3-way handshake required    | Connectionless                |
| Reliability     | Guaranteed delivery + order | Best-effort, no ordering      |
| Flow control    | Yes (sliding window)        | No                            |
| Congestion ctrl | Yes (slow-start, AIMD)      | No (app must implement)       |
| Head-of-line    | Yes (one lost packet stalls)| No                            |
| Overhead        | ~20 byte header + state     | ~8 byte header                |
| Use cases       | HTTP, DB, file transfer     | DNS, video, gaming, QUIC      |

**When UDP wins:** You can tolerate some packet loss (video, VoIP), or you need ultra-low latency and implement reliability yourself (QUIC does this).

### TCP 3-Way Handshake

```
Client                    Server
  │──── SYN ──────────────►│   Client picks initial seq number
  │◄─── SYN-ACK ───────────│   Server picks its seq number
  │──── ACK ──────────────►│   Connection established
  │                         │
  │──── GET /index.html ───►│   First request (1 RTT overhead)
  │◄─── 200 OK ─────────────│
```

Cost: **1 RTT before first byte**. TLS 1.2 adds 2 more RTTs (TLS 1.3 reduces to 1; QUIC/HTTP/3 achieves 0-RTT on reconnects).

TCP slow-start: new connections begin transmitting conservatively (cwnd=10 segments ≈ 14KB) and ramp up. Short connections never leave slow-start — another reason to reuse connections via keep-alive and connection pools.

### HTTP/1.1 vs HTTP/2 vs HTTP/3

| Feature               | HTTP/1.1           | HTTP/2               | HTTP/3 (QUIC)          |
|-----------------------|--------------------|----------------------|------------------------|
| Transport             | TCP                | TCP                  | UDP (QUIC)             |
| Multiplexing          | No (pipeline hack) | Yes, streams         | Yes, streams           |
| Head-of-line blocking | Per connection     | Per connection (TCP) | No (per stream)        |
| Header compression    | None               | HPACK                | QPACK                  |
| Server push           | No                 | Yes                  | Yes                    |
| 0-RTT reconnect       | No                 | No                   | Yes                    |
| Connection setup      | 1 RTT + TLS        | 1 RTT + TLS          | 1 RTT (0 on reconnect) |

**HTTP/2 gotcha:** TCP HOL blocking still exists — one lost TCP segment blocks ALL streams. HTTP/3/QUIC fixes this because each QUIC stream is independent.

### DNS Lookup Chain

```
Browser cache (TTL)
  └─► OS cache (/etc/hosts, nscd)
       └─► Recursive resolver (ISP or 8.8.8.8)
            └─► Root nameserver (.)
                 └─► TLD nameserver (.com)
                      └─► Authoritative nameserver (example.com)
                           └─► Returns A/AAAA record
```

- **TTL tradeoffs:** Low TTL (30s) → fast failover, high resolver load. High TTL (300s+) → slower propagation, fewer queries.
- **Negative caching:** NXDOMAIN results are cached too. Misconfigurations can take time to propagate.
- **DNS-based load balancing:** Return multiple A records; clients rotate. Add GeoDNS to return region-specific IPs.
- In microservices (Kubernetes): every pod has a DNS resolver; service discovery uses DNS internally. Cache DNS results or use connection pools to avoid per-request lookups.

### Bandwidth vs Latency

**Latency** = time for first byte = fixed cost (propagation + processing + queuing).
**Bandwidth** = throughput = bytes per second = variable with load.

```
Transfer time = latency + (size / bandwidth)

Example: 1MB file, 50ms RTT, 10Mbps link:
  = 50ms + (8_000_000 bits / 10_000_000 bps)
  = 50ms + 800ms
  = 850ms total
```

- Small payloads: dominated by latency → minimize round-trips
- Large payloads: dominated by bandwidth → compress, use CDN
- "Fat and slow" = high bandwidth + high latency (satellite)
- "Thin and fast" = low bandwidth + low latency (local LAN)

### Latency Numbers Every Engineer Must Know

| Operation                        | Latency        | Relative to L1     |
|----------------------------------|----------------|--------------------|
| L1 cache reference               | 0.5 ns         | 1x                 |
| Branch mispredict                | 5 ns           | 10x                |
| L2 cache reference               | 7 ns           | 14x                |
| Mutex lock/unlock                | 25 ns          | 50x                |
| Main memory (RAM) reference      | 100 ns         | 200x               |
| Compress 1KB (Snappy)            | 3,000 ns       | 6,000x             |
| Send 1KB over 1Gbps network      | 10,000 ns      | 20,000x            |
| Read 4KB randomly from SSD       | 150,000 ns     | 300,000x           |
| Read 1MB sequentially from RAM   | 250,000 ns     | 500,000x           |
| Round trip within same DC        | 500,000 ns     | 1,000,000x         |
| Read 1MB sequentially from SSD   | 1,000,000 ns   | 2,000,000x         |
| HDD disk seek                    | 10,000,000 ns  | 20,000,000x        |
| Read 1MB sequentially from HDD   | 20,000,000 ns  | 40,000,000x        |
| Send packet US → Europe → US     | 150,000,000 ns | 300,000,000x       |

**In human terms** (1 CPU cycle = 1 second):
- L1 cache = 1 second
- RAM access = 3.5 minutes
- SSD read = 2.5 days
- HDD seek = 3.9 months
- Cross-US trip = 4.8 years

### WebSocket vs SSE vs Long Polling

| Feature           | WebSocket              | SSE                       | Long Polling              |
|-------------------|------------------------|---------------------------|---------------------------|
| Direction         | Full-duplex            | Server → Client only      | Server → Client only      |
| Protocol          | ws:// upgrade from HTTP| HTTP/1.1 or HTTP/2        | Plain HTTP                |
| Browser support   | Universal              | Universal (IE needs poly) | Universal                 |
| Reconnect         | Manual                 | Automatic                 | Manual                    |
| Proxy/firewall    | Sometimes blocked      | Works (it's HTTP)         | Works                     |
| Use cases         | Chat, collab editing   | Dashboards, notifications | Legacy fallback            |

**Choose WebSocket** when the client also sends frequent messages (game state, chat).
**Choose SSE** when the server pushes only; simpler to implement and debuggable with curl.
**Choose long polling** only for legacy browser compatibility or firewall-constrained environments.

### Trade-offs

| Approach           | Pros                                              | Cons                                                  |
|--------------------|---------------------------------------------------|-------------------------------------------------------|
| TCP (HTTP/1.1)     | Universal support, simple debugging               | Per-connection HOL blocking, handshake overhead       |
| HTTP/2             | Multiplexing, header compression, single conn     | TCP-layer HOL blocking remains                        |
| HTTP/3 / QUIC      | No HOL blocking, 0-RTT reconnect                  | UDP often blocked by firewalls, newer tooling         |
| UDP (raw)          | Ultra-low overhead, no connection state           | No reliability — app must implement                  |
| WebSocket          | Full-duplex, low per-message overhead             | Not cacheable, stateful (harder to scale)             |
| SSE                | Simple, HTTP-native, auto-reconnect               | Server-to-client only, no binary frames               |

### Failure Modes

**TCP head-of-line blocking in HTTP/1.1:** A single slow request on a connection holds up all subsequent requests behind it. Browser workaround (opening 6 parallel connections per domain) wastes OS resources and still doesn't fully parallelize. HTTP/2 fixes this at the application layer; HTTP/3 fixes it at the transport layer.

**DNS failures and TTL too high:** DNS is a distributed cache. If your TTL is set to 3600 seconds (1 hour) and you need to cut traffic over to a new IP during an incident, clients will continue hitting the old address for up to an hour. During incidents, teams discover too late that their DNS TTL is too high. Best practice: lower TTL to 60–300s before a planned migration; keep it low for production traffic.

**Bandwidth saturation vs latency-bound systems:** When a system is slow, the diagnosis matters. A bandwidth-saturated system benefits from compression, CDN offloading, or more capacity. A latency-bound system (many small sequential requests) needs fewer round trips — batching, connection reuse, or moving computation closer to users. Applying the wrong fix (buying bandwidth for a latency problem) wastes money and doesn't help.

**TCP connection storms on service restart:** When a heavily loaded service restarts, all clients attempt to reconnect simultaneously. Thousands of TCP handshakes + TLS handshakes + application-level authentication hit the server at once while it is still warming up. This can trigger the server to crash again immediately (thundering herd). Mitigations: exponential backoff with jitter in clients, connection pools that reconnect gradually, or health-check gates that hold traffic until the service is ready.

---

## Interview Talking Points

- "A CDN reduces latency by moving content closer to users — the speed of light is the limit. You can't reduce the propagation delay between New York and Tokyo no matter how fast your servers are."
- "HTTP/2 multiplexing solves head-of-line blocking at the application layer but not at the TCP layer — HTTP/3/QUIC solves it at transport by running independent streams over UDP."
- "WebSockets for bidirectional real-time communication, SSE for server-to-client push (simpler, HTTP-native), long polling only as a fallback for legacy/firewall-constrained environments."
- "DNS TTL is a cache — during incidents, high TTL means slow cutover. Always check your TTL before a migration."
- "Bandwidth is cheap; latency is physics — you can buy more bandwidth but you can't move servers closer to users for free. The solution to latency is geographic distribution, not better hardware."

---

## Hands-on Lab

**Time:** ~20–30 minutes
**Services:** nginx (HTTP server)

### Setup

```bash
docker compose up -d
```

Wait ~5 seconds for nginx to be ready.

### Experiment

```bash
python experiment.py
```

The experiment runs four phases:

1. **Latency baseline** — measures raw TCP connection time vs first-byte time to quantify handshake overhead
2. **New connection vs keep-alive** — opens a new TCP connection for each request vs reusing one connection, demonstrating the cost of repeated handshakes
3. **Payload size vs transfer time** — sends requests for payloads of varying sizes to show the crossover point from latency-dominated to bandwidth-dominated transfer
4. **DNS resolution timing** — times the resolver lookup separately from the TCP connect to isolate DNS overhead

### Break It

Stop nginx mid-experiment to observe connection refused errors:

```bash
docker compose stop nginx
```

Re-run `python experiment.py` and observe that new-connection attempts fail immediately with `ConnectionRefusedError` while the keep-alive path shows the connection was already closed.

### Observe

- Compare new-connection timing vs keep-alive timing — keep-alive should be consistently faster by ~1 RTT
- Watch the latency numbers table: small payloads show nearly constant time (latency-bound); large payloads show time growing linearly with size (bandwidth-bound)
- DNS resolution time appears once and is then cached — subsequent requests pay zero DNS cost

### Teardown

```bash
docker compose down -v
```

---

## Real-World Examples

- **Cloudflare:** Uses anycast routing to direct users to the nearest point of presence (PoP), reducing round-trip time by routing packets to the geographically closest data center. Source: Cloudflare blog, "How we built Cloudflare's network"
- **Google:** Developed QUIC (now standardized as HTTP/3) to eliminate TCP head-of-line blocking on high-packet-loss mobile networks, where losing a single packet previously stalled all streams. Source: Langley et al., "The QUIC Transport Protocol," SIGCOMM 2017
- **Netflix:** Benchmarked TCP buffer tuning (SO_SNDBUF, TCP receive window scaling) and found 2–4x throughput improvement on congested intercontinental paths where default TCP buffers under-utilized available bandwidth. Source: Netflix Tech Blog

---

## Common Mistakes

- **Confusing latency and bandwidth** — adding more bandwidth does not reduce latency. A 10Gbps link still has the same propagation delay as a 1Mbps link between the same two cities. If your system is slow due to many sequential round trips, you need fewer trips, not more bandwidth.
- **Not accounting for TCP handshake overhead in per-request latency calculations** — a 50ms RTT link costs 50ms just for the SYN/SYN-ACK before any data flows. Short-lived connections (one request, then close) pay this cost on every request. Connection pooling amortizes it.
- **Using long-polling when WebSockets are available** — long-polling holds an HTTP connection open, wastes a server thread/file descriptor per client, and adds latency per message (the response must complete before the next poll starts). WebSockets eliminate this overhead.
- **Setting DNS TTL too high in production** — a TTL of 3600s means clients cache your IP for an hour. During an incident requiring an IP change (DDoS mitigation, region failover), traffic will continue hitting the old address for up to that TTL duration. Keep TTL at 60–300s for production-critical records.
