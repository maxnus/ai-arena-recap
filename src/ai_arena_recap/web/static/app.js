const DOWNLOAD_ICON = "⬇";

function escapeHtml(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

const RACE_ICON_FILES = { T: "terran.svg", Z: "zerg.svg", P: "protoss.svg", R: "random.svg" };

const DOWNLOADED_REPLAYS_KEY = "downloaded-replays-v1";

function loadDownloadedReplays() {
  try {
    return new Set(JSON.parse(localStorage.getItem(DOWNLOADED_REPLAYS_KEY) || "[]"));
  } catch {
    return new Set();
  }
}

function markReplayDownloaded(matchId) {
  const set = loadDownloadedReplays();
  set.add(matchId);
  localStorage.setItem(DOWNLOADED_REPLAYS_KEY, JSON.stringify([...set]));
}
const RACE_NAMES = { T: "Terran", Z: "Zerg", P: "Protoss", R: "Random" };

function raceBadge(v) {
  const r = v || "X";
  const file = RACE_ICON_FILES[r];
  const inner = file
    ? `<img class="race-icon" src="/static/${file}" alt="${escapeHtml(r)}">`
    : `<span class="race-letter">${escapeHtml(r)}</span>`;
  return `<span class="race race-${escapeHtml(r)}" title="${escapeHtml(r)}">${inner}</span>`;
}

function formatStarted(value) {
  if (!value) return "";
  const d = new Date(value);
  if (isNaN(d)) return value;
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function formatDuration(seconds) {
  if (seconds == null) return "";
  const total = Math.round(seconds);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}m ${String(s).padStart(2, "0")}s`;
}

// SC2 ladder runs at "Faster" game speed.
const GAME_STEPS_PER_SECOND = 22.4;
function formatGameStepsAsDuration(steps) {
  if (steps == null) return "";
  return formatDuration(steps / GAME_STEPS_PER_SECOND);
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
    const cls = v > 0 ? "up" : v < 0 ? "down" : "zero";
    const sign = v > 0 ? "+" : "";
    return `<span class="${cls}">${sign}${v}</span>`;
  },
  resultWithEloChange: (params) => {
    const r = params.data;
    if (!r) return "";
    const result = r.result;
    const change = r.elo_change;
    const label = result ? result.charAt(0).toUpperCase() + result.slice(1) : "?";
    const changePart = change == null ? "" : ` (${change > 0 ? "+" : ""}${change})`;
    const cls = result ? `result-${escapeHtml(result)}` : "";
    return `<span class="${cls}">${escapeHtml(label)}${escapeHtml(changePart)}</span>`;
  },
  startedAt: (params) => formatStarted(params.value),
};

// Build a checkbox-style "Columns" toolbar above an AG Grid (since the
// Columns Tool Panel is an Enterprise feature). Persists the full AG Grid
// column state (visibility, width, order, pinning, sort) to localStorage
// via getColumnState/applyColumnState, so user customizations survive
// reloads.
function buildColumnTogglePanel(api, storageKey, container) {
  container.innerHTML = "";
  container.classList.add("col-toggle-panel");

  const saved = JSON.parse(localStorage.getItem(storageKey) || "null");
  if (saved && Array.isArray(saved)) {
    api.applyColumnState({ state: saved, applyOrder: true });
  }

  const saveState = () => {
    localStorage.setItem(storageKey, JSON.stringify(api.getColumnState()));
  };

  // Persist on any column-state change (visibility, width, order, pin, sort).
  ["columnVisible", "columnResized", "columnMoved", "columnPinned", "sortChanged"].forEach((evt) => {
    api.addEventListener(evt, saveState);
  });

  const button = document.createElement("button");
  button.type = "button";
  button.className = "col-toggle-button";
  button.textContent = "Columns ▾";

  const popover = document.createElement("div");
  popover.className = "col-toggle-popover";

  // Build the checkbox list reflecting current visibility.
  const refreshCheckboxes = () => {
    popover.innerHTML = "";
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
      });
      label.appendChild(cb);
      label.appendChild(document.createTextNode(" " + def.headerName));
      popover.appendChild(label);
    });
  };
  refreshCheckboxes();

  // Keep checkboxes in sync if visibility changes via other means.
  api.addEventListener("columnVisible", refreshCheckboxes);

  button.addEventListener("click", (e) => {
    e.stopPropagation();
    popover.classList.toggle("open");
  });
  document.addEventListener("click", (e) => {
    if (!container.contains(e.target)) popover.classList.remove("open");
  });

  // "Reset" button to wipe persisted state.
  const resetBtn = document.createElement("button");
  resetBtn.type = "button";
  resetBtn.className = "col-toggle-button";
  resetBtn.textContent = "Reset";
  resetBtn.title = "Reset columns to defaults";
  resetBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    localStorage.removeItem(storageKey);
    api.resetColumnState();
    refreshCheckboxes();
  });

  container.appendChild(button);
  container.appendChild(resetBtn);
  container.appendChild(popover);
}

