// Bullpen Web GUI — app.js
// All frontend logic: panel navigation, pipeline control, SSE handling,
// suggestions, run history, review panel, MIKE placeholders, publish actions.

// ============================================================
// Shared state
// ============================================================

/** The run_id of the currently active (or most recently loaded) pipeline run. */
let currentRunId = null;

/** The active EventSource for SSE log tailing. */
let eventSource = null;

/** Raw Markdown content of the currently displayed file (used for MIKE edits). */
let currentRawMarkdown = '';

/** The filename currently displayed in the Review Panel. */
let currentFileName = '';

// ============================================================
// Utility functions
// ============================================================

/**
 * Escape HTML special characters to prevent XSS when inserting
 * user-supplied or server-supplied text into innerHTML.
 */
function escapeHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/**
 * Show an error message in the given element (by id).
 * Removes the 'hidden' class and sets the text content.
 */
function showError(elementId, message) {
  const el = document.getElementById(elementId);
  if (!el) return;
  el.textContent = message;
  el.classList.remove('hidden');
}

/**
 * Hide an error element (by id).
 */
function hideError(elementId) {
  const el = document.getElementById(elementId);
  if (!el) return;
  el.textContent = '';
  el.classList.add('hidden');
}

/**
 * Show a status message in the Publish Panel status area.
 * cls should be 'success', 'error', or '' for neutral.
 */
function showPublishStatus(msg, cls) {
  const el = document.getElementById('publish-status');
  if (!el) return;
  el.textContent = msg;
  el.className = cls;
}

// ============================================================
// Panel navigation
// ============================================================

/**
 * Show the panel with the given name and update the nav button state.
 * Panels are identified by their data-panel attribute.
 */
function showPanel(name) {
  document.querySelectorAll('[data-panel]').forEach(section => {
    section.classList.toggle('active', section.dataset.panel === name);
  });
  document.querySelectorAll('.nav-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.target === name);
  });
}

// ============================================================
// Run form submission (Task 11.1)
// ============================================================

/**
 * Handle the Run Pipeline form submission.
 * Validates inputs, POSTs to /api/run, starts SSE, navigates to Progress.
 */
async function handleRunSubmit(e) {
  e.preventDefault();
  hideError('run-error');

  const topic = document.getElementById('topic-input').value.trim();
  if (!topic) {
    showError('run-error', 'Topic is required.');
    return;
  }

  const outputCheckboxes = document.querySelectorAll('input[name="output_type"]:checked');
  if (outputCheckboxes.length === 0) {
    showError('run-error', 'Select at least one output type.');
    return;
  }

  const outputs = Array.from(outputCheckboxes).map(cb => cb.value);

  // Disable form while running
  setRunFormDisabled(true);

  try {
    const resp = await fetch('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ topic, outputs })
    });

    const data = await resp.json();

    if (resp.status === 202) {
      currentRunId = data.run_id;
      showPanel('progress');
      resetProgressView();
      startSSE();
    } else if (resp.status === 409) {
      showError('run-error', 'A pipeline run is already in progress.');
      setRunFormDisabled(false);
    } else {
      showError('run-error', data.detail || 'Failed to start pipeline.');
      setRunFormDisabled(false);
    }
  } catch (err) {
    showError('run-error', 'Network error: ' + err.message);
    setRunFormDisabled(false);
  }
}

/**
 * Enable or disable the run form controls.
 */
function setRunFormDisabled(disabled) {
  document.getElementById('run-btn').disabled = disabled;
  document.getElementById('topic-input').disabled = disabled;
  document.querySelectorAll('input[name="output_type"]').forEach(cb => {
    cb.disabled = disabled;
  });
  if (disabled) {
    document.getElementById('run-btn').textContent = 'Pipeline running...';
  } else {
    document.getElementById('run-btn').textContent = 'Run Pipeline';
  }
}

// ============================================================
// SSE handling (Task 11.2)
// ============================================================

/**
 * Reset the Progress View to its initial state before a new run.
 */
function resetProgressView() {
  document.querySelectorAll('#agent-sequence li').forEach(li => {
    li.classList.remove('active', 'complete', 'error');
  });
  document.getElementById('verdict-area').innerHTML = '';
  document.getElementById('pipeline-error').textContent = '';
  document.getElementById('pipeline-error').classList.add('hidden');
  document.getElementById('approval-actions').classList.add('hidden');
  document.getElementById('approve-btn').disabled = false;
  document.getElementById('reject-btn').disabled = false;
}

