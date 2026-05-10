// Bullpen Web GUI — app.js
// Covers: panel navigation, run form, SSE, approval gate,
//         suggestions, run history, review panel, publish panel.

'use strict';

/* ============================================================
   State
   ============================================================ */
let eventSource = null;
let currentRunId = null;
let currentRunFiles = [];   // files present in the active run bundle

/* ============================================================
   Panel navigation
   ============================================================ */
function showPanel(name) {
  document.querySelectorAll('[data-panel]').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(btn => btn.classList.remove('active'));

  const panel = document.querySelector(`[data-panel="${name}"]`);
  if (panel) panel.classList.add('active');

  const btn = document.querySelector(`.nav-btn[data-target="${name}"]`);
  if (btn) btn.classList.add('active');
}

// Wire nav buttons
document.querySelectorAll('.nav-btn').forEach(btn => {
  btn.addEventListener('click', () => showPanel(btn.dataset.target));
});

/* ============================================================
   Utility helpers
   ============================================================ */
function showError(id, msg) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = msg;
  el.classList.remove('hidden');
}

function hideError(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = '';
  el.classList.add('hidden');
}

function setRunFormDisabled(disabled) {
  document.getElementById('run-btn').disabled = disabled;
  document.getElementById('topic-input').disabled = disabled;
  document.querySelectorAll('input[name="output_type"]').forEach(cb => cb.disabled = disabled);
}

function resetProgressView() {
  document.querySelectorAll('#agent-sequence li').forEach(li => {
    li.classList.remove('active', 'complete', 'error');
  });
  document.getElementById('approval-actions').classList.add('hidden');
  document.getElementById('verdict-area').textContent = '';
  hideError('pipeline-error');
}

/* ============================================================
   11.1 — Run form submission
   ============================================================ */
document.getElementById('run-form').addEventListener('submit', async (e) => {
  e.preventDefault();

  const topic = document.getElementById('topic-input').value.trim();
  if (!topic) {
    showError('run-error', 'Topic is required');
    return;
  }

  const outputs = [...document.querySelectorAll('input[name="output_type"]:checked')].map(cb => cb.value);
  if (outputs.length === 0) {
    showError('run-error', 'Select at least one output type');
    return;
  }

  setRunFormDisabled(true);
  hideError('run-error');

  try {
    const resp = await fetch('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ topic, outputs }),
    });

    if (!resp.ok) {
      const err = await resp.json();
      showError('run-error', err.detail || 'Failed to start pipeline');
      setRunFormDisabled(false);
      return;
    }

    const { run_id } = await resp.json();
    currentRunId = run_id;
    showPanel('progress');
    resetProgressView();
    startSSE();
  } catch (err) {
    showError('run-error', 'Network error: ' + err.message);
    setRunFormDisabled(false);
  }
});

/* ============================================================
   11.2 — SSE event handling
   ============================================================ */
function startSSE() {
  if (eventSource) eventSource.close();
  eventSource = new EventSource('/api/run/status');

  eventSource.onmessage = (e) => {
    handlePipelineEvent(JSON.parse(e.data));
  };

  eventSource.addEventListener('pipeline_complete', (e) => {
    const data = JSON.parse(e.data);
    handlePipelineComplete(data);
    eventSource.close();
    eventSource = null;
    setRunFormDisabled(false);
  });

  eventSource.onerror = () => {
    showError('pipeline-error', 'Lost connection to pipeline');
    setRunFormDisabled(false);
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
  };
}

function handlePipelineEvent(data) {
  const type = data.event_type;
  if (type === 'agent_invoked') {
    markAgent(data.details?.agent_type, 'active');
  } else if (type === 'agent_completed') {
    markAgent(data.details?.agent_type, 'complete');
  } else if (type === 'agent_error') {
    markAgent(data.details?.agent_type, 'error');
    showError('pipeline-error', data.details?.error || 'Agent error');
  } else if (type === 'verdict') {
    showVerdict(data.details);
  } else if (type === 'approval_gate_presented') {
    showApprovalActions();
  } else if (type === 'pipeline_complete') {
    handlePipelineComplete(data.details);
  }
}

