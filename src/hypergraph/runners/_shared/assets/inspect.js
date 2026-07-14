/* Hypergraph inspect: semantic offline renderer. */
(function () {
  "use strict";

  var script = document.currentScript;
  var root = script && script.parentElement;
  if (!root) throw new Error("Inspect renderer asset has no owning root.");
  if (root.__hgInspectBound) return;

  var payloadElement = root.querySelector("[data-hg-inspect-payload]");
  if (!payloadElement) throw new Error("Inspect renderer payload is missing.");

  var titleElement = root.querySelector("[data-hg-title]");
  var runIdElement = root.querySelector("[data-hg-run-id]");
  var deliveryElement = root.querySelector("[data-hg-delivery]");
  var deliveryLabelElement = root.querySelector("[data-hg-delivery-label]");
  var sequenceElement = root.querySelector("[data-hg-sequence]");
  var summaryElement = root.querySelector("[data-hg-summary]");
  var alertElement = root.querySelector("[data-hg-alert]");
  var alertTextElement = root.querySelector("[data-hg-alert-text]");
  var showFailureElement = root.querySelector("[data-hg-show-failure]");
  var bodyElement = root.querySelector("[data-hg-body]");
  var itemsElement = root.querySelector("[data-hg-items]");
  var itemListElement = root.querySelector("[data-hg-item-list]");
  var filterElement = root.querySelector("[data-hg-filter]");
  var previousPageElement = root.querySelector("[data-hg-prev-page]");
  var nextPageElement = root.querySelector("[data-hg-next-page]");
  var pageLabelElement = root.querySelector("[data-hg-page-label]");
  var mainElement = root.querySelector("[data-hg-main]");
  var detailElement = root.querySelector("[data-hg-detail]");
  var footerStateElement = root.querySelector("[data-hg-state-proof]");
  var footerDeliveryElement = root.querySelector("[data-hg-delivery-note]");
  var tabs = Array.prototype.slice.call(root.querySelectorAll("[data-hg-view]"));
  var panels = Array.prototype.slice.call(root.querySelectorAll("[data-hg-panel]"));

  var required = [
    titleElement, runIdElement, deliveryElement, deliveryLabelElement,
    summaryElement, alertElement, alertTextElement, showFailureElement,
    bodyElement, itemsElement, itemListElement, filterElement,
    previousPageElement, nextPageElement, pageLabelElement, mainElement,
    detailElement, footerStateElement, footerDeliveryElement,
  ];
  if (required.some(function (element) { return !element; })) {
    throw new Error("Inspect renderer markup is incomplete.");
  }

  var payload = JSON.parse(payloadElement.textContent || "{}");
  var state = {
    activeView: payload.default_view || (payload.kind === "map" ? "items" : "timeline"),
    selectedItem: null,
    selectedExecution: null,
    failureSelectionUnmatched: false,
    filter: "all",
    page: 1,
    pageSize: 20,
    detailsOpen: Object.create(null),
    tablePages: Object.create(null),
    graphViewport: {
      zoom: 100,
      panX: 0,
      panY: 0,
      scrollLeft: 0,
      scrollTop: 0,
      expanded: Object.create(null),
    },
    scroll: { items: 0, main: 0, detail: 0 },
  };

  function element(tagName, className, text) {
    var node = document.createElement(tagName);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function button(text, className, action) {
    var node = element("button", className, text);
    node.type = "button";
    if (action) node.setAttribute("data-action", action);
    return node;
  }

  function code(text) {
    return element("code", "hg-inspect-scalar", text);
  }

  function statusBadge(status) {
    var badge = element("span", "hg-inspect-badge", status || "unknown");
    badge.setAttribute("data-status", status || "unknown");
    return badge;
  }

  function summaryItem(label, value) {
    var item = element("span", "hg-inspect-summary-item");
    item.appendChild(element("span", "", label));
    item.appendChild(element("strong", "", value));
    return item;
  }

  function formatDuration(milliseconds) {
    var value = Number(milliseconds);
    if (!isFinite(value) || value < 0) return "not available";
    if (value < 1) return value.toFixed(2) + "ms";
    if (value < 100) return value.toFixed(1).replace(/\.0$/, "") + "ms";
    if (value < 1000) return Math.round(value) + "ms";
    return (value / 1000).toFixed(value < 10000 ? 2 : 1).replace(/0+$/, "").replace(/\.$/, "") + "s";
  }

  function executionId(node) {
    return [node.run_id, node.span_id, node.superstep, node.sequence].join("|");
  }

  function currentMap() {
    return payload.kind === "map" && payload.map ? payload.map : null;
  }

  function mapItems() {
    var map = currentMap();
    if (!map) return [];
    var claimed = Array.isArray(map.items) ? map.items.slice() : [];
    var unstarted = Array.isArray(map.unstarted_item_indexes) ? map.unstarted_item_indexes : [];
    unstarted.forEach(function (itemIndex) {
      claimed.push({
        item_index: itemIndex,
        status: "unstarted",
        requested_inputs: null,
        run: null,
        restored: false,
      });
    });
    claimed.sort(function (left, right) { return left.item_index - right.item_index; });
    return claimed;
  }

  function selectedMapItem() {
    var items = mapItems();
    for (var index = 0; index < items.length; index += 1) {
      if (items[index].item_index === state.selectedItem) return items[index];
    }
    return null;
  }

  function currentRun() {
    if (payload.kind === "run") return payload.run || null;
    var item = selectedMapItem();
    return item && item.run ? item.run : null;
  }

  function nodesForCurrentRun() {
    var run = currentRun();
    return run && Array.isArray(run.nodes) ? run.nodes : [];
  }

  function nodeByExecutionId(identifier) {
    var nodes = nodesForCurrentRun();
    for (var index = 0; index < nodes.length; index += 1) {
      if (executionId(nodes[index]) === identifier) return nodes[index];
    }
    return null;
  }

  function firstFailedItem() {
    var items = mapItems();
    for (var index = 0; index < items.length; index += 1) {
      var item = items[index];
      if (item.status === "failed" || (item.run && item.run.status === "failed")) return item;
    }
    return null;
  }

  function firstFailedNode(run) {
    var nodes = run && Array.isArray(run.nodes) ? run.nodes : [];
    for (var index = 0; index < nodes.length; index += 1) {
      if (nodes[index].status === "failed" || nodes[index].failure) return nodes[index];
    }
    return null;
  }

  function primaryFailedNode(run) {
    var failures = run && Array.isArray(run.failures) ? run.failures : [];
    var nodes = run && Array.isArray(run.nodes) ? run.nodes : [];
    if (!failures.length) {
      var nodesWithFailure = nodes.filter(function (node) { return Boolean(node.failure); });
      for (var embeddedIndex = 0; embeddedIndex < nodesWithFailure.length; embeddedIndex += 1) {
        var embedded = nodesWithFailure[embeddedIndex];
        if (embedded.qualified_name === embedded.failure.node_name) return embedded;
      }
      return nodesWithFailure.length ? null : firstFailedNode(run);
    }
    var failure = failures[0];
    var matching = nodes.filter(function (node) {
      return node.failure && sameFailure(failure, node.failure);
    });
    for (var index = 0; index < matching.length; index += 1) {
      if (matching[index].qualified_name === failure.node_name) return matching[index];
    }
    return null;
  }

  function normalizeState(initial) {
    if (payload.kind === "map") {
      var items = mapItems();
      var selectedExists = items.some(function (item) { return item.item_index === state.selectedItem; });
      if (!selectedExists) state.selectedItem = items.length ? items[0].item_index : null;
    } else {
      state.selectedItem = null;
      if (initial) state.activeView = "timeline";
    }
    var nodes = nodesForCurrentRun();
    var executionExists = nodes.some(function (node) { return executionId(node) === state.selectedExecution; });
    if (!executionExists) {
      state.selectedExecution = state.failureSelectionUnmatched || !nodes.length
        ? null
        : executionId(nodes[0]);
    }
  }

  function captureScroll() {
    state.scroll.items = itemsElement.scrollTop;
    state.scroll.main = mainElement.scrollTop;
    state.scroll.detail = detailElement.scrollTop;
    var viewport = root.querySelector("[data-hg-graph-viewport]");
    if (viewport) {
      state.graphViewport.scrollLeft = viewport.scrollLeft;
      state.graphViewport.scrollTop = viewport.scrollTop;
    }
  }

  function restoreScroll() {
    itemsElement.scrollTop = state.scroll.items;
    mainElement.scrollTop = state.scroll.main;
    detailElement.scrollTop = state.scroll.detail;
    var viewport = root.querySelector("[data-hg-graph-viewport]");
    if (viewport) {
      viewport.scrollLeft = state.graphViewport.scrollLeft;
      viewport.scrollTop = state.graphViewport.scrollTop;
    }
  }

  function serializedCount(value) {
    if (!value) return 0;
    if (typeof value.original_size === "number" || typeof value.original_size === "string") return value.original_size;
    if (Array.isArray(value.entries)) return value.entries.length;
    if (Array.isArray(value.items)) return value.items.length;
    return 0;
  }

  function scalarText(value) {
    if (!value) return "not available";
    if (value.kind === "null") return "null";
    if (value.kind === "boolean" || value.kind === "number") return String(value.value);
    if (value.kind === "text") return value.text || "";
    if (value.kind === "exception") return value.type_name + ": " + (value.text || "");
    if (value.kind === "placeholder") return value.type_name + ": " + (value.reason || "unavailable");
    return value.type_name || value.kind || "value";
  }

  function keyText(value) {
    return scalarText(value) || "empty key";
  }

  function addTruncation(parent, value) {
    if (!value || !value.truncated) return;
    var hasOriginalSize = typeof value.original_size === "number" || typeof value.original_size === "string";
    var original = hasOriginalSize ? "; original size " + value.original_size : "";
    parent.appendChild(element("div", "hg-inspect-truncated", "Truncated" + original + "."));
  }

  function bindDetailsState(details, path) {
    details.open = state.detailsOpen[path] === true;
    details.addEventListener("toggle", function () {
      state.detailsOpen[path] = details.open;
    });
  }

  function renderMappingRows(value, path) {
    var list = element("div", "hg-inspect-value-list");
    var entries = value && Array.isArray(value.entries) ? value.entries : [];
    if (!entries.length) list.appendChild(element("span", "hg-inspect-muted", "Empty mapping."));
    entries.forEach(function (entry, index) {
      var row = element("div", "hg-inspect-value-row");
      row.appendChild(code(keyText(entry.key)));
      row.appendChild(renderValue(entry.value, path + ".entry." + index));
      list.appendChild(row);
    });
    addTruncation(list, value);
    return list;
  }

  function renderSequence(value, path) {
    var details = element("details", "hg-inspect-value");
    var count = serializedCount(value);
    details.appendChild(element("summary", "", value.type_name + " · " + count + " item" + (count === 1 || count === "1" ? "" : "s")));
    bindDetailsState(details, path);
    var body = element("div", "hg-inspect-value-body");
    var items = Array.isArray(value.items) ? value.items : [];
    items.forEach(function (item, index) {
      var row = element("div", "hg-inspect-value-row");
      row.appendChild(code(String(index)));
      row.appendChild(renderValue(item, path + ".item." + index));
      body.appendChild(row);
    });
    addTruncation(body, value);
    details.appendChild(body);
    return details;
  }

  function renderTable(value, path) {
    var details = element("details", "hg-inspect-value");
    var table = value.table || {};
    var rowCount = typeof table.original_row_count === "number" || typeof table.original_row_count === "string" ? String(table.original_row_count) : "0";
    var columnCount = typeof table.original_column_count === "number" || typeof table.original_column_count === "string" ? String(table.original_column_count) : "0";
    var columnCountLabel = table.original_column_count_exact === false ? "≥" + columnCount : columnCount;
    details.appendChild(element("summary", "", rowCount + " × " + columnCountLabel + " table"));
    bindDetailsState(details, path);
    var body = element("div", "hg-inspect-value-body");
    var wrap = element("div", "hg-inspect-table-wrap");
    var tableElement = element("table", "hg-inspect-table");
    var head = element("thead");
    var headerRow = element("tr");
    var columns = Array.isArray(table.columns) ? table.columns : [];
    columns.forEach(function (column) {
      headerRow.appendChild(element("th", "", keyText(column)));
    });
    head.appendChild(headerRow);
    tableElement.appendChild(head);
    var tableBody = element("tbody");
    var rows = Array.isArray(table.rows) ? table.rows : [];
    var pageSize = 25;
    var page = state.tablePages[path] || 1;
    var pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
    page = Math.min(page, pageCount);
    state.tablePages[path] = page;
    rows.slice((page - 1) * pageSize, page * pageSize).forEach(function (row) {
      var tr = element("tr");
      (Array.isArray(row) ? row : []).forEach(function (cell, columnIndex) {
        var td = element("td");
        td.appendChild(renderValue(cell, path + ".cell." + columnIndex));
        tr.appendChild(td);
      });
      tableBody.appendChild(tr);
    });
    tableElement.appendChild(tableBody);
    wrap.appendChild(tableElement);
    body.appendChild(wrap);
    if (pageCount > 1) {
      var pager = element("div", "hg-inspect-pager");
      var previous = button("Prev", "hg-inspect-button", "table-prev");
      previous.disabled = page <= 1;
      previous.setAttribute("data-table-path", path);
      var next = button("Next", "hg-inspect-button", "table-next");
      next.disabled = page >= pageCount;
      next.setAttribute("data-table-path", path);
      pager.appendChild(previous);
      pager.appendChild(element("span", "", "Page " + page + " of " + pageCount));
      pager.appendChild(next);
      body.appendChild(pager);
    }
    addTruncation(body, value);
    details.appendChild(body);
    return details;
  }

  function renderValue(value, path) {
    if (!value) return element("span", "hg-inspect-muted", "not available");
    if (value.kind === "mapping") {
      var details = element("details", "hg-inspect-value");
      var count = serializedCount(value);
      details.appendChild(element("summary", "", value.type_name + " · " + count + " entr" + (count === 1 || count === "1" ? "y" : "ies")));
      bindDetailsState(details, path);
      var body = element("div", "hg-inspect-value-body");
      body.appendChild(renderMappingRows(value, path));
      details.appendChild(body);
      return details;
    }
    if (value.kind === "sequence") return renderSequence(value, path);
    if (value.kind === "table") return renderTable(value, path);
    if (value.kind === "placeholder") {
      var placeholder = element("span", "hg-inspect-placeholder", scalarText(value));
      if (value.original_size !== undefined && value.original_size !== null) {
        placeholder.appendChild(document.createTextNode(" · original size " + value.original_size));
      }
      return placeholder;
    }
    var scalar = code(scalarText(value));
    addTruncation(scalar, value);
    return scalar;
  }

  function missingCaptureMessage(node) {
    if (node.status === "restored" && !node.values_captured) return "restored values not captured";
    if (!node.values_captured) return "not captured; rerun with inspect=True";
    return "not available";
  }

  function renderCapture(label, value, node, path) {
    var block = element("div", "hg-inspect-detail-block");
    if (!value) {
      block.appendChild(element("div", "hg-inspect-section-title", label));
      block.appendChild(element("p", "hg-inspect-muted", missingCaptureMessage(node)));
      return block;
    }
    var details = element("details", "hg-inspect-capture");
    var count = serializedCount(value);
    details.appendChild(element("summary", "", label + " · " + count + " value" + (count === 1 || count === "1" ? "" : "s")));
    bindDetailsState(details, path);
    var body = element("div", "hg-inspect-capture-body");
    if (value.kind === "mapping") body.appendChild(renderMappingRows(value, path));
    else body.appendChild(renderValue(value, path));
    details.appendChild(body);
    block.appendChild(details);
    return block;
  }

  function errorText(serialized) {
    return exceptionPresentation(serialized, "Exact exception").text;
  }

  function reprWithType(typeName, reprText) {
    if (reprText.indexOf(typeName) === 0) {
      var remainder = reprText.slice(typeName.length);
      if (!remainder || " \t\r\n([{<:".indexOf(remainder.charAt(0)) !== -1) return reprText;
    }
    return typeName + ": " + reprText;
  }

  function exceptionPresentation(serialized, exactLabel) {
    if (!serialized) return { label: "Exception details unavailable", text: "Error — serialized exception unavailable" };
    var typeName = serialized.type_name || "Error";
    var kind = serialized.kind;
    if (kind === "placeholder" || ((kind === "text" || kind === "exception") && typeof serialized.text !== "string")) {
      return {
        label: "Exception details unavailable",
        text: typeName + " — " + (serialized.reason || "serialized exception text unavailable"),
      };
    }
    if (kind === "text") {
      var reprLabel = "Exception preview (bounded repr)";
      if (serialized.truncated) reprLabel = "Exception preview (bounded repr; truncated from " + (serialized.original_size || "unknown") + " characters)";
      return { label: reprLabel, text: reprWithType(typeName, serialized.text) };
    }
    if (kind === "exception") {
      var exceptionLabel = exactLabel;
      if (serialized.truncated) exceptionLabel = "Exception preview (truncated from " + (serialized.original_size || "unknown") + " characters)";
      return { label: exceptionLabel, text: typeName + ": " + serialized.text };
    }
    return { label: "Exception preview (serialized value)", text: typeName + ": " + scalarText(serialized) };
  }

  function appendException(parent, serialized, exactLabel) {
    var presentation = exceptionPresentation(serialized, exactLabel);
    var block = element("div", "hg-inspect-detail-block");
    block.appendChild(element("div", "hg-inspect-section-title", presentation.label));
    var error = element("div", "hg-inspect-error");
    error.appendChild(code(presentation.text));
    block.appendChild(error);
    parent.appendChild(block);
  }

  function appendIdentity(parent, node, relativeStart) {
    var block = element("div", "hg-inspect-detail-block");
    block.appendChild(element("div", "hg-inspect-section-title", "Execution identity"));
    var list = element("dl", "hg-inspect-kv-list");
    var pairs = [
      ["Run ID", node.run_id],
      ["Span ID", node.span_id],
      ["Graph", node.graph_name],
      ["Item", node.item_index === null || node.item_index === undefined ? "single run" : node.item_index],
      ["Superstep", node.superstep],
      ["Sequence", node.sequence],
      ["Relative start", formatDuration(relativeStart)],
      ["Duration", formatDuration(node.duration_ms)],
      ["Cache", node.cached ? "cached" : "not cached"],
    ];
    pairs.forEach(function (pair) {
      var row = element("div", "hg-inspect-kv");
      row.appendChild(element("dt", "", pair[0]));
      var value = element("dd");
      value.appendChild(code(pair[1]));
      row.appendChild(value);
      list.appendChild(row);
    });
    block.appendChild(list);
    parent.appendChild(block);
  }

  function runnerAwaitPrefix() {
    var data = payload.kind === "map" ? payload.map : payload.run;
    var runnerKind = data && data.runner_kind;
    if (runnerKind === "sync") return "";
    if (runnerKind === "async") return "await ";
    return null;
  }

  function mapRerunArguments() {
    var data = payload.map || {};
    var mapOver = Array.isArray(data.map_over) ? data.map_over : [];
    var mapOverLiteral = mapOver.length === 1
      ? JSON.stringify(mapOver[0])
      : "[" + mapOver.map(function (value) { return JSON.stringify(value); }).join(", ") + "]";
    return {
      mapOver: mapOverLiteral,
      mapMode: JSON.stringify(data.map_mode || "zip"),
    };
  }

  function rerunCallSnippet(indentation) {
    var awaitPrefix = runnerAwaitPrefix();
    if (awaitPrefix === null) return null;
    if (payload.kind === "map") {
      var args = mapRerunArguments();
      return indentation + "batch = " + awaitPrefix + "runner.map(\n"
        + indentation + "    graph,\n"
        + indentation + "    values,\n"
        + indentation + "    map_over=" + args.mapOver + ",\n"
        + indentation + "    map_mode=" + args.mapMode + ",\n"
        + indentation + "    inspect=True,\n"
        + indentation + "    error_handling=\"continue\",\n"
        + indentation + ")";
    }
    return indentation + "result = " + awaitPrefix + "runner.run(\n"
      + indentation + "    graph,\n"
      + indentation + "    values,\n"
      + indentation + "    inspect=True,\n"
      + indentation + "    error_handling=\"continue\",\n"
      + indentation + ")";
  }

  function indentSnippet(snippet) {
    return snippet.split("\n").map(function (line) { return "    " + line; }).join("\n");
  }

  function guardedRerunSnippet(settledSnippet) {
    var rerunCall = rerunCallSnippet("    ");
    if (rerunCall === null) return null;
    return "try:\n"
      + rerunCall + "\n"
      + "except Exception as error:\n"
      + "    print(f\"{type(error).__name__}: {error}\")\n"
      + "else:\n"
      + indentSnippet(settledSnippet);
  }

  function recoverySnippet(source, itemIndex) {
    if (source === "node" && payload.kind === "map") {
      return guardedRerunSnippet("failure = next(\n"
        + "    (\n"
        + "        item.failure\n"
        + "        for item in batch.failures\n"
        + "        if item.failure is not None\n"
        + "        and item.failure.item_index == " + itemIndex + "\n"
        + "    ),\n"
        + "    None,\n"
        + ")\n"
        + "if failure is None:\n"
        + "    print(batch)\n"
        + "else:\n"
        + "    print(failure.inputs)\n"
        + "    print(failure.error)");
    }
    if (source === "node") {
      return guardedRerunSnippet("failure = result.failure\n"
        + "if failure is None:\n"
        + "    print(result)\n"
        + "else:\n"
        + "    print(failure.inputs)\n"
        + "    print(failure.error)");
    }
    if (source === "run" && payload.kind === "map") {
      return guardedRerunSnippet("items = getattr(batch, \"results\", ())\n"
        + "failed = (\n"
        + "    items[" + itemIndex + "]\n"
        + "    if not getattr(batch, \"unstarted_item_indexes\", ())\n"
        + "    and 0 <= " + itemIndex + " < len(items)\n"
        + "    else None\n"
        + ")\n"
        + "if failed is None or failed.error is None:\n"
        + "    print(batch)\n"
        + "else:\n"
        + "    print(f\"{type(failed.error).__name__}: {failed.error}\")");
    }
    if (payload.kind === "map") {
      return guardedRerunSnippet("errors = [\n"
        + "    failed.error\n"
        + "    for failed in batch.failures\n"
        + "    if failed.error is not None\n"
        + "]\n"
        + "if not errors:\n"
        + "    print(batch)\n"
        + "else:\n"
        + "    for error in errors:\n"
        + "        print(f\"{type(error).__name__}: {error}\")");
    }
    return guardedRerunSnippet("if result.error is None:\n"
      + "    print(result)\n"
      + "else:\n"
      + "    print(f\"{type(result.error).__name__}: {result.error}\")");
  }

  function appendRecovery(parent, source, itemIndex) {
    var evidence = element("div", "hg-inspect-detail-block");
    var snippet = recoverySnippet(source, itemIndex);
    var codeLabel = source === "start" || source === "batch"
      ? "Smallest useful recovery code"
      : "Smallest useful evidence";

    if (snippet === null) {
      evidence.appendChild(element("div", "hg-inspect-section-title", "Recovery code unavailable"));
      evidence.appendChild(element("p", "hg-inspect-muted", "Runner kind was not captured."));
    } else {
      evidence.appendChild(element("div", "hg-inspect-section-title", codeLabel));
      var pre = element("pre", "hg-inspect-code");
      pre.appendChild(code(snippet));
      evidence.appendChild(pre);
    }
    parent.appendChild(evidence);
  }

  function appendFailure(parent, failure, itemIndex) {
    if (!failure) return;
    appendException(parent, failure.error, "Exact exception");
    appendRecovery(parent, "node", itemIndex);
  }

  function sameFailure(left, right) {
    return Boolean(
      left
      && right
      && typeof left.failure_key === "string"
      && left.failure_key
      && left.failure_key === right.failure_key
    );
  }

  function appendOrderedFailures(parent, run, selectedFailure) {
    var failures = run && Array.isArray(run.failures) ? run.failures : [];
    var removedSelected = false;
    if (selectedFailure) failures = failures.filter(function (failure) {
      if (!removedSelected && sameFailure(failure, selectedFailure)) {
        removedSelected = true;
        return false;
      }
      return true;
    });
    if (!failures.length) return;
    var block = element("div", "hg-inspect-detail-block");
    block.appendChild(element("div", "hg-inspect-section-title", "Run failures · " + failures.length));
    var list = element("ol", "hg-inspect-failure-list");
    failures.forEach(function (failure) {
      var item = element("li");
      item.appendChild(code(failure.node_name + " — " + errorText(failure.error)));
      list.appendChild(item);
    });
    block.appendChild(list);
    parent.appendChild(block);
  }

  function appendBatchError(parent) {
    var map = currentMap();
    if (!map || !map.error) return;
    appendException(parent, map.error, "Exact batch exception");
    appendRecovery(parent, "batch", null);
  }

  function renderHeader() {
    var data = payload.kind === "map" ? payload.map : payload.run;
    data = data || {};
    titleElement.textContent = data.graph_name || (payload.kind === "map" ? "Hypergraph map" : "Hypergraph run");
    runIdElement.textContent = data.run_id || "no run id";
    var delivery = payload.delivery || { state: "saved", label: "Saved snapshot" };
    root.setAttribute("data-delivery-state", delivery.state || "saved");
    deliveryLabelElement.textContent = delivery.label || "Saved snapshot";
    deliveryElement.setAttribute("data-tone", delivery.state || "saved");
    if (sequenceElement) sequenceElement.textContent = data.terminal ? "terminal" : "in progress";
    summaryElement.replaceChildren();
    if (payload.kind === "map") {
      var counts = data.counts || {};
      [
        ["Requested", counts.requested || 0],
        ["Completed", counts.completed || 0],
        ["Failed", counts.failed || 0],
        ["Running", counts.running || 0],
        ["Pending", counts.pending || 0],
        ["Unstarted", counts.unstarted || 0],
        ["Status", data.status || "unknown"],
      ].forEach(function (pair) { summaryElement.appendChild(summaryItem(pair[0], pair[1])); });
    } else {
      var nodes = Array.isArray(data.nodes) ? data.nodes : [];
      summaryElement.appendChild(summaryItem("Status", data.status || "unknown"));
      summaryElement.appendChild(summaryItem("Nodes", nodes.length));
      summaryElement.appendChild(summaryItem("Elapsed", formatDuration(data.total_duration_ms)));
    }
  }

  function renderAlert() {
    var delivery = payload.delivery || {};
    if (delivery.state === "stale") {
      alertElement.hidden = false;
      alertElement.setAttribute("data-kind", "stale");
      alertTextElement.textContent = "Live updates are unavailable. Showing the last confirmed snapshot; this view is not live.";
      showFailureElement.hidden = true;
      return;
    }
    alertElement.removeAttribute("data-kind");
    var map = currentMap();
    if (map && map.error) {
      alertElement.hidden = false;
      showFailureElement.hidden = true;
      alertTextElement.textContent = "The batch failed at its execution boundary. Exception evidence is shown in the detail panel.";
      return;
    }
    var failureItem = firstFailedItem();
    var run = payload.kind === "run" ? payload.run : failureItem && failureItem.run;
    var failureNode = primaryFailedNode(run);
    var hasPublicFailure = run && Array.isArray(run.failures) && run.failures.length;
    if (!failureNode && !(run && run.error) && !hasPublicFailure) {
      alertElement.hidden = true;
      showFailureElement.hidden = true;
      return;
    }
    alertElement.hidden = false;
    showFailureElement.hidden = false;
    var path = failureNode ? failureNode.qualified_name : "run boundary";
    alertTextElement.textContent = payload.kind === "map"
      ? "Item " + failureItem.item_index + " failed at " + path + ". Your current selection was kept."
      : "The run failed at " + path + ". Your current selection was kept.";
  }

  function renderItems() {
    var isMap = payload.kind === "map";
    itemsElement.hidden = !isMap;
    bodyElement.setAttribute("data-kind", isMap ? "map" : "run");
    tabs.forEach(function (tab) {
      if (tab.getAttribute("data-hg-view") === "items") tab.hidden = !isMap;
    });
    if (!isMap) return;
    var allItems = mapItems();
    var filtered = state.filter === "all"
      ? allItems
      : allItems.filter(function (item) { return item.status === state.filter; });
    var pageCount = Math.max(1, Math.ceil(filtered.length / state.pageSize));
    state.page = Math.max(1, Math.min(state.page, pageCount));
    var visible = filtered.slice((state.page - 1) * state.pageSize, state.page * state.pageSize);
    itemListElement.replaceChildren();
    if (!visible.length) itemListElement.appendChild(element("span", "hg-inspect-muted", "No matching items."));
    visible.forEach(function (item) {
      var row = button("", "hg-inspect-item", "select-item");
      row.setAttribute("data-item-index", String(item.item_index));
      row.setAttribute("aria-current", item.item_index === state.selectedItem ? "true" : "false");
      row.setAttribute("aria-label", "Item " + item.item_index + " " + item.status);
      row.appendChild(code("Item " + item.item_index));
      row.appendChild(statusBadge(item.status));
      itemListElement.appendChild(row);
    });
    filterElement.value = state.filter;
    pageLabelElement.textContent = "Page " + state.page + " of " + pageCount;
    previousPageElement.disabled = state.page <= 1;
    nextPageElement.disabled = state.page >= pageCount;
  }

  function renderTabs() {
    tabs.forEach(function (tab) {
      var view = tab.getAttribute("data-hg-view");
      var selected = view === state.activeView;
      tab.setAttribute("aria-selected", selected ? "true" : "false");
      tab.setAttribute("tabindex", selected ? "0" : "-1");
    });
    panels.forEach(function (panel) {
      var selected = panel.getAttribute("data-hg-panel") === state.activeView;
      panel.hidden = !selected;
    });
  }

  function renderItemOverview() {
    var panel = root.querySelector('[data-hg-panel="items"]');
    panel.replaceChildren();
    var item = selectedMapItem();
    if (!item) {
      panel.appendChild(element("p", "hg-inspect-muted", "No map item selected."));
      return;
    }
    panel.appendChild(element("div", "hg-inspect-section-title", "Item " + item.item_index));
    panel.appendChild(statusBadge(item.status));
    var captureNode = { status: item.status, values_captured: payload.map.captured };
    panel.appendChild(renderCapture("Requested map inputs", item.requested_inputs, captureNode, "item." + item.item_index + ".requested"));
    if (item.run) {
      panel.appendChild(button("Open node timeline", "hg-inspect-button", "open-timeline"));
    } else {
      panel.appendChild(element("p", "hg-inspect-muted", item.status === "unstarted" ? "This requested item was never claimed; no run history was invented." : "This item has not published a run yet."));
    }
  }

  function timelineBounds(nodes, run) {
    var starts = nodes.map(function (node) { return node.started_at_ms; }).filter(function (value) { return typeof value === "number" && isFinite(value); });
    var origin = starts.length ? Math.min.apply(Math, starts) : 0;
    var maximum = origin;
    nodes.forEach(function (node) {
      var start = typeof node.started_at_ms === "number" ? node.started_at_ms : origin;
      var end = typeof node.ended_at_ms === "number" ? node.ended_at_ms : start + Number(node.duration_ms || 0);
      maximum = Math.max(maximum, end);
    });
    var span = Math.max(1, maximum - origin, Number(run && run.total_duration_ms || 0));
    return { origin: origin, span: span };
  }

  function renderTimeline() {
    var panel = root.querySelector('[data-hg-panel="timeline"]');
    panel.replaceChildren();
    var run = currentRun();
    if (!run) {
      panel.appendChild(element("p", "hg-inspect-muted", "No run timeline exists for this item."));
      return;
    }
    var heading = payload.kind === "map" ? "Item " + state.selectedItem + " · node timeline" : "Node timeline";
    panel.appendChild(element("div", "hg-inspect-section-title", heading));
    var nodes = Array.isArray(run.nodes) ? run.nodes : [];
    if (!nodes.length) {
      panel.appendChild(element("p", "hg-inspect-muted", "No node executions were recorded."));
      return;
    }
    var bounds = timelineBounds(nodes, run);
    var list = element("div", "hg-inspect-timeline");
    nodes.forEach(function (node) {
      var identifier = executionId(node);
      var relativeStart = typeof node.started_at_ms === "number" ? Math.max(0, node.started_at_ms - bounds.origin) : 0;
      var duration = Math.max(0, Number(node.duration_ms || 0));
      var row = button("", "hg-inspect-node", "select-node");
      row.setAttribute("data-hg-timeline-row", "");
      row.setAttribute("data-execution-id", identifier);
      row.setAttribute("data-offset-ms", String(relativeStart));
      row.setAttribute("aria-current", identifier === state.selectedExecution ? "true" : "false");
      row.setAttribute("aria-label", node.qualified_name + " " + node.status + " " + formatDuration(duration));
      row.appendChild(code(node.qualified_name));
      var track = element("span", "hg-inspect-track");
      track.setAttribute("aria-hidden", "true");
      var bar = element("span", "hg-inspect-bar");
      bar.setAttribute("data-status", node.cached ? "cached" : node.status);
      bar.style.left = Math.min(100, relativeStart / bounds.span * 100) + "%";
      bar.style.width = Math.max(1.5, Math.min(100, duration / bounds.span * 100)) + "%";
      track.appendChild(bar);
      row.appendChild(track);
      var meta = element("span", "hg-inspect-kv-list");
      meta.appendChild(element("span", "", formatDuration(duration)));
      meta.appendChild(statusBadge(node.status));
      if (node.cached) meta.appendChild(statusBadge("cached"));
      row.appendChild(meta);
      list.appendChild(row);
    });
    panel.appendChild(list);
  }

  function renderGraph() {
    var panel = root.querySelector('[data-hg-panel="graph"]');
    panel.replaceChildren();
    var controls = element("div", "hg-inspect-graph-controls");
    var label = element("div");
    label.appendChild(element("div", "hg-inspect-section-title", "Executed graph paths"));
    label.appendChild(element("div", "hg-inspect-muted", "Topology context was not captured; showing truthful qualified execution paths."));
    controls.appendChild(label);
    var zoom = element("div", "hg-inspect-header-meta");
    var zoomOut = button("−", "hg-inspect-button", "zoom-out");
    zoomOut.setAttribute("aria-label", "Zoom out");
    var zoomIn = button("+", "hg-inspect-button", "zoom-in");
    zoomIn.setAttribute("aria-label", "Zoom in");
    zoom.appendChild(zoomOut);
    zoom.appendChild(element("span", "", state.graphViewport.zoom + "%"));
    zoom.appendChild(zoomIn);
    controls.appendChild(zoom);
    panel.appendChild(controls);
    var viewport = element("div", "hg-inspect-graph-viewport");
    viewport.setAttribute("data-hg-graph-viewport", "");
    var graph = element("div", "hg-inspect-graph-list");
    graph.style.transform = "translate(" + state.graphViewport.panX + "px," + state.graphViewport.panY + "px) scale(" + state.graphViewport.zoom / 100 + ")";
    graph.style.transformOrigin = "top left";
    nodesForCurrentRun().forEach(function (node) {
      var identifier = executionId(node);
      var nodeButton = button("", "hg-inspect-graph-node", "select-graph-node");
      nodeButton.setAttribute("data-execution-id", identifier);
      nodeButton.setAttribute("aria-current", identifier === state.selectedExecution ? "true" : "false");
      nodeButton.appendChild(code(node.qualified_name));
      nodeButton.appendChild(statusBadge(node.cached ? "cached" : node.status));
      graph.appendChild(nodeButton);
    });
    if (!nodesForCurrentRun().length) graph.appendChild(element("p", "hg-inspect-muted", "No executed paths were recorded."));
    viewport.appendChild(graph);
    panel.appendChild(viewport);
  }

  function renderDetail() {
    detailElement.replaceChildren();
    appendBatchError(detailElement);
    var startError = payload.message || null;
    if (startError) {
      appendException(detailElement, startError, "Exact start exception");
      appendRecovery(detailElement, "start", null);
    }
    var run = currentRun();
    var node = nodeByExecutionId(state.selectedExecution);
    if (!run) {
      var item = selectedMapItem();
      detailElement.appendChild(element("div", "hg-inspect-section-title", item ? "Item " + item.item_index : "Selection"));
      detailElement.appendChild(element("p", "hg-inspect-muted", item && item.status === "unstarted" ? "Unstarted item: no node inputs, outputs, errors, or run ID were invented." : "Select a run or item to inspect it."));
      return;
    }
    if (!node) {
      detailElement.appendChild(element("div", "hg-inspect-section-title", "Run detail"));
      if (run.error) {
        appendException(detailElement, run.error, "Exact run exception");
        appendRecovery(detailElement, "run", payload.kind === "map" ? state.selectedItem : run.item_index);
      } else if (!startError) {
        detailElement.appendChild(element("p", "hg-inspect-muted", "Select a node execution."));
      }
      appendOrderedFailures(detailElement, run);
      return;
    }
    var heading = element("div", "hg-inspect-detail-heading");
    heading.appendChild(code(node.qualified_name));
    heading.appendChild(statusBadge(node.cached ? "cached" : node.status));
    detailElement.appendChild(heading);
    var bounds = timelineBounds(Array.isArray(run.nodes) ? run.nodes : [], run);
    var relativeStart = typeof node.started_at_ms === "number" ? Math.max(0, node.started_at_ms - bounds.origin) : 0;
    appendIdentity(detailElement, node, relativeStart);
    detailElement.appendChild(renderCapture("Inputs", node.inputs || (node.failure && node.failure.inputs), node, "node." + executionId(node) + ".inputs"));
    detailElement.appendChild(renderCapture("Outputs", node.outputs, node, "node." + executionId(node) + ".outputs"));
    appendFailure(
      detailElement,
      node.failure,
      payload.kind === "map" ? state.selectedItem : node.item_index
    );
    if (!node.failure && run.error) {
      appendException(detailElement, run.error, "Exact run exception");
      appendRecovery(detailElement, "run", payload.kind === "map" ? state.selectedItem : node.item_index);
    }
    appendOrderedFailures(
      detailElement,
      run,
      node.failure
    );
  }

  function renderFooter() {
    var selection = payload.kind === "map" ? "item " + state.selectedItem + " · " : "";
    footerStateElement.textContent = "Kept locally: " + selection + state.activeView + " · execution " + (state.selectedExecution || "none") + " · filter " + state.filter + " · page " + state.page + " · graph zoom " + state.graphViewport.zoom + "%";
    var delivery = payload.delivery || {};
    if (delivery.state === "live") footerDeliveryElement.textContent = "Live payload; view state is separate from execution updates.";
    else if (delivery.state === "stale") footerDeliveryElement.textContent = "Last confirmed snapshot; updates are unavailable.";
    else footerDeliveryElement.textContent = "Saved output is locally interactive without a kernel or network.";
  }

  function render() {
    captureScroll();
    normalizeState(false);
    renderHeader();
    renderAlert();
    renderItems();
    renderTabs();
    renderItemOverview();
    renderTimeline();
    renderGraph();
    renderDetail();
    renderFooter();
    restoreScroll();
  }

  function selectFailedExecution() {
    if (payload.kind === "map") {
      var item = firstFailedItem();
      if (!item) return;
      state.selectedItem = item.item_index;
      var failedNode = primaryFailedNode(item.run);
      state.selectedExecution = failedNode ? executionId(failedNode) : null;
      state.failureSelectionUnmatched = !failedNode;
      if (failedNode) state.detailsOpen["node." + executionId(failedNode) + ".inputs"] = true;
    } else {
      var node = primaryFailedNode(payload.run);
      state.selectedExecution = node ? executionId(node) : null;
      state.failureSelectionUnmatched = !node;
      if (node) state.detailsOpen["node." + executionId(node) + ".inputs"] = true;
    }
    state.activeView = "timeline";
  }

  function handleClick(event) {
    var target = event.target.closest("[data-action]");
    if (!target || !root.contains(target)) return;
    captureScroll();
    var action = target.getAttribute("data-action");
    if (action === "view") state.activeView = target.getAttribute("data-hg-view");
    if (action === "select-item") {
      state.selectedItem = Number(target.getAttribute("data-item-index"));
      state.selectedExecution = null;
      state.failureSelectionUnmatched = false;
      normalizeState(false);
      state.activeView = "timeline";
    }
    if (action === "select-node" || action === "select-graph-node") {
      state.selectedExecution = target.getAttribute("data-execution-id");
      state.failureSelectionUnmatched = false;
    }
    if (action === "open-timeline") state.activeView = "timeline";
    if (action === "show-failure") selectFailedExecution();
    if (action === "prev-page") state.page = Math.max(1, state.page - 1);
    if (action === "next-page") state.page += 1;
    if (action === "zoom-out") state.graphViewport.zoom = Math.max(70, state.graphViewport.zoom - 10);
    if (action === "zoom-in") state.graphViewport.zoom = Math.min(150, state.graphViewport.zoom + 10);
    if (action === "table-prev" || action === "table-next") {
      var path = target.getAttribute("data-table-path");
      var current = state.tablePages[path] || 1;
      state.tablePages[path] = Math.max(1, current + (action === "table-next" ? 1 : -1));
    }
    render();
  }

  function handleChange(event) {
    if (event.target !== filterElement) return;
    state.filter = filterElement.value;
    state.page = 1;
    render();
  }

  function scriptSafeStringify(value) {
    return JSON.stringify(value).replace(/</g, "\\u003c").replace(/>/g, "\\u003e").replace(/&/g, "\\u0026");
  }

  function updatePayload(nextPayload) {
    if (!nextPayload || nextPayload.schema !== "hypergraph.inspect/v1") {
      throw new Error("Unsupported inspect payload schema.");
    }
    captureScroll();
    payload = nextPayload;
    payloadElement.textContent = scriptSafeStringify(nextPayload);
    state.failureSelectionUnmatched = false;
    normalizeState(false);
    render();
  }

  normalizeState(true);
  root.addEventListener("click", handleClick);
  root.addEventListener("change", handleChange);
  root.__hypergraphInspect = {
    updatePayload: updatePayload,
    state: state,
    payload: function () { return payload; },
  };
  root.__hgInspectBound = true;
  render();
})();
