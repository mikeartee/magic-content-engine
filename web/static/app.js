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

  // Topic Ideas panel (#51, Requirement 6). On load it fills from the vault
  // recency list (GET /api/suggestions); the search box queries
  // GET /api/suggestions/search?q=. Clicking a suggestion pre-fills the Run
  // topic input. A missing/empty vault surfaces the API's `warning` text, never
  // an error. No AWS call is involved: both endpoints are vault-only.
  var ideasSearch = document.getElementById("ideas-search");
  var ideasWarning = document.getElementById("ideas-warning");
  var ideasList = document.getElementById("ideas-list");
  var ideasEmpty = document.getElementById("ideas-empty");
  var ideasSearchTimer = null;
  // ideasReqSeq guards against out-of-order responses: only the newest request
  // is allowed to render, so a slow recency load cannot clobber a later search.
  var ideasReqSeq = 0;

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

  // ----- Topic Ideas panel (#51, Requirement 6) -----

  // setIdeasWarning shows or clears the API-provided warning text (e.g. a
  // missing vault). An empty/absent warning hides the banner entirely.
  function setIdeasWarning(text) {
    if (text) {
      ideasWarning.textContent = text;
      ideasWarning.style.display = "block";
    } else {
      ideasWarning.textContent = "";
      ideasWarning.style.display = "none";
    }
  }

  // suggestionMeta builds the right-hand label from the suggestion's
  // last_covered date and days_since count, when present.
  function suggestionMeta(s) {
    var parts = [];
    if (s.last_covered) { parts.push(s.last_covered); }
    if (typeof s.days_since === "number") {
      parts.push(s.days_since === 1 ? "1 day ago" : s.days_since + " days ago");
    }
    return parts.join(" · ");
  }

  // renderSuggestions paints the list of suggestions. Each row is a button so it
  // is keyboard-focusable; clicking it fills the Run topic input.
  function renderSuggestions(items) {
    ideasList.innerHTML = "";
    items.forEach(function (s) {
      var li = document.createElement("li");
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "idea";

      var topic = document.createElement("span");
      topic.className = "idea-topic";
      topic.textContent = s.topic || "(untitled)";
      btn.appendChild(topic);

      var metaText = suggestionMeta(s);
      if (metaText) {
        var meta = document.createElement("span");
        meta.className = "idea-meta";
        meta.textContent = metaText;
        btn.appendChild(meta);
      }

      btn.addEventListener("click", function () { fillTopic(s.topic || ""); });
      li.appendChild(btn);
      ideasList.appendChild(li);
    });
  }

  // fillTopic pre-fills the free-text Run topic field with the chosen
  // suggestion (Requirement 6.7) and moves focus there so the user can edit it.
  function fillTopic(topic) {
    topicInput.value = topic;
    topicInput.focus();
  }

  // applySuggestions renders the body of a /api/suggestions(/search) response:
  // the warning (if any) and the suggestion list. emptyMsg shows when there are
  // no items and no warning to explain the emptiness.
  function applySuggestions(data, emptyMsg) {
    data = data || {};
    setIdeasWarning(data.warning || "");
    var items = data.suggestions || [];
    renderSuggestions(items);
    ideasEmpty.textContent = (items.length === 0 && !data.warning) ? emptyMsg : "";
  }

  // loadSuggestions fetches a recency or search result and renders it. A network
  // failure shows a small inline note rather than throwing; the API itself never
  // errors for a missing vault (it returns a warning instead).
  function loadSuggestions(url, emptyMsg) {
    var seq = ++ideasReqSeq;
    fetch(url)
      .then(function (resp) { return resp.json(); })
      .then(function (data) {
        if (seq !== ideasReqSeq) { return; } // a newer request superseded us
        applySuggestions(data, emptyMsg);
      })
      .catch(function () {
        if (seq !== ideasReqSeq) { return; }
        setIdeasWarning("");
        ideasList.innerHTML = "";
        ideasEmpty.textContent = "Could not load topic ideas.";
      });
  }

  function loadRecency() {
    loadSuggestions("/api/suggestions", "No topic ideas yet.");
  }

  function runSearch(query) {
    loadSuggestions("/api/suggestions/search?q=" + encodeURIComponent(query), "No matching topics.");
  }

  ideasSearch.addEventListener("input", function () {
    var query = ideasSearch.value.trim();
    if (ideasSearchTimer) { clearTimeout(ideasSearchTimer); }
    // Debounce keystrokes; an empty box returns to the recency list.
    ideasSearchTimer = setTimeout(function () {
      if (query === "") { loadRecency(); } else { runSearch(query); }
    }, 250);
  });

  // Submitting the search box (Enter) runs the query immediately.
  ideasSearch.addEventListener("keydown", function (e) {
    if (e.key !== "Enter") { return; }
    e.preventDefault();
    if (ideasSearchTimer) { clearTimeout(ideasSearchTimer); }
    var query = ideasSearch.value.trim();
    if (query === "") { loadRecency(); } else { runSearch(query); }
  });

  // Populate the recency list on load (Requirement 6.2).
  loadRecency();

  // ----- Run History + file browser (#52) -----

  // Reads the existing GET /api/runs endpoint, whose response shape is
  // {"runs":[{"id":"<run_id>","files":["post.md","subdir/file.md", ...]}, ...]}.
  // The API already orders runs by id descending and already excludes internal
  // files (agent-log.jsonl, checkpoints.json), so this panel just renders what
  // it returns. Selecting a run lists its files; each file entry carries
  // data-run-id + data-file so the next slice (#53, in-app viewer) can open it.
  var runsList = document.getElementById("runs-list");
  var runsEmpty = document.getElementById("runs-empty");
  var runFilesList = document.getElementById("run-files-list");
  var runFilesEmpty = document.getElementById("run-files-empty");
  var selectedRunBtn = null;

  // renderRunFiles paints the file list for the selected run. Each file is a
  // focusable button tagged with data-run-id + data-file; the viewer slice (#53)
  // wires the open behaviour against those attributes. A subdir entry arrives as
  // "subdir/filename" and is shown verbatim, with the directory segment dimmed.
  function renderRunFiles(runID, files) {
    runFilesList.innerHTML = "";
    files = files || [];
    if (files.length === 0) {
      runFilesEmpty.textContent = "This run produced no files.";
      return;
    }
    runFilesEmpty.textContent = "";
    files.forEach(function (name) {
      var li = document.createElement("li");
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "run-file";
      // Click target for #53: identify the run and the file unambiguously.
      btn.setAttribute("data-run-id", runID);
      btn.setAttribute("data-file", name);
      btn.title = name;

      var slash = name.indexOf("/");
      if (slash !== -1) {
        var dir = document.createElement("span");
        dir.className = "file-dir";
        dir.textContent = name.slice(0, slash + 1);
        btn.appendChild(dir);
        btn.appendChild(document.createTextNode(name.slice(slash + 1)));
      } else {
        btn.textContent = name;
      }
      li.appendChild(btn);
      runFilesList.appendChild(li);
    });
  }

  // selectRun highlights the chosen run row and renders its files.
  function selectRun(btn, run) {
    if (selectedRunBtn) { selectedRunBtn.classList.remove("selected"); }
    selectedRunBtn = btn;
    btn.classList.add("selected");
    renderRunFiles(run.id, run.files);
    // #54: the selected run becomes the publish target and drives the legacy
    // quick-action helpers. onRunSelected is hoisted from the publish section.
    onRunSelected(run);
  }

  // renderRuns paints the run history list, newest first. The API already sorts
  // by id descending; we sort client-side too so render order is strictly
  // newest-first regardless of server ordering.
  function renderRuns(runs) {
    runs = (runs || []).slice().sort(function (a, b) {
      if (a.id < b.id) { return 1; }
      if (a.id > b.id) { return -1; }
      return 0;
    });
    runsList.innerHTML = "";
    runFilesList.innerHTML = "";
    runFilesEmpty.textContent = "";
    selectedRunBtn = null;
    // #54: clearing the run list also clears the publish target until a run is
    // selected again. resetPublishPanel is hoisted from the publish section.
    resetPublishPanel();

    if (runs.length === 0) {
      runsEmpty.textContent = "No runs yet. Start one above to see it here.";
      return;
    }
    runsEmpty.textContent = "";
    runs.forEach(function (run) {
      var li = document.createElement("li");
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "run-item";
      btn.textContent = run.id;
      btn.title = run.id;
      btn.addEventListener("click", function () { selectRun(btn, run); });
      li.appendChild(btn);
      runsList.appendChild(li);
    });
  }

  // loadRuns fetches the run history. A network failure shows a small inline
  // note rather than throwing; an empty list is a friendly message, not an error.
  function loadRuns() {
    fetch("/api/runs")
      .then(function (resp) { return resp.json(); })
      .then(function (data) { renderRuns((data && data.runs) || []); })
      .catch(function () {
        runsList.innerHTML = "";
        runFilesList.innerHTML = "";
        runFilesEmpty.textContent = "";
        runsEmpty.textContent = "Could not load run history.";
      });
  }

  // Populate the run history on load (#52).
  loadRuns();

  // ----- In-app file viewer/editor + download (#53) -----
  //
  // Clicking a #52 file entry (a `.run-file` button carrying data-run-id +
  // data-file) opens the file IN the Console window: its content loads into a
  // viewer pane, rendered readably (a tiny in-house XSS-safe Markdown renderer
  // for .md/.markdown, clean monospace for everything else) with an editable
  // textarea behind a Preview/Edit toggle. Save POSTs the edited content via
  // POST /api/runs/{id}/file; Download triggers GET
  // /api/runs/{id}/download/{file...}. Traversal (403) and not-found (404)
  // surface as friendly inline messages, never crashes. Names with one subdir
  // segment ("subdir/filename") are URL-encoded correctly for each transport:
  // the read uses ?name=<encoded> (slash percent-encoded), the download uses a
  // path of per-segment-encoded parts joined by literal slashes. No heavy
  // dependency is added; the renderer escapes HTML first, then applies a small
  // set of Markdown rules.
  var viewerPanel = document.getElementById("viewer-panel");
  var viewerFilename = document.getElementById("viewer-filename");
  var viewerMessage = document.getElementById("viewer-message");
  var viewerMike = document.getElementById("viewer-mike");
  var viewerMikeList = document.getElementById("viewer-mike-list");
  var viewerRendered = document.getElementById("viewer-rendered");
  var viewerEditor = document.getElementById("viewer-editor");
  var viewerPreviewBtn = document.getElementById("viewer-preview-btn");
  var viewerEditBtn = document.getElementById("viewer-edit-btn");
  var viewerSaveBtn = document.getElementById("viewer-save-btn");
  var viewerDownloadBtn = document.getElementById("viewer-download-btn");

  var viewerRunID = null;   // run_id of the file currently open
  var viewerFile = null;    // name (may be "subdir/filename") currently open
  var viewerMode = "preview";

  function isMarkdown(name) {
    return /\.(md|markdown)$/i.test(name || "");
  }

  // escapeHtml neutralises every HTML-significant character so nothing in a
  // file's bytes can inject markup. The Markdown renderer runs only AFTER this,
  // and only ever wraps already-escaped text in a fixed set of safe tags.
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // renderInline applies inline Markdown (code spans, links, bold, italic) to a
  // single already-escaped line. Inline code is pulled out first so emphasis
  // markers inside it are left alone, then restored last. Link URLs are limited
  // to http(s)/relative/anchor/mailto so no javascript: URL can slip through.
  function renderInline(text) {
    var codes = [];
    text = text.replace(/`([^`]+)`/g, function (_m, c) {
      codes.push(c);
      return "\u0000" + (codes.length - 1) + "\u0000";
    });
    text = text.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, function (m, label, url) {
      if (!/^(https?:\/\/|\/|#|mailto:)/i.test(url)) { return m; }
      return '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + label + "</a>";
    });
    text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    text = text.replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
    text = text.replace(/(^|[^_\w])_([^_\s][^_]*)_/g, "$1<em>$2</em>");
    text = text.replace(/\u0000(\d+)\u0000/g, function (_m, i) {
      return "<code>" + codes[parseInt(i, 10)] + "</code>";
    });
    return text;
  }

  // renderMarkdown turns raw Markdown into safe HTML. It escapes the whole
  // document first (so the structural pass only ever sees inert text), then
  // walks line by line recognising fenced code, headings, horizontal rules,
  // blockquotes, ordered/unordered lists, and paragraphs.
  function renderMarkdown(raw) {
    var lines = escapeHtml(raw).split(/\r?\n/);
    var html = [];
    var inCode = false;
    var codeBuf = [];
    var listType = null;

    function closeList() {
      if (listType) { html.push("</" + listType + ">"); listType = null; }
    }

    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];

      if (/^\s*```/.test(line)) {
        if (inCode) {
          html.push("<pre><code>" + codeBuf.join("\n") + "</code></pre>");
          codeBuf = [];
          inCode = false;
        } else {
          closeList();
          inCode = true;
        }
        continue;
      }
      if (inCode) { codeBuf.push(line); continue; }

      if (/^\s*$/.test(line)) { closeList(); continue; }

      // MIKE placeholder lines (<!-- MIKE: ... -->) are surfaced as a visible
      // callout instead of rendering as literal text, so the human reviewer
      // cannot miss the sections they own (#54). The instruction text is already
      // HTML-escaped by escapeHtml above, so it is inert here.
      var mike = line.match(/^\s*&lt;!--\s*MIKE:\s*([\s\S]*?)\s*--&gt;\s*$/);
      if (mike) {
        closeList();
        html.push('<div class="mike-placeholder"><span class="mike-label">\u270E MIKE:</span>' + mike[1] + "</div>");
        continue;
      }

      var h = line.match(/^(#{1,6})\s+(.*)$/);
      if (h) {
        closeList();
        var level = h[1].length;
        html.push("<h" + level + ">" + renderInline(h[2]) + "</h" + level + ">");
        continue;
      }

      if (/^\s*(---|\*\*\*|___)\s*$/.test(line)) {
        closeList();
        html.push("<hr>");
        continue;
      }

      var bq = line.match(/^\s*&gt;\s?(.*)$/);
      if (bq) {
        closeList();
        html.push("<blockquote>" + renderInline(bq[1]) + "</blockquote>");
        continue;
      }

      var ul = line.match(/^\s*[-*+]\s+(.*)$/);
      if (ul) {
        if (listType !== "ul") { closeList(); html.push("<ul>"); listType = "ul"; }
        html.push("<li>" + renderInline(ul[1]) + "</li>");
        continue;
      }

      var ol = line.match(/^\s*\d+\.\s+(.*)$/);
      if (ol) {
        if (listType !== "ol") { closeList(); html.push("<ol>"); listType = "ol"; }
        html.push("<li>" + renderInline(ol[1]) + "</li>");
        continue;
      }

      closeList();
      html.push("<p>" + renderInline(line) + "</p>");
    }
    if (inCode) { html.push("<pre><code>" + codeBuf.join("\n") + "</code></pre>"); }
    closeList();
    return html.join("\n");
  }

  // fileReadURL builds the read URL. encodeURIComponent percent-encodes the
  // slash in a "subdir/filename" name, which the server decodes back to a single
  // subdir segment (Requirement 4.3).
  function fileReadURL(runID, name) {
    return "/api/runs/" + encodeURIComponent(runID) + "/file?name=" + encodeURIComponent(name);
  }

  // fileDownloadURL builds the download URL. The route is a path wildcard
  // (/download/{file...}), so each segment is encoded independently and rejoined
  // with literal slashes, keeping a one-level subdir intact.
  function fileDownloadURL(runID, name) {
    var segs = String(name).split("/").map(encodeURIComponent);
    return "/api/runs/" + encodeURIComponent(runID) + "/download/" + segs.join("/");
  }

  function setViewerMessage(text, isError) {
    viewerMessage.textContent = text || "";
    viewerMessage.className = isError ? "viewer-message error" : "viewer-message";
  }

  // setViewerFilename shows the open file, dimming the directory segment so a
  // "subdir/filename" reads the same way it does in the #52 file list.
  function setViewerFilename(name) {
    viewerFilename.innerHTML = "";
    viewerFilename.title = name;
    var slash = name.indexOf("/");
    if (slash !== -1) {
      var dir = document.createElement("span");
      dir.className = "file-dir";
      dir.textContent = name.slice(0, slash + 1);
      viewerFilename.appendChild(dir);
      viewerFilename.appendChild(document.createTextNode(name.slice(slash + 1)));
    } else {
      viewerFilename.textContent = name;
    }
  }

  // findMikePlaceholders extracts every `<!-- MIKE: instruction -->` section
  // from raw content. These mark the personal writing the human owns (the hook,
  // cold open, closing) and must be filled before publishing (#54). The returned
  // instructions are raw text; callers escape before inserting into the DOM.
  function findMikePlaceholders(text) {
    var out = [];
    var re = /<!--\s*MIKE:\s*([\s\S]*?)-->/g;
    var m;
    while ((m = re.exec(String(text))) !== null) {
      out.push(m[1].trim());
    }
    return out;
  }

  // showMikePlaceholders renders the banner listing the MIKE sections in the
  // open file. An empty list hides the banner entirely.
  function showMikePlaceholders(list) {
    viewerMikeList.innerHTML = "";
    if (!list || list.length === 0) {
      viewerMike.style.display = "none";
      return;
    }
    list.forEach(function (instruction) {
      var li = document.createElement("li");
      li.textContent = instruction || "(no instruction given)";
      viewerMikeList.appendChild(li);
    });
    viewerMike.style.display = "block";
  }

  // renderPreview paints the read-only view from the given text: rendered
  // Markdown for .md/.markdown, otherwise clean monospace with line breaks
  // preserved (textContent keeps it inert).
  function renderPreview(text, name) {
    if (isMarkdown(name)) {
      viewerRendered.className = "viewer-rendered markdown";
      viewerRendered.innerHTML = renderMarkdown(text);
    } else {
      viewerRendered.className = "viewer-rendered plain";
      viewerRendered.innerHTML = "";
      var pre = document.createElement("pre");
      pre.textContent = text;
      viewerRendered.appendChild(pre);
    }
  }

  // showMode toggles between the rendered preview and the editable textarea and
  // marks the active tab. Switching to Preview re-renders from the textarea so
  // the preview always reflects unsaved edits.
  function showMode(mode) {
    viewerMode = mode;
    if (mode === "edit") {
      viewerRendered.style.display = "none";
      viewerEditor.style.display = "block";
      viewerEditBtn.classList.add("active");
      viewerPreviewBtn.classList.remove("active");
    } else {
      renderPreview(viewerEditor.value, viewerFile || "");
      viewerRendered.style.display = "block";
      viewerEditor.style.display = "none";
      viewerPreviewBtn.classList.add("active");
      viewerEditBtn.classList.remove("active");
    }
  }

  function setEditingEnabled(enabled) {
    viewerEditBtn.disabled = !enabled;
    viewerSaveBtn.disabled = !enabled;
    viewerDownloadBtn.disabled = !enabled;
  }

  // openFile loads a file's content into the viewer pane. A 403 (traversal) or
  // 404 (not found) is shown as a friendly inline message instead of crashing.
  function openFile(runID, name) {
    viewerRunID = runID;
    viewerFile = name;
    viewerPanel.style.display = "block";
    setViewerFilename(name);
    setViewerMessage("Loading " + name + "...", false);
    viewerRendered.className = "viewer-rendered";
    viewerRendered.innerHTML = "";
    viewerEditor.value = "";
    setEditingEnabled(false);
    showMode("preview");
    showMikePlaceholders([]);

    fetch(fileReadURL(runID, name))
      .then(function (resp) {
        if (!resp.ok) {
          return resp.json().catch(function () { return {}; }).then(function (body) {
            throw { status: resp.status, detail: body.detail || body.error };
          });
        }
        return resp.text();
      })
      .then(function (text) {
        viewerEditor.value = text;
        renderPreview(text, name);
        showMode("preview");
        showMikePlaceholders(findMikePlaceholders(text));
        setEditingEnabled(true);
        setViewerMessage("", false);
        viewerPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
      })
      .catch(function (err) {
        setViewerMessage(fileErrorMessage(err && err.status, err, "open"), true);
        setEditingEnabled(false);
      });
  }

  // fileErrorMessage turns a status/body into a friendly, action-specific note.
  function fileErrorMessage(status, body, action) {
    var verb = action === "save" ? "Nothing was saved." : "Nothing was opened.";
    if (status === 403) { return "That file path isn't allowed (403 forbidden). " + verb; }
    if (status === 404) { return "That file could not be found (404). It may have been moved or deleted."; }
    if (status === 422) { return "Could not save: " + ((body && body.detail) || "the content is empty."); }
    if (body && body.detail) { return "Could not " + (action || "open") + " the file: " + body.detail; }
    return "Could not reach the Console to " + (action || "open") + " the file.";
  }

  // saveFile POSTs the edited content. Success shows a confirmation and refreshes
  // the preview; 403/404/422 and network failures surface inline.
  function saveFile() {
    if (!viewerRunID || !viewerFile) { return; }
    var content = viewerEditor.value;
    setEditingEnabled(false);
    setViewerMessage("Saving " + viewerFile + "...", false);

    fetch("/api/runs/" + encodeURIComponent(viewerRunID) + "/file", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: viewerFile, content: content })
    })
      .then(function (resp) {
        return resp.json().catch(function () { return {}; }).then(function (body) {
          return { resp: resp, body: body };
        });
      })
      .then(function (r) {
        setEditingEnabled(true);
        if (r.resp.ok && r.body.saved) {
          setViewerMessage("Saved " + viewerFile + ".", false);
          renderPreview(content, viewerFile);
          showMikePlaceholders(findMikePlaceholders(content));
        } else {
          setViewerMessage(fileErrorMessage(r.resp.status, r.body, "save"), true);
        }
      })
      .catch(function () {
        setEditingEnabled(true);
        setViewerMessage("Could not reach the Console to save the file.", true);
      });
  }

  // downloadFile triggers the attachment download via a transient anchor, so the
  // Console window itself never navigates away.
  function downloadFile() {
    if (!viewerRunID || !viewerFile) { return; }
    var a = document.createElement("a");
    a.href = fileDownloadURL(viewerRunID, viewerFile);
    a.download = String(viewerFile).split("/").pop();
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setViewerMessage("Downloading " + viewerFile + "...", false);
  }

  // A single delegated listener handles every #52 file entry, including those
  // rendered after this code runs (selecting a different run re-paints the list).
  runFilesList.addEventListener("click", function (e) {
    var btn = e.target.closest ? e.target.closest(".run-file") : null;
    if (!btn || !runFilesList.contains(btn)) { return; }
    var runID = btn.getAttribute("data-run-id");
    var name = btn.getAttribute("data-file");
    if (runID && name) { openFile(runID, name); }
  });

  viewerPreviewBtn.addEventListener("click", function () { showMode("preview"); });
  viewerEditBtn.addEventListener("click", function () {
    showMode("edit");
    viewerEditor.focus();
  });
  viewerSaveBtn.addEventListener("click", saveFile);
  viewerDownloadBtn.addEventListener("click", downloadFile);

  // ----- Review + publish panel (#54) -----
  //
  // The run selected in Run History (#52) becomes the publish target. The panel
  // collects a dev.to title/tags/published flag and POSTs to the existing
  // POST /api/publish/devto with the selected run_id (the server reads post.md
  // itself). Success shows the live URL (clickable) + article id; each failure
  // maps to a clear, actionable message. No API key is ever entered, shown, or
  // logged here: the key lives only in the Console's server environment.
  //
  // Legacy convenience helpers (Copy / Download) are offered only for the
  // generated files that exist in the selected run (digest/LinkedIn text,
  // youtube/script), reusing the #53 read + download URL builders.
  var publishRunEl = document.getElementById("publish-run");
  var legacyActionsEl = document.getElementById("legacy-actions");
  var publishTitle = document.getElementById("publish-title");
  var publishTags = document.getElementById("publish-tags");
  var publishPublished = document.getElementById("publish-published");
  var publishBtn = document.getElementById("publish-btn");
  var publishResult = document.getElementById("publish-result");

  var publishRunID = null;

  // legacyActionsFor decides which helper controls a generated file gets. Only
  // known legacy artefacts qualify; plain-text bodies (digest/LinkedIn) also get
  // a Copy. basename ignores any one-level subdir prefix.
  function legacyActionsFor(name) {
    var base = String(name).split("/").pop().toLowerCase();
    var isLegacy =
      base.indexOf("digest") !== -1 ||
      base.indexOf("linkedin") !== -1 ||
      base.indexOf("youtube") !== -1 ||
      base.indexOf("script") !== -1;
    if (!isLegacy) { return null; }
    return { copy: /\.(txt|md|markdown)$/.test(base), download: true };
  }

  // appendDimmedName fills a node with the file name, dimming any subdir prefix
  // exactly like the #52 file list does.
  function appendDimmedName(node, name) {
    node.title = name;
    var slash = name.indexOf("/");
    if (slash !== -1) {
      var dir = document.createElement("span");
      dir.className = "file-dir";
      dir.textContent = name.slice(0, slash + 1);
      node.appendChild(dir);
      node.appendChild(document.createTextNode(name.slice(slash + 1)));
    } else {
      node.textContent = name;
    }
  }

  // copyToClipboard prefers the async Clipboard API and falls back to a hidden
  // textarea + execCommand for older/insecure-context browsers.
  function copyToClipboard(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      try {
        var ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        var ok = document.execCommand("copy");
        document.body.removeChild(ta);
        if (ok) { resolve(); } else { reject(new Error("copy failed")); }
      } catch (e) { reject(e); }
    });
  }

  // flashButton briefly swaps a button's label to give click feedback.
  function flashButton(btn, label) {
    var prev = btn.getAttribute("data-label") || btn.textContent;
    btn.setAttribute("data-label", prev);
    btn.textContent = label;
    setTimeout(function () { btn.textContent = btn.getAttribute("data-label") || prev; }, 1500);
  }

  // copyLegacy reads a file (reusing the #53 read URL) and copies its body.
  function copyLegacy(runID, name, btn) {
    fetch(fileReadURL(runID, name))
      .then(function (resp) { if (!resp.ok) { throw new Error(String(resp.status)); } return resp.text(); })
      .then(function (text) { return copyToClipboard(text); })
      .then(function () { flashButton(btn, "Copied"); })
      .catch(function () { flashButton(btn, "Copy failed"); });
  }

  // downloadLegacy triggers the attachment download via a transient anchor,
  // reusing the #53 download URL builder so subdir names stay intact.
  function downloadLegacy(runID, name) {
    var a = document.createElement("a");
    a.href = fileDownloadURL(runID, name);
    a.download = String(name).split("/").pop();
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  }

  // renderLegacyActions paints a Copy/Download row for each matching file in the
  // selected run. Files that do not match show no controls (best-effort).
  function renderLegacyActions(run) {
    legacyActionsEl.innerHTML = "";
    var files = (run && run.files) || [];
    files.forEach(function (name) {
      var spec = legacyActionsFor(name);
      if (!spec) { return; }

      var li = document.createElement("li");
      var label = document.createElement("span");
      label.className = "legacy-name";
      appendDimmedName(label, name);
      li.appendChild(label);

      if (spec.copy) {
        var copyBtn = document.createElement("button");
        copyBtn.type = "button";
        copyBtn.textContent = "Copy";
        copyBtn.addEventListener("click", function () { copyLegacy(run.id, name, copyBtn); });
        li.appendChild(copyBtn);
      }
      if (spec.download) {
        var dlBtn = document.createElement("button");
        dlBtn.type = "button";
        dlBtn.textContent = "Download";
        dlBtn.addEventListener("click", function () { downloadLegacy(run.id, name); });
        li.appendChild(dlBtn);
      }
      legacyActionsEl.appendChild(li);
    });
  }

  function clearPublishResult() {
    publishResult.style.display = "none";
    publishResult.className = "";
    publishResult.innerHTML = "";
  }

  // onRunSelected makes the chosen run the publish target and renders its legacy
  // helpers. Called from #52's selectRun (hoisted).
  function onRunSelected(run) {
    publishRunID = run.id;
    publishRunEl.innerHTML = "";
    publishRunEl.appendChild(document.createTextNode("Reviewing run "));
    var span = document.createElement("span");
    span.className = "run-id";
    span.textContent = run.id;
    publishRunEl.appendChild(span);
    publishRunEl.appendChild(document.createTextNode(". Open its files above to review, then publish below."));
    publishBtn.disabled = false;
    clearPublishResult();
    renderLegacyActions(run);
  }

  // resetPublishPanel returns the panel to its no-run-selected state. Called
  // from #52's renderRuns (hoisted).
  function resetPublishPanel() {
    publishRunID = null;
    publishRunEl.innerHTML = "";
    var none = document.createElement("span");
    none.className = "none";
    none.textContent = "Select a run in Run History to review and publish it.";
    publishRunEl.appendChild(none);
    legacyActionsEl.innerHTML = "";
    publishBtn.disabled = true;
    clearPublishResult();
  }

  function showPublishPending() {
    publishResult.className = "";
    publishResult.textContent = "Publishing to dev.to...";
    publishResult.style.display = "block";
  }

  function showPublishError(msg) {
    publishResult.className = "error";
    publishResult.textContent = msg;
    publishResult.style.display = "block";
  }

  function showPublishSuccess(body) {
    publishResult.className = "success";
    publishResult.innerHTML = "";
    var head = document.createElement("strong");
    head.textContent = "Published to dev.to.";
    publishResult.appendChild(head);

    // Only treat the returned URL as a link if it is a real http(s) URL; never
    // trust an arbitrary scheme from an upstream response.
    if (body.url && /^https?:\/\//i.test(body.url)) {
      var urlLine = document.createElement("span");
      urlLine.className = "publish-meta";
      urlLine.appendChild(document.createTextNode("URL: "));
      var a = document.createElement("a");
      a.href = body.url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.textContent = body.url;
      urlLine.appendChild(a);
      publishResult.appendChild(urlLine);
    } else if (body.url) {
      var urlText = document.createElement("span");
      urlText.className = "publish-meta";
      urlText.textContent = "URL: " + body.url;
      publishResult.appendChild(urlText);
    }

    if (typeof body.id === "number" && body.id) {
      var idLine = document.createElement("span");
      idLine.className = "publish-meta";
      idLine.textContent = "Article ID: " + body.id;
      publishResult.appendChild(idLine);
    }
    publishResult.style.display = "block";
  }

  // handlePublishResponse maps each documented status to a clear message. The
  // server never returns the API key in any field, so nothing secret is shown.
  function handlePublishResponse(status, body) {
    body = body || {};
    if (status === 201 && body.success) {
      showPublishSuccess(body);
      return;
    }
    if (status === 400 && body.error === "missing_api_key") {
      showPublishError("Set DEVTO_API_KEY in the Console's environment, then publish again.");
      return;
    }
    if (status === 404) {
      showPublishError("No post.md was found for this run. Generate a blog post for it before publishing to dev.to.");
      return;
    }
    if (status === 502) {
      if (body.status_code) {
        showPublishError("dev.to rejected the publish (HTTP " + body.status_code + ")." +
          (body.error ? " " + body.error : ""));
      } else {
        showPublishError("Couldn't reach dev.to." +
          (body.error ? " " + body.error : " Check the connection and try again."));
      }
      return;
    }
    if (status === 422) {
      showPublishError(body.detail || "The publish request was invalid.");
      return;
    }
    showPublishError(body.detail || body.error || "Publishing failed. Please try again.");
  }

  // publishToDevto collects the form values + selected run_id and calls the
  // existing publish endpoint. The button is disabled while in flight.
  function publishToDevto() {
    if (!publishRunID) {
      showPublishError("Select a run in Run History first.");
      return;
    }
    var title = publishTitle.value.trim();
    var tags = publishTags.value
      .split(",")
      .map(function (t) { return t.trim(); })
      .filter(function (t) { return t.length > 0; });

    publishBtn.disabled = true;
    showPublishPending();

    fetch("/api/publish/devto", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        run_id: publishRunID,
        title: title,
        tags: tags,
        published: publishPublished.checked
      })
    })
      .then(function (resp) {
        return resp.json().catch(function () { return {}; }).then(function (body) {
          return { resp: resp, body: body };
        });
      })
      .then(function (r) {
        publishBtn.disabled = false;
        handlePublishResponse(r.resp.status, r.body);
      })
      .catch(function () {
        publishBtn.disabled = false;
        showPublishError("Could not reach the Console to publish. Check it's running and try again.");
      });
  }

  publishBtn.addEventListener("click", publishToDevto);

  // Start in the no-run-selected state.
  resetPublishPanel();
})();
