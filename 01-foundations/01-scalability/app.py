import os
import time
import random
from http.server import HTTPServer, BaseHTTPRequestHandler

# SIMULATE_LATENCY_MS: set via environment to make one backend "slower"
# e.g. docker compose run -e SIMULATE_LATENCY_MS=200 app2
SIMULATE_LATENCY_MS = int(os.environ.get("SIMULATE_LATENCY_MS", 0))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        hostname = os.environ.get("HOSTNAME", "unknown")

        if self.path == "/health":
            body = f"OK: {hostname}\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body.encode())
            return

        # Simulate optional per-backend latency to demonstrate least-connections
        # advantage over round-robin when backends have uneven response times.
        if SIMULATE_LATENCY_MS > 0:
            time.sleep(SIMULATE_LATENCY_MS / 1000.0)

        body = f"Handled by: {hostname}\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, format, *args):
        pass  # suppress access logs for cleaner experiment output


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"Serving on port {port} (hostname={os.environ.get('HOSTNAME', 'unknown')}, "
          f"simulated_latency={SIMULATE_LATENCY_MS}ms)", flush=True)
    server.serve_forever()
