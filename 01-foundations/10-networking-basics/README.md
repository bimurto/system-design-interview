# 10 — Networking Basics

**Prerequisites:** [09 — Indexes](../09-indexes/)
**Next:** [11 — API Design](../11-api-design/)

---

## Concept

Networking fundamentals are not optional knowledge for system designers — they are the physical and mathematical
constraints within which every distributed system operates. Latency is bounded by the speed of light: a packet traveling
from New York to London and back takes at minimum ~70ms simply because of distance, regardless of how fast your servers
are. Bandwidth, by contrast, is a cost question: you can buy more of it, but you cannot buy lower latency between two
geographically separated data centers. Understanding this distinction shapes every architectural decision from CDN
placement to microservice co-location.

The TCP/IP stack is the foundation of virtually all distributed systems communication. TCP provides the reliable,
ordered, connection-oriented transport that databases, HTTP servers, and message queues depend on. The three-way
handshake (SYN, SYN-ACK, ACK) guarantees both sides are ready before data flows, but it costs one round-trip before the
first byte is sent. TCP's congestion control (slow-start, AIMD) means new connections begin transmitting
conservatively — a behavior that punishes short-lived connections and rewards connection reuse through keep-alive and
connection pooling.

Latency and bandwidth are frequently confused but solve different problems. Latency is the fixed per-request overhead:
propagation delay plus processing plus queuing. Bandwidth is throughput capacity. For small API payloads (a few KB),
latency dominates — the network trip costs more than the transfer time. For large file transfers (video, backups),
bandwidth dominates — the speed of the link determines total time. This means the solutions are different: reducing
latency requires moving servers closer to users (CDN, edge computing, geographic distribution), while increasing
bandwidth requires better links or parallel transfers. Throwing bandwidth at a latency problem accomplishes nothing.

HTTP has evolved significantly to address TCP's limitations in the web context. HTTP/1.1 introduced persistent
connections and pipelining, but pipelining suffered from head-of-line blocking — a slow response blocked all subsequent
responses on the same connection. HTTP/2 introduced multiplexed streams over a single TCP connection, solving
application-layer HOL blocking and adding header compression (HPACK) and server push. However, HTTP/2 still runs over
TCP: a single lost TCP segment blocks all streams. HTTP/3 solves this by running over QUIC, a UDP-based transport that
implements reliability per-stream — a lost packet only blocks the stream it belongs to, not all streams. QUIC also
achieves 0-RTT connection resumption for returning clients.

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

In practice, engineers work with L3 (IP), L4 (TCP/UDP), and L7 (HTTP/gRPC) daily. Layers 5–6 are largely folded into TLS
and the application layer.

### TCP vs UDP

| Property        | TCP                          | UDP                      |
|-----------------|------------------------------|--------------------------|
| Connection      | 3-way handshake required     | Connectionless           |
| Reliability     | Guaranteed delivery + order  | Best-effort, no ordering |
| Flow control    | Yes (sliding window)         | No                       |
| Congestion ctrl | Yes (slow-start, AIMD)       | No (app must implement)  |
| Head-of-line    | Yes (one lost packet stalls) | No                       |
| Overhead        | ~20 byte header + state      | ~8 byte header           |
| Use cases       | HTTP, DB, file transfer      | DNS, video, gaming, QUIC |

**When UDP wins:** You can tolerate some packet loss (video, VoIP), or you need ultra-low latency and implement
reliability yourself (QUIC does this).

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

Cost: **1 RTT before first byte**. TLS 1.2 adds 2 more RTTs (TLS 1.3 reduces to 1; QUIC/HTTP/3 achieves 0-RTT on
reconnects).

TCP slow-start: new connections begin transmitting conservatively (cwnd=10 segments ≈ 14KB) and ramp up. Short
connections never leave slow-start — another reason to reuse connections via keep-alive and connection pools.

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

**HTTP/2 gotcha:** TCP HOL blocking still exists — one lost TCP segment blocks ALL streams. HTTP/3/QUIC fixes this
because each QUIC stream is independent.

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

- **TTL tradeoffs:** Low TTL (30s) → fast failover, high resolver load. High TTL (300s+) → slower propagation, fewer
  queries.
