(function (global) {
  "use strict";

  var state = {
    payload: null,
    graphHtml: null,
    selectedNodeName: null,
    detailClosed: false,
    activeTab: "timeline",
    tablePages: {},
    root: null,
    widgetId: null,
    theme: "dark",
    themeBindingInstalled: false,
    heightObserver: null,
  };

  var PAGE_SIZE = 12;
  var ROW_H = 30;
  var LABEL_W = 160;
  var PAD_TOP = 24;
  var BAR_OFFSET = 0.22;
  var BAR_HEIGHT = 0.56;
  var STATUS_COLORS = {
    completed: "#22c55e",
    cached: "#10b981",
    failed: "#ef4444",
    running: "#3b82f6",
    paused: "#f59e0b",
    partial: "#fb923c",
    stopped: "#6b7280",
    pending: "#374151",
  };

  function esc(value) {
    return String(value === undefined || value === null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function parseColorString(value) {
    if (!value || value === "transparent" || value === "rgba(0, 0, 0, 0)") return null;
    var scratch = global.document.createElement("div");
    scratch.style.backgroundColor = value;
    scratch.style.display = "none";
    global.document.body.appendChild(scratch);
    var resolved = global.getComputedStyle(scratch).color || "";
    scratch.remove();
    var nums = resolved.match(/[\d.]+/g);
    if (nums && nums.length >= 3) {
      var r = Number(nums[0]);
      var g = Number(nums[1]);
      var b = Number(nums[2]);
      if (nums.length >= 4 && Number(nums[3]) < 0.1) return null;
      return {
        r: r,
        g: g,
        b: b,
        luminance: 0.299 * r + 0.587 * g + 0.114 * b,
      };
    }
    return null;
  }

  function detectHostTheme() {
    var attempts = [];
    var push = function (value) {
      if (value && value !== "transparent" && value !== "rgba(0, 0, 0, 0)") attempts.push(value.trim());
    };

    var parentDoc;
    try {
      parentDoc = global.parent && global.parent.document;
      if (parentDoc) {
        var rootStyle = global.getComputedStyle(parentDoc.documentElement);
        var bodyStyle = global.getComputedStyle(parentDoc.body);
        push(rootStyle.getPropertyValue("--vscode-editor-background"));
        push(rootStyle.getPropertyValue("--jp-layout-color0"));
        push(rootStyle.getPropertyValue("--jp-layout-color1"));
        push(bodyStyle.backgroundColor);
        push(rootStyle.backgroundColor);
      }
    } catch (_err) {}
    push(global.getComputedStyle(global.document.body).backgroundColor);

    var parsed = null;
    for (var i = 0; i < attempts.length; i += 1) {
      parsed = parseColorString(attempts[i]);
      if (parsed) break;
    }
    var theme = parsed && parsed.luminance > 150 ? "light" : "dark";

    try {
      parentDoc = global.parent && global.parent.document;
      if (parentDoc) {
        var jpTheme = parentDoc.body.dataset.jpThemeLight;
        if (jpTheme === "true") return "light";
        if (jpTheme === "false") return "dark";

        var bodyClass = parentDoc.body.className || "";
        if (bodyClass.includes("jp-mod-light")) return "light";
        if (bodyClass.includes("jp-mod-dark")) return "dark";

        var vscodeKind = parentDoc.body.getAttribute("data-vscode-theme-kind");
        if (vscodeKind) return vscodeKind.includes("light") ? "light" : "dark";
        if (bodyClass.includes("vscode-light")) return "light";
        if (bodyClass.includes("vscode-dark")) return "dark";

        var dataTheme = parentDoc.body.dataset.theme || parentDoc.documentElement.dataset.theme;
        var dataMode = parentDoc.body.dataset.mode || parentDoc.documentElement.dataset.mode;
        if (dataTheme === "light" || dataMode === "light") return "light";
        if (dataTheme === "dark" || dataMode === "dark") return "dark";

        var colorScheme = global.getComputedStyle(parentDoc.documentElement).getPropertyValue("color-scheme").trim();
        if (colorScheme.includes("light")) return "light";
        if (colorScheme.includes("dark")) return "dark";
      }
    } catch (_err2) {}

    if (global.matchMedia) {
      if (global.matchMedia("(prefers-color-scheme: light)").matches) return "light";
      if (global.matchMedia("(prefers-color-scheme: dark)").matches) return "dark";
    }

    return theme;
  }

  function applyTheme(theme) {
    var nextTheme = theme || "dark";
    if (state.theme === nextTheme && global.document.documentElement.getAttribute("data-hg-theme") === nextTheme) return;
    state.theme = nextTheme;
    global.document.documentElement.setAttribute("data-hg-theme", nextTheme);
    global.document.body.setAttribute("data-hg-theme", nextTheme);
    global.document.documentElement.style.colorScheme = nextTheme;
  }

  function syncTheme() {
    applyTheme(detectHostTheme());
  }

  function bindThemeSync() {
    if (state.themeBindingInstalled) return;
    state.themeBindingInstalled = true;
    syncTheme();

    try {
      var parentDoc = global.parent && global.parent.document;
      if (parentDoc && global.MutationObserver) {
        var observer = new global.MutationObserver(syncTheme);
        observer.observe(parentDoc.body, {
          attributes: true,
          attributeFilter: ["class", "data-vscode-theme-kind", "data-theme", "data-mode", "data-jp-theme-light", "style"],
        });
        observer.observe(parentDoc.documentElement, {
          attributes: true,
          attributeFilter: ["class", "data-vscode-theme-kind", "data-theme", "data-mode", "data-jp-theme-light", "style"],
        });
      }
    } catch (_err3) {}

    if (global.matchMedia) {
      var media = global.matchMedia("(prefers-color-scheme: dark)");
      if (media.addEventListener) {
        media.addEventListener("change", syncTheme);
      } else if (media.addListener) {
        media.addListener(syncTheme);
      }
    }
  }

  function attr(value) {
    return esc(value).replace(/"/g, "&quot;");
  }

  function formatDuration(ms) {
    if (ms === null || ms === undefined || Number.isNaN(Number(ms))) return "—";
    var value = Number(ms);
    if (value < 1000) return value.toFixed(value < 10 ? 2 : value < 100 ? 1 : 0) + "ms";
    return (value / 1000).toFixed(value < 10_000 ? 2 : 1) + "s";
  }

  function statusTone(status) {
    return ({
      completed: "completed",
      cached: "cached",
      running: "running",
      failed: "failed",
      stopped: "stopped",
      paused: "paused",
      partial: "partial",
    })[status] || "neutral";
  }

  function defaultSelection(payload) {
    if (payload.failure && payload.failure.node_name) return payload.failure.node_name;
    if (payload.nodes && payload.nodes.length) return payload.nodes[payload.nodes.length - 1].node_name;
    return null;
  }

  function fallbackTimedNodes(nodes, totalDurationMs) {
    var cursor = 0;
    return (nodes || []).map(function (node) {
      var started = node.timeline_started_at_ms;
      var ended = node.timeline_ended_at_ms;
      var duration = Number(node.duration_ms || 0);
      if (started === null || started === undefined) started = cursor;
      if (ended === null || ended === undefined) ended = started + duration;
      cursor = Math.max(cursor, ended);
      return Object.assign({}, node, {
        started_at_ms: started,
        ended_at_ms: ended,
      });
    });
  }

  function timelineNodes(payload) {
    return fallbackTimedNodes(payload.nodes || [], payload.total_duration_ms || 0);
  }

  function timelineExtent(payload) {
    var nodes = timelineNodes(payload);
    var maxEnd = Number(payload.timeline_total_duration_ms || 0);
    for (var i = 0; i < nodes.length; i += 1) {
      maxEnd = Math.max(maxEnd, Number(nodes[i].ended_at_ms || 0));
    }
    return Math.max(maxEnd, 1);
  }

  function inlineSummary(value) {
    if (!value) return "—";
    if (value.kind === "null") return "null";
    if (value.kind === "boolean" || value.kind === "number") return esc(String(value.value));
    if (value.kind === "text" || value.kind === "markdown") return esc(value.preview || value.text || "");
    if (value.kind === "image") return "image";
    if (value.kind === "table") return value.row_count + " rows";
    if (value.kind === "array") return value.length + " items";
    if (value.kind === "mapping" || value.kind === "dataclass" || value.kind === "pydantic") return value.length + " fields";
    return esc(value.summary || value.type_name || value.kind);
  }

  function renderScalar(value) {
    if (value.kind === "null") return '<span class="hg-scalar hg-null">null</span>';
    if (value.kind === "boolean") return '<span class="hg-scalar hg-bool">' + esc(String(value.value)) + "</span>";
    if (value.kind === "number") return '<span class="hg-scalar hg-num">' + esc(String(value.value)) + "</span>";
    return '<span class="hg-scalar">' + esc(String(value.value)) + "</span>";
  }

  function renderTextValue(value) {
    var meta = '<div class="hg-value-meta">' + esc(value.type_name || "text");
    if (value.length !== undefined) meta += " • " + esc(String(value.length)) + " chars";
    if (value.truncated) meta += " • capture trimmed";
    meta += "</div>";
    if (!value.preview || value.preview === value.text) {
      return meta + '<pre class="hg-text-block">' + esc(value.text || "") + "</pre>";
    }
    return (
      '<details class="hg-details"><summary>' +
      esc(value.preview) +
      "</summary>" +
      meta +
      '<pre class="hg-text-block">' +
      esc(value.text || "") +
      "</pre></details>"
    );
  }

  function renderMarkdownValue(value) {
    var meta = '<div class="hg-value-meta">markdown';
    if (value.length !== undefined) meta += " • " + esc(String(value.length)) + " chars";
    if (value.truncated) meta += " • capture trimmed";
    meta += "</div>";
    return (
      '<details class="hg-details" open><summary>' +
      esc(value.preview || "Markdown") +
      "</summary>" +
      meta +
      '<div class="hg-markdown">' +
      (value.html || "") +
      "</div></details>"
    );
  }

  function renderImageValue(value) {
    return (
      '<div class="hg-image-wrap"><img class="hg-image" src="' +
      attr(value.src || "") +
      '" alt="Inspect output image" loading="lazy" /></div>'
    );
  }

  function renderArrayValue(value, path) {
    var items = value.items || [];
    var body = items
      .map(function (item, index) {
        return (
          '<div class="hg-tree-row"><span class="hg-tree-key">[' +
          index +
          ']</span><div class="hg-tree-value">' +
          renderValue(item, path + ".item" + index) +
          "</div></div>"
        );
      })
      .join("");
    var summary = (value.preview || value.length + " items") + (value.truncated ? " • capture trimmed" : "");
    return (
      '<details class="hg-details"><summary>' +
      esc(summary) +
      "</summary><div class=\"hg-tree\">" +
      body +
      "</div></details>"
    );
  }

  function renderMappingValue(value, path) {
    var entries = value.entries || [];
    var summary = (value.type_name || value.kind) + " • " + value.length + " fields";
    if (value.truncated) summary += " • capture trimmed";
    var body = entries
      .map(function (entry, index) {
        return (
          '<div class="hg-tree-row"><span class="hg-tree-key">' +
          esc(entry.key) +
          '</span><div class="hg-tree-value">' +
          renderValue(entry.value, path + ".entry" + index) +
          "</div></div>"
        );
      })
      .join("");
    return (
      '<details class="hg-details" open><summary>' +
      esc(summary) +
      "</summary><div class=\"hg-tree\">" +
      body +
      "</div></details>"
    );
  }

  function renderTableValue(value, path) {
    var rows = value.rows || [];
    var columns = value.columns || [];
    var page = state.tablePages[path] || 0;
    var totalPages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
    if (page >= totalPages) {
      page = totalPages - 1;
      state.tablePages[path] = page;
    }
    var start = page * PAGE_SIZE;
    var pageRows = rows.slice(start, start + PAGE_SIZE);
    var head = columns.map(function (column) { return "<th>" + esc(column) + "</th>"; }).join("");
    var body = pageRows
      .map(function (row) {
        var cells = columns
          .map(function (column) {
            return "<td>" + renderInlineCell(row[column]) + "</td>";
          })
          .join("");
        return "<tr>" + cells + "</tr>";
      })
      .join("");
    var footer = '<div class="hg-table-footer"><span>' +
      esc(String(value.row_count)) +
      " rows" +
      (value.truncated ? " • capture trimmed" : "") +
      '</span><span class="hg-table-controls">' +
      '<button type="button" class="hg-mini-btn" data-table-prev="' + attr(path) + '"' + (page <= 0 ? " disabled" : "") + ">Prev</button>" +
      '<span>Page ' + (page + 1) + " / " + totalPages + "</span>" +
      '<button type="button" class="hg-mini-btn" data-table-next="' + attr(path) + '"' + (page >= totalPages - 1 ? " disabled" : "") + ">Next</button>" +
      "</span></div>";
    return (
      '<div class="hg-table-wrap"><table class="hg-data-table"><thead><tr>' +
      head +
      "</tr></thead><tbody>" +
      body +
      "</tbody></table>" +
      footer +
      "</div>"
    );
  }

  function renderInlineCell(value) {
    if (!value) return "—";
    if (value.kind === "boolean" || value.kind === "number" || value.kind === "null") return renderScalar(value);
    if (value.kind === "image") return '<span class="hg-inline-chip">image</span>';
    return '<span class="hg-inline-text">' + esc(value.preview || value.summary || value.type_name || value.kind) + "</span>";
  }

  function renderValue(value, path) {
    if (!value) return '<span class="hg-inline-text">—</span>';
    if (value.kind === "number" || value.kind === "boolean" || value.kind === "null") return renderScalar(value);
    if (value.kind === "text") return renderTextValue(value);
    if (value.kind === "markdown") return renderMarkdownValue(value);
    if (value.kind === "image") return renderImageValue(value);
    if (value.kind === "table") return renderTableValue(value, path);
    if (value.kind === "array") return renderArrayValue(value, path);
    if (value.kind === "mapping" || value.kind === "dataclass" || value.kind === "pydantic") return renderMappingValue(value, path);
    return '<span class="hg-inline-text">' + esc(value.summary || value.type_name || value.kind) + "</span>";
  }

  function renderFailure(payload) {
    return "";
  }

  function renderSummary(payload) {
    return "";
  }

  function renderStatusBadge(status) {
    var tone = statusTone(status);
    return '<span class="badge badge-' + esc(tone) + '"><span class="dot"></span>' + esc(String(status || "unknown").toUpperCase()) + "</span>";
  }

  function renderControls(payload) {
    var tabs = ['<button type="button" class="speed-btn' + (state.activeTab === "timeline" ? " active" : "") + '" data-tab="timeline">Timeline</button>'];
    tabs.push('<button type="button" class="speed-btn' + (state.activeTab === "values" ? " active" : "") + '" data-tab="values">Values</button>');
    if (state.graphHtml) {
      tabs.push('<button type="button" class="speed-btn' + (state.activeTab === "graph" ? " active" : "") + '" data-tab="graph">Graph</button>');
    }
    var counts = esc(String((payload.nodes || []).length)) + " nodes";
    var activeDuration = formatDuration(payload.timeline_total_duration_ms || payload.total_duration_ms);
    var wallDuration = formatDuration(payload.total_duration_ms);
    var timing = activeDuration === wallDuration ? activeDuration : activeDuration + " active / " + wallDuration + " wall";
    var selection = esc(state.selectedNodeName || "no selection");
    return (
      '<div class="controls">' +
      '<div class="speed-group">' + tabs.join("") + "</div>" +
      '<div class="divider"></div>' +
      '<span class="sub">' + counts + "</span>" +
      '<div class="divider"></div>' +
      '<span class="sub">' + timing + "</span>" +
      '<div class="divider"></div>' +
      '<span class="sub">' + selection + "</span>" +
      "</div>"
    );
  }

  function renderTimeline(payload) {
    var nodes = timelineNodes(payload);
    var total = timelineExtent(payload);
    var totalHeight = nodes.length * ROW_H + PAD_TOP + 10;
    var headerDuration = formatDuration(payload.timeline_total_duration_ms || payload.total_duration_ms);

    if (!nodes.length) {
      return (
        '<section class="gantt-panel">' +
        '<div class="gantt-bar-top"><span>Execution timeline</span><span>' + esc(headerDuration) + '</span></div>' +
        '<div class="gantt-scroll"><div class="hg-empty-state">The run is live, but no node snapshots have been captured yet.</div></div>' +
        "</section>"
      );
    }

    var labels = '<div style="height:' + PAD_TOP + 'px"></div>';
    var svg = '<svg width="100%" height="' + totalHeight + '">';
    var ticks = [0, 25, 50, 75, 100];

    for (var t = 0; t < ticks.length; t += 1) {
      var pct = ticks[t];
      var anchor = pct === 0 ? "start" : pct === 100 ? "end" : "middle";
      svg += '<line x1="' + pct + '%" y1="0" x2="' + pct + '%" y2="' + totalHeight + '" stroke="rgba(255,255,255,0.05)" stroke-width="1"></line>';
      svg += '<text x="' + pct + '%" y="14" text-anchor="' + anchor + '" fill="#4b5563" font-size="10" font-family="JetBrains Mono, monospace">' + esc((total * pct / 100 / 1000).toFixed(2) + "s") + "</text>";
    }

    for (var i = 0; i < nodes.length; i += 1) {
      var node = nodes[i];
      var y = PAD_TOP + i * ROW_H;
      var barY = y + ROW_H * BAR_OFFSET;
      var barHeight = ROW_H * BAR_HEIGHT;
      var selected = node.node_name === state.selectedNodeName && !state.detailClosed;
      var start = Number(node.started_at_ms || 0);
      var end = Number(node.ended_at_ms || start + Number(node.duration_ms || 0));
      var startPct = total <= 0 ? 0 : (start / total) * 100;
      var endPct = total <= 0 ? 0.5 : Math.max(startPct + 0.5, (end / total) * 100);
      var widthPct = Math.max(endPct - startPct, 0.5);
      var color = STATUS_COLORS[node.status] || STATUS_COLORS.pending;
      var labelClass = "name" + (node.status === "failed" ? " failed" : "");

      labels += '<div class="label-row' + (selected ? " selected" : "") + '" style="height:' + ROW_H + 'px" data-node-name="' + attr(node.node_name) + '">' +
        '<span style="width:16px;display:flex;align-items:center;justify-content:center;flex-shrink:0"><span class="dot"></span></span>' +
        '<span class="' + labelClass + '">' + esc(node.node_name) + "</span>" +
        '<div style="flex:1"></div>' +
        '<span class="si si-' + esc(statusTone(node.status)) + '"></span>' +
        "</div>";

      if (selected) {
        svg += '<rect x="0" y="' + y + '" width="100%" height="' + ROW_H + '" fill="rgba(255,255,255,0.05)"></rect>';
      }

      svg += '<rect x="0" y="' + barY + '" width="100%" height="' + barHeight + '" fill="rgba(255,255,255,0.02)" rx="2"></rect>';
      svg += '<rect x="' + startPct + '%" y="' + barY + '" width="' + widthPct + '%" height="' + barHeight + '" rx="3" fill="' + color + '" opacity="' + (selected ? "1" : "0.8") + '"';
      if (selected) {
        svg += ' stroke="white" stroke-width="1.5"';
      }
      svg += "></rect>";

      if (node.duration_ms && widthPct > 8) {
        svg += '<text x="' + (startPct + widthPct / 2) + '%" y="' + (barY + barHeight / 2 + 4) + '" text-anchor="middle" fill="white" font-size="10" font-family="JetBrains Mono, monospace" style="pointer-events:none">' + esc(formatDuration(node.duration_ms)) + "</text>";
      }

      svg += '<line x1="0" y1="' + (y + ROW_H - 0.5) + '" x2="100%" y2="' + (y + ROW_H - 0.5) + '" stroke="rgba(255,255,255,0.04)" stroke-width="1"></line>';
    }

    svg += "</svg>";
    return (
      '<section class="gantt-panel">' +
      '<div class="gantt-bar-top"><span>Execution timeline</span><span>' + esc(headerDuration) + '</span></div>' +
      '<div class="gantt-scroll"><div class="gantt-flex">' +
      '<div class="label-col">' + labels + "</div>" +
      '<div class="bar-col">' + svg + "</div>" +
      "</div></div></section>"
    );
  }

  function selectedNode(payload) {
    var nodes = payload.nodes || [];
    for (var i = 0; i < nodes.length; i += 1) {
      if (nodes[i].node_name === state.selectedNodeName) return nodes[i];
    }
    return nodes.length ? nodes[nodes.length - 1] : null;
  }

  function renderNodeDetail(payload) {
    var node = selectedNode(payload);
    if (!node || state.detailClosed) {
      return '<aside class="detail"><div class="detail-inner"></div></aside>';
    }
    var sections = "";
    if (node.error) {
      sections += '<div class="detail-sect"><div class="detail-sect-title">Error</div><div class="hg-error-block">' + esc(node.error) + "</div></div>";
    }
    sections += '<div class="detail-sect"><div class="detail-sect-title">Inputs</div>' + renderValue(node.inputs, "node.inputs." + node.node_name) + "</div>";
    sections += '<div class="detail-sect"><div class="detail-sect-title">Outputs</div>' + renderValue(node.outputs, "node.outputs." + node.node_name) + "</div>";
    return (
      '<aside class="detail open">' +
      '<div class="detail-inner">' +
      '<div class="detail-head">' +
      '<h3>' + esc(node.node_name) + '</h3>' +
      '<button type="button" class="detail-close" data-close-detail="1">✕</button>' +
      "</div>" +
      '<div class="detail-body">' +
      '<div class="detail-sect">' +
      '<div class="detail-sect-title">Status</div>' +
      '<div class="detail-kv"><span class="k">Status</span><span class="v">' + renderStatusBadge(node.status) + "</span></div>" +
      '<div class="detail-kv"><span class="k">Duration</span><span class="v">' + esc(formatDuration(node.duration_ms)) + "</span></div>" +
      '<div class="detail-kv"><span class="k">Superstep</span><span class="v">' + esc(String(node.superstep)) + "</span></div>" +
      "</div>" +
      sections +
      "</div></div></aside>"
    );
  }

  function renderValuesIndex(payload) {
    var nodes = payload.nodes || [];
    if (!nodes.length) {
      return '<section class="gantt-panel"><div class="gantt-bar-top"><span>Values</span><span>0 nodes</span></div><div class="hg-empty-state">No node values are available yet.</div></section>';
    }
    var cards = nodes
      .map(function (node) {
        return (
          '<button type="button" class="hg-value-card' + (node.node_name === state.selectedNodeName ? " is-selected" : "") + '" data-node-name="' + attr(node.node_name) + '">' +
          '<div class="hg-value-card-head"><span>' + esc(node.node_name) + "</span>" + renderStatusBadge(node.status) + "</div>" +
          '<div class="hg-value-card-body"><div><span class="hg-muted">inputs</span> ' + inlineSummary(node.inputs) + '</div><div><span class="hg-muted">outputs</span> ' + inlineSummary(node.outputs) + "</div></div>" +
          "</button>"
        );
      })
      .join("");
    return '<section class="gantt-panel"><div class="gantt-bar-top"><span>Values</span><span>' + esc(String(nodes.length)) + ' nodes</span></div><div class="gantt-scroll"><div class="hg-value-grid">' + cards + "</div></div></section>";
  }

  function renderGraphPanel() {
    if (!state.graphHtml) {
      return '<section class="gantt-panel"><div class="hg-empty-state">Graph view is unavailable for this inspect artifact.</div></section>';
    }
    return (
      '<section class="gantt-panel hg-graph-panel">' +
      '<iframe class="hg-graph-frame" srcdoc="' + attr(state.graphHtml) + '" sandbox="allow-scripts allow-same-origin"></iframe>' +
      "</section>"
    );
  }

  function renderBody(payload) {
    if (state.activeTab === "graph") {
      return '<div class="main main--graph">' + renderGraphPanel() + renderNodeDetail(payload) + "</div>";
    }
    if (state.activeTab === "values") {
      return '<div class="main main--values">' + renderValuesIndex(payload) + renderNodeDetail(payload) + "</div>";
    }
    return '<div class="main main--timeline">' + renderTimeline(payload) + renderNodeDetail(payload) + "</div>";
  }

  function ensureSelection() {
    if (!state.payload) return;
    var nodes = state.payload.nodes || [];
    if (!nodes.length) {
      state.selectedNodeName = null;
      return;
    }
    if (!state.selectedNodeName || !nodes.some(function (node) { return node.node_name === state.selectedNodeName; })) {
      state.selectedNodeName = defaultSelection(state.payload);
      state.detailClosed = false;
    }
  }

  function syncFrameHeight() {
    var frame = global.frameElement;
    if (!frame) return;
    var body = global.document.body;
    var docEl = global.document.documentElement;
    var content = state.root && state.root.firstElementChild ? state.root.firstElementChild : state.root;
    var contentHeight = Math.max(
      content ? Math.ceil(content.getBoundingClientRect().height) : 0,
      content ? content.scrollHeight : 0
    );
    var documentHeight = Math.max(
      body ? body.scrollHeight : 0,
      body ? body.offsetHeight : 0,
      docEl ? docEl.scrollHeight : 0,
      docEl ? docEl.offsetHeight : 0
    );
    var nextHeight = contentHeight || documentHeight;
    if (!nextHeight) return;
    var padded = Math.max(260, nextHeight + 2);
    frame.style.height = padded + "px";
    frame.style.minHeight = "260px";
    frame.setAttribute("height", String(padded));
  }

  function scheduleFrameHeightSync() {
    if (global.requestAnimationFrame) {
      global.requestAnimationFrame(syncFrameHeight);
      return;
    }
    global.setTimeout(syncFrameHeight, 0);
  }

  function bindFrameHeightSync() {
    if (state.heightObserver || !global.ResizeObserver) return;
    state.heightObserver = new global.ResizeObserver(function () {
      scheduleFrameHeightSync();
    });
    state.heightObserver.observe(global.document.body);
    state.heightObserver.observe(global.document.documentElement);
  }

  function render() {
    if (!state.root || !state.payload) return;
    syncTheme();
    ensureSelection();
    var payload = state.payload;
    state.root.innerHTML =
      '<div class="app">' +
      '<div class="header">' +
      '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color:#60a5fa"><path d="M3 3v18h18"></path><path d="M7 16l4-8 4 4 4-8"></path></svg>' +
      '<h1>Inspect</h1><span class="sep">/</span><span class="sub">' + esc(payload.run_id) + '</span><span class="sub">' + esc((payload.status || "running") === "running" ? "live" : "saved") + '</span>' +
      '<div style="margin-left:auto">' + renderStatusBadge(payload.status) + "</div>" +
      "</div>" +
      renderControls(payload) +
      renderBody(payload) +
      "</div>";
    scheduleFrameHeightSync();
  }

  function handleClick(event) {
    var target = event.target;
    var tab = target.closest("[data-tab]");
    if (tab) {
      state.activeTab = tab.getAttribute("data-tab");
      render();
      return;
    }
    var row = target.closest("[data-node-name]");
    if (row) {
      state.selectedNodeName = row.getAttribute("data-node-name");
      state.detailClosed = false;
      render();
      return;
    }
    var close = target.closest("[data-close-detail]");
    if (close) {
      state.detailClosed = true;
      render();
      return;
    }
    var next = target.closest("[data-table-next]");
    if (next) {
      var nextPath = next.getAttribute("data-table-next");
      state.tablePages[nextPath] = (state.tablePages[nextPath] || 0) + 1;
      render();
      return;
    }
    var prev = target.closest("[data-table-prev]");
    if (prev) {
      var prevPath = prev.getAttribute("data-table-prev");
      state.tablePages[prevPath] = Math.max(0, (state.tablePages[prevPath] || 0) - 1);
      render();
    }
  }

  function bind() {
    if (!state.root || state.root.__hypergraphInspectBound) return;
    state.root.__hypergraphInspectBound = true;
    bindThemeSync();
    bindFrameHeightSync();
    state.root.addEventListener("click", handleClick);
    global.addEventListener("resize", scheduleFrameHeightSync);
    global.addEventListener("message", function (event) {
      var data = event && event.data;
      if (!data || data.type !== "hypergraph-inspect-update") return;
      if (state.widgetId && data.widgetId && data.widgetId !== state.widgetId) return;
      if (data.payload) {
        state.payload = data.payload;
        render();
      }
    });
  }

  function init(config) {
    state.root = global.document.getElementById(config.rootId);
    state.widgetId = config.widgetId || null;
    state.graphHtml = config.graphHtml || null;
    state.payload = config.payload || null;
    state.selectedNodeName = defaultSelection(state.payload || {});
    state.detailClosed = false;
    bind();
    render();
    scheduleFrameHeightSync();
  }

  function update(payload) {
    state.payload = payload;
    render();
  }

  global.HypergraphInspect = {
    init: init,
    update: update,
  };
})(window);