/**
 * Open an SSE connection to /api/run/status and handle incoming events.
 */
function startSSE() {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }

  eventSource = new EventSource('/api/run/status');

  eventSource.onmessage = (e) => {
    try {
      const event = JSON.parse(e.data);
      handlePipelineEvent(event);
    } catch (err) {
      console.warn('Failed to parse SSE event:', e.data, err);
    }
  };

  eventSource.onerror = () => {
    // Only show reconnect message if we haven't received a pipeline_complete
    const errorEl = document.getElementById('pipeline-error');
    if (!errorEl.classList.contains('hidden') && errorEl.textContent) return;
    // Silently reconnect — EventSource handles this automatically
  };
}

/**
 * Map agent_type values from the API to the data-agent attribute values in the HTML.
 */
const AGENT_TYPE_MAP = {
  researcher: 'researcher',
  desk_editor: 'desk_editor',
  writer: 'writer',
  subeditor: 'subeditor',
  publisher: 'publisher',
  approval_gate: 'approval_gate',
};

/**
 * Mark an agent step as active in the sequence display.
 */
function markAgentActive(agentType) {
  const key = AGENT_TYPE_MAP[agentType] || agentType;
  const li = document.querySelector(`#agent-sequence li[data-agent="${key}"]`);
  if (li) {
    li.classList.add('active');
    li.classList.remove('complete', 'error');
  }
}

/**
 * Mark an agent step as complete in the sequence display.
 */
function markAgentDone(agentType) {
  const key = AGENT_TYPE_MAP[agentType] || agentType;
  const li = document.querySelector(`#agent-sequence li[data-agent="${key}"]`);
  if (li) {
    li.classList.add('complete');
    li.classList.remove('active', 'error');
  }
}

/**
 * Show a verdict event in the verdict area.
 */
function showVerdict(details) {
  if (!details) return;
  const area = document.getElementById('verdict-area');
  const filename = escapeHtml(details.filename || '');
  const verdict = escapeHtml(details.verdict || '');
  const feedback = escapeHtml(details.feedback || '');
  area.innerHTML += `<p><strong>${filename}</strong>: ${verdict}${feedback ? ' — ' + feedback : ''}</p>`;
}

/**
 * Show the Approve/Reject buttons when the approval gate is reached.
 */
function showApprovalButtons() {
  markAgentActive('approval_gate');
  document.getElementById('approval-actions').classList.remove('hidden');
  // Load the current run's files into the Review panel (unless user is already editing)
  const contentArea = document.getElementById('content-area');
  const hasEditor = contentArea && contentArea.querySelector('#article-editor');
  if (currentRunId && !hasEditor) {
    loadRunFiles(currentRunId);
  }
  showPanel('review');
}

/**
 * Handle the pipeline completing (success, halted, or error).
 */
function onPipelineComplete(event) {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }

  setRunFormDisabled(false);

  const status = event.status || 'unknown';
  const area = document.getElementById('verdict-area');
  area.innerHTML += `<p><strong>Pipeline ${status}.</strong></p>`;

  if (status === 'error') {
    const msg = (event.details && event.details.error) || 'An error occurred during the pipeline run.';
    showError('pipeline-error', msg);
  }

  // Refresh run history once
  loadRunHistory();

  // Only auto-load files if the Review panel is empty — don't clobber active edits
  const contentArea = document.getElementById('content-area');
  const hasEditor = contentArea && contentArea.querySelector('#article-editor');
  if (currentRunId && !hasEditor) {
    loadRunFiles(currentRunId);
  }
}

/**
 * Dispatch a pipeline event to the appropriate handler.
 */
function handlePipelineEvent(event) {
  switch (event.event_type) {
    case 'agent_invoked':
      markAgentActive(event.agent_type);
      break;
    case 'agent_completed':
      markAgentDone(event.agent_type);
      break;
    case 'agent_error': {
      const key = AGENT_TYPE_MAP[event.agent_type] || event.agent_type;
      const li = document.querySelector(`#agent-sequence li[data-agent="${key}"]`);
      if (li) {
        li.classList.add('error');
        li.classList.remove('active');
      }
      const msg = (event.details && event.details.error) || event.agent_type + ' encountered an error.';
      showError('pipeline-error', msg);
      break;
    }
    case 'verdict':
      showVerdict(event.details);
      break;
    case 'approval_gate_presented':
    case 'file_escalated':
      showApprovalButtons();
      break;
    case 'pipeline_complete':
      onPipelineComplete(event);
      break;
    default:
      // Unknown event — ignore silently
      break;
  }
}

