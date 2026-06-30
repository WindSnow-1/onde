import json
import time
import uuid
import os
import asyncio
from pathlib import Path
from itertools import cycle

import httpx
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

app = FastAPI()

KEYS_FILE = Path(__file__).parent / "keys.json"
CONFIG_FILE = Path(__file__).parent / "config.json"

def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {"admin_password": os.environ.get("ADMIN_PASSWORD", "admin123")}

def save_config(cfg: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

def get_admin_password() -> str:
    return load_config().get("admin_password", "admin123")
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

# --- Key management ---

def load_keys() -> list[dict]:
    if not KEYS_FILE.exists():
        return []
    with open(KEYS_FILE, "r") as f:
        return json.load(f)

def save_keys(keys: list[dict]):
    with open(KEYS_FILE, "w") as f:
        json.dump(keys, f, indent=2)

_key_cycle = None
_key_cycle_keys = None

def get_next_key() -> dict:
    global _key_cycle, _key_cycle_keys
    keys = load_keys()
    active = [k for k in keys if k.get("active", True)]
    if not active:
        raise HTTPException(status_code=500, detail="No active API keys available")
    key_ids = [k["id"] for k in active]
    if _key_cycle_keys != key_ids:
        _key_cycle_keys = key_ids
        _key_cycle = cycle(active)
    return next(_key_cycle)

def increment_key_usage(key_id: str):
    keys = load_keys()
    for k in keys:
        if k["id"] == key_id:
            k["usage"] = k.get("usage", 0) + 1
            break
    save_keys(keys)

# --- Auth ---

def verify_admin(request: Request):
    pw = get_admin_password()
    auth = request.headers.get("Authorization", "")
    if auth == f"Bearer {pw}":
        return True
    cookie_pw = request.cookies.get("admin_password", "")
    if cookie_pw == pw:
        return True
    raise HTTPException(status_code=401, detail="Unauthorized")

# --- on-demand.io helpers ---

async def create_session(api_key: str) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{BASE_URL}/sessions",
            headers={"apikey": api_key, "Content-Type": "application/json"},
            json={"pluginIds": [], "externalUserId": "proxy"},
        )
        resp.raise_for_status()
        return resp.json()["data"]["id"]

def build_query_from_messages(messages: list[dict]) -> str:
    parts = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            text_parts = [c["text"] for c in content if c.get("type") == "text"]
            content = "\n".join(text_parts)
        if role == "system":
            parts.append(f"[System]: {content}")
        elif role == "assistant":
            parts.append(f"[Assistant]: {content}")
        else:
            parts.append(content)
    return "\n\n".join(parts)

async def query_sync(api_key: str, session_id: str, query: str, endpoint_id: str) -> dict:
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{BASE_URL}/sessions/{session_id}/query",
            headers={"apikey": api_key, "Content-Type": "application/json"},
            json={"query": query, "endpointId": endpoint_id, "responseMode": "sync"},
        )
        resp.raise_for_status()
        return resp.json()