- **Negative caching:** NXDOMAIN results are cached too. Misconfigurations can take time to propagate.
- **DNS-based load balancing:** Return multiple A records; clients rotate. Add GeoDNS to return region-specific IPs.
- In microservices (Kubernetes): every pod has a DNS resolver; service discovery uses DNS internally. Cache DNS results
  or use connection pools to avoid per-request lookups.

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

| Operation                      | Latency        | Relative to L1 |
|--------------------------------|----------------|----------------|
| L1 cache reference             | 0.5 ns         | 1x             |
| Branch mispredict              | 5 ns           | 10x            |
| L2 cache reference             | 7 ns           | 14x            |
| Mutex lock/unlock              | 25 ns          | 50x            |
| Main memory (RAM) reference    | 100 ns         | 200x           |
| Compress 1KB (Snappy)          | 3,000 ns       | 6,000x         |
| Send 1KB over 1Gbps network    | 10,000 ns      | 20,000x        |
| Read 4KB randomly from SSD     | 150,000 ns     | 300,000x       |
| Read 1MB sequentially from RAM | 250,000 ns     | 500,000x       |
| Round trip within same DC      | 500,000 ns     | 1,000,000x     |
| Read 1MB sequentially from SSD | 1,000,000 ns   | 2,000,000x     |
| HDD disk seek                  | 10,000,000 ns  | 20,000,000x    |
| Read 1MB sequentially from HDD | 20,000,000 ns  | 40,000,000x    |
| Send packet US → Europe → US   | 150,000,000 ns | 300,000,000x   |

**In human terms** (1 CPU cycle = 1 second):

- L1 cache = 1 second
- RAM access = 3.5 minutes
- SSD read = 2.5 days
- HDD seek = 3.9 months
- Cross-US trip = 4.8 years

### WebSocket vs SSE vs Long Polling

