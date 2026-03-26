/* ══════════════════════════════════════════════════════
   Ollama Project Planner — Kanban Frontend
   ══════════════════════════════════════════════════════ */
'use strict';

// ─── App State ──────────────────────────────────────────────────
let activeTaskId = null;      // currently open in modal
let activeRunId  = null;      // currently running task id
let cachedModels = [];

// ─── Eel callbacks ──────────────────────────────────────────────

eel.expose(board_updated);
function board_updated(board) {
  renderBoard(board);
}

eel.expose(task_updated);
function task_updated(task) {
  // Update card on board
  renderCard(task);
  // If modal open for this task, refresh it
  if (activeTaskId === task.id) {
    populateModal(task);
  }
}

eel.expose(task_log_added);
function task_log_added(taskId, logEntry) {
  if (activeTaskId !== taskId) return;
  appendLogEntry(logEntry);
  scrollLogsToBottom();
}

eel.expose(task_step_changed);
function task_step_changed(taskId, phase, step) {
  if (activeTaskId === taskId) {
    // Could show current step in modal header - no-op for now
  }
}

// ─── Initialization ─────────────────────────────────────────────
async function init() {
  // Load models
  cachedModels = await eel.get_ollama_models()();
  populateModelSelects(cachedModels);

  // Load initial board
  const board = await eel.get_board()();
  renderBoard(board);

  // Restore working dir display
  const wd = await eel.get_working_dir()();
  if (wd) {
    document.getElementById('project-dir').textContent = wd;
    document.getElementById('project-name').textContent = wd.split(/[\\/]/).pop() || wd;
    document.getElementById('dir-input').value = wd;
  }

  // Show dir modal on first load if no dir set
  if (!wd) {
    setTimeout(showDirModal, 400);
  }
}

// ─── Board rendering ────────────────────────────────────────────
function renderBoard(board) {
  const COLS = ['planning','queue','in_progress','ai_review','human_review','done'];
  COLS.forEach(col => {
    const tasks = board[col] || [];
    const bodyEl = document.getElementById(`col-${col}`);
    const countEl = document.getElementById(`count-${col}`);
    if (!bodyEl) return;

    countEl.textContent = tasks.length;
    bodyEl.innerHTML = '';

    if (!tasks.length) {
      bodyEl.innerHTML = `
        <div class="col-empty">
          <div class="col-empty-icon">○</div>
          <div>${emptyMsg(col)}</div>
        </div>`;
      return;
    }

    tasks.forEach(task => {
      bodyEl.appendChild(buildCard(task));
    });
  });
}

function emptyMsg(col) {
  const msgs = {
    planning:     'No tasks planned<br><small>Add a task to get started</small>',
    queue:        'Queue is empty<br><small>Tasks will wait here when parallel task limit is reached</small>',
    in_progress:  'Nothing running<br><small>Start a task from Planning</small>',
    ai_review:    'No tasks in review<br><small>AI will review completed tasks</small>',
    human_review: 'No tasks here',
    done:         '',
  };
  return msgs[col] || '';
}

// Update single card without full board re-render
function renderCard(task) {
  const col = task.column;
  const bodyEl = document.getElementById(`col-${col}`);
  if (!bodyEl) return;

  // Remove from all columns first
  document.querySelectorAll('.task-card[data-id]').forEach(el => {
    if (el.dataset.id === task.id) el.remove();
  });

  // Remove empty state if present
  const empty = bodyEl.querySelector('.col-empty');
  if (empty) empty.remove();

  bodyEl.prepend(buildCard(task));

  // Update counts
  document.querySelectorAll('.col').forEach(colEl => {
    const c = colEl.dataset.col;
    const countEl = document.getElementById(`count-${c}`);
    if (countEl) {
      countEl.textContent = document.querySelectorAll(`#col-${c} .task-card`).length;
    }
  });
}

