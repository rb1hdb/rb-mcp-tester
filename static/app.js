const $ = (s) => document.querySelector(s);

let configData = [];
let currentConfig = null;
let eventSource = null;

// ── Init ──

async function init() {
    const res = await fetch("/api/configs");
    configData = await res.json();
    renderConfigSelect(configData);
}

function renderConfigSelect(configs) {
    const sel = $("#config-select");
    sel.innerHTML = '<option value="">— selecione —</option>';
    for (const c of configs) {
        const opt = document.createElement("option");
        opt.value = c.file;
        opt.textContent = c.name;
        opt.dataset.desc = c.description || "";
        opt.dataset.transport = c.transport || "";
        sel.appendChild(opt);
    }
}

$("#config-select").addEventListener("change", (e) => {
    const val = e.target.value;
    currentConfig = val || null;
    const opt = e.target.selectedOptions[0];
    const desc = opt ? (opt.dataset.desc || "") : "";
    const transport = opt ? (opt.dataset.transport || "") : "";
    const descEl = $("#config-description");
    if (val) {
        descEl.textContent = (transport ? `[${transport}] ` : "") + desc;
    } else {
        descEl.textContent = "";
    }
    updateButtons();
});

function updateButtons() {
    $("#btn-connect").disabled = !currentConfig;
}

// ── Connect / Disconnect ──

$("#btn-connect").addEventListener("click", async () => {
    if (!currentConfig) return;
    setConnecting();
    clearChat();
    clearLog();
    clearInspector();
    addLog("info", `Conectando a ${currentConfig} …`);

    try {
        const res = await fetch("/api/connect", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ config: currentConfig }),
        });
        const data = await res.json();

        if (data.error) {
            addLog("error", data.error);
            setDisconnected();
            return;
        }

        const urlOrCmd = data.url || data.command || "";
        addLog("ok", `Conectado — ${data.transport} · ${urlOrCmd}`);
        populateInspector(data);
        setConnected();

        if (data.tools && data.tools.length > 0) {
            addLog("info", `📋 Tools (${data.tools.length}):`);
            for (const t of data.tools) {
                addLog("tool", `  ${t.name}`, true);
            }
        }
        if (data.resources && data.resources.length > 0) {
            addLog("info", `📋 Resources (${data.resources.length}):`);
            for (const r of data.resources) {
                addLog("resource", `  ${r.uri}${r.description ? " — " + r.description : ""}`);
            }
        }
    } catch (err) {
        addLog("error", `Connection failed: ${err.message}`);
        setDisconnected();
    }
});

$("#btn-disconnect").addEventListener("click", async () => {
    if (eventSource) { eventSource.close(); eventSource = null; }
    try {
        await fetch("/api/disconnect", { method: "POST" });
    } catch {}
    addLog("info", "Desconectado.");
    setDisconnected();
});

function setConnecting() {
    $("#status-mcp").textContent = "⟳ Conectando…";
    $("#status-mcp").className = "status-indicator off";
    $("#btn-connect").disabled = true;
    $("#btn-disconnect").disabled = true;
    $("#chat-input").disabled = true;
    $("#chat-send").disabled = true;
}

function setConnected() {
    $("#status-mcp").textContent = "● Conectado";
    $("#status-mcp").className = "status-indicator on";
    $("#btn-connect").disabled = true;
    $("#btn-disconnect").disabled = false;
    $("#chat-input").disabled = false;
    $("#chat-send").disabled = false;
    $("#chat-input").focus();
}

function setDisconnected() {
    $("#status-mcp").textContent = "○ Desconectado";
    $("#status-mcp").className = "status-indicator off";
    updateButtons();
    $("#btn-disconnect").disabled = true;
    $("#chat-input").disabled = true;
    $("#chat-send").disabled = true;
}

// ── Chat ──

