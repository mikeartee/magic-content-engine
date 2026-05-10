// Bullpen Web GUI

// ---------------------------------------------------------------------------
// Shared state (used across Task 11 and Task 12)
// ---------------------------------------------------------------------------
let currentRunId = null;

// ---------------------------------------------------------------------------
// Utility
// ---------------------------------------------------------------------------

function escapeHtml(str) {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ---------------------------------------------------------------------------
// Task 12.1 — Suggestions loading
// ---------------------------------------------------------------------------

async function loadSuggestions() {
  const list = document.getElementById('suggestions-list');
  list.innerHTML = '<p>Loading suggestions...</p>';

  try {
    const resp = await fetch('/api/suggestions');
    const data = await resp.json();

    if (data.warning) {
      list.innerHTML = `<div class="suggestions-warning">${data.warning}</div>`;
      return;
    }

    if (!data.suggestions || data.suggestions.length === 0) {
      list.innerHTML = '<p>No suggestions available.</p>';
      return;
    }

    list.innerHTML = data.suggestions.map(s => `
      <div class="suggestion-item">
        <span class="suggestion-topic">${escapeHtml(s.topic)}</span>
        <span class="suggestion-meta">${s.last_covered ? 'Last: ' + s.last_covered : 'Never covered'}</span>
        <button onclick="useSuggestion(${JSON.stringify(s.topic)})">Use this topic</button>
      </div>
    `).join('');
  } catch (err) {
    list.innerHTML = `<div class="suggestions-warning">Could not load suggestions: ${err.message}</div>`;
  }
}

function useSuggestion(topic) {
  document.getElementById('topic-input').value = topic;
  document.getElementById('topic-input').focus();
}

// ---------------------------------------------------------------------------
// Task 12.2 — Run history loading
// ---------------------------------------------------------------------------

async function loadRunHistory() {
  const list = document.getElementById('run-history-list');

  try {
    const resp = await fetch('/api/runs');
    const data = await resp.json();

    if (!data.runs || data.runs.length === 0) {
      list.innerHTML = '<li>No runs yet.</li>';
      return;
    }

    list.innerHTML = data.runs.map(run => `
      <li data-run-id="${run.id}" onclick="selectRun(${JSON.stringify(run.id)}, ${JSON.stringify(run.files)})">
        <div class="run-history-name">${escapeHtml(run.id)}</div>
        <div class="run-history-files">${run.files.join(', ') || 'No files'}</div>
      </li>
    `).join('');
  } catch (err) {
    list.innerHTML = `<li>Error loading history: ${err.message}</li>`;
  }
}

function selectRun(runId, files) {
  // Update selected state
  document.querySelectorAll('#run-history-list li').forEach(li => li.classList.remove('selected'));
  const li = document.querySelector(`#run-history-list [data-run-id="${runId}"]`);
  if (li) li.classList.add('selected');

  currentRunId = runId;

  // Populate file selector in Review Panel
  const selector = document.getElementById('file-selector');
  const reviewableFiles = files.filter(f => f.endsWith('.md') || f.endsWith('.txt'));

  if (reviewableFiles.length === 0) {
    selector.innerHTML = '<option>No reviewable files</option>';
    document.getElementById('content-area').innerHTML = '';
  } else {
    selector.innerHTML = reviewableFiles.map(f => `<option value="${f}">${f}</option>`).join('');
    loadFile(runId, reviewableFiles[0]);
  }

  // Update Publish Panel buttons visibility
  updatePublishPanel(runId, files);
}

async function loadFile(runId, filename) {
  try {
    const resp = await fetch(`/api/runs/${runId}/file?name=${encodeURIComponent(filename)}`);
    if (!resp.ok) {
      document.getElementById('content-area').innerHTML = 'File not found.';
      return;
    }
    const text = await resp.text();
    // Store raw content for Task 13 MIKE placeholder processing
    window._currentFileContent = text;
    window._currentFileName = filename;
    renderContent(text);
  } catch (err) {
    document.getElementById('content-area').innerHTML = 'Error loading file: ' + err.message;
  }
}

function renderContent(text) {
  // Basic render — Task 13 will enhance with marked.js and MIKE placeholder detection
  const area = document.getElementById('content-area');
  if (typeof marked !== 'undefined') {
    area.innerHTML = marked.parse(text);
  } else {
    area.textContent = text;
  }
}

function updatePublishPanel(runId, files) {
  // Show/hide LinkedIn and YouTube buttons based on available files
  const linkedinBtn = document.getElementById('copy-linkedin-btn');
  const scriptBtn = document.getElementById('download-script-btn');

  if (files.includes('digest-email.txt')) {
    linkedinBtn.classList.remove('hidden');
  } else {
    linkedinBtn.classList.add('hidden');
  }

  if (files.includes('script.md')) {
    scriptBtn.classList.remove('hidden');
  } else {
    scriptBtn.classList.add('hidden');
  }

  // Pre-populate title from post.md if available
  if (files.includes('post.md')) {
    loadPostTitle(runId);
  }
}

async function loadPostTitle(runId) {
  try {
    const resp = await fetch(`/api/runs/${runId}/file?name=post.md`);
    if (!resp.ok) return;
    const text = await resp.text();
    const match = text.match(/^#\s+(.+)$/m);
    if (match) document.getElementById('publish-title').value = match[1].trim();
  } catch (_) {}
}

// ---------------------------------------------------------------------------
// File selector change handler
// ---------------------------------------------------------------------------

document.getElementById('file-selector').addEventListener('change', (e) => {
  if (currentRunId && e.target.value && e.target.value !== 'No reviewable files') {
    loadFile(currentRunId, e.target.value);
  }
});

// ---------------------------------------------------------------------------
// Page load
// ---------------------------------------------------------------------------

document.addEventListener('DOMContentLoaded', () => {
  loadSuggestions();
  loadRunHistory();
});