function buildCard(task) {
  const card = document.createElement('div');
  card.className = 'task-card' +
    (task.has_errors ? ' has-errors' : '') +
    (activeRunId === task.id ? ' running' : '');
  card.dataset.id = task.id;
  card.onclick = () => openTaskModal(task.id);

  // Tags
  const tagHTML = (task.tags || []).map(t => tagBadge(t)).join('');

  // Progress
  const pct = task.subtask_progress !== undefined ? task.subtask_progress : task.progress;
  const hasProgress = pct > 0;
  const progressHTML = hasProgress ? `
    <div class="task-card-progress">
      <div class="progress-label">
        <span>Progress</span><span>${pct}%</span>
      </div>
      <div class="progress-bar">
        <div class="progress-fill${task.has_errors ? ' error' : ''}" style="width:${pct}%"></div>
      </div>
    </div>` : '';

  // Pipeline steps
  const phases = task.phases_selected || ['planning','coding','qa'];
  const phaseLabels = { planning:'Plan', coding:'Code', qa:'QA' };
  const pipeHTML = phases.map((ph, i) => {
    const dot = `<div class="step-dot pending" data-phase="${ph}"></div>`;
    const label = `<span class="step-label">${phaseLabels[ph] || ph}</span>`;
    const line = i < phases.length - 1 ? '<div class="step-line"></div>' : '';
    return dot + label + line;
  }).join('');

  // Timestamp
  const ts = relativeTime(task.updated_at || task.created_at);

  card.innerHTML = `
    <div class="task-card-title">${esc(task.title)}</div>
    ${task.description ? `<div class="task-card-desc">${esc(task.description)}</div>` : ''}
    ${tagHTML ? `<div class="task-card-tags">${tagHTML}</div>` : ''}
    ${progressHTML}
    <div class="task-card-dots">${pipeHTML}</div>
    <div class="task-card-footer">
      <span class="task-card-time">${ts}</span>
      <button class="task-card-menu" onclick="event.stopPropagation();cardMenu(event,'${esc(task.id)}')">⋮</button>
    </div>`;

  return card;
}

function tagBadge(tag) {
  const cls = {
    'Needs Review': 'tag-review',
    'Has Errors':   'tag-errors',
    'Complete':     'tag-complete',
    'Fast':         'tag-fast',
    'Aborted':      'tag-aborted',
  }[tag] || 'tag-review';
  const icon = tag === 'Fast' ? '⚡ ' : '';
  return `<span class="tag ${cls}">${icon}${esc(tag)}</span>`;
}

function cardMenu(e, taskId) {
  // Simple inline menu — future: context menu
  const task = getTaskFromDOM(taskId);
}

function getTaskFromDOM(taskId) {
  // We don't cache tasks in JS — just open the modal
  return null;
}

// ─── New Task Modal ──────────────────────────────────────────────
function showNewTaskModal() {
  document.getElementById('overlay-new').classList.add('open');
  document.getElementById('new-title').focus();
}

function closeNewTaskModal() {
  document.getElementById('overlay-new').classList.remove('open');
}

async function createTask() {
  const title = document.getElementById('new-title').value.trim();
  if (!title) { document.getElementById('new-title').focus(); return; }

  const phases = [...document.querySelectorAll('.phase-chk input:checked')].map(el => el.value);

  const cfg = {
    title,
    description:    document.getElementById('new-desc').value.trim(),
    project_path:   document.getElementById('new-dir').value.trim() || '',
    git_branch:     document.getElementById('new-branch').value.trim() || 'main',
    planning_model: document.getElementById('new-planning-model').value,
    coding_model:   document.getElementById('new-coding-model').value,
    qa_model:       document.getElementById('new-qa-model').value,
    phases,
  };

  const res = await eel.add_task(cfg)();
  if (res.ok) {
    closeNewTaskModal();
    const board = await eel.get_board()();
    renderBoard(board);
    // Clear form
    document.getElementById('new-title').value = '';
    document.getElementById('new-desc').value = '';
    document.getElementById('new-dir').value = '';
  } else {
    alert('Error: ' + res.error);
  }
}

// ─── Task Detail Modal ───────────────────────────────────────────
async function openTaskModal(taskId) {
  activeTaskId = taskId;
  const task = await eel.get_task(taskId)();
  if (!task) return;
  populateModal(task);
  document.getElementById('overlay-task').classList.add('open');
  // Switch to overview tab
  switchMTab(document.querySelector('.mt-tab[data-mtab="overview"]'), 'overview');
}

function closeTaskModal() {
  document.getElementById('overlay-task').classList.remove('open');
  activeTaskId = null;
}