| Feature         | WebSocket               | SSE                       | Long Polling         |
|-----------------|-------------------------|---------------------------|----------------------|
| Direction       | Full-duplex             | Server → Client only      | Server → Client only |
| Protocol        | ws:// upgrade from HTTP | HTTP/1.1 or HTTP/2        | Plain HTTP           |
| Browser support | Universal               | Universal (IE needs poly) | Universal            |
| Reconnect       | Manual                  | Automatic                 | Manual               |
| Proxy/firewall  | Sometimes blocked       | Works (it's HTTP)         | Works                |
| Use cases       | Chat, collab editing    | Dashboards, notifications | Legacy fallback      |

**Choose WebSocket** when the client also sends frequent messages (game state, chat).
**Choose SSE** when the server pushes only; simpler to implement and debuggable with curl.
**Choose long polling** only for legacy browser compatibility or firewall-constrained environments.

### Trade-offs

| Approach       | Pros                                          | Cons                                            |
|----------------|-----------------------------------------------|-------------------------------------------------|
| TCP (HTTP/1.1) | Universal support, simple debugging           | Per-connection HOL blocking, handshake overhead |
| HTTP/2         | Multiplexing, header compression, single conn | TCP-layer HOL blocking remains                  |
| HTTP/3 / QUIC  | No HOL blocking, 0-RTT reconnect              | UDP often blocked by firewalls, newer tooling   |
| UDP (raw)      | Ultra-low overhead, no connection state       | No reliability — app must implement             |
| WebSocket      | Full-duplex, low per-message overhead         | Not cacheable, stateful (harder to scale)       |
| SSE            | Simple, HTTP-native, auto-reconnect           | Server-to-client only, no binary frames         |

### Failure Modes

**TCP head-of-line blocking in HTTP/1.1:** A single slow request on a connection holds up all subsequent requests behind
it. Browser workaround (opening 6 parallel connections per domain) wastes OS resources and still doesn't fully
parallelize. HTTP/2 fixes this at the application layer; HTTP/3 fixes it at the transport layer.

**DNS failures and TTL too high:** DNS is a distributed cache. If your TTL is set to 3600 seconds (1 hour) and you need
to cut traffic over to a new IP during an incident, clients will continue hitting the old address for up to an hour.
During incidents, teams discover too late that their DNS TTL is too high. Best practice: lower TTL to 60–300s before a
planned migration; keep it low for production traffic.

**Bandwidth saturation vs latency-bound systems:** When a system is slow, the diagnosis matters. A bandwidth-saturated
system benefits from compression, CDN offloading, or more capacity. A latency-bound system (many small sequential
requests) needs fewer round trips — batching, connection reuse, or moving computation closer to users. Applying the
wrong fix (buying bandwidth for a latency problem) wastes money and doesn't help.

**TCP connection storms on service restart:** When a heavily loaded service restarts, all clients attempt to reconnect
simultaneously. Thousands of TCP handshakes + TLS handshakes + application-level authentication hit the server at once
while it is still warming up. This can trigger the server to crash again immediately (thundering herd). Mitigations:
exponential backoff with jitter in clients, connection pools that reconnect gradually, or health-check gates that hold
traffic until the service is ready.

**TCP TIME_WAIT accumulation and port exhaustion:** When a TCP connection is closed, the active closer holds the socket
in TIME_WAIT for 2×MSL (typically 60–120 seconds) to ensure any delayed packets are absorbed. A service making 1,000 new
connections per second accumulates up to 60,000–120,000 TIME_WAIT sockets. The default Linux ephemeral port range (~
28,000 ports: 32768–60999) is smaller — the result is port exhaustion and `EADDRNOTAVAIL` errors on new connection
attempts. Primary mitigation: connection pooling (keep-alive). Secondary: `SO_REUSEADDR`, `net.ipv4.tcp_tw_reuse=1`.
Never use `net.ipv4.tcp_tw_recycle` — it is broken under NAT and was removed in Linux 4.12.

**Kubernetes ndots:5 DNS amplification:** By default, Kubernetes sets `ndots: 5` in pod resolv.conf. A hostname with
fewer than 5 dots (e.g. `redis`, `api`) triggers up to 5 search-domain suffix lookups before the absolute name is
tried — multiplying DNS query volume by up to 5x per connection. On microservice meshes with hundreds of services making
thousands of calls per second, this becomes a meaningful CoreDNS load spike. Mitigation: use FQDNs (trailing dot:
`redis.default.svc.cluster.local.`) or reduce `ndots` in your pod spec.

---

## Interview Talking Points

- "A CDN reduces latency by moving content closer to users — the speed of light is the limit. You can't reduce the
  propagation delay between New York and Tokyo no matter how fast your servers are. The only lever is geographic
  distribution."
- "HTTP/2 multiplexing solves head-of-line blocking at the application layer but not at the TCP layer — a single lost
  TCP segment still blocks all multiplexed streams. HTTP/3/QUIC solves it at transport: each QUIC stream handles its own
  retransmission independently."
- "WebSockets for bidirectional real-time communication, SSE for server-to-client push (simpler, HTTP-native,
  auto-reconnect, works through proxies), long polling only as a fallback for legacy/firewall-constrained environments."
- "DNS TTL is a cache — during incidents, high TTL means slow cutover. Always check (and pre-lower) your TTL before a
  planned migration. A TTL of 3600s means clients will hit an old IP for up to an hour after a change."
- "Bandwidth is cheap; latency is physics — you can buy more bandwidth but you can't move servers closer to users for
  free. The solution to latency is geographic distribution, not better hardware or bigger pipes."
- "Connection pooling solves two problems at once: it eliminates TCP handshake overhead on every request AND prevents
  TIME_WAIT port exhaustion at high connection churn rates. Any service hitting a database or upstream API without
  connection pooling is leaving performance on the table."
- "TCP slow-start means a brand-new connection transmits conservatively (initial cwnd ≈ 10 segments ≈ 14KB) and ramps
  up. Short-lived connections never leave slow-start — which is why HTTP/1.1's one-request-per-connection model was so
  costly, and why keep-alive / HTTP/2 multiplexing matter."
- "Kubernetes ndots:5 means a short hostname triggers up to 5 DNS lookups before resolving. On a high-traffic service
  mesh this is a meaningful CoreDNS load multiplier. Use FQDNs or reduce ndots in production."

