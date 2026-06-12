// Bullpen Console client logic. Clicking Run POSTs /api/run, then opens an SSE
// stream at /api/run/status?run_id=... and renders each agent event exactly
// once (slice #40, Requirements 1, 7, 10). The gate controls land in slice #43
// (Requirement 2). This slice (#45, Requirement 3) makes the client settle into
// EXACTLY ONE of three terminal states and render nothing after it:
//
//   - Gate presented : approval_gate_presented seen, files pending approval
//                      listed with Approve/Reject controls.
//   - Escalated      : no publish verdict and one or more file_escalated events;
//                      a manual-review message lists each file and its reason.
//   - Errored        : an agent_error halt, a pipeline_complete of status
//                      error/halted, OR a nonzero subprocess exit reconciled
//                      server-side into a terminal frame of status "error"; the
//                      failing step and message are shown.
//
// The single synthetic `pipeline_complete` terminal frame carries the
// server-reconciled status ("complete" | "escalated" | "error"). The
// terminalShown guard plus the DedupKey set guarantee exactly one terminal
// frame even across a browser refresh (replay + dedup from #40).
//
// There is no JS unit-test harness in this repo; this client is covered at the
// Go integration layer (TestEndToEndRunSpawnsRunnerAndStreamsEvents,
// TestEndToEndApprovalGate*, and the #45 terminal-state e2e tests), which drive
// POST /api/run -> stub runner -> SSE stream and assert single-render,
// refresh-replay-without-duplication, and a single terminal frame of the
// correct kind.