function populateModal(task) {
  // Header
  document.getElementById('mt-title').textContent = task.title;
  document.getElementById('mt-slug').textContent  = task.id;

  // Column tag
  const colLabels = {
    planning:'Planning', queue:'Queue', in_progress:'In Progress',
    ai_review:'AI Review', human_review:'Human Review', done:'Done',
  };
  const colTag = document.getElementById('mt-col-tag');
  colTag.textContent = colLabels[task.column] || task.column;

  // Error tag
  const errTag = document.getElementById('mt-err-tag');
  errTag.classList.toggle('hidden', !task.has_errors);

  // Subtask count
  const stCount = document.getElementById('mt-subtask-count');
  if (task.subtasks && task.subtasks.length) {
    const done = task.subtasks.filter(s => s.status === 'done').length;
    stCount.textContent = `${done}/${task.subtasks.length} subtasks`;
    stCount.classList.remove('hidden');
    document.getElementById('mtab-subtasks-count').textContent = task.subtasks.length;
  } else {
    stCount.classList.add('hidden');
    document.getElementById('mtab-subtasks-count').textContent = '';
  }

  // Progress bar
  const pct = computeProgress(task);
  const progressWrap = document.getElementById('mt-progress-wrap');
  if (pct > 0 || task.column === 'in_progress') {
    progressWrap.classList.remove('hidden');
    document.getElementById('mt-progress-fill').style.width = pct + '%';
    document.getElementById('mt-progress-pct').textContent  = pct + '%';
  } else {
    progressWrap.classList.add('hidden');
  }

  // Overview
  document.getElementById('ov-desc').textContent    = task.description || '—';
  document.getElementById('ov-branch').textContent  = task.git_branch || '—';
  document.getElementById('ov-path').textContent    = task.project_path || '—';
  document.getElementById('ov-models').textContent  =
    `Planning: ${task.models?.planning || '—'}  |  Coding: ${task.models?.coding || '—'}  |  QA: ${task.models?.qa || '—'}`;
  document.getElementById('ov-created').textContent = task.created_at || '—';
  document.getElementById('ov-updated').textContent = task.updated_at || '—';

  // Run/Abort buttons
  const isRunning = activeRunId === task.id;
  document.getElementById('btn-run').classList.toggle('hidden', isRunning);
  document.getElementById('btn-abort').classList.toggle('hidden', !isRunning);

  // Subtasks tab
  renderSubtasks(task.subtasks || []);

  // Logs tab
  renderLogs(task.logs || []);
}

function computeProgress(task) {
  if (!task.subtasks || !task.subtasks.length) return task.progress || 0;
  const done = task.subtasks.filter(s => s.status === 'done').length;
  return Math.round(done / task.subtasks.length * 100);
}

// ─── Subtasks Tab ────────────────────────────────────────────────
function renderSubtasks(subtasks) {
  const wrap = document.getElementById('subtasks-list');
  if (!subtasks.length) {
    wrap.innerHTML = '<div style="color:var(--text3);font-size:12px;padding:20px 0">No subtasks yet — run Planning phase first</div>';
    return;
  }

  const STATUS_ICON = { pending:'', in_progress:'◉', done:'✓', failed:'✕' };
  const STATUS_CLS  = { pending:'pending', in_progress:'active', done:'done', failed:'failed' };

  wrap.innerHTML = subtasks.map((t, i) => {
    const status   = t.status || 'pending';
    const iconCls  = STATUS_CLS[status] || 'pending';
    const iconChar = STATUS_ICON[status] || '';
    return `
    <div class="subtask-row" onclick="toggleSubtask(this)">
      <div class="subtask-check ${iconCls}">${iconChar}</div>
      <div class="subtask-content">
        <div class="subtask-header">
          <span class="subtask-num">#${i+1}</span>
          <span class="subtask-title">${esc(t.title)}</span>
        </div>
        <div class="subtask-detail">
          <div class="subtask-desc">${esc(t.description || '')}</div>
          <div class="subtask-cond"><strong>⚙ Structural:</strong> ${esc(t.completion_without_ollama || '')}</div>
          <div class="subtask-cond"><strong>🤖 Quality:</strong>    ${esc(t.completion_with_ollama || '')}</div>
        </div>
      </div>
    </div>`;
  }).join('');
}

function toggleSubtask(row) {
  row.classList.toggle('expanded');
}

// ─── Logs Tab ──────────────────────────────────────────────────────────────
// Architecture: one hidden .log-phase-bucket div per phase inside
// #log-entries-panel. switchLogPhase() shows/hides buckets.
// appendLogEntry() adds to the right bucket + "all" bucket.

const LOG_PHASES = ['all', 'planning', 'coding', 'qa'];

