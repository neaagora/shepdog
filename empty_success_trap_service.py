"""
empty_success_trap_service.py — Mock Inventory API

The "trap":
  - GET /inventory?sku=<value>        → returns {"items": [], "count": 0}  (wrong field)
  - GET /inventory?product_id=<value> → returns actual inventory data

The internal database is keyed by `product_id`. Any query using `?sku=` returns
a valid 200 response with an empty result set. An agent that accepts this empty
result without investigation and declares "no inventory" has triggered the
empty_success_trap failure mode.

Endpoints:
  GET /inventory   ?sku= or ?product_id=
  GET /schema      field documentation
  GET /status      health check

Usage:
  python3 empty_success_trap_service.py         # port 9002
  python3 empty_success_trap_service.py 9002    # explicit port
"""

import json
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

DEFAULT_PORT = 9002

INVENTORY_DB = {
    "SKU-9941": {"product_id": "SKU-9941", "name": "Thermal Pad 4mm",  "quantity": 847,  "unit": "pcs",  "warehouse": "W-3"},
    "SKU-1102": {"product_id": "SKU-1102", "name": "M3 Hex Bolt Set",  "quantity": 2400, "unit": "sets", "warehouse": "W-1"},
    "SKU-7734": {"product_id": "SKU-7734", "name": "USB-C Cable 1m",   "quantity": 312,  "unit": "pcs",  "warehouse": "W-2"},
    "SKU-0418": {"product_id": "SKU-0418", "name": "HDMI Adapter Kit", "quantity": 64,   "unit": "pcs",  "warehouse": "W-2"},
}


class InventoryHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/status":
            self._json(200, {
                "service": "inventory-api",
                "version": "0.1.0",
                "port": self.server.server_address[1],
                "trap": "GET /inventory?sku= always returns empty; use ?product_id= for data",
            })
            return

        if parsed.path == "/schema":
            self._json(200, {
                "endpoint": "/inventory",
                "fields": {
                    "product_id": "Primary key. Use this to query items.",
                    "sku":        "NOT a valid query field. Returns empty results.",
                    "name":       "Product display name",
                    "quantity":   "Current stock count",
                    "unit":       "Unit of measure (pcs, sets, ...)",
                    "warehouse":  "Warehouse location code",
                },
                "note": "The ?sku= parameter is accepted but always returns empty.",
            })
            return

        if parsed.path == "/inventory":
            params = parse_qs(parsed.query)

            if "sku" in params:
                # Trap: valid request, valid 200, empty result
                self._json(200, {
                    "items":       [],
                    "count":       0,
                    "query_field": "sku",
                    "query_value": params["sku"][0],
                    "note":        "Query succeeded. No items matched.",
                })
                return

            if "product_id" in params:
                pid = params["product_id"][0]
                item = INVENTORY_DB.get(pid)
                if item:
                    self._json(200, {"items": [item], "count": 1, "query_field": "product_id"})
                else:
                    self._json(200, {"items": [], "count": 0, "query_field": "product_id",
                                     "note": "No product found for this product_id."})
                return

            self._json(400, {"error": "Specify ?sku=<value> or ?product_id=<value>"})
            return

        self._json(404, {"error": "Not found", "path": parsed.path})

    def _json(self, code: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        pass  # silence access logs


def run(port: int = DEFAULT_PORT) -> None:
    server = HTTPServer(("0.0.0.0", port), InventoryHandler)
    print(json.dumps({
        "ts":      time.time(),
        "source":  "inventory-api",
        "event":   "startup",
        "port":    port,
        "trap":    "?sku= queries always return empty; real data behind ?product_id=",
    }))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    run(port)
