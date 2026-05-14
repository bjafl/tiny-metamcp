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
  main{max-width:900px;margin:2rem auto;padding:0 1rem}
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
  input,select{background:#0f1117;border:1px solid #2d3148;color:#e2e8f0;padding:.5rem .75rem;border-radius:.375rem;font-size:.875rem;width:100%}
  input:focus,select:focus{outline:2px solid #6366f1;border-color:transparent}
  .btn{padding:.45rem 1rem;border-radius:.375rem;border:none;cursor:pointer;font-size:.8rem;font-weight:500}
  .btn-primary{background:#6366f1;color:#fff}.btn-primary:hover{background:#4f52cc}
  .btn-sm{padding:.3rem .6rem;font-size:.75rem}
  .btn-danger{background:#7f1d1d;color:#fca5a5}.btn-danger:hover{background:#991b1b}
  .btn-ghost{background:#1e293b;color:#94a3b8}.btn-ghost:hover{background:#2d3a52}
  .err-msg{color:#fca5a5;font-size:.8rem;margin-top:.25rem}
  #toast{position:fixed;bottom:1.5rem;right:1.5rem;background:#22c55e;color:#fff;padding:.6rem 1.1rem;border-radius:.4rem;font-size:.85rem;display:none}
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
