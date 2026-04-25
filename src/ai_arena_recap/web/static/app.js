function escapeHtml(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function raceBadge(v) {
  const r = v || "X";
  return `<span class="race race-${escapeHtml(r)}">${escapeHtml(r)}</span>`;
}

function formatStarted(value) {
  if (!value) return "";
  const d = new Date(value);
  if (isNaN(d)) return value;
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// AG Grid cell renderers (return HTML strings or DOM nodes).
const cellRenderers = {
  link: (params) => {
    const { href, text } = params.value || {};
    if (!href) return "";
    return `<a href="${escapeHtml(href)}">${escapeHtml(text)}</a>`;
  },
  race: (params) => raceBadge(params.value),
  result: (params) => {
    const v = params.value;
    return v ? `<span class="result-${escapeHtml(v)}">${escapeHtml(v)}</span>` : "";
  },
  eloChange: (params) => {
    const v = params.value;
    if (v == null) return "";
    const cls = v > 0 ? "up" : v < 0 ? "down" : "";
    const sign = v > 0 ? "+" : "";
    return `<span class="${cls}">${sign}${v}</span>`;
  },
  startedAt: (params) => formatStarted(params.value),
};

// Build a checkbox-style "Columns" toolbar above an AG Grid (since the
// Columns Tool Panel is an Enterprise feature). Persists hidden columns
// to localStorage under storageKey.
function buildColumnTogglePanel(api, storageKey, container) {
  container.innerHTML = "";
  container.classList.add("col-toggle-panel");

  const stored = JSON.parse(localStorage.getItem(storageKey) || "{}");
  const hiddenSet = new Set(stored.hidden || []);

  // Apply persisted hidden state.
  api.getColumns().forEach((col) => {
    const id = col.getColId();
    if (hiddenSet.has(id)) api.setColumnsVisible([id], false);
  });

  const button = document.createElement("button");
  button.type = "button";
  button.className = "col-toggle-button";
  button.textContent = "Columns ▾";

  const popover = document.createElement("div");
  popover.className = "col-toggle-popover";
  popover.hidden = true;

  api.getColumns().forEach((col) => {
    const def = col.getColDef();
    if (!def.headerName) return;
    const id = col.getColId();

    const label = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = col.isVisible();
    cb.addEventListener("change", () => {
      api.setColumnsVisible([id], cb.checked);
      if (cb.checked) hiddenSet.delete(id); else hiddenSet.add(id);
      localStorage.setItem(storageKey, JSON.stringify({ hidden: [...hiddenSet] }));
    });
    label.appendChild(cb);
    label.appendChild(document.createTextNode(" " + def.headerName));
    popover.appendChild(label);
  });

  button.addEventListener("click", (e) => {
    e.stopPropagation();
    popover.hidden = !popover.hidden;
  });
  document.addEventListener("click", (e) => {
    if (!container.contains(e.target)) popover.hidden = true;
  });

  container.appendChild(button);
  container.appendChild(popover);
}

// Server-side datasource for AG Grid Infinite Row Model, talking to our
// page/size JSON endpoints.
function buildPaginatedDatasource(url, pageSize) {
  return {
    rowCount: undefined,
    getRows: async (params) => {
      const page = Math.floor(params.startRow / pageSize) + 1;
      try {
        const res = await fetch(`${url}?page=${page}&size=${pageSize}`);
        const json = await res.json();
        const data = json.data || [];
        const total = json.total != null ? json.total : params.startRow + data.length;
        params.successCallback(data, total);
      } catch (err) {
        console.error("Datasource error:", err);
        params.failCallback();
      }
    },
  };
}
