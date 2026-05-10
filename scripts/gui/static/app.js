// Bullpen Web GUI

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

window._currentRunId = null;
window._currentFileName = null;

// ---------------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------------

document.querySelectorAll('.nav-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const target = btn.dataset.target;
    document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    document.querySelectorAll('[data-panel]').forEach(p => p.classList.remove('active'));
    const panel = document.querySelector(`[data-panel="${target}"]`);
    if (panel) panel.classList.add('active');
  });
});

function showPanel(name) {
  document.querySelectorAll('.nav-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.target === name);
  });
  document.querySelectorAll('[data-panel]').forEach(p => {
    p.classList.toggle('active', p.dataset.panel === name);
  });
}

// ---------------------------------------------------------------------------
// Utility
// ---------------------------------------------------------------------------

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function escapeAttr(str) {
  return String(str)
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// ---------------------------------------------------------------------------
// MIKE placeholder pattern: <!-- MIKE: [instruction, ~N words] -->
// ---------------------------------------------------------------------------

const MIKE_PATTERN = /<!--\s*MIKE:\s*\[([^\]]+)\]\s*-->/g;

function renderWithMikePlaceholders(rawText, runId, filename) {
  const area = document.getElementById('content-area');

  // Replace MIKE placeholders with tokens BEFORE markdown rendering
  let processedText = rawText;
  const placeholders = [];
  let idx = 0;

  processedText = rawText.replace(MIKE_PATTERN, (match, instruction) => {
    const placeholder = `MIKE_PLACEHOLDER_${idx}`;
    placeholders.push({ placeholder, instruction, original: match });
    idx++;
    return placeholder;
  });

  // Reset regex state after use
  MIKE_PATTERN.lastIndex = 0;

  // Render markdown
  let html = typeof marked !== 'undefined'
    ? marked.parse(processedText)
    : processedText.replace(/\n/g, '<br>');

  // Replace placeholder tokens with interactive divs
  placeholders.forEach(({ placeholder, instruction }) => {
    const div = `<div class="mike-placeholder" data-instruction="${escapeAttr(instruction)}" onclick="editMikePlaceholder(this, ${JSON.stringify(runId)}, ${JSON.stringify(filename)})">${escapeHtml(instruction)}</div>`;
    html = html.replace(placeholder, div);
  });

  area.innerHTML = html;
}

function editMikePlaceholder(el, runId, filename) {
  const instruction = el.dataset.instruction;

  const editor = document.createElement('div');
  editor.className = 'mike-editor';
  editor.innerHTML = `
    <textarea placeholder="${escapeAttr(instruction)}" rows="4"></textarea>
    <div class="mike-editor-actions">
      <button onclick="saveMikePlaceholder(this, ${JSON.stringify(runId)}, ${JSON.stringify(filename)})">Save</button>
      <button onclick="cancelMikeEdit(this)">Cancel</button>
    </div>
  `;

  el.replaceWith(editor);
  editor.querySelector('textarea').focus();
}

async function saveMikePlaceholder(btn, runId, filename) {
  const editor = btn.closest('.mike-editor');
  const textarea = editor.querySelector('textarea');
  const userText = textarea.value;

  // Read current file, replace the placeholder with user text
  try {
    const resp = await fetch(`/api/runs/${runId}/file?name=${encodeURIComponent(filename)}`);
    if (!resp.ok) throw new Error('Could not read file');
    let content = await resp.text();

    // Replace the first unresolved MIKE placeholder with user text
    MIKE_PATTERN.lastIndex = 0;
    let replaced = false;
    content = content.replace(MIKE_PATTERN, (match) => {
      if (!replaced) {
        replaced = true;
        return userText;
      }
      return match;
    });
    // Reset regex state
    MIKE_PATTERN.lastIndex = 0;

    const saveResp = await fetch(`/api/runs/${runId}/file`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: filename, content })
    });

    if (!saveResp.ok) throw new Error('Save failed');

    // Show saved confirmation
    const status = document.getElementById('save-status');
    status.textContent = 'Saved';
    status.className = 'success';
    setTimeout(() => { status.textContent = ''; status.className = ''; }, 3000);

    // Reload the file to reflect changes
    loadFile(runId, filename);

  } catch (err) {
    const status = document.getElementById('save-status');
    status.textContent = 'Error: ' + err.message;
    status.className = 'error';
    // Retain the editor so user can try again
  }
}

function cancelMikeEdit(btn) {
  const editor = btn.closest('.mike-editor');
  // Reload the file to restore the placeholder
  if (window._currentRunId && window._currentFileName) {
    loadFile(window._currentRunId, window._currentFileName);
  }
}

