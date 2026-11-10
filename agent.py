import asyncio
import json
import logging
import os
import re
import time
from contextlib import AsyncExitStack
from typing import Any, AsyncGenerator

import ollama
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

log = logging.getLogger("pandora.agent")

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
SYSTEM_PROMPT_BASE = "You are an MCP client. Use the available tools and resources to answer the user."
MAX_TOOL_ROUNDS = 15
# Raciocínio (<think>) do qwen3 desligado por padrão: medido ~51 s -> ~4 s por turno.
# OLLAMA_THINK=1 liga por padrão; a UI também pode ligar por mensagem.
OLLAMA_THINK = os.environ.get("OLLAMA_THINK", "0").strip().lower() in ("1", "true", "yes", "on")
# keep_alive: por padrão NÃO é enviado, então vale o do servidor Ollama (no AI-SRV, 1 h).
# Só sobrescreve se OLLAMA_KEEP_ALIVE estiver definido (ex.: "10m"); None = omitido.
OLLAMA_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE") or None


def _round_timing(response, wall_s: float) -> dict:
    """Extract Ollama's per-call metrics (durations come in ns, may be None)."""
    def s(v):
        return round((v or 0) / 1e9, 2)
    eval_s = (getattr(response, "eval_duration", 0) or 0) / 1e9
    eval_n = getattr(response, "eval_count", 0) or 0
    return {
        "phase": "llm",
        "wall_s": round(wall_s, 2),
        "load_s": s(getattr(response, "load_duration", 0)),
        "prompt_tokens": getattr(response, "prompt_eval_count", 0) or 0,
        "prompt_s": s(getattr(response, "prompt_eval_duration", 0)),
        "gen_tokens": eval_n,
        "gen_s": round(eval_s, 2),
        "tok_per_s": round(eval_n / eval_s, 1) if eval_s else 0,
    }


def _strip_think(text: str) -> tuple[str, str]:
    """Return (clean_text, thinking) splitting <think> blocks from qwen3."""
    thinking_parts = []
    def _collect(m):
        thinking_parts.append(m.group(1).strip())
        return ""
    clean = re.sub(r"<think>(.*?)</think>", _collect, text, flags=re.DOTALL).strip()
    return clean, "\n".join(thinking_parts)


def _mcp_tools_to_ollama(mcp_tools: list) -> list[dict]:
    """Convert MCP tool schemas to Ollama/OpenAI function-calling format."""
    result = []
    for t in mcp_tools:
        schema = t.input_schema or {"type": "object", "properties": {}}
        result.append({
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or "",
                "parameters": schema,
            },
        })
    return result


