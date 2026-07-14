/* Hypergraph inspect: authenticated offline notebook shell/channel bridge. */
(function (global) {
  "use strict";

  var VERSION = 1;
  var READY = "hypergraph.inspect.ready";
  var UPDATE = "hypergraph.inspect.update";
  var ACCEPTED = "hypergraph.inspect.accepted";
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

  function hideSupersededChannel(channelId) {
    if (!channelId) return;
    var channel = global.document.getElementById(channelId);
    if (!channel) return;
    hideChannelFallback(channelId);
    var message = channel.querySelector("[data-hg-inspect-channel-message]");
    if (message) message.hidden = true;
  }

  function markLiveChannelFallback(channelId, envelope, labelText, stateText) {
    var delivery = envelope && envelope.payload && envelope.payload.delivery;
    if (!channelId || !delivery || delivery.state !== "live") return;
    var channel = global.document.getElementById(channelId);
    if (!channel) return;
    var fallback = channel.querySelector("[data-hg-inspect-channel-fallback]");
    if (!fallback) return;
    var label = fallback.querySelector("strong");
    if (label) label.textContent = labelText;
    fallback.hidden = false;
    fallback.setAttribute("data-delivery-state", stateText);
  }

  function installParent(config) {
    var frame = global.document.getElementById(config.frameId);
    var status = global.document.getElementById(config.statusId);
    if (!frame || !status) throw new Error("Inspect notebook shell is incomplete.");

    var key = keyOf(config.widgetId, config.nonce);
    var state = {
      ready: false,
      readyCount: 0,
      handshakeTimedOut: false,
      lastPostedSequence: 0,
      lastPostedChannelId: null,
      lastAcceptedSequence: 0,
      pendingChannels: Object.create(null),
      deliver: deliver,
    };
    hosts[key] = state;
    var queuedBeforeReady = queues[key];
    if (queuedBeforeReady) {
      markLiveChannelFallback(
        queuedBeforeReady.channelId,
        queuedBeforeReady.envelope,
        "Waiting for live inspection",
        "waiting"
      );
    }

    function deliver(envelope, channelId) {
      if (!exactIdentity(envelope, config) || envelope.type !== UPDATE) return false;
      if (!Number.isInteger(envelope.sequence)) return false;
      if (envelope.sequence < state.lastPostedSequence) {
        hideSupersededChannel(channelId);
        return false;
      }
      if (envelope.sequence === state.lastPostedSequence) {
        if (channelId !== state.lastPostedChannelId) hideSupersededChannel(channelId);
        return false;
      }
      if (!state.ready || !frame.contentWindow) {
        markLiveChannelFallback(
          channelId,
          envelope,
          state.handshakeTimedOut ? "Live inspection unavailable" : "Waiting for live inspection",
          state.handshakeTimedOut ? "stale" : "waiting"
        );
        var queued = queues[key];
        if (queued && envelope.sequence <= queued.envelope.sequence) {
          hideSupersededChannel(channelId);
          return false;
        }
        if (queued) hideSupersededChannel(queued.channelId);
        queues[key] = { envelope: envelope, channelId: channelId };
        return false;
      }
      queues[key] = { envelope: envelope, channelId: channelId };

      // sandbox="allow-scripts" gives srcdoc an opaque origin, so there is no
      // stable target origin. Exact contentWindow source checks plus version,
      // widget ID, nonce, and monotonic sequence are the authentication boundary.
      frame.contentWindow.postMessage(envelope, "*");
      state.lastPostedSequence = envelope.sequence;
      state.lastPostedChannelId = channelId;
      state.pendingChannels[envelope.sequence] = channelId;
      return true;
    }

    function accept(sequence) {
      if (!Number.isInteger(sequence)) return;
      if (sequence > state.lastPostedSequence || sequence <= state.lastAcceptedSequence) return;
      if (!Object.prototype.hasOwnProperty.call(state.pendingChannels, sequence)) return;

      state.lastAcceptedSequence = sequence;
      Object.keys(state.pendingChannels).forEach(function (pendingSequence) {
        var numericSequence = Number(pendingSequence);
        if (numericSequence > sequence) return;
        var channelId = state.pendingChannels[pendingSequence];
        if (numericSequence === sequence) {
          // The portable iframe is progressive fallback for isolated-output hosts.
          // A shared shell owns presentation only after its child applies the payload.
          // Exact pre-start error text is a separate sibling and remains visible.
          hideChannelFallback(channelId);
        } else {
          hideSupersededChannel(channelId);
        }
        delete state.pendingChannels[pendingSequence];
      });
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

      if (message.type === ACCEPTED) {
        accept(message.sequence);
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
      state.handshakeTimedOut = true;
      status.hidden = false;
      status.setAttribute("data-state", "stale");
      status.textContent = "The interactive inspector did not connect. Showing the latest saved snapshot below; this view is not live.";
      var queued = queues[key];
      if (queued) {
        markLiveChannelFallback(
          queued.channelId,
          queued.envelope,
          "Live inspection unavailable",
          "stale"
        );
      }
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
      post({
        type: ACCEPTED,
        version: VERSION,
        widget_id: config.widgetId,
        nonce: config.nonce,
        sequence: message.sequence,
      });
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