function markAgent(agentType, state) {
  const li = document.querySelector(`#agent-sequence [data-agent="${agentType}"]`);
  if (li) {
    li.classList.remove('active', 'complete', 'error');
    li.classList.add(state);
  }
}

function showVerdict(details) {
  const area = document.getElementById('verdict-area');
  if (!details) return;
  if (typeof details === 'object' && details.verdict) {
    const verdict = details.verdict;
    const feedback = details.feedback ? ` — ${details.feedback}` : '';
    const filename = details.filename ? `${details.filename}: ` : '';
    area.textContent = `${filename}${verdict}${feedback}`;
  } else {
    area.textContent = JSON.stringify(details);
  }
}

function showApprovalActions() {
  document.getElementById('approval-actions').classList.remove('hidden');
  // Also navigate to progress panel so the user sees the buttons
  showPanel('progress');
}

function handlePipelineComplete(details) {
  if (details?.status === 'error') {
    showError('pipeline-error', details.error || 'Pipeline failed');
  }
  loadRunHistory();
}

/* ============================================================
   11.3 — Approve / Reject button handlers
   ============================================================ */
document.getElementById('approve-btn').addEventListener('click', async () => {
  disableApprovalButtons();
  await fetch('/api/run/approve', { method: 'POST' });
  document.getElementById('approval-actions').classList.add('hidden');
  document.getElementById('verdict-area').textContent = 'Approved — pipeline continuing...';
});

document.getElementById('reject-btn').addEventListener('click', async () => {
  disableApprovalButtons();
  await fetch('/api/run/reject', { method: 'POST' });
  document.getElementById('approval-actions').classList.add('hidden');
  document.getElementById('verdict-area').textContent = 'Rejected — pipeline stopped.';
});

function disableApprovalButtons() {
  document.getElementById('approve-btn').disabled = true;
  document.getElementById('reject-btn').disabled = true;
}

/* ============================================================
   12.1 — Suggestions loading (Ideas Panel)
   ============================================================ */
async function loadSuggestions() {
  const container = document.getElementById('suggestions-list');
  container.innerHTML = '';

  try {
    const resp = await fetch('/api/suggestions');
    const data = await resp.json();

    if (data.warning) {
      const warn = document.createElement('div');
      warn.className = 'suggestions-warning';
      warn.textContent = data.warning;
      container.appendChild(warn);
    }

    const suggestions = Array.isArray(data) ? data : (data.suggestions || []);
    suggestions.forEach(s => {
      const item = document.createElement('div');
      item.className = 'suggestion-item';

      const topicSpan = document.createElement('span');
      topicSpan.className = 'suggestion-topic';
      topicSpan.textContent = s.topic;

      const metaSpan = document.createElement('span');
      metaSpan.className = 'suggestion-meta';
      metaSpan.textContent = s.last_covered ? `Last: ${s.last_covered}` : 'Never covered';

      const useBtn = document.createElement('button');
      useBtn.type = 'button';
      useBtn.textContent = 'Use this topic';
      useBtn.addEventListener('click', () => {
        document.getElementById('topic-input').value = s.topic;
        document.getElementById('topic-input').focus();
      });

      item.appendChild(topicSpan);
      item.appendChild(metaSpan);
      item.appendChild(useBtn);
      container.appendChild(item);
    });
  } catch (err) {
    const warn = document.createElement('div');
    warn.className = 'suggestions-warning';
    warn.textContent = 'Could not load suggestions — check AWS credentials and region';
    container.appendChild(warn);
  }
}

/* ============================================================
   12.2 — Run history loading
   ============================================================ */