// ---------------------------------------------------------------------------
// Review Panel — file loading
// ---------------------------------------------------------------------------

function renderContent(rawText, runId, filename) {
  if (runId && filename) {
    renderWithMikePlaceholders(rawText, runId, filename);
  } else {
    const area = document.getElementById('content-area');
    area.innerHTML = typeof marked !== 'undefined'
      ? marked.parse(rawText)
      : rawText.replace(/\n/g, '<br>');
  }
}

async function loadFile(runId, filename) {
  window._currentRunId = runId;
  window._currentFileName = filename;

  try {
    const resp = await fetch(`/api/runs/${runId}/file?name=${encodeURIComponent(filename)}`);
    if (resp.status === 404) {
      document.getElementById('content-area').innerHTML =
        '<p class="error">File not generated for this run.</p>';
      return;
    }
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const text = await resp.text();
    renderContent(text, runId, filename);
  } catch (err) {
    document.getElementById('content-area').innerHTML =
      `<p class="error">Could not load file: ${escapeHtml(err.message)}</p>`;
  }
}

// ---------------------------------------------------------------------------
// Run History
// ---------------------------------------------------------------------------

async function loadRunHistory() {
  try {
    const resp = await fetch('/api/runs');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    const list = document.getElementById('run-history-list');
    list.innerHTML = '';

    if (!data.runs || data.runs.length === 0) {
      list.innerHTML = '<li>No previous runs found.</li>';
      return;
    }

    data.runs.forEach(run => {
      const li = document.createElement('li');
      li.innerHTML = `
        <div class="run-history-name">${escapeHtml(run.id)}</div>
        <div class="run-history-files">${run.files.map(f => escapeHtml(f)).join(', ') || 'No files'}</div>
      `;
      li.addEventListener('click', () => {
        document.querySelectorAll('#run-history-list li').forEach(el => el.classList.remove('selected'));
        li.classList.add('selected');
        selectRun(run);
      });
      list.appendChild(li);
    });
  } catch (err) {
    document.getElementById('run-history-list').innerHTML =
      `<li class="error">Could not load run history: ${escapeHtml(err.message)}</li>`;
  }
}

function selectRun(run) {
  window._currentRunId = run.id;

  // Populate file selector
  const selector = document.getElementById('file-selector');
  selector.innerHTML = '';
  const reviewableFiles = run.files.filter(f => f !== 'agent-log.jsonl' && f !== 'checkpoints.json');

  if (reviewableFiles.length === 0) {
    document.getElementById('content-area').innerHTML = '<p>No reviewable files for this run.</p>';
    return;
  }

  reviewableFiles.forEach(f => {
    const opt = document.createElement('option');
    opt.value = f;
    opt.textContent = f;
    selector.appendChild(opt);
  });

  // Load first file
  loadFile(run.id, reviewableFiles[0]);
  showPanel('review');
}

// File selector change handler
document.getElementById('file-selector').addEventListener('change', (e) => {
  if (window._currentRunId && e.target.value) {
    loadFile(window._currentRunId, e.target.value);
  }
});

// ---------------------------------------------------------------------------
// Suggestions (Ideas Panel)
// ---------------------------------------------------------------------------

async function loadSuggestions() {
  try {
    const resp = await fetch('/api/suggestions');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    const list = document.getElementById('suggestions-list');
    list.innerHTML = '';

    if (data.warning) {
      const warn = document.createElement('div');
      warn.className = 'suggestions-warning';
      warn.textContent = data.warning;
      list.appendChild(warn);
    }

    if (!data.suggestions || data.suggestions.length === 0) return;

    data.suggestions.slice(0, 10).forEach(s => {
      const item = document.createElement('div');
      item.className = 'suggestion-item';
      const lastCovered = s.last_covered ? s.last_covered : 'Never';
      item.innerHTML = `
        <span class="suggestion-topic">${escapeHtml(s.topic)}</span>
        <span class="suggestion-meta">Last covered: ${escapeHtml(lastCovered)}</span>
        <button onclick="useSuggestion(${JSON.stringify(s.topic)})">Use this topic</button>
      `;
      list.appendChild(item);
    });
  } catch (err) {
    const list = document.getElementById('suggestions-list');
    const warn = document.createElement('div');
    warn.className = 'suggestions-warning';
    warn.textContent = 'Could not load suggestions — check AWS credentials and region';
    list.appendChild(warn);
  }
}

function useSuggestion(topic) {
  document.getElementById('topic-input').value = topic;
}

// ---------------------------------------------------------------------------
// Pipeline run form
// ---------------------------------------------------------------------------