function runChat(prompt, showUserMsg = true) {
    const input = $("#chat-input");
    if (showUserMsg) addChatMsg("user", prompt);
    input.disabled = true;
    $("#chat-send").disabled = true;

    if (eventSource) { eventSource.close(); }

    const think = $("#chk-think").checked ? 1 : 0;
    const url = `/api/chat?prompt=${encodeURIComponent(prompt)}&think=${think}`;
    eventSource = new EventSource(url);

    resetLive();

    // Sinal imediato do servidor: aparece antes de qualquer token do modelo.
    eventSource.addEventListener("status", (e) => {
        const s = JSON.parse(e.data);
        if (s.phase !== "llm_start") return;
        showStatus(s.round > 1 ? "⏳ gerando resposta…" : (s.think ? "🧠 pensando…" : "⏳ processando…"));
    });

    // Raciocínio chegando token a token: uma linha viva no log.
    eventSource.addEventListener("thinking_delta", (e) => {
        hideStatus();
        if (!liveThink) liveThink = addLog("thinking", "🧠 ");
        liveThink.appendChild(document.createTextNode(JSON.parse(e.data)));
        $("#log-messages").scrollTop = $("#log-messages").scrollHeight;
    });

    // Raciocínio completo (só quando veio embutido como <think> no texto).
    eventSource.addEventListener("thinking", (e) => {
        addLog("thinking", `🧠 ${JSON.parse(e.data)}`);
    });

    // Resposta chegando token a token: uma bolha viva no chat.
    eventSource.addEventListener("response_delta", (e) => {
        hideStatus();
        if (!liveBot) liveBot = addChatMsg("bot", "");
        liveBot.textContent += JSON.parse(e.data);
        $("#chat-messages").scrollTop = $("#chat-messages").scrollHeight;
    });

    eventSource.addEventListener("response_fix", (e) => {
        const clean = JSON.parse(e.data);
        if (liveBot) liveBot.textContent = clean;
        else if (clean) addChatMsg("bot", clean);
    });

    eventSource.addEventListener("tool_call", (e) => {
        hideStatus();
        liveBot = null;
        liveThink = null;
        const data = JSON.parse(e.data);
        addChatTool("call", data.name, data.arguments);
        addLog("tool", `→ ${data.name}(${fmtArgs(data.arguments)})`);
    });

    eventSource.addEventListener("tool_result", (e) => {
        const data = JSON.parse(e.data);
        addChatTool("result", data.name, data.result);
        // Retorno completo, sem corte: o valor exato é o que interessa num teste de segurança.
        addLog("tool", `← ${data.result}`);
    });

    eventSource.addEventListener("timing", (e) => {
        const t = JSON.parse(e.data);
        if (t.phase === "llm") {
            hideStatus();
            liveBot = null;
            liveThink = null;
            addLog("info", `⏱ LLM r${t.round}: ${t.wall_s}s (1º token ${t.first_token_s ?? "-"}s · carga ${t.load_s}s · prompt ${t.prompt_tokens}tok ${t.prompt_s}s · geração ${t.gen_tokens}tok ${t.gen_s}s = ${t.tok_per_s} tok/s)`);
        } else if (t.phase === "tool") {
            addLog("info", `⏱ tool ${t.name}: ${t.wall_s}s`);
        } else if (t.phase === "turn") {
            addLog("info", `⏱ turno total: ${t.wall_s}s (raciocínio ${t.think ? "on" : "off"})`);
        }
    });

    eventSource.addEventListener("error", (e) => {
        hideStatus();
        if (e.data) {
            const text = JSON.parse(e.data);
            addChatMsg("error", text);
            addLog("error", text);
        }
    });

    const onDone = () => {
        hideStatus();
        eventSource.close();
        eventSource = null;
        input.disabled = false;
        $("#chat-send").disabled = false;
        input.focus();
    };
    eventSource.addEventListener("done", onDone);
    eventSource.onerror = onDone;
}

$("#chat-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const input = $("#chat-input");
    const prompt = input.value.trim();
    if (!prompt) return;
    input.value = "";
    runChat(prompt);
});

// ── DOM helpers ──

// Estado do streaming do turno atual (elementos "vivos" que recebem tokens).
let liveThink = null;
let liveBot = null;
let statusEl = null;

function resetLive() { liveThink = null; liveBot = null; hideStatus(); }

function showStatus(text) {
    hideStatus();
    statusEl = addChatMsg("status", text);
}

function hideStatus() {
    if (statusEl) { statusEl.remove(); statusEl = null; }
}

function addChatMsg(type, text) {
    const container = $("#chat-messages");
    const div = document.createElement("div");
    div.className = `msg msg-${type}`;
    div.textContent = text;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
    return div;
}

function addChatTool(kind, name, data) {
    const container = $("#chat-messages");
    const div = document.createElement("div");
    div.className = "msg msg-tool";
    const label = kind === "call" ? "🔧 Tool call" : "📥 Result";
    const content = typeof data === "string" ? data : JSON.stringify(data, null, 2);
    div.innerHTML = `<span class="tool-label">${label}: ${escHtml(name)}</span>\n${escHtml(content)}`;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
}

function addLog(type, text, isDetail) {
    const container = $("#log-messages");
    const div = document.createElement("div");
    div.className = `log-entry log-${type}`;
    const time = new Date().toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    if (isDetail) {
        div.innerHTML = `<span class="log-detail">${escHtml(text)}</span>`;
    } else {
        div.innerHTML = `<span class="log-time">${time}</span>${escHtml(text)}`;
    }
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
    return div;
}

function clearChat() { $("#chat-messages").innerHTML = ""; }
function clearLog() { $("#log-messages").innerHTML = ""; }
function clearInspector() {
    ["inspector-tools","inspector-resources","inspector-templates","inspector-prompts"].forEach(id => {
        document.getElementById(id).innerHTML = "";
    });
    ["inspector-tools-count","inspector-resources-count","inspector-templates-count","inspector-prompts-count"].forEach(id => {
        document.getElementById(id).textContent = "";
    });
}

// ── Inspector ──

function populateInspector(data) {
    renderInspectorSection("inspector-tools", "inspector-tools-count",
        data.tools || [], t => ({ primary: t.name, secondary: null, uri: null }));
    renderInspectorSection("inspector-resources", "inspector-resources-count",
        data.resources || [], r => ({ primary: r.uri, secondary: r.description || null, uri: r.uri }));
    renderTemplateSection("inspector-templates", "inspector-templates-count", data.templates || []);
    renderInspectorSection("inspector-prompts", "inspector-prompts-count",
        data.prompts || [], p => ({ primary: p.name, secondary: p.description || null, uri: null }));
}