// ============================================================
// Approve / Reject handlers (Task 11.3)
// ============================================================

async function handleApprove() {
  document.getElementById('approve-btn').disabled = true;
  document.getElementById('reject-btn').disabled = true;
  try {
    await fetch('/api/run/approve', { method: 'POST' });
    markAgentDone('approval_gate');
    document.getElementById('approval-actions').classList.add('hidden');
  } catch (err) {
    showError('pipeline-error', 'Network error sending approval: ' + err.message);
  }
}

async function handleReject() {
  document.getElementById('approve-btn').disabled = true;
  document.getElementById('reject-btn').disabled = true;
  try {
    await fetch('/api/run/reject', { method: 'POST' });
    markAgentDone('approval_gate');
    document.getElementById('approval-actions').classList.add('hidden');
  } catch (err) {
    showError('pipeline-error', 'Network error sending rejection: ' + err.message);
  }
}

// ============================================================
// Suggestions loading (Task 12.1)
// ============================================================

/**
 * Fetch topic suggestions from /api/suggestions and render them.
 */
async function loadSuggestions() {
  const container = document.getElementById('suggestions-list');
  container.innerHTML = '<p style="color:#666;font-size:0.85rem;">Loading suggestions...</p>';

  try {
    const resp = await fetch('/api/suggestions');
    const data = await resp.json();

    container.innerHTML = '';

    if (data.warning) {
      const warn = document.createElement('div');
      warn.className = 'suggestions-warning';
      warn.textContent = data.warning;
      container.appendChild(warn);
    }

    const suggestions = data.suggestions || [];
    if (suggestions.length === 0 && !data.warning) {
      container.innerHTML = '<p style="color:#666;font-size:0.85rem;">No suggestions available.</p>';
      return;
    }

    suggestions.forEach(s => {
      const item = document.createElement('div');
      item.className = 'suggestion-item';

      const topicSpan = document.createElement('span');
      topicSpan.className = 'suggestion-topic';
      topicSpan.textContent = s.topic;

      const metaSpan = document.createElement('span');
      metaSpan.className = 'suggestion-meta';
      if (s.last_covered) {
        metaSpan.textContent = `Last covered: ${s.last_covered} (${s.days_since}d ago)`;
      } else {
        metaSpan.textContent = 'Never covered';
      }

      const useBtn = document.createElement('button');
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
    container.innerHTML = `<div class="suggestions-warning">Could not load suggestions — check AWS credentials and region.</div>`;
  }
}

// ============================================================
// Run history loading (Task 12.2)
// ============================================================

/**
 * Fetch the list of past runs from /api/runs and render the history list.
 */
async function loadRunHistory() {
  const list = document.getElementById('run-history-list');
  list.innerHTML = '';

  try {
    const resp = await fetch('/api/runs');
    const data = await resp.json();
    const runs = data.runs || [];

    if (runs.length === 0) {
      list.innerHTML = '<li style="color:#666;font-size:0.85rem;cursor:default;">No runs yet.</li>';
      return;
    }

    runs.forEach(run => {
      const li = document.createElement('li');

      const nameDiv = document.createElement('div');
      nameDiv.className = 'run-history-name';
      nameDiv.textContent = run.id;

      const filesDiv = document.createElement('div');
      filesDiv.className = 'run-history-files';
      filesDiv.textContent = run.files.length > 0 ? run.files.join(', ') : 'No output files';

      li.appendChild(nameDiv);
      li.appendChild(filesDiv);

      li.addEventListener('click', () => {
        // Deselect all, select this one
        list.querySelectorAll('li').forEach(el => el.classList.remove('selected'));
        li.classList.add('selected');
        currentRunId = run.id;
        loadRunFiles(run.id);
        showPanel('review');
      });

      list.appendChild(li);
    });
  } catch (err) {
    list.innerHTML = '<li style="color:#cc0000;font-size:0.85rem;cursor:default;">Failed to load run history.</li>';
  }
}

// ============================================================
// Review Panel — file loading and rendering (Tasks 12.2, 13.1, 13.2)
// ============================================================

/**
 * Load the file list for a run and populate the file selector.
 * Then load the first reviewable file (post.md preferred).
 */
async function loadRunFiles(runId) {
  try {
    const resp = await fetch('/api/runs');
    const data = await resp.json();
    const run = (data.runs || []).find(r => r.id === runId);

    const selector = document.getElementById('file-selector');
    selector.innerHTML = '';

    if (!run || run.files.length === 0) {
      document.getElementById('content-area').innerHTML =
        '<p style="color:#666;">No reviewable files for this run.</p>';
      updatePublishPanelButtons([]);
      return;
    }

    run.files.forEach(filename => {
      const opt = document.createElement('option');
      opt.value = filename;
      opt.textContent = filename;
      selector.appendChild(opt);
    });

    // Prefer post.md; fall back to first file
    const preferred = run.files.includes('post.md') ? 'post.md' : run.files[0];
    selector.value = preferred;

    updatePublishPanelButtons(run.files);
    await loadFileContent(runId, preferred);
  } catch (err) {
    document.getElementById('content-area').innerHTML =
      `<p style="color:#cc0000;">Failed to load files: ${escapeHtml(err.message)}</p>`;
  }
}

/**
 * Show or hide Publish Panel buttons based on which files exist in the run.
 */
function updatePublishPanelButtons(files) {
  const hasLinkedIn = files.includes('digest-email.txt');
  const hasScript = files.includes('script.md');

  document.getElementById('copy-linkedin-btn').classList.toggle('hidden', !hasLinkedIn);
  document.getElementById('download-script-btn').classList.toggle('hidden', !hasScript);
}

/**
 * Fetch a file from the run bundle and render it in the Review Panel.
 * Handles Markdown rendering and MIKE placeholder detection.
 */
async function loadFileContent(runId, filename) {
  const contentArea = document.getElementById('content-area');
  contentArea.innerHTML = '<p style="color:#666;font-size:0.85rem;">Loading...</p>';
  document.getElementById('save-status').textContent = '';

  try {
    const resp = await fetch(`/api/runs/${encodeURIComponent(runId)}/file?name=${encodeURIComponent(filename)}`);

    if (resp.status === 404) {
      contentArea.innerHTML = '<p style="color:#cc0000;">File not generated for this run.</p>';
      return;
    }
    if (!resp.ok) {
      contentArea.innerHTML = `<p style="color:#cc0000;">Error loading file (${resp.status}).</p>`;
      return;
    }

    const text = await resp.text();
    currentRawMarkdown = text;
    currentFileName = filename;

    // Pre-populate publish title from first H1 if viewing post.md
    if (filename === 'post.md') {
      const h1Match = text.match(/^#\s+(.+)$/m);
      if (h1Match) {
        const titleInput = document.getElementById('publish-title');
        if (titleInput && !titleInput.value) {
          titleInput.value = h1Match[1].trim();
        }
      }
    }

    renderFileContent(text, filename);
  } catch (err) {
    contentArea.innerHTML = `<p style="color:#cc0000;">Network error: ${escapeHtml(err.message)}</p>`;
  }
}

/**
 * Render file content in the Review Panel as an editable textarea.
 * Users can edit the raw Markdown directly and save with the Save button.
 */
function renderFileContent(text, filename) {
  const contentArea = document.getElementById('content-area');

  // Build the editor: textarea + Save button
  contentArea.innerHTML = '';
  contentArea.style.cssText = 'padding: 0;';

  const textarea = document.createElement('textarea');
  textarea.id = 'article-editor';
  textarea.value = text;
  textarea.spellcheck = true;
  textarea.style.cssText = 'width: 100%; min-height: 600px; padding: 16px; border: 1px solid #ddd; border-radius: 4px; font-family: Consolas, Monaco, monospace; font-size: 0.9rem; line-height: 1.5; resize: vertical; background: #fff; color: #1a1a1a; box-sizing: border-box;';

  const actionBar = document.createElement('div');
  actionBar.style.cssText = 'display: flex; gap: 10px; margin-top: 8px; align-items: center;';

  const saveBtn = document.createElement('button');
  saveBtn.textContent = 'Save';
  saveBtn.style.cssText = 'padding: 8px 16px;';

  const previewBtn = document.createElement('button');
  previewBtn.textContent = 'Preview';
  previewBtn.style.cssText = 'padding: 8px 16px; background: #fff; color: #0066cc;';

  actionBar.appendChild(saveBtn);
  actionBar.appendChild(previewBtn);

  contentArea.appendChild(textarea);
  contentArea.appendChild(actionBar);

  // Save button — POST the current textarea value to /api/runs/<id>/file
  saveBtn.addEventListener('click', async () => {
    const statusEl = document.getElementById('save-status');
    statusEl.textContent = 'Saving...';
    statusEl.className = '';
    try {
      const resp = await fetch(`/api/runs/${encodeURIComponent(currentRunId)}/file`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: currentFileName, content: textarea.value })
      });
      if (resp.ok) {
        currentRawMarkdown = textarea.value;
        statusEl.textContent = 'Saved';
        statusEl.className = 'success';
        setTimeout(() => { statusEl.textContent = ''; statusEl.className = ''; }, 3000);
      } else {
        const data = await resp.json().catch(() => ({}));
        statusEl.textContent = 'Save failed: ' + (data.detail || resp.status);
        statusEl.className = 'error';
      }
    } catch (err) {
      statusEl.textContent = 'Network error: ' + err.message;
      statusEl.className = 'error';
    }
  });

  // Preview button — swap textarea for rendered Markdown, click "Edit" to return
  previewBtn.addEventListener('click', () => {
    const markdown = textarea.value;
    contentArea.innerHTML = '';

    const previewDiv = document.createElement('div');
    previewDiv.style.cssText = 'padding: 16px; background: #fff; border: 1px solid #ddd; border-radius: 4px; line-height: 1.6;';
    if (typeof marked !== 'undefined') {
      previewDiv.innerHTML = marked.parse(markdown);
    } else {
      previewDiv.textContent = markdown;
    }

    const editBtn = document.createElement('button');
    editBtn.textContent = 'Back to editor';
    editBtn.style.cssText = 'margin-top: 10px; padding: 8px 16px; background: #fff; color: #0066cc;';
    editBtn.addEventListener('click', () => renderFileContent(markdown, filename));

    contentArea.appendChild(previewDiv);
    contentArea.appendChild(editBtn);
  });
}