---

## Hands-on Lab

**Time:** ~25–35 minutes
**Services:** nginx (HTTP server serving static payloads of 1KB, 100KB, 1MB, 10MB)

### Setup

```bash
# Generate static payload files (idempotent — safe to run multiple times)
python create_static.py

# Start nginx
docker compose up -d
```

Wait ~10 seconds for the nginx health check to pass (the compose file polls `/health` before marking the service ready).

### Experiment

```bash
python experiment.py
```

The experiment runs five phases:

1. **Latency numbers** — reference table of canonical latency figures with design implications
2. **New connection vs keep-alive** — 20 requests each, per-request timing printed so you can see variance; computes
   speedup ratio and total time saved
3. **Bandwidth vs latency crossover** — 1KB, 100KB, 1MB, 10MB payloads; reports avg, min/max, and throughput in Mbps so
   the crossover from latency-dominated to bandwidth-dominated is visible
4. **DNS resolution timing** — measures IP literal vs `/etc/hosts` lookup; explains what the same test would show for an
   uncached external hostname
5. **TCP TIME_WAIT accumulation** — creates 40 rapid short-lived connections, then queries `netstat`/`ss` to count
   TIME_WAIT sockets and explain port exhaustion risk

### Break It

Stop nginx mid-experiment to observe connection-refused errors:

```bash
docker compose stop nginx
```

Re-run `python experiment.py`. New-connection attempts in Phase 2 fail immediately with `ConnectionRefusedError`. The
keep-alive connection in Phase 2 raises `RemoteDisconnected` (the server closed the already-established socket). This
mirrors what happens during a rolling deploy with no graceful drain.

### Observe

- Keep-alive speedup should be 2–5× on localhost; on a WAN link with 20ms RTT the ratio is larger
- The 10MB payload should show a visibly higher transfer time than 1MB, making the bandwidth-dominated regime clear even
  on loopback
- TIME_WAIT socket count correlates with how many connections were opened; watch it decay over 60 seconds if you re-run
  the netstat query manually
- The throughput column in Phase 3 shows Mbps achieved on loopback; on a 10Mbps WAN link that number would cap out at ~
  10 Mbps and the transfer times would be 100–1000× larger

### Teardown

```bash
docker compose down -v
```

---

## Real-World Examples

- **Cloudflare:** Uses anycast routing to direct users to the nearest point of presence (PoP), reducing round-trip time
  by routing packets to the geographically closest data center. Source: Cloudflare blog, "How we built Cloudflare's
  network"
- **Google:** Developed QUIC (now standardized as HTTP/3) to eliminate TCP head-of-line blocking on high-packet-loss
  mobile networks, where losing a single packet previously stalled all streams. Source: Langley et al., "The QUIC
  Transport Protocol," SIGCOMM 2017
- **Netflix:** Benchmarked TCP buffer tuning (SO_SNDBUF, TCP receive window scaling) and found 2–4x throughput
  improvement on congested intercontinental paths where default TCP buffers under-utilized available bandwidth. Source:
  Netflix Tech Blog

---

## Common Mistakes

- **Confusing latency and bandwidth** — adding more bandwidth does not reduce latency. A 10Gbps link still has the same
  propagation delay as a 1Mbps link between the same two cities. If your system is slow due to many sequential round
  trips, you need fewer trips, not more bandwidth.
- **Not accounting for TCP handshake overhead in per-request latency calculations** — a 50ms RTT link costs 50ms just
  for the SYN/SYN-ACK before any data flows. Short-lived connections (one request, then close) pay this cost on every
  request. Connection pooling amortizes it.
- **Using long-polling when WebSockets are available** — long-polling holds an HTTP connection open, wastes a server
  thread/file descriptor per client, and adds latency per message (the response must complete before the next poll
  starts). WebSockets eliminate this overhead.
- **Setting DNS TTL too high in production** — a TTL of 3600s means clients cache your IP for an hour. During an
  incident requiring an IP change (DDoS mitigation, region failover), traffic will continue hitting the old address for
  up to that TTL duration. Keep TTL at 60–300s for production-critical records.