async function loadRunHistory() {
  try {
    const resp = await fetch('/api/runs');
    if (!resp.ok) return;
    const runs = await resp.json();

    const list = document.getElementById('run-history-list');
    list.innerHTML = '';

    runs.forEach(run => {
      const li = document.createElement('li');

      const nameDiv = document.createElement('div');
      nameDiv.className = 'run-history-name';
      nameDiv.textContent = run.run_id;

      const filesDiv = document.createElement('div');
      filesDiv.className = 'run-history-files';
      filesDiv.textContent = (run.files || []).join(', ') || 'No output files';

      li.appendChild(nameDiv);
      li.appendChild(filesDiv);

      li.addEventListener('click', () => {
        document.querySelectorAll('#run-history-list li').forEach(el => el.classList.remove('selected'));
        li.classList.add('selected');
        selectRun(run.run_id, run.files || []);
      });

      list.appendChild(li);
    });
  } catch (err) {
    // History load failure is non-blocking
    console.warn('Could not load run history:', err);
  }
}

function selectRun(runId, files) {
  currentRunId = runId;
  currentRunFiles = files;
  populateFileSelector(files);
  updatePublishPanelButtons(files);
  showPanel('review');

  // Load first reviewable file
  const reviewable = files.filter(f => f !== 'agent-log.jsonl' && f !== 'checkpoints.json');
  if (reviewable.length > 0) {
    loadFile(runId, reviewable[0]);
  } else {
    document.getElementById('content-area').innerHTML = '<p><em>No reviewable files for this run.</em></p>';
  }
}

/* ============================================================
   Review Panel — file selector
   ============================================================ */
function populateFileSelector(files) {
  const sel = document.getElementById('file-selector');
  sel.innerHTML = '';
  const reviewable = files.filter(f => f !== 'agent-log.jsonl' && f !== 'checkpoints.json');
  reviewable.forEach(f => {
    const opt = document.createElement('option');
    opt.value = f;
    opt.textContent = f;
    sel.appendChild(opt);
  });
}

document.getElementById('file-selector').addEventListener('change', (e) => {
  if (currentRunId && e.target.value) {
    loadFile(currentRunId, e.target.value);
  }
});

async function loadFile(runId, filename) {
  const contentArea = document.getElementById('content-area');
  contentArea.innerHTML = '<p><em>Loading...</em></p>';

  try {
    const resp = await fetch(`/api/runs/${encodeURIComponent(runId)}/file?name=${encodeURIComponent(filename)}`);
    if (!resp.ok) {
      contentArea.innerHTML = '<p class="error">File not generated for this run.</p>';
      return;
    }
    const raw = await resp.text();

    // Sync file selector to the loaded file
    const sel = document.getElementById('file-selector');
    if (sel.value !== filename) sel.value = filename;

    renderFileContent(runId, filename, raw);
  } catch (err) {
    contentArea.innerHTML = `<p class="error">Could not load file: ${err.message}</p>`;
  }
}

function renderFileContent(runId, filename, raw) {
  const contentArea = document.getElementById('content-area');

  if (filename.endsWith('.md')) {
    // Detect MIKE placeholders before rendering
    const processed = processMikePlaceholders(raw, runId, filename);
    contentArea.innerHTML = processed;
  } else {
    // Plain text files
    const pre = document.createElement('pre');
    pre.textContent = raw;
    contentArea.innerHTML = '';
    contentArea.appendChild(pre);
  }
}

/* ============================================================
   13.2 — MIKE placeholder detection and inline editing
   ============================================================ */
const MIKE_RE = /<!--\s*MIKE:\s*(.*?)\s*-->/g;