/**
 * Replace a MIKE placeholder div with an inline editor.
 */
function openMikeEditor(div) {
  const instruction = div.dataset.instruction || '';

  const wrapper = document.createElement('div');
  wrapper.className = 'mike-editor';

  const ta = document.createElement('textarea');
  ta.placeholder = instruction;
  ta.rows = 4;

  const actions = document.createElement('div');
  actions.className = 'mike-editor-actions';

  const saveBtn = document.createElement('button');
  saveBtn.textContent = 'Save';

  const cancelBtn = document.createElement('button');
  cancelBtn.textContent = 'Cancel';
  cancelBtn.style.cssText = 'background:#fff;color:#0066cc;';

  actions.appendChild(saveBtn);
  actions.appendChild(cancelBtn);
  wrapper.appendChild(ta);
  wrapper.appendChild(actions);

  div.replaceWith(wrapper);
  ta.focus();

  saveBtn.addEventListener('click', async () => {
    const userText = ta.value;
    const mikePattern = `<!-- MIKE: ${instruction} -->`;
    // Also match variations with different spacing
    const updatedMarkdown = currentRawMarkdown.replace(
      new RegExp(`<!--\\s*MIKE:\\s*${escapeRegex(instruction)}\\s*-->`, 'g'),
      userText
    );

    const statusEl = document.getElementById('save-status');
    statusEl.textContent = 'Saving...';
    statusEl.className = '';

    try {
      const resp = await fetch(`/api/runs/${encodeURIComponent(currentRunId)}/file`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: currentFileName, content: updatedMarkdown })
      });

      if (resp.ok) {
        currentRawMarkdown = updatedMarkdown;
        statusEl.textContent = 'Saved';
        statusEl.className = 'success';
        // Re-render with the updated content
        renderFileContent(currentRawMarkdown, currentFileName);
        setTimeout(() => { statusEl.textContent = ''; statusEl.className = ''; }, 3000);
      } else {
        const data = await resp.json().catch(() => ({}));
        statusEl.textContent = 'Save failed: ' + (data.detail || resp.status);
        statusEl.className = 'error';
        // Restore the editor so the user keeps their edits
        wrapper.replaceWith(div);
      }
    } catch (err) {
      statusEl.textContent = 'Network error: ' + err.message;
      statusEl.className = 'error';
      wrapper.replaceWith(div);
    }
  });

  cancelBtn.addEventListener('click', () => {
    wrapper.replaceWith(div);
  });
}