function renderTemplateSection(itemsId, countId, templates) {
    const container = document.getElementById(itemsId);
    const countEl = document.getElementById(countId);
    container.innerHTML = "";
    countEl.textContent = `(${templates.length})`;
    if (templates.length === 0) {
        const el = document.createElement("div");
        el.className = "inspector-empty";
        el.textContent = "Nenhum";
        container.appendChild(el);
        return;
    }
    for (const t of templates) {
        const params = [...t.uriTemplate.matchAll(/\{([^}]+)\}/g)].map(m => m[1]);
        const el = document.createElement("div");
        el.className = "inspector-item";
        let html = `<span class="inspector-primary">${escHtml(t.uriTemplate)}</span>`;
        if (t.description) html += `<span class="inspector-secondary">${escHtml(t.description)}</span>`;
        if (params.length > 0) {
            html += `<div class="template-resolve">`;
            for (const p of params) {
                html += `<input class="template-param-input" data-param="${escHtml(p)}" placeholder="${escHtml(p)}">`;
            }
            html += `<button class="btn-read-resource btn-resolve-template" data-template="${escHtml(t.uriTemplate)}" title="Resolver e ler">▶</button>`;
            html += `</div>`;
        }
        el.innerHTML = html;
        container.appendChild(el);
    }
}

function renderInspectorSection(itemsId, countId, items, fn) {
    const container = document.getElementById(itemsId);
    const countEl = document.getElementById(countId);
    container.innerHTML = "";
    countEl.textContent = `(${items.length})`;
    if (items.length === 0) {
        const el = document.createElement("div");
        el.className = "inspector-empty";
        el.textContent = "Nenhum";
        container.appendChild(el);
        return;
    }
    for (const item of items) {
        const { primary, secondary, uri } = fn(item);
        const el = document.createElement("div");
        el.className = "inspector-item";
        const btn = uri ? `<button class="btn-read-resource" data-uri="${escHtml(uri)}" title="Ler recurso">▶</button>` : "";
        let html = `<div class="inspector-row"><span class="inspector-primary">${escHtml(primary)}</span>${btn}</div>` +
            (secondary ? `<span class="inspector-secondary">${escHtml(secondary)}</span>` : "");
        el.innerHTML = html;
        container.appendChild(el);
    }
}

document.addEventListener("click", async (e) => {
    if (!e.target.classList.contains("btn-read-resource")) return;
    let uri;
    if (e.target.classList.contains("btn-resolve-template")) {
        const tmpl = e.target.dataset.template;
        const inputs = e.target.closest(".template-resolve").querySelectorAll(".template-param-input");
        uri = tmpl;
        for (const inp of inputs) {
            uri = uri.replace(`{${inp.dataset.param}}`, inp.value.trim() || inp.dataset.param);
        }
    } else {
        uri = e.target.dataset.uri;
    }
    const item = e.target.closest(".inspector-item");
    const existing = item.querySelector(".resource-content-block");
    if (existing) { existing.remove(); e.target.textContent = "▶"; return; }

    e.target.disabled = true;
    e.target.textContent = "…";
    try {
        const res = await fetch("/api/read_resource", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ uri }),
        });
        const data = await res.json();
        if (data.error) {
            const el = document.createElement("div");
            el.className = "resource-content-block resource-error";
            el.textContent = data.error;
            item.appendChild(el);
        } else {
            const el = document.createElement("pre");
            el.className = "resource-content-block";
            el.textContent = data.content;
            item.appendChild(el);
        }
        e.target.textContent = "▼";
    } catch (err) {
        const el = document.createElement("div");
        el.className = "resource-content-block resource-error";
        el.textContent = err.message;
        item.appendChild(el);
        e.target.textContent = "▶";
    }
    e.target.disabled = false;
});

// Formato do trace dos labs: chave="valor", chave2=123
function fmtArgs(args) {
    if (!args || typeof args !== "object") return "";
    return Object.entries(args).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", ");
}

function truncate(s, n) {
    if (!s) return "";
    return s.length > n ? s.slice(0, n) + "…" : s;
}

function escHtml(s) {
    const d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
}

// ── Check LLM status on load ──

async function checkHealth() {
    const el = $("#status-ollama");
    try {
        const res = await fetch("/api/health");
        const data = await res.json();
        if (data.status === "ok") {
            el.textContent = `✓ ${data.model || "ok"}`;
            el.className = "status-indicator on";
        }
    } catch {
        el.textContent = "✗ offline";
        el.className = "status-indicator error";
    }
}

// ── Log toggle ──

document.getElementById("btn-log-toggle").addEventListener("click", () => {
    const isHidden = document.getElementById("right-panel").classList.toggle("hidden");
    document.querySelector("main").classList.toggle("log-hidden", isHidden);
    document.getElementById("btn-log-toggle").classList.toggle("log-off", isHidden);
});

init();
checkHealth();