function getOrCreateBucket(phase) {
  const panel = document.getElementById('log-entries-panel');
  let bucket = panel.querySelector(`.log-phase-bucket[data-phase="${phase}"]`);
  if (!bucket) {
    bucket = document.createElement('div');
    bucket.className = 'log-phase-bucket';
    bucket.dataset.phase = phase;
    // "all" bucket is visible by default
    if (phase === 'all') bucket.classList.add('visible');
    panel.appendChild(bucket);
  }
  return bucket;
}

function renderLogs(logs) {
  const panel = document.getElementById('log-entries-panel');
  panel.innerHTML = '';   // clear all buckets

  // Reset counts
  LOG_PHASES.forEach(ph => {
    const el = document.getElementById(`lpt-count-${ph}`);
    if (el) el.textContent = '0';
    const st = document.getElementById(`lpt-status-${ph}`);
    if (st) { st.className = 'lpt-status'; st.textContent = ''; }
  });

  if (!logs.length) {
    const bucket = getOrCreateBucket('all');
    bucket.innerHTML = '<div style="color:var(--text3);font-size:12px;padding:20px 16px">No logs yet</div>';
    return;
  }

  // Pre-create all buckets
  LOG_PHASES.forEach(ph => getOrCreateBucket(ph));

  // Populate
  const counts = {};
  logs.forEach(entry => {
    const ph = entry.phase || 'all';
    counts[ph] = (counts[ph] || 0) + 1;
    counts['all'] = (counts['all'] || 0) + 1;
    buildLogEntry(entry, getOrCreateBucket(ph));
    buildLogEntry(entry, getOrCreateBucket('all'));
  });

  // Update counts & statuses
  Object.entries(counts).forEach(([ph, n]) => {
    const el = document.getElementById(`lpt-count-${ph}`);
    if (el) el.textContent = n;
  });

  // Set phase statuses from log content
  ['planning','coding','qa'].forEach(ph => {
    const entries = (logs || []).filter(e => e.phase === ph);
    updatePhaseStatus(ph, entries);
  });

  // Restore active tab selection
  const activeTab = document.querySelector('.log-phase-tab.active');
  const activePhase = activeTab ? activeTab.dataset.phase : 'all';
  showBucket(activePhase);

  scrollLogsToBottom();
}

function updatePhaseStatus(phase, entries) {
  const st = document.getElementById(`lpt-status-${phase}`);
  if (!st || !entries.length) return;
  const hasError    = entries.some(e => e.type === 'error' || (e.msg || '').includes('[FAIL]'));
  const hasComplete = entries.some(e => (e.msg || '').includes('PHASE COMPLETE'));
  if (hasError)    { st.className = 'lpt-status failed';  st.textContent = 'Failed'; }
  else if (hasComplete) { st.className = 'lpt-status complete'; st.textContent = 'Done'; }
  else if (entries.length) { st.className = 'lpt-status running';  st.textContent = 'Running'; }
}

function showBucket(phase) {
  document.querySelectorAll('.log-phase-bucket').forEach(b => b.classList.remove('visible'));
  const bucket = getOrCreateBucket(phase);
  bucket.classList.add('visible');
}

function switchLogPhase(btn, phase) {
  document.querySelectorAll('.log-phase-tab').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  showBucket(phase);
  scrollLogsToBottom();
}

function buildLogEntry(entry, container) {
  const type = entry.type || 'info';
  const msg  = entry.msg  || '';

  // Collapse long tool_result entries
  if (type === 'tool_result' && msg.length > 120) {
    const toggle = document.createElement('div');
    toggle.className = 'log-result-toggle';
    const body = document.createElement('div');
    body.className = 'log-result-body';
    body.textContent = msg;
    toggle.innerHTML = `<span>▸ Show output</span>`;
    toggle.onclick = () => {
      const open = body.classList.toggle('open');
      toggle.querySelector('span').textContent = open ? '▾ Hide output' : '▸ Show output';
    };
    container.appendChild(toggle);
    container.appendChild(body);
    return;
  }

  const row = document.createElement('div');
  row.className = `log-entry type-${type}`;

  const ts = document.createElement('div');
  ts.className = 'log-ts';
  ts.textContent = entry.ts || '';

  const msgEl = document.createElement('div');
  msgEl.className = 'log-msg';
  msgEl.textContent = msg;

  row.appendChild(ts);
  row.appendChild(msgEl);
  container.appendChild(row);
}