/**
 * Escape a string for use in a RegExp constructor.
 */
function escapeRegex(str) {
  return str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

// ============================================================
// Publish Panel — dev.to publish and draft (Task 14.1)
// ============================================================

/**
 * Publish or save as draft to dev.to.
 * published=true for Publish, published=false for Draft.
 */
async function publishToDevTo(published) {
  const title = document.getElementById('publish-title').value.trim();
  if (!title) {
    showPublishStatus('Title is required', 'error');
    return;
  }

  const tagsRaw = document.getElementById('publish-tags').value.trim();
  const tags = tagsRaw ? tagsRaw.split(',').map(t => t.trim()).filter(Boolean) : [];

  const publishBtn = document.getElementById('publish-devto-btn');
  const draftBtn = document.getElementById('draft-devto-btn');
  publishBtn.disabled = true;
  draftBtn.disabled = true;

  showPublishStatus('Publishing...', '');

  try {
    const resp = await fetch('/api/publish/devto', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: currentRunId, title, tags, published })
    });

    const data = await resp.json();

    if (resp.status === 201) {
      showPublishStatus(`Published! ${data.url || ''}`, 'success');
    } else if (resp.status === 400) {
      // DEVTO_API_KEY not configured — disable buttons with a note
      showPublishStatus('DEVTO_API_KEY is not configured', 'error');
      publishBtn.title = 'DEVTO_API_KEY is not set — configure it in .env';
      draftBtn.title = 'DEVTO_API_KEY is not set — configure it in .env';
      // Leave buttons disabled since the key is missing
      return;
    } else {
      showPublishStatus(`Failed (${resp.status}): ${data.error || data.detail || 'Unknown error'}`, 'error');
    }
  } catch (err) {
    showPublishStatus('Network error: ' + err.message, 'error');
  } finally {
    // Only re-enable if we didn't hit the missing-key case (which returns early)
    publishBtn.disabled = false;
    draftBtn.disabled = false;
  }
}