document.getElementById('run-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const errorEl = document.getElementById('run-error');
  errorEl.classList.add('hidden');
  errorEl.textContent = '';

  const topic = document.getElementById('topic-input').value.trim();
  if (!topic) {
    errorEl.textContent = 'Please enter a topic.';
    errorEl.classList.remove('hidden');
    return;
  }

  const outputs = Array.from(document.querySelectorAll('input[name="output_type"]:checked'))
    .map(cb => cb.value);
  if (outputs.length === 0) {
    errorEl.textContent = 'Please select at least one output type.';
    errorEl.classList.remove('hidden');
    return;
  }

  const runBtn = document.getElementById('run-btn');
  runBtn.disabled = true;
  document.getElementById('topic-input').disabled = true;

  try {
    const resp = await fetch('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ topic, outputs })
    });

    const data = await resp.json();

    if (resp.status === 409) {
      errorEl.textContent = 'A pipeline run is already in progress.';
      errorEl.classList.remove('hidden');
      runBtn.disabled = false;
      document.getElementById('topic-input').disabled = false;
      return;
    }

    if (!resp.ok) {
      errorEl.textContent = data.detail || 'Failed to start pipeline.';
      errorEl.classList.remove('hidden');
      runBtn.disabled = false;
      document.getElementById('topic-input').disabled = false;
      return;
    }

    window._currentRunId = data.run_id;
    showPanel('progress');
    startSSE();

  } catch (err) {
    errorEl.textContent = 'Network error: ' + err.message;
    errorEl.classList.remove('hidden');
    runBtn.disabled = false;
    document.getElementById('topic-input').disabled = false;
  }
});

// ---------------------------------------------------------------------------
// SSE — pipeline progress
// ---------------------------------------------------------------------------

let _sseSource = null;

function startSSE() {
  if (_sseSource) {
    _sseSource.close();
    _sseSource = null;
  }

  // Reset agent sequence
  document.querySelectorAll('#agent-sequence li').forEach(li => {
    li.classList.remove('active', 'complete', 'error');
  });
  document.getElementById('verdict-area').innerHTML = '';
  document.getElementById('pipeline-error').classList.add('hidden');
  document.getElementById('approval-actions').classList.add('hidden');

  _sseSource = new EventSource('/api/run/status');

  _sseSource.onmessage = (e) => {
    try {
      const event = JSON.parse(e.data);
      handlePipelineEvent(event);
    } catch (_) { /* ignore malformed frames */ }
  };

  _sseSource.onerror = () => {
    const errEl = document.getElementById('pipeline-error');
    errEl.textContent = 'Lost connection to server. Refresh to reconnect.';
    errEl.classList.remove('hidden');
  };
}

function handlePipelineEvent(event) {
  switch (event.event_type) {
    case 'agent_invoked':
      markAgentActive(event.agent_type);
      break;
    case 'agent_completed':
      markAgentDone(event.agent_type);
      break;
    case 'agent_error':
      showPipelineError(event);
      break;
    case 'verdict':
      showVerdict(event.details || event);
      break;
    case 'approval_gate_presented':
      showApprovalButtons();
      break;
    case 'pipeline_complete':
      onPipelineComplete(event);
      break;
  }
}

function markAgentActive(agentType) {
  document.querySelectorAll('#agent-sequence li').forEach(li => {
    if (li.dataset.agent === agentType) {
      li.classList.add('active');
      li.classList.remove('complete', 'error');
    }
  });
}

function markAgentDone(agentType) {
  document.querySelectorAll('#agent-sequence li').forEach(li => {
    if (li.dataset.agent === agentType) {
      li.classList.remove('active', 'error');
      li.classList.add('complete');
    }
  });
}

function showPipelineError(event) {
  const errEl = document.getElementById('pipeline-error');
  const msg = (event.details && event.details.message) || event.message || 'An error occurred.';
  errEl.textContent = msg;
  errEl.classList.remove('hidden');
}

function showVerdict(details) {
  const area = document.getElementById('verdict-area');
  const filename = details.filename || '';
  const verdict = details.verdict || '';
  const feedback = details.feedback || '';
  area.innerHTML = `<strong>${escapeHtml(filename)}</strong>: ${escapeHtml(verdict)}<br>${escapeHtml(feedback)}`;
}

function showApprovalButtons() {
  markAgentActive('approval_gate');
  document.getElementById('approval-actions').classList.remove('hidden');
  document.getElementById('approve-btn').disabled = false;
  document.getElementById('reject-btn').disabled = false;
}

