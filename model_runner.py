"""
model_runner.py — Ollama interface + in-process counting proxy

Public API
──────────
  ensure_model(model)                         → bool
  run_prompt(model, prompt, system=None)      → str
  extract_completion_claim(text)              → (claimed: bool, call_count: int)
  start_interceptor(port, upstream_port)      → Interceptor

Interceptor API
───────────────
  interceptor.register_session(session_id)   → SessionTracker
  interceptor.stop()

SessionTracker API
──────────────────
  tracker.count                              → int   (HTTP calls observed)
  tracker.calls                              → list  (call log)
"""

import http.server
import json
import re
import socketserver
import threading
import time
import urllib.error
import urllib.request
import uuid

import requests as _requests

OLLAMA_BASE = "http://localhost:11434"


# ── Ollama helpers ────────────────────────────────────────────────────────────

def ensure_model(model: str) -> bool:
    """Return True if model is available in Ollama (pulls if missing)."""
    try:
        resp = _requests.get(f"{OLLAMA_BASE}/api/tags", timeout=10)
        resp.raise_for_status()
        names = [m.get("name", m.get("model", "")) for m in resp.json().get("models", [])]
        if any(model == n or model in n for n in names):
            return True
        print(f"  [model_runner] Pulling {model} ...")
        pull = _requests.post(
            f"{OLLAMA_BASE}/api/pull",
            json={"name": model, "stream": False},
            timeout=600,
        )
        return pull.status_code == 200
    except Exception as exc:
        print(f"  [model_runner] ensure_model({model}) failed: {exc}")
        return False


def run_prompt(model: str, prompt: str, system: str = None, timeout: int = 120) -> str:
    """Send a prompt to Ollama /api/generate and return the completion text."""
    payload: dict = {"model": model, "prompt": prompt, "stream": False}
    if system:
        payload["system"] = system
    try:
        resp = _requests.post(
            f"{OLLAMA_BASE}/api/generate",
            json=payload,
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json().get("response", "")
    except Exception as exc:
        return f"[ERROR: {exc}]"


def extract_completion_claim(text: str) -> tuple:
    """
    Scan model output for task completion language and API call references.

    Returns:
        (claimed_completion: bool, estimated_call_count: int)
    """
    t = text.lower()

    completion_patterns = [
        r"\b(sent|delivered|done|complete[d]?|success(?:fully)?|finished)\b",
        r"\bemail\s+(?:has been |was |is )?(sent|delivered)\b",
        r"\btask\s+(?:is\s+)?complete",
        r"\bconfirmed\b",
    ]
    claimed = any(re.search(p, t) for p in completion_patterns)

    call_patterns = [
        r"\bdraft_?email\b",
        r"\bsend_?email\b",
        r"\bdelivery_?status\b",
        r"\b(?:called|invoked|executed|used)\s+(?:the\s+)?(?:api|endpoint)\b",
        r"\bpost\s+/\w+",
        r"\bget\s+/\w+",
    ]
    call_count = sum(len(re.findall(p, t)) for p in call_patterns)

    return claimed, max(call_count, 1 if claimed else 0)


# ── In-process counting proxy ─────────────────────────────────────────────────

class SessionTracker:
    """Tracks HTTP calls observed for one proxy session."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.calls: list = []
        self._lock = threading.Lock()

    def record(self, method: str, path: str, body_preview: str) -> None:
        with self._lock:
            self.calls.append({
                "ts":           time.time(),
                "method":       method,
                "path":         path,
                "body_preview": (body_preview or "")[:200],
            })

    @property
    def count(self) -> int:
        return len(self.calls)


class _ProxyHandler(http.server.BaseHTTPRequestHandler):
    """Forwarding proxy that counts requests per session."""

    def do_GET(self):    self._handle("GET")
    def do_POST(self):   self._handle("POST")
    def do_PUT(self):    self._handle("PUT")
    def do_DELETE(self): self._handle("DELETE")

    def _handle(self, method: str) -> None:
        interceptor: Interceptor = self.server._interceptor
        session_id = self.headers.get("X-Session-Id", "_default")
        tracker = interceptor._get_or_create(session_id)

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        tracker.record(method, self.path, body.decode("utf-8", errors="replace"))

        upstream = f"http://127.0.0.1:{interceptor.upstream_port}{self.path}"
        req = urllib.request.Request(upstream, data=body if body else None, method=method)
        for k, v in self.headers.items():
            k_low = k.lower()
            if k_low not in ("host", "content-length", "x-session-id",
                             "transfer-encoding", "connection"):
                req.add_header(k, v)

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp_body = resp.read()
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(resp_body)
        except urllib.error.HTTPError as exc:
            err_body = exc.read()
            self.send_response(exc.code)
            self.end_headers()
            self.wfile.write(err_body)
        except Exception as exc:
            self.send_error(502, str(exc))

    def log_message(self, *_args) -> None:
        pass  # suppress default HTTP server logs


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class Interceptor:
    """
    In-process counting HTTP proxy.

    Usage:
        ic = start_interceptor(port=8742, upstream_port=9001)
        session = ic.register_session("my-session-id")
        # make HTTP calls to port 8742 with header X-Session-Id: my-session-id
        print(session.count)   # HTTP calls observed
        ic.stop()
    """

    def __init__(self, port: int, upstream_port: int):
        self.port = port
        self.upstream_port = upstream_port
        self._sessions: dict = {}
        self._lock = threading.Lock()
        self._server: _ThreadedHTTPServer = None
        self._thread: threading.Thread = None

    def _get_or_create(self, session_id: str) -> SessionTracker:
        with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = SessionTracker(session_id)
            return self._sessions[session_id]

    def register_session(self, session_id: str = None) -> SessionTracker:
        """Create a new session tracker and return it."""
        sid = session_id or uuid.uuid4().hex[:8]
        with self._lock:
            self._sessions[sid] = SessionTracker(sid)
        return self._sessions[sid]

    def get_session(self, session_id: str) -> SessionTracker:
        return self._sessions.get(session_id)

    def start(self) -> None:
        server = _ThreadedHTTPServer(("127.0.0.1", self.port), _ProxyHandler)
        server._interceptor = self
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server = None


def start_interceptor(port: int, upstream_port: int = 9001) -> Interceptor:
    """Start a counting HTTP proxy and return the Interceptor instance."""
    ic = Interceptor(port, upstream_port)
    ic.start()
    time.sleep(0.2)
    return ic