// ============================================================
// Publish Panel — Copy LinkedIn post (Task 14.2)
// ============================================================

async function handleCopyLinkedIn() {
  if (!currentRunId) return;

  try {
    const resp = await fetch(`/api/runs/${encodeURIComponent(currentRunId)}/file?name=digest-email.txt`);
    if (!resp.ok) {
      showPublishStatus('LinkedIn post not found', 'error');
      return;
    }
    const text = await resp.text();

    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      showPublishStatus('Copied!', 'success');
      setTimeout(() => showPublishStatus('', ''), 3000);
    } else {
      // Fallback: show a selectable textarea the user can copy from manually
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.cssText = 'position:fixed;top:10px;left:10px;width:80%;height:200px;z-index:9999;';
      document.body.appendChild(ta);
      ta.select();
      showPublishStatus('Select all and copy from the text area above', '');
      ta.addEventListener('blur', () => {
        ta.remove();
        showPublishStatus('', '');
      });
    }
  } catch (err) {
    showPublishStatus('Error: ' + err.message, 'error');
  }
}

// ============================================================
// Publish Panel — Download YouTube script (Task 14.3)
// ============================================================

function handleDownloadScript() {
  if (!currentRunId) return;
  const a = document.createElement('a');
  a.href = `/api/runs/${encodeURIComponent(currentRunId)}/download/script.md`;
  a.download = 'script.md';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

// ============================================================
// DOMContentLoaded — wire up all event listeners and init
// ============================================================

document.addEventListener('DOMContentLoaded', () => {

  // --- Panel navigation ---
  document.querySelectorAll('.nav-btn').forEach(btn => {
    btn.addEventListener('click', () => showPanel(btn.dataset.target));
  });

  // --- Run form ---
  document.getElementById('run-form').addEventListener('submit', handleRunSubmit);

  // --- Approve / Reject ---
  document.getElementById('approve-btn').addEventListener('click', handleApprove);
  document.getElementById('reject-btn').addEventListener('click', handleReject);

  // --- File selector (Review Panel) ---
  document.getElementById('file-selector').addEventListener('change', (e) => {
    if (currentRunId && e.target.value) {
      loadFileContent(currentRunId, e.target.value);
    }
  });

  // --- Publish Panel: dev.to ---
  document.getElementById('publish-devto-btn').addEventListener('click', () => publishToDevTo(true));
  document.getElementById('draft-devto-btn').addEventListener('click', () => publishToDevTo(false));

  // --- Publish Panel: LinkedIn ---
  document.getElementById('copy-linkedin-btn').addEventListener('click', handleCopyLinkedIn);

  // --- Publish Panel: YouTube script download ---
  document.getElementById('download-script-btn').addEventListener('click', handleDownloadScript);

  // --- Initial data load ---
  loadSuggestions();
  loadRunHistory();
});