(function () {
  "use strict";

  var form = document.getElementById("run-form");
  var topicInput = document.getElementById("topic");
  var outputsBox = document.getElementById("outputs");
  var runBtn = document.getElementById("run-btn");
  var statusEl = document.getElementById("status");
  var timelineEl = document.getElementById("timeline");
  var terminalEl = document.getElementById("terminal");
  var approvalEl = document.getElementById("approval");
  var approvalFilesEl = document.getElementById("approval-files");
  var approveBtn = document.getElementById("approve-btn");
  var rejectBtn = document.getElementById("reject-btn");
  var escalatedEl = document.getElementById("escalated");
  var escalatedFilesEl = document.getElementById("escalated-files");

  var source = null;
  // Dedup set keyed by timestamp|event_type|agent_type — the exact DedupKey the
  // server uses — so replayed events never double-render in the timeline.
  var rendered = Object.create(null);
  var terminalShown = false;

  // Accumulated terminal-state signals, derived from the event stream so the
  // single terminal frame can be rendered as exactly one of the three states.
  var publishVerdictSeen = false;     // a verdict event with verdict "publish"
  var escalatedFiles = [];            // [{filename, reason}] from file_escalated
  var errorInfo = null;               // {step, message} from agent_error / error complete

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
    publishVerdictSeen = false;
    escalatedFiles = [];
    errorInfo = null;
    timelineEl.innerHTML = "";
    terminalEl.style.display = "none";
    terminalEl.className = "";
    terminalEl.textContent = "";
    escalatedEl.style.display = "none";
    escalatedFilesEl.innerHTML = "";
    hideApproval();
  }

  // showApproval renders the Gate-presented state: the list of files pending
  // approval and the Approve/Reject controls. Driven by the
  // approval_gate_presented event (Requirement 3.3).
  function showApproval(ev) {
    var details = (ev && ev.details) || {};
    var pending = details.files_pending_approval || [];
    approvalFilesEl.innerHTML = "";
    pending.forEach(function (name) {
      var li = document.createElement("li");
      li.textContent = name;
      approvalFilesEl.appendChild(li);
    });
    approveBtn.disabled = false;
    rejectBtn.disabled = false;
    approvalEl.style.display = "block";
  }

  function hideApproval() {
    approvalEl.style.display = "none";
  }

  // decide POSTs the human decision to the Console, which writes
  // approval-decision.json for the Python poller to consume (Requirement 2).
  function decide(approved) {
    approveBtn.disabled = true;
    rejectBtn.disabled = true;
    var path = approved ? "/api/run/approve" : "/api/run/reject";
    fetch(path, { method: "POST", headers: { "Content-Type": "application/json" } })
      .then(function (resp) {
        if (resp.status === 200) {
          hideApproval();
          setStatus(approved ? "Approved. Publishing..." : "Rejected. Files retained.");
          return;
        }
        approveBtn.disabled = false;
        rejectBtn.disabled = false;
        if (resp.status === 409) {
          setStatus("No approval gate is currently waiting.", true);
        } else {
          setStatus("Could not record the decision.", true);
        }
      })
      .catch(function () {
        approveBtn.disabled = false;
        rejectBtn.disabled = false;
        setStatus("Could not reach the Console to record the decision.", true);
      });
  }

  // appendEvent renders one agent event, suppressing duplicates by DedupKey, and
  // accumulates the signals used to pick the terminal state (Requirement 3).
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

    var details = ev.details || {};
    switch (ev.event_type) {
      case "approval_gate_presented":
        // Entering the Gate-presented state surfaces the Approve/Reject controls.
        showApproval(ev);
        break;
      case "approval_decision":
      case "approval_rejected":
        // Any resolving event closes the gate UI.
        hideApproval();
        break;
      case "verdict":
        if (details.verdict === "publish") { publishVerdictSeen = true; }
        break;
      case "file_escalated":
        escalatedFiles.push({
          filename: details.filename || "(unknown file)",
          reason: details.reason || ""
        });
        break;
      case "agent_error":
        // First failing step wins; it is what halted the pipeline.
        if (!errorInfo) {
          errorInfo = { step: details.step || ev.agent_type || "pipeline", message: details.error || "" };
        }
        break;
      case "pipeline_complete":
        // A terminal event reporting an error carries the failing context.
        if ((details.status === "error" || details.status === "halted") && !errorInfo) {
          errorInfo = { step: details.step || ev.agent_type || "pipeline", message: details.error || details.status };
        }
        break;
    }
  }

  // renderErrored shows the Errored terminal state: the failing step and message
  // (Requirement 3.5/3.6).
  function renderErrored() {
    var info = errorInfo || { step: "pipeline", message: "The Run ended with an error." };
    terminalEl.className = "error";
    terminalEl.textContent = "Run failed.";
    var detail = document.createElement("span");
    detail.className = "terminal-detail";
    var step = document.createElement("span");
    step.className = "terminal-step";
    step.textContent = info.step;
    detail.appendChild(document.createTextNode("Failing step: "));
    detail.appendChild(step);
    if (info.message) {
      detail.appendChild(document.createTextNode(" — " + info.message));
    }
    terminalEl.appendChild(detail);
    terminalEl.style.display = "block";
    setStatus("Run failed at " + info.step + ".", true);
  }

  // renderEscalated shows the Escalated terminal state: a manual-review message
  // listing each escalated file and its reason (Requirement 3.4).
  function renderEscalated() {
    escalatedFilesEl.innerHTML = "";
    escalatedFiles.forEach(function (f) {
      var li = document.createElement("li");
      li.textContent = f.filename;
      if (f.reason) {
        var reason = document.createElement("span");
        reason.className = "reason";
        reason.textContent = " — " + f.reason;
        li.appendChild(reason);
      }
      escalatedFilesEl.appendChild(li);
    });
    escalatedEl.style.display = "block";
    setStatus("Run held for manual review (" + escalatedFiles.length + " file(s) escalated).");
  }

  // renderComplete shows the happy terminal state.
  function renderComplete() {
    terminalEl.className = "";
    terminalEl.textContent = "Run complete.";
    terminalEl.style.display = "block";
    setStatus("Run complete.");
  }

  // showTerminal renders the SINGLE terminal frame and closes the stream,
  // settling into exactly one of {Errored, Escalated, Complete}. The server
  // reconciles the subprocess exit against the terminal event and encodes the
  // outcome in payload.status; the client also falls back to its own derivation
  // from the accumulated event signals (Requirement 3.2).
  function showTerminal(payload) {
    if (terminalShown) { return; } // exactly one terminal frame, ever
    terminalShown = true;
    hideApproval();

    var status = (payload && payload.status) || "complete";
    var isError = status === "error" || errorInfo !== null;
    var isEscalated = status === "escalated" || (!publishVerdictSeen && escalatedFiles.length > 0);

    if (isError) {
      renderErrored();
    } else if (isEscalated) {
      renderEscalated();
    } else {
      renderComplete();
    }

    closeStream();
    runBtn.disabled = false;
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

  approveBtn.addEventListener("click", function () { decide(true); });
  rejectBtn.addEventListener("click", function () { decide(false); });
})();