async def query_stream(api_key: str, session_id: str, query: str, endpoint_id: str):
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream(
            "POST",
            f"{BASE_URL}/sessions/{session_id}/query",
            headers={"apikey": api_key, "Content-Type": "application/json"},
            json={"query": query, "endpointId": endpoint_id, "responseMode": "stream"},
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                yield line

# --- OpenAI compatible endpoints ---

def resolve_endpoint_id(model: str) -> str:
    if model in MODEL_MAP:
        return MODEL_MAP[model]
    if model.startswith("predefined-"):
        return model
    return f"predefined-{model}"

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    model = body.get("model", "claude-4-6-opus")
    messages = body.get("messages", [])
    stream = body.get("stream", False)

    endpoint_id = resolve_endpoint_id(model)
    query = build_query_from_messages(messages)
    key_info = get_next_key()
    api_key = key_info["key"]

    try:
        session_id = await create_session(api_key)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to create session: {e}")

    chat_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    if stream:
        return StreamingResponse(
            _stream_response(api_key, session_id, query, endpoint_id, model, chat_id, created, key_info["id"]),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        result = await query_sync(api_key, session_id, query, endpoint_id)
        data = result["data"]
        answer = data.get("answer", "")
        metrics = data.get("metrics", {})
        increment_key_usage(key_info["id"])
        return {
            "id": chat_id,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": answer},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": metrics.get("inputTokens", 0),
                "completion_tokens": metrics.get("outputTokens", 0),
                "total_tokens": metrics.get("totalTokens", 0),
            },
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Query failed: {e}")

async def _stream_response(api_key, session_id, query, endpoint_id, model, chat_id, created, key_id):
    try:
        async for line in query_stream(api_key, session_id, query, endpoint_id):
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
                "id": chat_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": {"content": answer_piece},
                    "finish_reason": None,
                }],
            }
            yield f"data: {json.dumps(openai_chunk)}\n\n"

        stop_chunk = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": "stop",
            }],
        }
        yield f"data: {json.dumps(stop_chunk)}\n\n"
        yield "data: [DONE]\n\n"
        increment_key_usage(key_id)
    except Exception as e:
        error_chunk = {"error": {"message": str(e), "type": "server_error"}}
        yield f"data: {json.dumps(error_chunk)}\n\n"
        yield "data: [DONE]\n\n"

@app.get("/v1/models")
async def list_models():
    models = []
    for name in MODEL_MAP:
        models.append({
            "id": name,
            "object": "model",
            "created": 1700000000,
            "owned_by": "ondemand-proxy",
        })
    return {"object": "list", "data": models}

# --- Admin API ---

@app.post("/admin/api/login")
async def admin_login(request: Request):
    body = await request.json()
    pw = get_admin_password()
    if body.get("password") != pw:
        raise HTTPException(status_code=401, detail="Wrong password")
    resp = JSONResponse({"ok": True})
    resp.set_cookie("admin_password", pw, httponly=True, max_age=86400 * 7)
    return resp

@app.post("/admin/api/password", dependencies=[Depends(verify_admin)])
async def change_password(request: Request):
    body = await request.json()
    new_pw = body.get("password", "").strip()
    if len(new_pw) < 4:
        raise HTTPException(status_code=400, detail="Password too short")
    cfg = load_config()
    cfg["admin_password"] = new_pw
    save_config(cfg)
    resp = JSONResponse({"ok": True})
    resp.set_cookie("admin_password", new_pw, httponly=True, max_age=86400 * 7)
    return resp

@app.get("/admin/api/keys", dependencies=[Depends(verify_admin)])
async def get_keys():
    return load_keys()

@app.post("/admin/api/keys", dependencies=[Depends(verify_admin)])
async def add_keys(request: Request):
    body = await request.json()
    raw_keys = body.get("keys", [])
    keys = load_keys()
    added = 0
    existing = {k["key"] for k in keys}
    for k in raw_keys:
        k = k.strip()
        if k and k not in existing:
            keys.append({"id": uuid.uuid4().hex[:12], "key": k, "active": True, "usage": 0})
            existing.add(k)
            added += 1
    save_keys(keys)
    global _key_cycle_keys
    _key_cycle_keys = None
    return {"added": added, "total": len(keys)}

@app.delete("/admin/api/keys/{key_id}", dependencies=[Depends(verify_admin)])
async def delete_key(key_id: str):
    keys = load_keys()
    keys = [k for k in keys if k["id"] != key_id]
    save_keys(keys)
    global _key_cycle_keys
    _key_cycle_keys = None
    return {"ok": True}

@app.patch("/admin/api/keys/{key_id}", dependencies=[Depends(verify_admin)])
async def toggle_key(key_id: str, request: Request):
    body = await request.json()
    keys = load_keys()
    for k in keys:
        if k["id"] == key_id:
            k["active"] = body.get("active", k["active"])
            break
    save_keys(keys)
    global _key_cycle_keys
    _key_cycle_keys = None
    return {"ok": True}

# --- Admin frontend ---

@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    html_path = Path(__file__).parent / "templates" / "admin.html"
    return html_path.read_text(encoding="utf-8")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8181"))
    uvicorn.run(app, host="0.0.0.0", port=port)
