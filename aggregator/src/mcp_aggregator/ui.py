"""Minimal admin UI – rendered as inline HTML from FastAPI."""

ADMIN_HTML = """<!DOCTYPE html>
<html lang="no">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MCP Aggregator</title>
<script src="https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js"></script>
<script src="https://unpkg.com/alpinejs@3.x.x/dist/cdn.min.js" defer></script>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:system-ui,sans-serif;background:#0f1117;color:#e2e8f0;min-height:100vh}
  .top{background:#1a1d27;border-bottom:1px solid #2d3148;padding:1rem 1.5rem;display:flex;align-items:center;gap:1rem}
  .top h1{font-size:1.1rem;font-weight:600}
  .badge{font-size:.7rem;background:#6366f1;color:#fff;padding:.2rem .5rem;border-radius:99px}
  main{max-width:960px;margin:2rem auto;padding:0 1rem}
  section{background:#1a1d27;border:1px solid #2d3148;border-radius:.5rem;margin-bottom:1.5rem;padding:1.25rem}
  h2{font-size:.85rem;text-transform:uppercase;letter-spacing:.08em;color:#94a3b8;margin-bottom:1rem}
  table{width:100%;border-collapse:collapse;font-size:.875rem}
  th{text-align:left;padding:.5rem .75rem;color:#64748b;font-weight:500;border-bottom:1px solid #2d3148}
  td{padding:.5rem .75rem;border-bottom:1px solid #1e2235;vertical-align:middle}
  tr:last-child td{border:none}
  .dot{display:inline-block;width:.5rem;height:.5rem;border-radius:50%;margin-right:.4rem}
  .dot.ok{background:#22c55e}.dot.err{background:#ef4444}.dot.off{background:#475569}
  .tag{font-size:.7rem;background:#1e293b;padding:.15rem .45rem;border-radius:4px;color:#94a3b8}
  form{display:flex;flex-direction:column;gap:.75rem}
  .row{display:flex;gap:.75rem;flex-wrap:wrap}
  input,select,textarea{background:#0f1117;border:1px solid #2d3148;color:#e2e8f0;padding:.5rem .75rem;border-radius:.375rem;font-size:.875rem;width:100%}
  input:focus,select:focus,textarea:focus{outline:2px solid #6366f1;border-color:transparent}
  textarea{font-family:monospace;resize:vertical}
  .btn{padding:.45rem 1rem;border-radius:.375rem;border:none;cursor:pointer;font-size:.8rem;font-weight:500}
  .btn-primary{background:#6366f1;color:#fff}.btn-primary:hover{background:#4f52cc}
  .btn-sm{padding:.3rem .6rem;font-size:.75rem}
  .btn-danger{background:#7f1d1d;color:#fca5a5}.btn-danger:hover{background:#991b1b}
  .btn-ghost{background:#1e293b;color:#94a3b8}.btn-ghost:hover{background:#2d3a52}
  .btn:disabled{opacity:.5;cursor:not-allowed}
  .err-msg{color:#fca5a5;font-size:.8rem;margin-top:.25rem}
  #toast{position:fixed;bottom:1.5rem;right:1.5rem;background:#22c55e;color:#fff;padding:.6rem 1.1rem;border-radius:.4rem;font-size:.85rem;display:none}
  /* Logs */
  .log-box{background:#0a0c14;border:1px solid #1e2235;border-radius:.375rem;height:260px;overflow-y:auto;padding:.5rem;font-family:monospace;font-size:.78rem;line-height:1.5}
  .log-line{display:flex;gap:.5rem;min-width:0}
  .log-ts{color:#475569;min-width:8ch;flex-shrink:0;white-space:nowrap}
  .log-lvl{min-width:5ch;flex-shrink:0;font-weight:700;white-space:nowrap}
  .log-srv{min-width:10ch;max-width:14ch;flex-shrink:0;color:#818cf8;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .log-msg{color:#cbd5e1;flex:1;word-break:break-word;white-space:pre-wrap}
  .lvl-DEBUG{color:#475569}.lvl-INFO{color:#38bdf8}.lvl-WARNING{color:#fbbf24}.lvl-ERROR{color:#f87171}
  .tab-btn{background:none;border:none;border-bottom:2px solid transparent;color:#64748b;padding:.35rem .75rem;cursor:pointer;font-size:.8rem;font-weight:500}
  .tab-btn.active{color:#e2e8f0;border-bottom-color:#6366f1}
  /* Tool tester */
  .schema-box{background:#0a0c14;border:1px solid #1e2235;border-radius:.375rem;padding:.5rem;font-family:monospace;font-size:.75rem;color:#94a3b8;overflow:auto;max-height:180px;white-space:pre;margin-top:.5rem}
  .result-box{background:#0a0c14;border:1px solid #1e2235;border-radius:.375rem;padding:.75rem;font-family:monospace;font-size:.8rem;white-space:pre-wrap;word-break:break-all;max-height:300px;overflow-y:auto;margin-top:.5rem}
  .result-err{border-color:#7f1d1d}
</style>
</head>
<body>
<div class="top">
  <h1>MCP Aggregator</h1>
  <span class="badge" id="tool-count" hx-get="/api/tools" hx-trigger="load, every 15s"
        hx-swap="none" hx-on::after-request="updateToolCount(event)">...</span>
</div>
<main>

  <!-- Server list -->
  <section>
    <h2>MCP Servere</h2>
    <div id="server-list" hx-get="/admin/servers-table" hx-trigger="load, serverUpdated from:body" hx-swap="innerHTML">
      Laster...
    </div>
  </section>

  <!-- Logger -->
  <section x-data="logsPanel()" x-init="init()">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:.75rem">
      <h2 style="margin:0">Logger</h2>
      <div style="display:flex;gap:.4rem;align-items:center">
        <select x-model="server" @change="onServerChange()"
                style="width:auto;padding:.25rem .5rem;font-size:.75rem">
          <option value="">Alle servere</option>
          <template x-for="s in servers" :key="s.name">
            <option :value="s.name" x-text="s.name"></option>
          </template>
        </select>
        <button class="btn btn-sm" :class="streaming ? 'btn-primary' : 'btn-ghost'"
                @click="toggleStream()">
          <span x-show="!streaming">▶ Live</span>
          <span x-show="streaming">⏸ Live</span>
        </button>
        <button class="btn btn-sm btn-ghost" title="Last inn på nytt" @click="reload()">↺</button>
        <button class="btn btn-sm btn-ghost" @click="clear()">Tøm</button>
      </div>
    </div>
    <div style="display:flex;border-bottom:1px solid #2d3148;margin-bottom:.75rem">
      <button class="tab-btn" :class="{active:tab==='app'}" @click="tab='app'">App-logger</button>
      <button class="tab-btn" :class="{active:tab==='stderr'}"
              @click="tab='stderr';loadStderr()" x-show="!!server">Stderr</button>
    </div>

    <!-- App logs tab -->
    <div x-show="tab==='app'" class="log-box" x-ref="logBox">
      <template x-if="!entries.length">
        <p style="color:#475569;padding:.25rem">Ingen loggoppføringer ennå.</p>
      </template>
      <template x-for="(e,i) in entries" :key="i">
        <div class="log-line">
          <span class="log-ts" x-text="fmtTs(e.ts)"></span>
          <span class="log-lvl" :class="'lvl-'+e.level" x-text="lvlShort(e.level)"></span>
          <span class="log-srv" x-text="e.server||'agg'"></span>
          <span class="log-msg" x-text="e.msg"></span>
        </div>
      </template>
    </div>

    <!-- Stderr tab -->
    <div x-show="tab==='stderr'">
      <div x-show="stderrLoading" style="color:#64748b;font-size:.8rem;padding:.5rem">Laster...</div>
      <div x-show="!stderrLoading" class="log-box">
        <template x-if="!stderrLines.length">
          <p style="color:#475569;padding:.25rem">Ingen stderr-output funnet.</p>
        </template>
        <template x-for="(line,i) in stderrLines" :key="i">
          <div style="font-family:monospace;font-size:.78rem;color:#94a3b8;line-height:1.5;word-break:break-all"
               x-text="line"></div>
        </template>
      </div>
    </div>
  </section>

  <!-- Tool tester -->
  <section x-data="toolTester()" x-init="init()">
    <h2>Test verktøy</h2>
    <div class="row" style="margin-bottom:.75rem">
      <div style="flex:1;min-width:160px">
        <label style="font-size:.8rem;color:#64748b;display:block;margin-bottom:.25rem">Server</label>
        <select x-model="selectedServer" @change="onServerChange()">
          <option value="">Velg server...</option>
          <template x-for="s in servers" :key="s.name">
            <option :value="s.name" x-text="s.name + ' (' + s.tool_count + ')'"></option>
          </template>
        </select>
      </div>
      <div style="flex:1;min-width:160px">
        <label style="font-size:.8rem;color:#64748b;display:block;margin-bottom:.25rem">Verktøy</label>
        <select x-model="selectedTool" @change="onToolChange()" :disabled="!tools.length">
          <option value="">Velg verktøy...</option>
          <template x-for="t in tools" :key="t.tool">
            <option :value="t.tool" x-text="t.tool"></option>
          </template>
        </select>
      </div>
    </div>

    <!-- Tool description + schema -->
    <div x-show="toolDef" style="margin-bottom:.75rem">
      <p style="font-size:.8rem;color:#94a3b8;line-height:1.5" x-text="toolDef && toolDef.description || '—'"></p>
      <details style="margin-top:.5rem">
        <summary style="font-size:.75rem;color:#475569;cursor:pointer;user-select:none">Input schema</summary>
        <pre class="schema-box" x-text="toolDef ? JSON.stringify(toolDef.inputSchema, null, 2) : ''"></pre>
      </details>
    </div>

    <!-- Args + call -->
    <div x-show="selectedTool">
      <label style="font-size:.8rem;color:#64748b;display:block;margin-bottom:.25rem">Argumenter (JSON)</label>
      <textarea x-model="argsJson" rows="4" placeholder="{}"></textarea>
      <span x-show="argsError" class="err-msg" x-text="argsError"></span>
      <div style="display:flex;gap:.5rem;align-items:center;margin-top:.75rem">
        <button class="btn btn-primary" @click="callTool()" :disabled="loading">
          <span x-show="!loading">Kjør</span>
          <span x-show="loading">Kjører...</span>
        </button>
        <button class="btn btn-sm btn-ghost" @click="result=null" x-show="result">Tøm resultat</button>
      </div>
    </div>

    <!-- Result -->
    <div x-show="result">
      <div style="font-size:.75rem;color:#64748b;margin-top:.75rem">
        Resultat
        <span x-show="result && result.isError" style="color:#fca5a5">(feil)</span>
      </div>
      <pre class="result-box" :class="result && result.isError ? 'result-err' : ''"
           x-text="fmtResult(result)"></pre>
    </div>
  </section>

  <!-- Add server -->
  <section x-data="{type:'pypi'}">
    <h2>Legg til server</h2>
    <form hx-post="/admin/add-server" hx-target="#add-result" hx-swap="innerHTML"
          hx-on::after-request="if(event.detail.successful){htmx.trigger('body','serverUpdated');this.reset()}">
      <div class="row">
        <div style="flex:1;min-width:180px">
          <label style="font-size:.8rem;color:#64748b;display:block;margin-bottom:.25rem">Navn</label>
          <input name="name" placeholder="mitt-mcp-server" required>
        </div>
        <div style="width:120px">
          <label style="font-size:.8rem;color:#64748b;display:block;margin-bottom:.25rem">Type</label>
          <select name="type" x-model="type">
            <option value="pypi">PyPI</option>
            <option value="npm">npm</option>
            <option value="git">git</option>
            <option value="cmd">cmd</option>
          </select>
        </div>
      </div>
      <div>
        <label style="font-size:.8rem;color:#64748b;display:block;margin-bottom:.25rem">
          <span x-show="type==='pypi'">PyPI pakke (f.eks. mcp-server-fetch)</span>
          <span x-show="type==='npm'">npm pakke (f.eks. @modelcontextprotocol/server-filesystem)</span>
          <span x-show="type==='git'">Git URL (https://...)</span>
          <span x-show="type==='cmd'">Kommando (f.eks. /usr/bin/mytool)</span>
        </label>
        <input name="package" placeholder="" required>
      </div>
      <div>
        <label style="font-size:.8rem;color:#64748b;display:block;margin-bottom:.25rem">Args (kommaseparert, valgfritt)</label>
        <input name="args" placeholder="">
      </div>
      <div>
        <label style="font-size:.8rem;color:#64748b;display:block;margin-bottom:.25rem">Env vars (KEY=VALUE, kommaseparert, valgfritt)</label>
        <input name="env" placeholder="">
      </div>
      <div>
        <button type="submit" class="btn btn-primary">Installer og start</button>
      </div>
    </form>
    <div id="add-result" style="margin-top:.75rem"></div>
  </section>

</main>

<div id="toast">Lagret</div>

<script>
function updateToolCount(evt) {
  try {
    const data = JSON.parse(evt.detail.xhr.responseText);
    document.getElementById('tool-count').textContent = data.length + ' tools';
  } catch {}
}

function logsPanel() {
  return {
    entries: [], servers: [], server: '',
    tab: 'app', streaming: false, _es: null,
    stderrLines: [], stderrLoading: false,

    async init() {
      try {
        const r = await fetch('/api/servers');
        this.servers = await r.json();
      } catch {}
      await this.reload();
    },

    async reload() {
      try {
        const qs = this.server ? `?server=${encodeURIComponent(this.server)}&limit=200` : '?limit=200';
        const r = await fetch('/api/logs' + qs);
        this.entries = await r.json();
        this.$nextTick(() => this._scrollBottom());
      } catch {}
    },

    async onServerChange() {
      this.tab = 'app';
      if (this.streaming) { this.stopStream(); this.startStream(); }
      else { await this.reload(); }
    },

    async loadStderr() {
      if (!this.server) return;
      this.stderrLoading = true;
      try {
        const r = await fetch(`/api/logs/${encodeURIComponent(this.server)}/stderr?limit=300`);
        const d = await r.json();
        this.stderrLines = d.lines;
      } catch {} finally { this.stderrLoading = false; }
    },

    toggleStream() { this.streaming ? this.stopStream() : this.startStream(); },

    startStream() {
      if (this._es) this._es.close();
      const qs = this.server ? `?server=${encodeURIComponent(this.server)}` : '';
      this._es = new EventSource('/api/logs/stream' + qs);
      this._es.onmessage = (ev) => {
        this.entries.push(JSON.parse(ev.data));
        if (this.entries.length > 500) this.entries.shift();
        this.$nextTick(() => this._scrollBottom());
      };
      this._es.onerror = () => { this.streaming = false; this._es = null; };
      this.streaming = true;
    },

    stopStream() {
      if (this._es) { this._es.close(); this._es = null; }
      this.streaming = false;
    },

    clear() { this.entries = []; this.stderrLines = []; },

    _scrollBottom() {
      const b = this.$refs.logBox;
      if (b) b.scrollTop = b.scrollHeight;
    },

    fmtTs(ts) { return new Date(ts * 1000).toTimeString().slice(0, 8); },

    lvlShort(l) {
      return {DEBUG:'DBG ',INFO:'INFO',WARNING:'WARN',ERROR:'ERR '}[l] ?? l.slice(0,4);
    },
  };
}

function toolTester() {
  return {
    servers: [], tools: [],
    selectedServer: '', selectedTool: '',
    toolDef: null, argsJson: '{}', argsError: '',
    result: null, loading: false,

    async init() { await this.loadServers(); },

    async loadServers() {
      try {
        const r = await fetch('/api/servers');
        const all = await r.json();
        this.servers = all.filter(s => s.running);
      } catch {}
    },

    async onServerChange() {
      this.tools = []; this.selectedTool = ''; this.toolDef = null; this.result = null;
      if (!this.selectedServer) return;
      try {
        const r = await fetch('/api/tools');
        const all = await r.json();
        this.tools = all.filter(t => t.server === this.selectedServer);
      } catch {}
    },

    onToolChange() {
      this.toolDef = this.tools.find(t => t.tool === this.selectedTool) ?? null;
      this.argsJson = '{}'; this.argsError = ''; this.result = null;
    },

    async callTool() {
      this.argsError = '';
      let args;
      try { args = JSON.parse(this.argsJson); }
      catch { this.argsError = 'Ugyldig JSON'; return; }
      this.loading = true; this.result = null;
      try {
        const r = await fetch('/api/tools/call', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({server: this.selectedServer, tool: this.selectedTool, arguments: args}),
        });
        this.result = await r.json();
      } catch(e) { this.result = {detail: e.message}; }
      finally { this.loading = false; }
    },

    fmtResult(res) {
      if (!res) return '';
      if (res.detail) return 'Feil: ' + res.detail;
      if (res.content) {
        return res.content.map(c => c.type === 'text' ? c.text : JSON.stringify(c, null, 2)).join('\\n─────\\n');
      }
      return JSON.stringify(res, null, 2);
    },
  };
}
</script>
</body>
</html>
"""


