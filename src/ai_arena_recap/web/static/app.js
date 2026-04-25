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

function resultCell(cell) {
  const v = cell.getValue();
  return v ? `<span class="result-${escapeHtml(v)}">${escapeHtml(v)}</span>` : "";
}

function eloChangeCell(cell) {
  const v = cell.getValue();
  if (v == null) return "";
  const cls = v > 0 ? "up" : v < 0 ? "down" : "";
  const sign = v > 0 ? "+" : "";
  return `<span class="${cls}">${sign}${v}</span>`;
}

function formatStarted(cell) {
  const v = cell.getValue();
  if (!v) return "";
  const d = new Date(v);
  if (isNaN(d)) return v;
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// Tabulator 6 column-header menu: returns a list of checkbox-style entries,
// one per column, that toggle the visibility of that column when clicked.
// Tabulator calls this with (event, column); we ignore the event.
function columnVisibilityMenu(_e, headerColumn) {
  const table = headerColumn.getTable();
  const menu = [];
  for (const column of table.getColumns()) {
    const def = column.getDefinition();
    if (!def.title) continue; // skip unlabeled columns
    const visible = column.isVisible();
    menu.push({
      label: `${visible ? "☑" : "☐"} ${def.title}`,
      action: function (event) {
        event.stopPropagation();
        column.toggle();
      },
    });
  }
  return menu;
}
