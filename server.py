import json
import time
import uuid
import os
import ssl
import threading
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from http.cookies import SimpleCookie
from urllib.request import Request, urlopen
from urllib.error import HTTPError

BASE_DIR = Path(__file__).parent
KEYS_FILE = BASE_DIR / "keys.json"
CONFIG_FILE = BASE_DIR / "config.json"
TEMPLATE_FILE = BASE_DIR / "templates" / "admin.html"

BASE_URL = "https://api.on-demand.io/chat/v1"

MODEL_MAP = {
    "claude-4-8-opus": "predefined-claude-4-8-opus",
    "claude-4-6-opus": "predefined-claude-4-6-opus",
    "claude-sonnet-4-6": "predefined-claude-sonnet-4-6",
    "claude-haiku-4-5": "predefined-claude-haiku-4-5",
    "gpt-4.1": "predefined-openai-gpt4.1",
    "gpt-4.1-mini": "predefined-openai-gpt4.1-mini",
    "gpt-4.1-nano": "predefined-openai-gpt4.1-nano",
    "gpt-4o": "predefined-openai-gpt4o",
    "gpt-4o-mini": "predefined-openai-gpt4o-mini",
    "o3": "predefined-openai-o3",
    "o3-mini": "predefined-openai-o3-mini",
    "o4-mini": "predefined-openai-o4-mini",
    "glm-5.2": "predefined-glm-5.2",
    "deepseek-v3": "predefined-deepseek-v3",
    "deepseek-r1": "predefined-deepseek-r1",
    "deepseek-v4-pro": "predefined-deepseek-v4-pro",
    "gemini-2.5-pro": "predefined-gemini-2.5-pro",
    "gemini-2.5-flash": "predefined-gemini-2.5-flash",
}

_ssl_ctx = ssl.create_default_context()
_key_index = 0
_key_lock = threading.Lock()

def load_config():
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {"admin_password": os.environ.get("ADMIN_PASSWORD", "admin123")}

def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

def load_keys():
    if not KEYS_FILE.exists():
        return []
    return json.loads(KEYS_FILE.read_text())

def save_keys(keys):
    KEYS_FILE.write_text(json.dumps(keys, indent=2))

def get_next_key():
    global _key_index
    keys = load_keys()
    active = [k for k in keys if k.get("active", True)]
    if not active:
        return None
    with _key_lock:
        _key_index = _key_index % len(active)
        key = active[_key_index]
        _key_index += 1
    return key

def increment_key_usage(key_id):
    keys = load_keys()
    for k in keys:
        if k["id"] == key_id:
            k["usage"] = k.get("usage", 0) + 1
            break
    save_keys(keys)

def api_post(url, headers, body, timeout=120):
    data = json.dumps(body).encode()
    req = Request(url, data=data, headers=headers, method="POST")
    resp = urlopen(req, timeout=timeout, context=_ssl_ctx)
    return json.loads(resp.read().decode())

def api_post_stream(url, headers, body, timeout=120):
    data = json.dumps(body).encode()
    req = Request(url, data=data, headers=headers, method="POST")
    return urlopen(req, timeout=timeout, context=_ssl_ctx)

def create_session(api_key):
    return api_post(
        f"{BASE_URL}/sessions",
        {"apikey": api_key, "Content-Type": "application/json"},
        {"pluginIds": [], "externalUserId": "proxy"},
        timeout=30,
    )["data"]["id"]

def build_query(messages):
    parts = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = "\n".join(c["text"] for c in content if c.get("type") == "text")
        if role == "system":
            parts.append(f"[System]: {content}")
        elif role == "assistant":
            parts.append(f"[Assistant]: {content}")
        else:
            parts.append(content)
    return "\n\n".join(parts)