def servers_table_html(servers: list[dict]) -> str:
    if not servers:
        return "<p style='color:#64748b;font-size:.875rem'>Ingen servere konfigurert ennå.</p>"

    rows = ""
    for s in servers:
        if s["running"]:
            dot = '<span class="dot ok"></span>'
            status = f"Kjører ({s['tool_count']} tools)"
        elif not s["enabled"]:
            dot = '<span class="dot off"></span>'
            status = "Deaktivert"
        else:
            dot = '<span class="dot err"></span>'
            status = s.get("error") or "Stoppet"

        toggle_url = f"/admin/servers/{s['id']}/{'disable' if s['enabled'] else 'enable'}"
        toggle_lbl = "Deaktiver" if s["enabled"] else "Aktiver"

        rows += f"""
        <tr>
          <td>{dot}{s['name']}</td>
          <td><span class="tag">{s['type']}</span></td>
          <td style="color:#94a3b8;font-size:.8rem">{s['package']}</td>
          <td style="font-size:.8rem">{status}</td>
          <td>
            <button class="btn btn-sm btn-ghost"
              hx-post="{toggle_url}"
              hx-swap="none"
              hx-on::after-request="htmx.trigger('body','serverUpdated')">{toggle_lbl}</button>
            <button class="btn btn-sm btn-ghost"
              hx-post="/admin/servers/{s['id']}/restart"
              hx-swap="none"
              hx-on::after-request="htmx.trigger('body','serverUpdated')"
              style="margin-left:.25rem">↺</button>
            <button class="btn btn-sm btn-danger"
              hx-delete="/admin/servers/{s['id']}"
              hx-swap="none"
              hx-confirm="Slett {s['name']}?"
              hx-on::after-request="htmx.trigger('body','serverUpdated')"
              style="margin-left:.25rem">Slett</button>
          </td>
        </tr>"""

    return f"""
    <table>
      <thead>
        <tr>
          <th>Navn</th><th>Type</th><th>Pakke</th><th>Status</th><th>Handlinger</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>"""


def add_result_html(server: dict, tools: list, error: str | None) -> str:
    if error and not server:
        return f'<p class="err-msg">Feil: {error}</p>'
    msg = f"<strong>{server['name']}</strong> startet med {len(tools)} tools."
    if error:
        msg += f'<br><span class="err-msg">Advarsel: {error}</span>'
    return f'<p style="color:#86efac;font-size:.875rem">{msg}</p>'
