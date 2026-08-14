#!/usr/bin/env python3
"""Turn results/results.json into results/results.txt and results/index.html."""
import json
import os

OUTPUT_DIR = "results"
JSON_PATH = os.path.join(OUTPUT_DIR, "results.json")
TXT_PATH = os.path.join(OUTPUT_DIR, "results.txt")
HTML_PATH = os.path.join(OUTPUT_DIR, "index.html")


def load():
    with open(JSON_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def write_txt(data):
    results = data["results"]
    instant = [
        r for r in results
        if r["status"] == "working" and not r["needs_auth"] and not r["browser_required"]
    ]

    lines = [
        "g4f Provider & Model Health Report",
        f"Generated: {data['generated_at']}",
        f"g4f version: {data['g4f_version']}",
        f"Total provider/model pairs tested: {data['total_pairs']}",
        "",
        f"WORKING INSTANTLY, NO AUTH, NO BROWSER ({len(instant)}):",
        "-" * 60,
    ]
    for r in sorted(instant, key=lambda x: x["response_time_ms"] or 9e9):
        lines.append(f"{r['provider']:<25} {r['model']:<25} {r['response_time_ms']}ms")

    lines += ["", "ALL RESULTS:", "-" * 60]
    for r in results:
        err = f" error={r['error']}" if r.get("error") else ""
        lines.append(
            f"{r['provider']:<25} {r['model']:<25} status={r['status']:<8} "
            f"auth={str(r['needs_auth']):<5} browser={str(r['browser_required']):<5} "
            f"time={r['response_time_ms']}ms{err}"
        )

    with open(TXT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"Wrote {TXT_PATH}")


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>g4f Provider Health Report</title>
<style>
  :root {
    --bg: #0d1117;
    --bg-alt: #161b22;
    --border: #30363d;
    --text: #c9d1d9;
    --text-dim: #8b949e;
    --accent: #58a6ff;
    --green: #3fb950;
    --red: #f85149;
    --yellow: #d29922;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }
  header {
    padding: 24px 28px 16px;
    border-bottom: 1px solid var(--border);
  }
  header h1 { margin: 0 0 4px; font-size: 22px; }
  header p { margin: 0; color: var(--text-dim); font-size: 13px; }
  .stats {
    display: flex; gap: 18px; margin-top: 14px; flex-wrap: wrap;
  }
  .stat {
    background: var(--bg-alt);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 16px;
    min-width: 120px;
  }
  .stat .n { font-size: 20px; font-weight: 700; }
  .stat .l { font-size: 11px; color: var(--text-dim); text-transform: uppercase; letter-spacing: .04em; }
  .stat.green .n { color: var(--green); }
  .stat.red .n { color: var(--red); }

  .controls {
    display: flex; flex-wrap: wrap; gap: 10px;
    padding: 16px 28px;
    background: var(--bg-alt);
    border-bottom: 1px solid var(--border);
    position: sticky; top: 0; z-index: 5;
  }
  .controls input[type=text], .controls select {
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 7px 10px;
    font-size: 13px;
  }
  .controls label {
    display: flex; align-items: center; gap: 6px;
    font-size: 13px; color: var(--text-dim);
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 6px 10px;
    cursor: pointer;
    user-select: none;
  }
  .controls label:hover { border-color: var(--accent); }
  .controls input[type=checkbox] { accent-color: var(--accent); }

  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 9px 14px; border-bottom: 1px solid var(--border); }
  th {
    position: sticky; top: 61px;
    background: var(--bg-alt);
    color: var(--text-dim);
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
    z-index: 4;
  }
  th:hover { color: var(--accent); }
  tr:hover td { background: rgba(88,166,255,0.05); }

  .badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 600;
  }
  .badge.working { background: rgba(63,185,80,.15); color: var(--green); }
  .badge.error { background: rgba(248,81,73,.15); color: var(--red); }
  .badge.timeout { background: rgba(210,153,34,.15); color: var(--yellow); }
  .badge.skipped { background: rgba(139,148,158,.15); color: var(--text-dim); }

  .yes { color: var(--yellow); }
  .no { color: var(--text-dim); }
  .snippet { color: var(--text-dim); font-size: 12px; max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .empty { padding: 40px; text-align: center; color: var(--text-dim); }
  footer { padding: 20px 28px; color: var(--text-dim); font-size: 12px; }
</style>
</head>
<body>
<header>
  <h1>g4f Provider &amp; Model Health Report</h1>
  <p>Generated __GENERATED_AT__ &middot; g4f version __G4F_VERSION__ &middot; __TOTAL__ provider/model pairs tested</p>
  <div class="stats" id="stats"></div>
</header>

<div class="controls">
  <input type="text" id="search" placeholder="Search provider or model...">
  <select id="statusFilter">
    <option value="">All statuses</option>
    <option value="working">Working</option>
    <option value="error">Error</option>
    <option value="timeout">Timeout</option>
    <option value="skipped">Skipped</option>
  </select>
  <label><input type="checkbox" id="hideAuth"> Hide requires-auth</label>
  <label><input type="checkbox" id="hideBrowser"> Hide requires-browser/cookies</label>
  <label><input type="checkbox" id="instantOnly"> Instant-working only (no auth, no browser)</label>
</div>

<table id="resultsTable">
  <thead>
    <tr>
      <th data-key="provider">Provider</th>
      <th data-key="model">Model</th>
      <th data-key="status">Status</th>
      <th data-key="needs_auth">Auth</th>
      <th data-key="browser_required">Browser</th>
      <th data-key="response_time_ms">Time (ms)</th>
      <th data-key="response_snippet">Response / Error</th>
    </tr>
  </thead>
  <tbody id="tbody"></tbody>
</table>
<div class="empty" id="emptyState" style="display:none;">No results match these filters.</div>

<footer>Generated automatically by the g4f provider monitor workflow.</footer>

<script>
const DATA = __RESULTS_JSON__;

let sortKey = "response_time_ms";
let sortAsc = true;

function render() {
  const q = document.getElementById('search').value.toLowerCase();
  const status = document.getElementById('statusFilter').value;
  const hideAuth = document.getElementById('hideAuth').checked;
  const hideBrowser = document.getElementById('hideBrowser').checked;
  const instantOnly = document.getElementById('instantOnly').checked;

  let rows = DATA.filter(r => {
    if (q && !(r.provider.toLowerCase().includes(q) || r.model.toLowerCase().includes(q))) return false;
    if (status && r.status !== status) return false;
    if (hideAuth && r.needs_auth) return false;
    if (hideBrowser && r.browser_required) return false;
    if (instantOnly && (r.needs_auth || r.browser_required || r.status !== 'working')) return false;
    return true;
  });

  rows.sort((a, b) => {
    let av = a[sortKey], bv = b[sortKey];
    if (av === null || av === undefined) av = sortKey === 'response_time_ms' ? Infinity : '';
    if (bv === null || bv === undefined) bv = sortKey === 'response_time_ms' ? Infinity : '';
    if (typeof av === 'string') av = av.toLowerCase();
    if (typeof bv === 'string') bv = bv.toLowerCase();
    if (av < bv) return sortAsc ? -1 : 1;
    if (av > bv) return sortAsc ? 1 : -1;
    return 0;
  });

  const tbody = document.getElementById('tbody');
  tbody.innerHTML = rows.map(r => `
    <tr>
      <td>${escapeHtml(r.provider)}</td>
      <td>${escapeHtml(r.model)}</td>
      <td><span class="badge ${r.status}">${r.status}</span></td>
      <td class="${r.needs_auth ? 'yes' : 'no'}">${r.needs_auth ? 'Yes' : 'No'}</td>
      <td class="${r.browser_required ? 'yes' : 'no'}">${r.browser_required ? 'Yes' : 'No'}</td>
      <td>${r.response_time_ms !== null && r.response_time_ms !== undefined ? Math.round(r.response_time_ms) : '-'}</td>
      <td class="snippet" title="${escapeHtml(r.response_snippet || r.error || '')}">${escapeHtml(r.response_snippet || r.error || '')}</td>
    </tr>
  `).join('');

  document.getElementById('emptyState').style.display = rows.length ? 'none' : 'block';
  renderStats(rows);
}

function renderStats(rows) {
  const total = DATA.length;
  const working = DATA.filter(r => r.status === 'working').length;
  const instant = DATA.filter(r => r.status === 'working' && !r.needs_auth && !r.browser_required).length;
  const errored = DATA.filter(r => r.status === 'error' || r.status === 'timeout').length;
  document.getElementById('stats').innerHTML = `
    <div class="stat"><div class="n">${total}</div><div class="l">Total tested</div></div>
    <div class="stat green"><div class="n">${working}</div><div class="l">Working</div></div>
    <div class="stat green"><div class="n">${instant}</div><div class="l">Instant, no auth/browser</div></div>
    <div class="stat red"><div class="n">${errored}</div><div class="l">Failed / timed out</div></div>
    <div class="stat"><div class="n">${rows.length}</div><div class="l">Matching filters</div></div>
  `;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

document.querySelectorAll('th[data-key]').forEach(th => {
  th.addEventListener('click', () => {
    const key = th.dataset.key;
    if (sortKey === key) { sortAsc = !sortAsc; } else { sortKey = key; sortAsc = true; }
    render();
  });
});

['search', 'statusFilter'].forEach(id => document.getElementById(id).addEventListener('input', render));
['hideAuth', 'hideBrowser', 'instantOnly'].forEach(id => document.getElementById(id).addEventListener('change', render));

render();
</script>
</body>
</html>
"""


def write_html(data):
    results_json = json.dumps(data["results"])
    html = (
        HTML_TEMPLATE
        .replace("__RESULTS_JSON__", results_json)
        .replace("__GENERATED_AT__", data["generated_at"])
        .replace("__G4F_VERSION__", str(data["g4f_version"]))
        .replace("__TOTAL__", str(data["total_pairs"]))
    )
    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Wrote {HTML_PATH}")


if __name__ == "__main__":
    data = load()
    write_txt(data)
    write_html(data)