def resolve_endpoint(model):
    if model in MODEL_MAP:
        return MODEL_MAP[model]
    if model.startswith("predefined-"):
        return model
    return f"predefined-{model}"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(fmt % args)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def _json_body(self):
        return json.loads(self._read_body().decode())

    def _send_json(self, obj, status=200, cookie=None):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status, detail):
        self._send_json({"error": {"message": detail, "type": "error"}}, status)

    def _send_html(self, html, status=200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _get_cookie(self, name):
        raw = self.headers.get("Cookie", "")
        c = SimpleCookie(raw)
        if name in c:
            return c[name].value
        return ""

    def _check_admin(self):
        pw = load_config().get("admin_password", "admin123")
        auth = self.headers.get("Authorization", "")
        if auth == f"Bearer {pw}":
            return True
        if self._get_cookie("admin_password") == pw:
            return True
        self._send_error(401, "Unauthorized")
        return False

    def do_GET(self):
        if self.path == "/admin":
            self._send_html(TEMPLATE_FILE.read_text(encoding="utf-8"))
        elif self.path == "/admin/api/keys":
            if not self._check_admin():
                return
            self._send_json(load_keys())
        elif self.path == "/v1/models":
            models = [{"id": n, "object": "model", "created": 1700000000, "owned_by": "ondemand-proxy"} for n in MODEL_MAP]
            self._send_json({"object": "list", "data": models})
        else:
            self._send_error(404, "Not found")

    def do_POST(self):
        if self.path == "/v1/chat/completions":
            self._handle_chat()
        elif self.path == "/admin/api/login":
            self._handle_login()
        elif self.path == "/admin/api/password":
            if self._check_admin():
                self._handle_password()
        elif self.path == "/admin/api/keys":
            if self._check_admin():
                self._handle_add_keys()
        else:
            self._send_error(404, "Not found")

    def do_DELETE(self):
        if self.path.startswith("/admin/api/keys/"):
            if not self._check_admin():
                return
            key_id = self.path.split("/")[-1]
            keys = [k for k in load_keys() if k["id"] != key_id]
            save_keys(keys)
            self._send_json({"ok": True})
        else:
            self._send_error(404, "Not found")

    def do_PATCH(self):
        if self.path.startswith("/admin/api/keys/"):
            if not self._check_admin():
                return
            key_id = self.path.split("/")[-1]
            body = self._json_body()
            keys = load_keys()
            for k in keys:
                if k["id"] == key_id:
                    k["active"] = body.get("active", k["active"])
                    break
            save_keys(keys)
            self._send_json({"ok": True})
        else:
            self._send_error(404, "Not found")

    def _handle_login(self):
        body = self._json_body()
        pw = load_config().get("admin_password", "admin123")
        if body.get("password") != pw:
            self._send_error(401, "Wrong password")
            return
        self._send_json({"ok": True}, cookie=f"admin_password={pw}; HttpOnly; Max-Age=604800; Path=/")

    def _handle_password(self):
        body = self._json_body()
        new_pw = body.get("password", "").strip()
        if len(new_pw) < 4:
            self._send_error(400, "Password too short")
            return
        cfg = load_config()
        cfg["admin_password"] = new_pw
        save_config(cfg)
        self._send_json({"ok": True}, cookie=f"admin_password={new_pw}; HttpOnly; Max-Age=604800; Path=/")

    def _handle_add_keys(self):
        try:
            body = self._json_body()
            raw_keys = body.get("keys", [])
            keys = load_keys()
            existing = {k["key"] for k in keys}
            added = 0
            for k in raw_keys:
                k = k.strip()
                if k and k not in existing:
                    keys.append({"id": uuid.uuid4().hex[:12], "key": k, "active": True, "usage": 0})
                    existing.add(k)
                    added += 1
            save_keys(keys)
            self._send_json({"added": added, "total": len(keys)})
        except Exception as e:
            print(f"Error adding keys: {e}")
            self._send_error(500, str(e))

    def _handle_chat(self):
        body = self._json_body()
        model = body.get("model", "claude-4-6-opus")
        messages = body.get("messages", [])
        stream = body.get("stream", False)

        endpoint_id = resolve_endpoint(model)
        query = build_query(messages)
        key_info = get_next_key()
        if not key_info:
            self._send_error(500, "No active API keys")
            return
        api_key = key_info["key"]

        try:
            session_id = create_session(api_key)
        except Exception as e:
            self._send_error(502, f"Failed to create session: {e}")
            return

        chat_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())
        headers = {"apikey": api_key, "Content-Type": "application/json"}
        payload = {"query": query, "endpointId": endpoint_id}

        if stream:
            payload["responseMode"] = "stream"
            self._do_stream(headers, session_id, payload, model, chat_id, created, key_info["id"])
        else:
            payload["responseMode"] = "sync"
            try:
                result = api_post(f"{BASE_URL}/sessions/{session_id}/query", headers, payload)
                data = result["data"]
                answer = data.get("answer", "")
                metrics = data.get("metrics", {})
                increment_key_usage(key_info["id"])
                self._send_json({
                    "id": chat_id, "object": "chat.completion", "created": created, "model": model,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": metrics.get("inputTokens", 0), "completion_tokens": metrics.get("outputTokens", 0), "total_tokens": metrics.get("totalTokens", 0)},
                })
            except Exception as e:
                self._send_error(502, f"Query failed: {e}")

    def _do_stream(self, headers, session_id, payload, model, chat_id, created, key_id):
        try:
            resp = api_post_stream(f"{BASE_URL}/sessions/{session_id}/query", headers, payload)
        except Exception as e:
            self._send_error(502, f"Stream failed: {e}")
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        try:
            buf = b""
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line_bytes, buf = buf.split(b"\n", 1)
                    line = line_bytes.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        chunk_data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if chunk_data.get("eventType") != "fulfillment":
                        continue
                    answer_piece = chunk_data.get("answer", "")
                    if not answer_piece:
                        continue
                    openai_chunk = {
                        "id": chat_id, "object": "chat.completion.chunk", "created": created, "model": model,
                        "choices": [{"index": 0, "delta": {"content": answer_piece}, "finish_reason": None}],
                    }
                    self.wfile.write(f"data: {json.dumps(openai_chunk)}\n\n".encode())
                    self.wfile.flush()

            stop = {"id": chat_id, "object": "chat.completion.chunk", "created": created, "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            self.wfile.write(f"data: {json.dumps(stop)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            increment_key_usage(key_id)
        except Exception:
            pass
        finally:
            resp.close()


class ThreadedHTTPServer(HTTPServer):
    def process_request(self, request, client_address):
        t = threading.Thread(target=self._handle, args=(request, client_address))
        t.daemon = True
        t.start()

    def _handle(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8181"))
    server = ThreadedHTTPServer(("0.0.0.0", port), Handler)
    print(f"OnDemand Proxy running on http://0.0.0.0:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