class MCPAgent:
    MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:14b")

    def __init__(self):
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._tools_raw: list = []
        self._resources_raw: list = []
        self._templates_raw: list = []
        self._prompts_raw: list = []
        self._ollama_tools: list[dict] = []
        self._history: list[dict] = []
        self._system_prompt: str = SYSTEM_PROMPT_BASE
        self._client = ollama.AsyncClient(host=OLLAMA_HOST)
        self._warmup_task: asyncio.Task | None = None

    async def connect_sse(self, sse_url: str) -> dict:
        """Connect to an MCP server via SSE. Returns {tools, resources}."""
        await self.disconnect()
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()

        streams = await self._stack.enter_async_context(
            sse_client(sse_url, timeout=10)
        )
        read_stream, write_stream = streams
        self._session = await self._stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await self._session.initialize()
        return await self._post_connect()

    async def connect_stdio(self, command: str, args: list[str],
                            cwd: str | None = None,
                            env: dict[str, str] | None = None) -> dict:
        """Connect to an MCP server via stdio. Returns {tools, resources}."""
        await self.disconnect()
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()

        params = StdioServerParameters(
            command=command, args=args, cwd=cwd, env=env,
        )
        streams = await self._stack.enter_async_context(
            stdio_client(params)
        )
        read_stream, write_stream = streams
        self._session = await self._stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await self._session.initialize()
        return await self._post_connect()

    async def connect_streamable_http(self, url: str) -> dict:
        """Connect to an MCP server via Streamable HTTP. Returns {tools, resources}."""
        await self.disconnect()
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()

        streams = await self._stack.enter_async_context(
            streamable_http_client(url)
        )
        read_stream, write_stream = streams
        self._session = await self._stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await self._session.initialize()
        return await self._post_connect()

    async def _post_connect(self) -> dict:
        tools_result = await self._session.list_tools()
        self._tools_raw = tools_result.tools

        self._resources_raw = []
        try:
            res_result = await self._session.list_resources()
            self._resources_raw = res_result.resources
        except Exception:
            pass

        self._templates_raw = []
        try:
            tmpl_result = await self._session.list_resource_templates()
            self._templates_raw = tmpl_result.resource_templates
        except Exception:
            pass

        self._prompts_raw = []
        try:
            prompt_result = await self._session.list_prompts()
            self._prompts_raw = prompt_result.prompts
        except Exception:
            pass

        self._ollama_tools = _mcp_tools_to_ollama(self._tools_raw)
        self._append_read_resource_tool()
        self._build_system_prompt()
        self._history = []
        self._start_warmup()
        return self._describe()

    def _start_warmup(self):
        """Carrega o modelo na VRAM em segundo plano, sem atrasar o /api/connect."""
        if self._warmup_task and not self._warmup_task.done():
            return
        self._warmup_task = asyncio.create_task(self._warmup())

    async def _warmup(self):
        t0 = time.perf_counter()
        try:
            await self._client.chat(
                model=self.MODEL,
                messages=[{"role": "system", "content": self._system_prompt},
                          {"role": "user", "content": "ok"}],
                think=False,
                options={"num_predict": 1},
                keep_alive=OLLAMA_KEEP_ALIVE,
            )
            log.info("warmup %s ok em %.1fs (keep_alive=%s)", self.MODEL, time.perf_counter() - t0, OLLAMA_KEEP_ALIVE)
        except Exception as e:
            log.warning("warmup falhou: %s", e)

    def _append_read_resource_tool(self):
        """Add a virtual read_resource tool so the LLM can access MCP resources."""
        all_uris = [str(r.uri) for r in self._resources_raw]
        all_templates = [str(t.uri_template) for t in self._templates_raw]
        if not (all_uris or all_templates):
            return
        hint = ""
        if all_uris:
            hint += f"Available URIs: {', '.join(all_uris)}. "
        if all_templates:
            hint += f"URI templates: {', '.join(all_templates)}."
        self._ollama_tools.append({
            "type": "function",
            "function": {
                "name": "read_resource",
                "description": f"Read the contents of an MCP resource by URI. {hint}",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "uri": {
                            "type": "string",
                            "description": "Exact resource URI to read",
                        }
                    },
                    "required": ["uri"],
                },
            },
        })

    def _build_system_prompt(self):
        self._system_prompt = SYSTEM_PROMPT_BASE

    async def disconnect(self):
        if self._stack:
            try:
                await self._stack.aclose()
            except Exception:
                pass
            self._stack = None
            self._session = None
        self._tools_raw = []
        self._resources_raw = []
        self._templates_raw = []
        self._prompts_raw = []
        self._ollama_tools = []
        self._history = []

    def _describe(self) -> dict:
        tools = []
        for t in self._tools_raw:
            tools.append({
                "name": t.name,
                "description": t.description or "",
                "inputSchema": t.input_schema,
            })
        resources = []
        for r in self._resources_raw:
            resources.append({
                "uri": str(r.uri),
                "name": r.name or "",
                "description": r.description or "",
            })
        templates = []
        for t in self._templates_raw:
            templates.append({
                "uriTemplate": str(t.uri_template),
                "name": t.name or "",
                "description": t.description or "",
            })
        prompts = []
        for p in self._prompts_raw:
            prompts.append({
                "name": p.name,
                "description": p.description or "",
            })
        return {"tools": tools, "resources": resources, "templates": templates, "prompts": prompts}

    async def chat(self, user_prompt: str, think: bool | None = None) -> AsyncGenerator[dict, None]:
        """Run one turn of the agent loop, yielding SSE events.

        think: liga/desliga o raciocínio nesta mensagem (None = padrão OLLAMA_THINK).
        """
        use_think = OLLAMA_THINK if think is None else think
        turn_t0 = time.perf_counter()
        if not self._session:
            yield {"event": "error", "data": "Not connected to MCP server"}
            return

        try:
            tools_result = await self._session.list_tools()
            self._tools_raw = tools_result.tools
            self._ollama_tools = _mcp_tools_to_ollama(self._tools_raw)
            self._append_read_resource_tool()
        except Exception:
            pass

        # Sem histórico entre turnos: cada mensagem vai sozinha para a inferência.
        # Só o laço de tools do turno atual (chamada + resultado) acumula em `messages`.
        self._history = []
        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        for round_no in range(1, MAX_TOOL_ROUNDS + 1):
            # Sinal imediato: a UI mostra "pensando…" antes de qualquer token.
            yield {"event": "status", "data": {"phase": "llm_start", "round": round_no, "think": use_think}}

            t0 = time.perf_counter()
            content = ""
            tool_calls = []
            last = None
            first_token_s = None
            try:
                stream = await self._client.chat(
                    model=self.MODEL,
                    messages=messages,
                    tools=self._ollama_tools if self._ollama_tools else None,
                    think=use_think,
                    stream=True,
                    keep_alive=OLLAMA_KEEP_ALIVE,
                )
                async for chunk in stream:
                    last = chunk
                    m = chunk.message
                    if first_token_s is None and (m.content or m.thinking or m.tool_calls):
                        first_token_s = round(time.perf_counter() - t0, 2)
                    if m.thinking:
                        yield {"event": "thinking_delta", "data": m.thinking}
                    if m.content:
                        content += m.content
                        yield {"event": "response_delta", "data": m.content}
                    for tc in (m.tool_calls or []):
                        tool_calls.append(tc)
                        # app.py serializa "data" com json.dumps; aqui vai o objeto cru
                        # (json.dumps duplo faz o JSON.parse do cliente devolver string).
                        yield {
                            "event": "tool_call",
                            "data": {"name": tc.function.name,
                                     "arguments": dict(tc.function.arguments or {})},
                        }
            except Exception as e:
                yield {"event": "error", "data": f"Ollama error: {e}"}
                return

            timing = _round_timing(last, time.perf_counter() - t0)
            timing["first_token_s"] = first_token_s
            timing["round"] = round_no
            timing["think"] = use_think
            log.info("timing %s", timing)
            yield {"event": "timing", "data": timing}

            # Se o modelo embutiu <think> no texto, separa o raciocínio e corrige a bolha.
            clean, extra_think = _strip_think(content)
            if extra_think:
                yield {"event": "thinking", "data": extra_think}
            if clean != content.strip():
                yield {"event": "response_fix", "data": clean}

            if not tool_calls:
                break

            tool_results_for_history = []
            for tc in tool_calls:
                fn_name = tc.function.name
                fn_args = tc.function.arguments or {}

                t_tool = time.perf_counter()
                result_text = await self._execute_tool(fn_name, fn_args)
                tool_s = round(time.perf_counter() - t_tool, 2)

                yield {
                    "event": "tool_result",
                    "data": {"name": fn_name, "result": result_text},
                }
                tool_timing = {"phase": "tool", "name": fn_name, "wall_s": tool_s, "round": round_no}
                log.info("timing %s", tool_timing)
                yield {"event": "timing", "data": tool_timing}

                tool_results_for_history.append({
                    "role": "tool",
                    "content": result_text,
                    "tool_name": fn_name,
                })

            messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
            messages.extend(tool_results_for_history)

        total = {"phase": "turn", "wall_s": round(time.perf_counter() - turn_t0, 2), "think": use_think}
        log.info("timing %s", total)
        yield {"event": "timing", "data": total}

    async def _execute_tool(self, name: str, args: dict) -> str:
        if name == "read_resource" and "uri" in args:
            return await self._read_resource(args["uri"])
        try:
            result = await self._session.call_tool(name, args)
            parts = []
            for c in result.content:
                if hasattr(c, "text"):
                    parts.append(c.text)
                else:
                    parts.append(str(c))
            return "\n".join(parts) if parts else "(empty result)"
        except Exception as e:
            return f"Error calling {name}: {e}"

    async def _read_resource(self, uri: str) -> str:
        try:
            result = await self._session.read_resource(uri)
            parts = []
            for c in result.contents:
                if hasattr(c, "text"):
                    parts.append(c.text)
                else:
                    parts.append(str(c))
            return "\n".join(parts) if parts else "(empty resource)"
        except Exception as e:
            return f"Error reading resource {uri}: {e}"