function appendLogEntry(entry) {
  const phase = entry.phase || 'all';

  // Add to the phase-specific bucket and the "all" bucket
  buildLogEntry(entry, getOrCreateBucket(phase));
  buildLogEntry(entry, getOrCreateBucket('all'));

  // Update counts
  ['all', phase].forEach(ph => {
    const el = document.getElementById(`lpt-count-${ph}`);
    if (el) el.textContent = parseInt(el.textContent || '0') + 1;
  });

  // Update status for this phase
  const statusEl = document.getElementById(`lpt-status-${phase}`);
  if (statusEl) {
    const type = entry.type || '';
    const msg  = entry.msg  || '';
    if (type === 'error' || msg.includes('[FAIL]')) {
      statusEl.className = 'lpt-status failed'; statusEl.textContent = 'Failed';
    } else if (msg.includes('PHASE COMPLETE') && statusEl.textContent !== 'Failed') {
      statusEl.className = 'lpt-status complete'; statusEl.textContent = 'Done';
    } else if (!statusEl.classList.contains('failed') && !statusEl.classList.contains('complete')) {
      statusEl.className = 'lpt-status running'; statusEl.textContent = 'Running';
    }
  }

  scrollLogsToBottom();
}

function scrollLogsToBottom() {
  // Scroll the visible bucket's parent panel
  const panel = document.getElementById('log-entries-panel');
  if (panel) panel.scrollTop = panel.scrollHeight;
}

// ─── Files Tab ───────────────────────────────────────────────────
async function refreshTaskFiles() {
  if (!activeTaskId) return;
  const paths = await eel.get_task_files(activeTaskId)();
  renderFileTree(paths);
}

function renderFileTree(paths) {
  const container = document.getElementById('file-tree');
  const countEl   = document.getElementById('files-count');
  container.innerHTML = '';
  countEl.textContent = `${paths.length} files`;

  if (!paths.length) {
    container.innerHTML = '<div style="color:var(--text3);font-size:11px;padding:10px 0">No files cached</div>';
    return;
  }

  // Build tree object
  const tree = {};
  paths.forEach(p => {
    const parts = p.replace(/\\/g, '/').split('/');
    let node = tree;
    parts.forEach((part, i) => {
      if (i === parts.length - 1) {
        node[part] = null;  // file
      } else {
        if (!node[part]) node[part] = {};
        node = node[part];
      }
    });
  });

  renderTreeNode(tree, container);
}

function renderTreeNode(node, parent) {
  const entries = Object.entries(node).sort(([a, av], [b, bv]) => {
    // Dirs first
    const aIsDir = av !== null;
    const bIsDir = bv !== null;
    if (aIsDir !== bIsDir) return aIsDir ? -1 : 1;
    return a.localeCompare(b);
  });

  entries.forEach(([name, children]) => {
    const isDir = children !== null;

    const el = document.createElement('div');
    el.className = isDir ? 'ftree-dir' : 'ftree-file';

    if (isDir) {
      el.innerHTML = `<span class="ftree-arrow">▶</span><span class="ftree-icon">📁</span><span class="ftree-name">${esc(name)}</span>`;
      const childWrap = document.createElement('div');
      childWrap.className = 'ftree-children';
      el.onclick = (e) => {
        e.stopPropagation();
        el.classList.toggle('open');
      };
      renderTreeNode(children, childWrap);
      parent.appendChild(el);
      parent.appendChild(childWrap);
    } else {
      const icon = fileIcon(name);
      el.innerHTML = `<span class="ftree-arrow" style="opacity:0">▶</span><span class="ftree-icon">${icon}</span><span class="ftree-name">${esc(name)}</span>`;
      parent.appendChild(el);
    }
  });
}

function fileIcon(name) {
  const ext = name.split('.').pop().toLowerCase();
  const icons = {
    py:'🐍', js:'📜', ts:'📜', html:'🌐', css:'🎨',
    json:'📋', md:'📝', txt:'📄', sh:'⚙', yaml:'📋',
    yml:'📋', xml:'📋', svg:'🖼', png:'🖼', jpg:'🖼',
  };
  return icons[ext] || '📄';
}

// ─── Modal tab switching ─────────────────────────────────────────
function switchMTab(btn, tab) {
  document.querySelectorAll('.mt-tab').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.mt-pane').forEach(p => p.classList.remove('active'));
  if (btn) btn.classList.add('active');
  const pane = document.getElementById(`mtpane-${tab}`);
  if (pane) pane.classList.add('active');

  // Lazy load files when switching to files tab
  if (tab === 'files' && activeTaskId) {
    refreshTaskFiles();
  }
  // Scroll logs to bottom when opening
  if (tab === 'logs') {
    setTimeout(scrollLogsToBottom, 50);
  }
}