function onPipelineComplete(event) {
  if (_sseSource) {
    _sseSource.close();
    _sseSource = null;
  }

  const status = event.status || 'unknown';
  const verdictArea = document.getElementById('verdict-area');
  const existing = verdictArea.innerHTML;
  verdictArea.innerHTML = existing + `<p><strong>Pipeline ${escapeHtml(status)}.</strong></p>`;

  // Re-enable run controls
  document.getElementById('run-btn').disabled = false;
  document.getElementById('topic-input').disabled = false;

  // Reload run history
  loadRunHistory();

  // Load review panel if successful
  if (status === 'success' && window._currentRunId) {
    loadRunFilesForReview(window._currentRunId);
  }
}

async function loadRunFilesForReview(runId) {
  try {
    const resp = await fetch('/api/runs');
    if (!resp.ok) return;
    const data = await resp.json();
    const run = data.runs.find(r => r.id === runId);
    if (run) selectRun(run);
  } catch (_) { /* best-effort */ }
}

// ---------------------------------------------------------------------------
// Approval gate buttons
// ---------------------------------------------------------------------------

document.getElementById('approve-btn').addEventListener('click', async () => {
  document.getElementById('approve-btn').disabled = true;
  document.getElementById('reject-btn').disabled = true;
  try {
    await fetch('/api/run/approve', { method: 'POST' });
    markAgentDone('approval_gate');
  } catch (err) {
    showPipelineError({ message: 'Approve failed: ' + err.message });
  }
});

document.getElementById('reject-btn').addEventListener('click', async () => {
  document.getElementById('approve-btn').disabled = true;
  document.getElementById('reject-btn').disabled = true;
  try {
    await fetch('/api/run/reject', { method: 'POST' });
    markAgentDone('approval_gate');
  } catch (err) {
    showPipelineError({ message: 'Reject failed: ' + err.message });
  }
});

// ---------------------------------------------------------------------------
// Publish Panel
// ---------------------------------------------------------------------------

document.getElementById('publish-devto-btn').addEventListener('click', () => publishToDevTo(true));
document.getElementById('draft-devto-btn').addEventListener('click', () => publishToDevTo(false));

async function publishToDevTo(published) {
  const statusEl = document.getElementById('publish-status');
  statusEl.textContent = '';
  statusEl.className = '';

  const title = document.getElementById('publish-title').value.trim();
  if (!title) {
    statusEl.textContent = 'Please enter an article title.';
    statusEl.className = 'error';
    return;
  }

  const tagsRaw = document.getElementById('publish-tags').value;
  const tags = tagsRaw.split(',').map(t => t.trim()).filter(Boolean).slice(0, 4);

  try {
    const resp = await fetch('/api/publish/devto', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        run_id: window._currentRunId,
        title,
        tags,
        published
      })
    });

    const data = await resp.json();

    if (!resp.ok) {
      statusEl.textContent = `Error ${resp.status}: ${data.detail || data.error || 'Unknown error'}`;
      statusEl.className = 'error';
      return;
    }

    statusEl.textContent = published
      ? `Published: ${data.url || data.canonical_url || 'success'}`
      : `Draft saved: ${data.url || data.canonical_url || 'success'}`;
    statusEl.className = 'success';

  } catch (err) {
    statusEl.textContent = 'Could not reach dev.to — check your network connection';
    statusEl.className = 'error';
  }
}

document.getElementById('copy-linkedin-btn').addEventListener('click', async () => {
  const statusEl = document.getElementById('publish-status');
  try {
    const resp = await fetch(`/api/runs/${window._currentRunId}/file?name=digest-email.txt`);
    if (!resp.ok) throw new Error('Could not read file');
    const text = await resp.text();

    if (navigator.clipboard) {
      await navigator.clipboard.writeText(text);
      statusEl.textContent = 'Copied!';
      statusEl.className = 'success';
      setTimeout(() => { statusEl.textContent = ''; statusEl.className = ''; }, 3000);
    } else {
      // Fallback: show selectable textarea
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.width = '100%';
      ta.rows = 8;
      statusEl.textContent = '';
      statusEl.className = '';
      statusEl.appendChild(ta);
      ta.select();
    }
  } catch (err) {
    statusEl.textContent = 'Could not copy: ' + err.message;
    statusEl.className = 'error';
  }
});

document.getElementById('download-script-btn').addEventListener('click', () => {
  if (window._currentRunId) {
    window.location.href = `/api/runs/${window._currentRunId}/download/script.md`;
  }
});

// ---------------------------------------------------------------------------
// Initialise on page load
// ---------------------------------------------------------------------------

document.addEventListener('DOMContentLoaded', () => {
  loadSuggestions();
  loadRunHistory();
});
