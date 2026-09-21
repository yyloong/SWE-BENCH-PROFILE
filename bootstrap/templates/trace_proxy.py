#!/usr/bin/env python3
import argparse
import http.client
import json
import os
import signal
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def text_content(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            item.get("text", "")
            for item in value
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    return ""


def request_kind(payload):
    messages = payload.get("messages", [])
    content = "\n".join(
        text_content(message.get("content"))
        for message in messages
        if isinstance(message, dict)
    )
    if "Generate a title for this conversation:" in content:
        return "title"
    if payload.get("tools"):
        return "main"
    return "auxiliary"


def generated_delta(payload):
    for choice in payload.get("choices", []):
        delta = choice.get("delta", {})
        for key, value in delta.items():
            if key == "role":
                continue
            if isinstance(value, str) and value:
                return True
            if isinstance(value, (list, dict)) and value:
                return True
    return False


class TurnBudget:
    """Task-wide generation budget, including child agents and compaction.

    Title generation is an auxiliary UI cost and is traced separately.
    Every admitted attempt counts, including failed attempts; this is conservative.
    """
    def __init__(self, limit=250):
        self.limit = limit
        self.used = 0
        self.exhausted = False
        self.lock = threading.Lock()

    def admit(self, kind, path):
        if not path.endswith('/chat/completions') or kind == 'title':
            return True
        with self.lock:
            if self.used >= self.limit:
                self.exhausted = True
                return False
            self.used += 1
            return True

    def snapshot(self):
        with self.lock:
            return dict(limit=self.limit, used=self.used, exhausted=self.exhausted)


class TraceServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, upstream, trace):
        super().__init__(address, handler)
        self.upstream = urlsplit(upstream)
        self.trace = trace
        self.trace_lock = threading.Lock()
        self.budget = TurnBudget()
        self.cache_salt = uuid.uuid4().hex
        self.budget_file_lock = threading.Lock()

    def log_event(self, event):
        with self.trace_lock:
            with self.trace.open("a") as output:
                output.write(json.dumps(event, ensure_ascii=False) + "\n")
                output.flush()


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        return

    def do_GET(self):
        self.forward()

    def do_POST(self):
        self.forward()

    def forward(self):
        request_id = uuid.uuid4().hex
        started_at = time.time()
        started_monotonic = time.monotonic()
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        payload = None
        if body:
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                pass
        if isinstance(payload, dict) and payload.get("stream"):
            stream_options = payload.get("stream_options")
            if not isinstance(stream_options, dict):
                stream_options = {}
                payload["stream_options"] = stream_options
            stream_options["include_usage"] = True
            payload["return_token_ids"] = True
            payload["cache_salt"] = self.server.cache_salt
            payload["temperature"] = 0
            payload["top_p"] = 1.0
            payload["seed"] = 20260913
            payload["max_tokens"] = min(payload.get("max_tokens") or 32768, 32768)
            body = json.dumps(payload, separators=(",", ":")).encode()
        kind = self.headers.get("x-pro-agent") or (request_kind(payload) if isinstance(payload, dict) else "http")
        allowed = self.server.budget.admit(kind, self.path)
        with self.server.budget_file_lock:
            self.server.trace.with_name('turn-budget.json').write_text(json.dumps(self.server.budget.snapshot()))
        if not allowed:
            self.server.log_event(dict(event='turn_budget_exhausted', at=time.time(), kind=kind,
                                       session_id=self.headers.get('x-pro-session'), budget=self.server.budget.snapshot()))
            message = json.dumps({'error': {'message': 'Task-wide 250 model-turn budget exhausted',
                                           'type': 'invalid_request_error', 'code': 'turn_limit'}}).encode()
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(message)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(message)
            self.close_connection = True
            return
        metadata = {
            "event": "request_start",
            "session_id": self.headers.get("x-pro-session"),
            "agent": self.headers.get("x-pro-agent"),
            "message_id": self.headers.get("x-pro-message"),
            "request_id": request_id,
            "kind": kind,
            "method": self.command,
            "path": self.path,
            "model": payload.get("model") if isinstance(payload, dict) else None,
            "stream": payload.get("stream") if isinstance(payload, dict) else None,
            "payload": payload,
            "message_count": len(payload.get("messages", [])) if isinstance(payload, dict) else 0,
            "tool_count": len(payload.get("tools", [])) if isinstance(payload, dict) else 0,
            "started_at": started_at,
            "started_monotonic": started_monotonic,
            "started_iso": timestamp(),
        }
        self.server.log_event(metadata)
        connection = None
        status = None
        usage = None
        first_token_at = None
        try:
            connection_type = (
                http.client.HTTPSConnection
                if self.server.upstream.scheme == "https"
                else http.client.HTTPConnection
            )
            connection = connection_type(
                self.server.upstream.hostname,
                self.server.upstream.port,
                timeout=1800,
            )
            headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower()
                not in {
                    "connection",
                    "content-length",
                    "host",
                    "proxy-connection",
                    "transfer-encoding",
                }
            }
            if body:
                headers["Content-Length"] = str(len(body))
            path = self.server.upstream.path.rstrip("/") + self.path
            connection.request(self.command, path, body=body or None, headers=headers)
            response = connection.getresponse()
            status = response.status
            self.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() in {"connection", "content-length", "transfer-encoding"}:
                    continue
                self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            content_type = response.getheader("Content-Type", "")
            if "text/event-stream" in content_type:
                for line in response:
                    received_at = time.time()
                    self.wfile.write(line)
                    self.wfile.flush()
                    stripped = line.decode(errors="replace").strip()
                    if not stripped.startswith("data:"):
                        continue
                    data = stripped[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    self.server.log_event({"event": "response_chunk", "request_id": request_id, "kind": kind, "received_at": received_at, "chunk": chunk})
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    if first_token_at is not None or not generated_delta(chunk):
                        continue
                    first_token_at = time.time()
                    self.server.log_event(
                        {
                            "event": "request_first_token",
                            "request_id": request_id,
                            "kind": kind,
                            "first_token_at": first_token_at,
                            "first_token_monotonic": time.monotonic(),
                            "first_token_iso": timestamp(),
                        }
                    )
            else:
                response_body = response.read()
                self.wfile.write(response_body)
                self.wfile.flush()
                try:
                    response_payload = json.loads(response_body)
                except json.JSONDecodeError:
                    response_payload = {}
                usage = response_payload.get("usage")
            self.close_connection = True
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as error:
            if status is None:
                try:
                    message = json.dumps({"error": str(error)}).encode()
                    self.send_response(502)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(message)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(message)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            self.server.log_event(
                {
                    "event": "request_error",
                    "request_id": request_id,
                    "kind": kind,
                    "error": repr(error),
                    "at": time.time(),
                    "at_iso": timestamp(),
                }
            )
        finally:
            ended_at = time.time()
            self.server.log_event(
                {
                    "event": "request_end",
                    "request_id": request_id,
                    "kind": kind,
                    "status": status,
                    "started_at": started_at,
                    "first_token_at": first_token_at,
                    "ended_at": ended_at,
                    "duration_seconds": round(ended_at - started_at, 6),
                    "time_to_first_token_seconds": (
                        round(first_token_at - started_at, 6)
                        if first_token_at is not None
                        else None
                    ),
                    "decode_wall_seconds": (
                        round(ended_at - first_token_at, 6)
                        if first_token_at is not None
                        else None
                    ),
                    "usage": usage,
                    "ended_iso": timestamp(),
                }
            )
            if connection is not None:
                connection.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=0)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--ready", type=Path, required=True)
    args = parser.parse_args()
    args.trace.parent.mkdir(parents=True, exist_ok=True)
    args.trace.write_text("")
    args.ready.unlink(missing_ok=True)
    server = TraceServer(
        (args.listen_host, args.listen_port),
        ProxyHandler,
        args.upstream,
        args.trace,
    )
    args.ready.write_text(
        json.dumps(
            {
                "host": args.listen_host,
                "port": server.server_address[1],
                "pid": os.getpid(),
            }
        )
        + "\n"
    )

    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
