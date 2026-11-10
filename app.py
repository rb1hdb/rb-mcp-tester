import json
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from agent import MCPAgent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("rbt.app")

BASE_DIR = Path(__file__).resolve().parent
CONFIGS_DIR = BASE_DIR / "configs"

app = FastAPI(title="RB MCP Tester", version="1.0")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

agent = MCPAgent()
_current_config: str | None = None


def _expand(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(value))


def _expand_cfg(cfg: dict) -> dict:
    """Expand ~ and $VAR in string values of a config dict."""
    out = dict(cfg)
    for key in ("command", "cwd", "url"):
        if key in out and isinstance(out[key], str):
            out[key] = _expand(out[key])
    if "args" in out:
        out["args"] = [_expand(a) if isinstance(a, str) else a for a in out["args"]]
    return out


@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(BASE_DIR / "templates" / "index.html")


@app.get("/api/configs")
async def list_configs():
    configs = []
    if CONFIGS_DIR.exists():
        for f in sorted(CONFIGS_DIR.glob("*.json")):
            try:
                data = json.loads(f.read_text())
                configs.append({
                    "file": f.name,
                    "name": data.get("name", f.stem),
                    "description": data.get("description", ""),
                    "transport": data.get("transport", "?"),
                })
            except Exception:
                pass
    return configs


@app.post("/api/connect")
async def connect(request: Request):
    global _current_config

    body = await request.json()
    config_name = body.get("config", "").strip()
    if not config_name:
        return {"error": "config is required"}

    config_path = CONFIGS_DIR / config_name
    if not config_path.exists():
        return {"error": f"Config not found: {config_name}"}

    try:
        cfg = _expand_cfg(json.loads(config_path.read_text()))
    except Exception as e:
        return {"error": f"Invalid config: {e}"}

    await agent.disconnect()
    transport = cfg.get("transport", "sse")

    if transport == "sse":
        url = cfg.get("url")
        if not url:
            return {"error": "Missing 'url' in config"}
        try:
            info = await agent.connect_sse(url)
        except Exception as e:
            return {"error": f"SSE connection failed: {e}"}
        conn_info = {"transport": "sse", "url": url}

    elif transport == "stdio":
        command = cfg.get("command")
        args = cfg.get("args", [])
        cwd = cfg.get("cwd") or None
        env = cfg.get("env") or None
        if not command:
            return {"error": "Missing 'command' in config"}
        try:
            info = await agent.connect_stdio(command, args, cwd=cwd, env=env)
        except Exception as e:
            return {"error": f"stdio connection failed: {e}"}
        conn_info = {"transport": "stdio", "command": f"{command} {' '.join(args)}"}

    elif transport == "streamable_http":
        url = cfg.get("url")
        if not url:
            return {"error": "Missing 'url' in config"}
        try:
            info = await agent.connect_streamable_http(url)
        except Exception as e:
            return {"error": f"Streamable HTTP connection failed: {e}"}
        conn_info = {"transport": "streamable_http", "url": url}

    else:
        return {"error": f"Unknown transport: {transport}"}

    _current_config = config_name
    return {"status": "connected", "config": config_name, **conn_info, **info}


@app.get("/api/chat")
async def chat(prompt: str = "", think: str | None = None):
    if not prompt:
        return StreamingResponse(
            iter([f"data: {json.dumps({'event': 'error', 'data': 'Empty prompt'})}\n\n"]),
            media_type="text/event-stream",
        )

    async def event_stream():
        # think=1/0 vem da UI; ausente = padrão do servidor (OLLAMA_THINK)
        think_flag = None if think is None else think.strip().lower() in ("1", "true", "on", "yes")
        async for evt in agent.chat(prompt, think=think_flag):
            yield f"event: {evt['event']}\ndata: {json.dumps(evt['data'])}\n\n"
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/read_resource")
async def read_resource_route(request: Request):
    body = await request.json()
    uri = body.get("uri", "").strip()
    if not uri:
        return {"error": "URI obrigatório"}
    if not agent._session:
        return {"error": "Sem conexão MCP ativa"}
    content = await agent._read_resource(uri)
    return {"uri": uri, "content": content}


@app.post("/api/disconnect")
async def disconnect():
    global _current_config
    await agent.disconnect()
    _current_config = None
    return {"status": "disconnected"}


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "connected": agent._session is not None,
        "config": _current_config,
        "model": agent.MODEL,
    }


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("RBT_PORT", os.environ.get("PANDORA_PORT", "55009")))
    # Vigiar só o código-fonte: sem reload_dirs o uvicorn varre o venv/ inteiro (CPU alta).
    uvicorn.run(
        "app:app", host="0.0.0.0", port=port, reload=True,
        reload_dirs=[str(BASE_DIR)],
        reload_includes=["*.py"],
        reload_excludes=["venv/*", ".git/*", "__pycache__/*"],
    )
