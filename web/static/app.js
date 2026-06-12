// Bullpen Console client logic for the end-to-end Run (slice #40, Requirements
// 1, 7, 10). Clicking Run POSTs /api/run, then opens an SSE stream at
// /api/run/status?run_id=... and renders each agent event exactly once. A
// browser refresh re-opens the stream; the server replays the whole timeline
// from offset 0, and the dedup set below guarantees each event still renders
// once. The synthetic `pipeline_complete` event is shown as the single terminal
// frame.
//
// There is no JS unit-test harness in this repo; this client is covered at the
// Go integration layer by TestEndToEndRunSpawnsRunnerAndStreamsEvents, which
// drives POST /api/run -> stub runner -> SSE stream and asserts single-render,
// refresh-replay-without-duplication, and a single terminal frame.

(function () {
  "use strict";

  var form = document.getElementById("run-form");
  var topicInput = document.getElementById("topic");
  var outputsBox = document.getElementById("outputs");
  var runBtn = document.getElementById("run-btn");
  var statusEl = document.getElementById("status");
  var timelineEl = document.getElementById("timeline");
  var terminalEl = document.getElementById("terminal");

  var source = null;
  // Dedup set keyed by timestamp|event_type|agent_type — the exact DedupKey the
  // server uses — so replayed events never double-render in the timeline.
  var rendered = Object.create(null);
  var terminalShown = false;

  function setStatus(message, isError) {
    statusEl.textContent = message;
    statusEl.className = isError ? "status error" : "status";
  }

  // selectedOutputs returns ["all"] when the all box is ticked, otherwise the
  // ticked subset. The server validates this same contract.
  function selectedOutputs() {
    var boxes = outputsBox.querySelectorAll('input[type="checkbox"]');
    var all = null;
    var subset = [];
    boxes.forEach(function (b) {
      if (b.value === "all") { all = b; return; }
      if (b.checked) { subset.push(b.value); }
    });
    if (all && all.checked) { return ["all"]; }
    return subset;
  }

  function dedupKey(ev) {
    return (ev.timestamp || "") + "|" + (ev.event_type || "") + "|" + (ev.agent_type || "");
  }

  function resetTimeline() {
    rendered = Object.create(null);
    terminalShown = false;
    timelineEl.innerHTML = "";
    terminalEl.style.display = "none";
    terminalEl.className = "";
  }

  // appendEvent renders one agent event, suppressing duplicates by DedupKey.
  function appendEvent(ev) {
    var key = dedupKey(ev);
    if (rendered[key]) { return; } // already rendered: never double-render
    rendered[key] = true;

    var li = document.createElement("li");
    var ts = document.createElement("span");
    ts.className = "ts";
    ts.textContent = ev.timestamp || "";
    var agent = document.createElement("span");
    agent.className = "agent";
    agent.textContent = ev.agent_type || "pipeline";
    var label = document.createElement("span");
    label.textContent = " " + (ev.event_type || "event");
    li.appendChild(ts);
    li.appendChild(agent);
    li.appendChild(label);
    timelineEl.appendChild(li);
  }

  // showTerminal renders the single terminal frame and closes the stream.
  function showTerminal(payload) {
    if (terminalShown) { return; }
    terminalShown = true;
    var status = (payload && payload.status) || "complete";
    var isError = status === "error";
    terminalEl.textContent = isError ? "Run finished with errors." : "Run complete.";
    terminalEl.className = isError ? "error" : "";
    terminalEl.style.display = "block";
    closeStream();
    runBtn.disabled = false;
    setStatus(isError ? "Run finished with errors." : "Run complete.", isError);
  }

  function closeStream() {
    if (source) {
      source.close();
      source = null;
    }
  }

  function openStream(runID) {
    closeStream();
    source = new EventSource("/api/run/status?run_id=" + encodeURIComponent(runID));

    // Default (unnamed) SSE frames carry agent-log events as their JSON line.
    source.onmessage = function (e) {
      if (!e.data) { return; }
      var ev;
      try { ev = JSON.parse(e.data); } catch (_) { return; }
      appendEvent(ev);
    };

    // The hub emits exactly one named `pipeline_complete` terminal frame.
    source.addEventListener("pipeline_complete", function (e) {
      var payload = {};
      if (e.data) { try { payload = JSON.parse(e.data); } catch (_) {} }
      showTerminal(payload);
    });

    source.onerror = function () {
      // The hub closes the response after the terminal frame; an error after
      // we have shown the terminal is the expected end-of-stream, not a fault.
      if (!terminalShown) {
        setStatus("Connection to the Run stream was interrupted.", true);
      }
      closeStream();
      runBtn.disabled = false;
    };
  }

  function startRun(topic, outputs) {
    runBtn.disabled = true;
    resetTimeline();
    setStatus("Starting Run...");

    fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ topic: topic, outputs: outputs })
    }).then(function (resp) {
      return resp.json().then(function (body) { return { resp: resp, body: body }; });
    }).then(function (r) {
      if (r.resp.status === 202 && r.body.run_id) {
        setStatus("Run " + r.body.run_id + " started.");
        openStream(r.body.run_id);
        return;
      }
      runBtn.disabled = false;
      if (r.resp.status === 409) {
        setStatus("A Run is already in progress.", true);
      } else {
        setStatus((r.body && r.body.detail) || "Could not start the Run.", true);
      }
    }).catch(function () {
      runBtn.disabled = false;
      setStatus("Could not reach the Console to start the Run.", true);
    });
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var topic = topicInput.value.trim();
    if (!topic) {
      setStatus("Enter a topic to start a Run.", true);
      return;
    }
    var outputs = selectedOutputs();
    if (outputs.length === 0) {
      setStatus("Select at least one output (or all).", true);
      return;
    }
    startRun(topic, outputs);
  });
})();
