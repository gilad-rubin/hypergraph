/* Hypergraph inspect: authenticated offline notebook shell/channel bridge. */
(function (global) {
  "use strict";

  var VERSION = 1;
  var READY = "hypergraph.inspect.ready";
  var UPDATE = "hypergraph.inspect.update";
  var RESIZE = "hypergraph.inspect.resize";

  if (global.__hypergraphInspectTransport && global.__hypergraphInspectTransport.version === VERSION) {
    return;
  }

  var hosts = global.__hypergraphInspectHosts || Object.create(null);
  var queues = global.__hypergraphInspectQueues || Object.create(null);
  global.__hypergraphInspectHosts = hosts;
  global.__hypergraphInspectQueues = queues;

  function keyOf(widgetId, nonce) {
    return widgetId + "::" + nonce;
  }

  function exactIdentity(message, config) {
    return message
      && message.version === VERSION
      && message.widget_id === config.widgetId
      && message.nonce === config.nonce;
  }

  function hideChannelFallback(channelId) {
    if (!channelId) return;
    var channel = global.document.getElementById(channelId);
    if (!channel) return;
    var fallback = channel.querySelector("[data-hg-inspect-channel-fallback]");
    if (fallback) fallback.hidden = true;
    channel.setAttribute("data-delivered", "true");
  }

  function installParent(config) {
    var frame = global.document.getElementById(config.frameId);
    var status = global.document.getElementById(config.statusId);
    if (!frame || !status) throw new Error("Inspect notebook shell is incomplete.");

    var key = keyOf(config.widgetId, config.nonce);
    var state = {
      ready: false,
      readyCount: 0,
      lastSentSequence: 0,
      deliver: deliver,
    };
    hosts[key] = state;

    function deliver(envelope, channelId) {
      if (!exactIdentity(envelope, config) || envelope.type !== UPDATE) return false;
      if (!Number.isInteger(envelope.sequence) || envelope.sequence <= state.lastSentSequence) return false;
      queues[key] = { envelope: envelope, channelId: channelId };
      if (!state.ready || !frame.contentWindow) return false;

      // sandbox="allow-scripts" gives srcdoc an opaque origin, so there is no
      // stable target origin. Exact contentWindow source checks plus version,
      // widget ID, nonce, and monotonic sequence are the authentication boundary.
      frame.contentWindow.postMessage(envelope, "*");
      state.lastSentSequence = envelope.sequence;
      // A pre-start failure has no RunInspection failure evidence by design.
      // Keep its bounded exact error fallback visible beside the generic stale
      // renderer banner instead of hiding the only truthful error surface.
      if (envelope.message === null || envelope.message === undefined) {
        hideChannelFallback(channelId);
      }
      return true;
    }

    function receive(event) {
      // A same-page widget with copied labels is not the expected iframe.
      if (event.source !== frame.contentWindow) return;
      var message = event.data;
      if (!exactIdentity(message, config)) return;

      if (message.type === READY) {
        state.ready = true;
        state.readyCount += 1;
        status.hidden = true;
        status.setAttribute("data-state", "ready");
        var queued = queues[key];
        if (queued) deliver(queued.envelope, queued.channelId);
        return;
      }

      if (message.type === RESIZE && Number.isFinite(message.height)) {
        var height = Math.max(280, Math.min(2000, Math.ceil(message.height)));
        frame.style.height = height + "px";
      }
    }

    global.addEventListener("message", receive);
    global.setTimeout(function () {
      if (state.ready) return;
      status.hidden = false;
      status.setAttribute("data-state", "stale");
      status.textContent = "The interactive inspector did not connect. Showing the latest saved snapshot below; this view is not live.";
    }, config.handshakeTimeoutMs);
  }

  function installChild(config) {
    var root = global.document.querySelector("[data-hypergraph-inspect]");
    if (!root || !root.__hypergraphInspect) {
      throw new Error("Inspect renderer was not ready for the notebook bridge.");
    }
    var expectedKind = root.getAttribute("data-hypergraph-inspect");
    var state = {
      lastSequence: 0,
      accepted: 0,
      rejected: 0,
    };
    global.__hypergraphInspectBridgeState = state;

    function reject() {
      state.rejected += 1;
    }

    function receive(event) {
      // Updates are accepted only from this iframe's direct notebook parent.
      if (event.source !== global.parent) {
        reject();
        return;
      }
      var message = event.data;
      if (!exactIdentity(message, config) || message.type !== UPDATE) {
        reject();
        return;
      }
      if (!Number.isInteger(message.sequence) || message.sequence <= state.lastSequence) {
        reject();
        return;
      }
      var payload = message.payload;
      if (!payload || payload.schema !== "hypergraph.inspect/v1" || payload.kind !== expectedKind) {
        reject();
        return;
      }

      try {
        root.__hypergraphInspect.updatePayload(payload);
      } catch (_error) {
        reject();
        return;
      }
      state.lastSequence = message.sequence;
      state.accepted += 1;
    }

    function post(message) {
      // The parent notebook has no stable origin from an opaque sandboxed srcdoc.
      // Its receiver authenticates this exact contentWindow and full identity.
      global.parent.postMessage(message, "*");
    }

    function reportHeight() {
      post({
        type: RESIZE,
        version: VERSION,
        widget_id: config.widgetId,
        nonce: config.nonce,
        height: global.document.documentElement.scrollHeight,
      });
    }

    global.addEventListener("message", receive);
    post({
      type: READY,
      version: VERSION,
      widget_id: config.widgetId,
      nonce: config.nonce,
    });
    reportHeight();
    if (typeof global.ResizeObserver === "function") {
      new global.ResizeObserver(reportHeight).observe(global.document.body);
    }
  }

  global.__hypergraphInspectTransport = {
    version: VERSION,
    installParent: installParent,
    installChild: installChild,
  };
})(window);
