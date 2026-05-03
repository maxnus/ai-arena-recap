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

// Server renders <time class="local-datetime" datetime="...Z">UTC text</time>.
// On load, replace the visible text with the visitor's local-time render so
// timestamps match the user's clock without server-side timezone detection.
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("time.local-datetime").forEach((el) => {
    const local = formatStarted(el.getAttribute("datetime"));
    if (local) el.textContent = local;
  });
});

function formatDuration(seconds) {
  if (seconds == null) return "";
  const total = Math.round(seconds);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
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
    let label, cls;
    if (result) {
      label = result.charAt(0).toUpperCase() + result.slice(1);
      cls = `result-${escapeHtml(result)}`;
    } else if (r.result_type === "MatchCancelled") {
      label = "Cancelled";
      cls = "result-cancelled";
    } else {
      label = "?";
      cls = "";
    }
    const changePart = change == null ? "" : ` (${change > 0 ? "+" : ""}${change})`;
    return `<span class="${cls}">${escapeHtml(label)}${escapeHtml(changePart)}</span>`;
  },
  startedAt: (params) => formatStarted(params.value),
};

// --- Bot search box (header) -----------------------------------------------
// Debounced live search against /api/bots/search.json. Renders a dropdown
// below the input; arrow keys + Enter navigate, Esc / outside-click close.
function initBotSearch() {
  const root = document.getElementById("bot-search");
  if (!root) return;
  const input = document.getElementById("bot-search-input");
  const results = document.getElementById("bot-search-results");

  let activeIndex = -1;
  let currentItems = [];
  let lastQuery = "";
  let inflight = null;
  let debounceTimer = null;

  const closeResults = () => {
    results.hidden = true;
    results.innerHTML = "";
    activeIndex = -1;
    currentItems = [];
  };

  const renderResults = (items, query) => {
    currentItems = items;
    activeIndex = items.length ? 0 : -1;
    if (!items.length) {
      results.innerHTML = `<div class="bot-search-empty">No bots match “${escapeHtml(query)}”.</div>`;
      results.hidden = false;
      return;
    }
    const html = items.map((b, i) => {
      const race = b.race ? raceBadge(b.race) : "";
      const author = b.author ? `<span class="bot-search-author">by ${escapeHtml(b.author)}</span>` : "";
      const status = b.active
        ? ""
        : `<span class="bot-search-status">${b.in_competition ? "inactive" : "off ladder"}</span>`;
      const elo = b.elo != null
        ? `<span class="bot-search-elo">${b.elo}</span>`
        : (b.highest_elo != null ? `<span class="bot-search-elo bot-search-elo-old">${b.highest_elo}</span>` : "");
      return `<a class="bot-search-row${i === 0 ? " active" : ""}" role="option" data-index="${i}" href="/bots/${b.bot_id}">
        <span class="bot-search-race">${race}</span>
        <span class="bot-search-name">${escapeHtml(b.name)}</span>
        ${author}
        ${status}
        ${elo}
      </a>`;
    }).join("");
    results.innerHTML = html;
    results.hidden = false;
  };

  const setActive = (index) => {
    const rows = results.querySelectorAll(".bot-search-row");
    if (!rows.length) return;
    activeIndex = (index + rows.length) % rows.length;
    rows.forEach((el, i) => el.classList.toggle("active", i === activeIndex));
    rows[activeIndex].scrollIntoView({ block: "nearest" });
  };

  const runSearch = async (query) => {
    if (!query) {
      closeResults();
      return;
    }
    if (inflight) inflight.abort();
    const ac = new AbortController();
    inflight = ac;
    try {
      const res = await fetch(`/api/bots/search.json?q=${encodeURIComponent(query)}&limit=20`, { signal: ac.signal });
      if (!res.ok) return;
      const body = await res.json();
      // Drop stale responses if the user kept typing.
      if (query !== lastQuery) return;
      renderResults(body.data || [], query);
    } catch (err) {
      if (err.name !== "AbortError") console.error(err);
    } finally {
      if (inflight === ac) inflight = null;
    }
  };

  input.addEventListener("input", () => {
    const q = input.value.trim();
    lastQuery = q;
    clearTimeout(debounceTimer);
    if (!q) {
      closeResults();
      return;
    }
    debounceTimer = setTimeout(() => runSearch(q), 150);
  });

  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") {
      if (results.hidden) return;
      e.preventDefault();
      setActive(activeIndex + 1);
    } else if (e.key === "ArrowUp") {
      if (results.hidden) return;
      e.preventDefault();
      setActive(activeIndex - 1);
    } else if (e.key === "Enter") {
      if (results.hidden || activeIndex < 0 || !currentItems[activeIndex]) return;
      e.preventDefault();
      window.location.href = `/bots/${currentItems[activeIndex].bot_id}`;
    } else if (e.key === "Escape") {
      closeResults();
      input.blur();
    }
  });

  input.addEventListener("focus", () => {
    if (input.value.trim() && currentItems.length) {
      results.hidden = false;
    }
  });

  document.addEventListener("click", (e) => {
    if (!root.contains(e.target)) closeResults();
  });

  // "/" anywhere on the page focuses the search box (skip if typing in another input).
  document.addEventListener("keydown", (e) => {
    if (e.key !== "/") return;
    const tag = (e.target && e.target.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA" || (e.target && e.target.isContentEditable)) return;
    e.preventDefault();
    input.focus();
    input.select();
  });
}

document.addEventListener("DOMContentLoaded", initBotSearch);

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
        api.sizeColumnsToFit();
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
    api.sizeColumnsToFit();
    refreshCheckboxes();
  });

  container.appendChild(button);
  container.appendChild(resetBtn);
  container.appendChild(popover);
}