function processMikePlaceholders(raw, runId, filename) {
  // Split on MIKE placeholders, interleave rendered Markdown with editable divs
  const parts = [];
  let lastIndex = 0;
  let match;

  MIKE_RE.lastIndex = 0;
  while ((match = MIKE_RE.exec(raw)) !== null) {
    const before = raw.slice(lastIndex, match.index);
    if (before) {
      parts.push({ type: 'md', content: before });
    }
    parts.push({ type: 'mike', instruction: match[1], original: match[0] });
    lastIndex = match.index + match[0].length;
  }
  const after = raw.slice(lastIndex);
  if (after) {
    parts.push({ type: 'md', content: after });
  }

  // Build HTML string
  let html = '';
  parts.forEach(part => {
    if (part.type === 'md') {
      if (typeof marked !== 'undefined') {
        html += marked.parse(part.content);
      } else {
        html += `<pre>${escapeHtml(part.content)}</pre>`;
      }
    } else {
      const escaped = escapeHtml(part.instruction);
      html += `<div class="mike-placeholder" data-instruction="${escaped}" data-original="${escapeHtml(part.original)}" data-run-id="${escapeHtml(runId)}" data-filename="${escapeHtml(filename)}">${escaped}</div>`;
    }
  });

  return html;
}

function escapeHtml(str) {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// Delegate click on MIKE placeholders (content area may be re-rendered)
document.getElementById('content-area').addEventListener('click', (e) => {
  const placeholder = e.target.closest('.mike-placeholder');
  if (placeholder) {
    activateMikeEditor(placeholder);
  }
});

function activateMikeEditor(placeholder) {
  const instruction = placeholder.dataset.instruction || '';
  const runId = placeholder.dataset.runId;
  const filename = placeholder.dataset.filename;

  const editorDiv = document.createElement('div');
  editorDiv.className = 'mike-editor';

  const textarea = document.createElement('textarea');
  textarea.placeholder = instruction;
  textarea.rows = 4;

  const actions = document.createElement('div');
  actions.className = 'mike-editor-actions';

  const saveBtn = document.createElement('button');
  saveBtn.type = 'button';
  saveBtn.textContent = 'Save';

  const cancelBtn = document.createElement('button');
  cancelBtn.type = 'button';
  cancelBtn.textContent = 'Cancel';
  cancelBtn.style.background = '#fff';
  cancelBtn.style.color = '#333';
  cancelBtn.style.borderColor = '#ccc';

  actions.appendChild(saveBtn);
  actions.appendChild(cancelBtn);
  editorDiv.appendChild(textarea);
  editorDiv.appendChild(actions);

  placeholder.replaceWith(editorDiv);
  textarea.focus();

  cancelBtn.addEventListener('click', () => {
    // Reload the file to restore the placeholder
    if (runId && filename) loadFile(runId, filename);
  });

  saveBtn.addEventListener('click', async () => {
    const userText = textarea.value;
    saveBtn.disabled = true;
    cancelBtn.disabled = true;

    // Read the current raw file, replace the placeholder, save
    try {
      const getResp = await fetch(`/api/runs/${encodeURIComponent(runId)}/file?name=${encodeURIComponent(filename)}`);
      if (!getResp.ok) throw new Error('Could not read file for saving');
      const raw = await getResp.text();

      // Replace the specific placeholder with user text
      const original = placeholder.dataset.original || '';
      const updated = original
        ? raw.replace(original, userText)
        : raw; // fallback: no-op if original not tracked

      const postResp = await fetch(`/api/runs/${encodeURIComponent(runId)}/file`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: filename, content: updated }),
      });

      if (!postResp.ok) {
        const err = await postResp.json().catch(() => ({}));
        throw new Error(err.detail || 'Save failed');
      }

      showSaveStatus('Saved', 'success');
      // Reload to reflect saved state
      loadFile(runId, filename);
    } catch (err) {
      showSaveStatus('Save failed: ' + err.message, 'error');
      saveBtn.disabled = false;
      cancelBtn.disabled = false;
    }
  });
}

function showSaveStatus(msg, type) {
  const el = document.getElementById('save-status');
  el.textContent = msg;
  el.className = type;
  if (type === 'success') {
    setTimeout(() => { el.textContent = ''; el.className = ''; }, 3000);
  }
}

/* ============================================================
   Publish Panel — button visibility
   ============================================================ */