// ─── Run / Abort ─────────────────────────────────────────────────
async function runActiveTask() {
  if (!activeTaskId) return;
  const res = await eel.start_task(activeTaskId)();
  if (!res.ok) { alert('Error: ' + res.error); return; }
  activeRunId = activeTaskId;
  document.getElementById('btn-run').classList.add('hidden');
  document.getElementById('btn-abort').classList.remove('hidden');
}

async function abortActiveTask() {
  if (!activeTaskId) return;
  await eel.abort_task(activeTaskId)();
  activeRunId = null;
  document.getElementById('btn-run').classList.remove('hidden');
  document.getElementById('btn-abort').classList.add('hidden');
}

async function deleteActiveTask() {
  if (!activeTaskId) return;
  if (!confirm('Delete this task?')) return;
  await eel.delete_task(activeTaskId)();
  closeTaskModal();
  const board = await eel.get_board()();
  renderBoard(board);
}

async function moveTaskModal(col) {
  if (!activeTaskId) return;
  await eel.move_task(activeTaskId, col)();
  const board = await eel.get_board()();
  renderBoard(board);
  // Update tag in modal
  const task = await eel.get_task(activeTaskId)();
  if (task) populateModal(task);
}

// ─── Working directory ───────────────────────────────────────────
function showDirModal() {
  document.getElementById('overlay-dir').classList.add('open');
}
function closeDirModal() {
  document.getElementById('overlay-dir').classList.remove('open');
}

async function applyDir() {
  const path = document.getElementById('dir-input').value.trim();
  if (!path) return;
  const fb = document.getElementById('dir-feedback');
  fb.textContent = 'Checking…';
  fb.className = 'dir-feedback';

  const res = await eel.set_working_dir(path)();
  if (res.ok) {
    fb.textContent = `✓ ${res.path}  (${res.file_count} files)`;
    fb.className = 'dir-feedback ok';
    document.getElementById('project-dir').textContent  = res.path;
    document.getElementById('project-name').textContent = res.path.split(/[\\/]/).pop() || res.path;
    renderBoard(res.board || {});
    setTimeout(closeDirModal, 800);
  } else {
    fb.textContent = '✗ ' + res.error;
    fb.className = 'dir-feedback err';
  }
}

// ─── Misc ────────────────────────────────────────────────────────
async function refreshBoard() {
  const board = await eel.get_board()();
  renderBoard(board);
}

async function reloadModels() {
  cachedModels = await eel.get_ollama_models()();
  populateModelSelects(cachedModels);
}

function populateModelSelects(models) {
  const IDS = ['new-planning-model','new-coding-model','new-qa-model'];
  IDS.forEach(id => {
    const sel = document.getElementById(id);
    if (!sel) return;
    const prev = sel.value;
    sel.innerHTML = models.length
      ? models.map(m => `<option value="${esc(m)}"${m===prev?' selected':''}>${esc(m)}</option>`).join('')
      : '<option value="">No models (Ollama running?)</option>';
  });
}

function relativeTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const sec = Math.floor((Date.now() - d) / 1000);
  if (sec < 60)   return 'just now';
  if (sec < 3600) return Math.floor(sec/60) + 'm ago';
  if (sec < 86400) return Math.floor(sec/3600) + 'h ago';
  return Math.floor(sec/86400) + 'd ago';
}

function esc(str) {
  return String(str || '')
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;');
}

function toggleCol(btn) {
  btn.closest('.col').classList.toggle('collapsed');
}

// ─── Keyboard shortcuts ──────────────────────────────────────────
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    if (document.getElementById('overlay-task').classList.contains('open')) closeTaskModal();
    else if (document.getElementById('overlay-new').classList.contains('open')) closeNewTaskModal();
    else if (document.getElementById('overlay-dir').classList.contains('open')) closeDirModal();
  }
});

// ─── Boot ────────────────────────────────────────────────────────
init();

// ─── Prompt management (unused in board view but kept for extension) ──
async function loadPromptStep(step) { return await eel.load_prompt_file(step)(); }
async function savePromptStep(step, c) { return await eel.save_prompt_file(step, c)(); }
async function refreshCacheGlobal() { return await eel.refresh_file_cache()(); }