function updatePublishPanelButtons(files) {
  const hasLinkedIn = files.includes('digest-email.txt');
  const hasScript = files.includes('script.md');

  document.getElementById('copy-linkedin-btn').classList.toggle('hidden', !hasLinkedIn);
  document.getElementById('download-script-btn').classList.toggle('hidden', !hasScript);
}

/* ============================================================
   14.1 — dev.to publish and draft
   ============================================================ */
document.getElementById('publish-devto-btn').addEventListener('click', () => publishToDevTo(true));
document.getElementById('draft-devto-btn').addEventListener('click', () => publishToDevTo(false));

async function publishToDevTo(published) {
  const title = document.getElementById('publish-title').value.trim();
  if (!title) {
    setPublishStatus('Title is required before publishing.', 'error');
    return;
  }

  const tagsRaw = document.getElementById('publish-tags').value.trim();
  const tags = tagsRaw ? tagsRaw.split(',').map(t => t.trim()).filter(Boolean) : [];

  if (!currentRunId) {
    setPublishStatus('No run selected.', 'error');
    return;
  }

  document.getElementById('publish-devto-btn').disabled = true;
  document.getElementById('draft-devto-btn').disabled = true;
  setPublishStatus('Publishing...', '');

  try {
    const resp = await fetch('/api/publish/devto', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: currentRunId, title, tags, published }),
    });

    const data = await resp.json();

    if (resp.ok) {
      const url = data.url || data.canonical_url || '';
      setPublishStatus(
        published
          ? `Published! ${url}`
          : `Draft saved. ${url}`,
        'success'
      );
    } else {
      setPublishStatus(`Error ${resp.status}: ${data.detail || JSON.stringify(data)}`, 'error');
    }
  } catch (err) {
    setPublishStatus('Could not reach dev.to — check your network connection', 'error');
  } finally {
    document.getElementById('publish-devto-btn').disabled = false;
    document.getElementById('draft-devto-btn').disabled = false;
  }
}

function setPublishStatus(msg, type) {
  const el = document.getElementById('publish-status');
  el.textContent = msg;
  el.className = type;
}

/* ============================================================
   14.2 — Copy LinkedIn post
   ============================================================ */
document.getElementById('copy-linkedin-btn').addEventListener('click', async () => {
  if (!currentRunId) return;

  try {
    const resp = await fetch(`/api/runs/${encodeURIComponent(currentRunId)}/file?name=digest-email.txt`);
    if (!resp.ok) throw new Error('Could not load digest-email.txt');
    const text = await resp.text();

    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      const btn = document.getElementById('copy-linkedin-btn');
      const original = btn.textContent;
      btn.textContent = 'Copied!';
      setTimeout(() => { btn.textContent = original; }, 3000);
    } else {
      // Fallback: show selectable textarea
      showCopyFallback(text);
    }
  } catch (err) {
    setPublishStatus('Could not copy: ' + err.message, 'error');
  }
});

function showCopyFallback(text) {
  const existing = document.getElementById('copy-fallback-area');
  if (existing) existing.remove();

  const ta = document.createElement('textarea');
  ta.id = 'copy-fallback-area';
  ta.value = text;
  ta.rows = 6;
  ta.style.width = '100%';
  ta.style.marginTop = '8px';
  ta.readOnly = true;
  document.getElementById('publish-status').after(ta);
  ta.select();
}

/* ============================================================
   14.3 — Download YouTube script
   ============================================================ */
document.getElementById('download-script-btn').addEventListener('click', () => {
  if (!currentRunId) return;
  const url = `/api/runs/${encodeURIComponent(currentRunId)}/download/script.md`;
  const a = document.createElement('a');
  a.href = url;
  a.download = 'script.md';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
});

/* ============================================================
   Initialisation
   ============================================================ */
function init() {
  showPanel('ideas');
  loadSuggestions();
  loadRunHistory();
}

init();
