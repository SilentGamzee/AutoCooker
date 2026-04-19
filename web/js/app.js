/* ══════════════════════════════════════════════════════
   Ollama Project Planner — Kanban Frontend
   ══════════════════════════════════════════════════════ */
'use strict';

// ─── App State ──────────────────────────────────────────────────
let activeTaskId  = null;      // currently open in modal
let activeRunId   = null;      // currently running task id
let cachedModels  = [];
// Provider state: {id: {id, name, type, is_active, models: []}}
let cachedModelsByProvider = {};
// Edit mode: null = create new task, string = task id being edited
let _editingTaskId    = null;
let _currentTaskCache = null;   // last task opened in detail modal

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
    _currentTaskCache = task;
    populateModal(task);
  }
}

eel.expose(task_log_added);
function task_log_added(taskId, logEntry) {
  if (activeTaskId !== taskId) return;
  appendLogEntry(logEntry);
  scrollLogsToBottom();
}

// Live-updating single-line progress. Backend emits these for long-running
// streams (LLM token stream) instead of spamming a new log row per event.
// Same progress_id = overwrite; no progress_id = promote to normal log.
eel.expose(task_log_progress);
function task_log_progress(taskId, logEntry) {
  if (activeTaskId !== taskId) return;
  const pid = logEntry && logEntry.progress_id;
  if (!pid) {
    appendLogEntry(logEntry);
    scrollLogsToBottom();
    return;
  }
  const phase = logEntry.phase || 'all';
  const msg = logEntry.msg || '';
  const ts = logEntry.ts || '';
  const type = logEntry.type || 'info';

  // Update in each bucket (phase + all); create row on first sighting.
  ['all', phase].forEach(ph => {
    const bucket = getOrCreateBucket(ph);
    const sel = `.log-progress[data-progress-id="${CSS.escape(pid)}"]`;
    let row = bucket.querySelector(sel);
    if (!row) {
      row = document.createElement('div');
      row.className = `log-entry log-progress type-${type}`;
      row.dataset.progressId = pid;
      const tsEl = document.createElement('div');
      tsEl.className = 'log-ts';
      const msgEl = document.createElement('div');
      msgEl.className = 'log-msg';
      row.appendChild(tsEl);
      row.appendChild(msgEl);
      bucket.appendChild(row);
    }
    row.querySelector('.log-ts').textContent = ts;
    row.querySelector('.log-msg').textContent = msg;
  });
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
  // Load models grouped by provider
  cachedModelsByProvider = await eel.get_models_by_provider()();
  populateModelSelects(cachedModelsByProvider);

  // Also keep flat list for legacy use
  cachedModels = Object.values(cachedModelsByProvider).flatMap(p => p.models);

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

/**
 * Compute provider error message for a task using cached model data.
 * Returns empty string if all models are from active providers.
 */
function getProviderError(task) {
  const models = [
    task.models?.planning,
    task.models?.coding,
    task.models?.qa,
  ].filter(Boolean);
  if (!models.length) return '';
  // Normalize model id: strip provider prefix (e.g. "gemini-cli/gemini-2.5-flash" → "gemini-2.5-flash")
  const normalize = id => id.includes('/') ? id.split('/').slice(1).join('/') : id;

  const allProviders = Object.values(cachedModelsByProvider);
  // Build set of all active model IDs, both raw and normalized
  const activeModels = new Set();
  allProviders.filter(p => p.is_active).flatMap(p => p.models).forEach(m => {
    activeModels.add(m);
    activeModels.add(normalize(m));
  });

  const errors = [];
  const seen = new Set();
  for (const m of models) {
    if (seen.has(m)) continue;
    seen.add(m);
    if (!activeModels.has(m) && !activeModels.has(normalize(m))) {
      const owner = allProviders.find(p =>
        p.models.includes(m) || p.models.includes(normalize(m)) ||
        p.models.some(pm => normalize(pm) === normalize(m))
      );
      if (owner && !owner.is_active) {
        errors.push(`Model '${m}' — provider '${owner.name}' is inactive`);
      } else {
        errors.push(`Model '${m}' not available from any active provider`);
      }
    }
  }
  return errors.join(' · ');
}

function buildCard(task) {
  const provErr = getProviderError(task);

  const card = document.createElement('div');
  card.className = 'task-card' +
    (task.has_errors ? ' has-errors' : '') +
    (provErr ? ' provider-error' : '') +
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

  const provErrHTML = provErr
    ? `<div class="task-card-provider-err" title="${esc(provErr)}">⚠ Модель недоступна у активных провайдеров</div>`
    : '';

  card.innerHTML = `
    <div class="task-card-title">${esc(task.title)}</div>
    ${task.description ? `<div class="task-card-desc">${esc(task.description)}</div>` : ''}
    ${tagHTML ? `<div class="task-card-tags">${tagHTML}</div>` : ''}
    ${provErrHTML}
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
  _editingTaskId = null;
  document.getElementById('new-modal-title').textContent  = 'New Task';
  document.getElementById('new-modal-submit').textContent = 'Create Task';
  // Clear form
  document.getElementById('new-title').value  = '';
  document.getElementById('new-desc').value   = '';
  document.getElementById('new-dir').value    = '';
  document.getElementById('new-branch').value = 'main';
  // Reset phase checkboxes
  document.querySelectorAll('.phase-chk input').forEach(cb => { cb.checked = true; });
  document.getElementById('new-task-provider-warn').classList.add('hidden');
  document.getElementById('overlay-new').classList.add('open');
  document.getElementById('new-title').focus();
}

function closeNewTaskModal() {
  document.getElementById('overlay-new').classList.remove('open');
  _editingTaskId = null;
}

function enterEditMode() {
  if (!activeTaskId) return;
  document.getElementById('modal-task').classList.add('editing');

  // Populate edit fields from current view values
  const task = _currentTaskCache;
  if (!task) return;

  document.getElementById('mt-edit-title').value  = task.title || '';
  document.getElementById('ov-edit-desc').value   = task.description || '';
  document.getElementById('ov-edit-branch').value = task.git_branch || '';
  document.getElementById('ov-edit-path').value   = task.project_path || '';

  // Phase checkboxes
  const phases = task.phases_selected || ['planning','coding','qa'];
  ['planning','coding','qa'].forEach(p => {
    const cb = document.getElementById('ov-phase-' + p);
    if (cb) cb.checked = phases.includes(p);
  });

  // Model selects — build from cached providers
  _populateOvModelSelects(task);

  document.getElementById('mt-edit-title').focus();
}

function cancelEditMode() {
  const modal = document.getElementById('modal-task');
  if (modal) modal.classList.remove('editing');
}

async function saveTaskEdit() {
  if (!activeTaskId) return;
  const title = document.getElementById('mt-edit-title').value.trim();
  if (!title) { document.getElementById('mt-edit-title').focus(); return; }

  const phases = ['planning','coding','qa'].filter(p => {
    const cb = document.getElementById('ov-phase-' + p);
    return cb && cb.checked;
  });

  const cfg = {
    title,
    description:    document.getElementById('ov-edit-desc').value.trim(),
    project_path:   document.getElementById('ov-edit-path').value.trim(),
    git_branch:     document.getElementById('ov-edit-branch').value.trim() || 'main',
    planning_model: document.getElementById('ov-edit-planning-model').value,
    coding_model:   document.getElementById('ov-edit-coding-model').value,
    qa_model:       document.getElementById('ov-edit-qa-model').value,
    phases,
  };

  const res = await eel.update_task(activeTaskId, cfg)();
  if (!res || !res.ok) { alert('Error: ' + (res?.error || 'unknown')); return; }

  // Refresh the modal with updated task
  const updated = await eel.get_task(activeTaskId)();
  if (updated) {
    _currentTaskCache = updated;
    populateModal(updated);
  }
}

/** Populate the three ov-edit model selects, grouped by provider. */
function _populateOvModelSelects(task) {
  const selMap = {
    'ov-edit-planning-model': task.models?.planning || '',
    'ov-edit-coding-model':   task.models?.coding   || '',
    'ov-edit-qa-model':       task.models?.qa       || '',
  };
  const activeProviders = Object.values(cachedModelsByProvider).filter(p => p.is_active);

  Object.entries(selMap).forEach(([id, currentVal]) => {
    const sel = document.getElementById(id);
    if (!sel) return;

    let html = '';
    activeProviders.forEach(p => {
      if (!p.models.length) return;
      html += `<optgroup label="${esc(p.name)}">`;
      p.models.forEach(m => {
        html += `<option value="${esc(m)}"${m === currentVal ? ' selected' : ''}>${esc(m)}</option>`;
      });
      html += '</optgroup>';
    });

    // If current model not in active providers, show it as a stale option
    const allActive = activeProviders.flatMap(p => p.models);
    if (currentVal && !allActive.includes(currentVal)) {
      html = `<option value="${esc(currentVal)}" selected>${esc(currentVal)} ⚠</option>` + html;
    }

    sel.innerHTML = html || '<option value="">No models available</option>';
  });
}

async function submitTaskModal() {
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
  if (!res || !res.ok) { alert('Error: ' + (res?.error || 'unknown')); return; }
  closeNewTaskModal();
  const board = await eel.get_board()();
  renderBoard(board);
}

// ─── Task Detail Modal ───────────────────────────────────────────
async function openTaskModal(taskId) {
  activeTaskId = taskId;
  const task = await eel.get_task(taskId)();
  if (!task) return;
  _currentTaskCache = task;
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

  // Phase Status Bar
  updatePhaseStatusBar(task);

  // Resume Indicator
  updateResumeIndicator(task);

  // Patch Indicator
  updatePatchIndicator(task);

  // Overview — view fields
  document.getElementById('mt-title').textContent   = task.title;
  document.getElementById('ov-desc').textContent    = task.description || '—';
  document.getElementById('ov-branch').textContent  = task.git_branch || '—';
  document.getElementById('ov-path').textContent    = task.project_path || '—';
  document.getElementById('ov-models').textContent  =
    `Planning: ${task.models?.planning || '—'}  |  Coding: ${task.models?.coding || '—'}  |  QA: ${task.models?.qa || '—'}`;
  const phaseLabels = { planning:'Planning', coding:'Coding', qa:'QA' };
  document.getElementById('ov-phases-view').textContent =
    (task.phases_selected || ['planning','coding','qa']).map(p => phaseLabels[p] || p).join(' → ');
  document.getElementById('ov-created').textContent = task.created_at || '—';
  document.getElementById('ov-updated').textContent = task.updated_at || '—';

  // Ensure edit mode is off when re-populating
  cancelEditMode();

  // Patch / corrections info row
  _renderPatchInfo(task);

  // Run / Continue / Restart / Abort buttons
  _updateTaskButtons(task);

  // Corrections panel: show when task is done or in human_review
  const showCorrections = ['done', 'human_review'].includes(task.column);
  document.getElementById('corrections-panel').classList.toggle('hidden', !showCorrections);
  if (showCorrections) {
    const textarea = document.getElementById('corrections-input');
    if (document.activeElement !== textarea) {
      textarea.value = task.corrections || '';
    }
  }

  // Git merge actions — show after coding phase completed (not in_progress/planning)
  const codingDone = ['ai_review','human_review','done'].includes(task.column) ||
    (task.column === 'in_progress' && (task.phases_selected || []).includes('coding') &&
     (task.subtasks || []).length > 0);
  const hasWorkdir = task.task_dir && (task.phases_selected || []).includes('coding');
  const gitActionsEl = document.getElementById('ov-git-actions');
  gitActionsEl.classList.toggle('hidden', !(hasWorkdir && codingDone));
  if (task.git_branch) {
    document.getElementById('ov-git-branch').textContent = task.git_branch;
  }

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

function updatePhaseStatusBar(task) {
  const bar = document.getElementById('phase-status-bar');
  const phaseStatus = task.phase_status || {};
  const lastActive = task.last_active_phase || '';
  
  // Show bar if task is in progress or has phase status
  const shouldShow = task.column === 'in_progress' || 
                     Object.values(phaseStatus).some(s => s !== 'pending');
  bar.classList.toggle('hidden', !shouldShow);
  
  if (!shouldShow) return;
  
  const phases = ['planning', 'coding', 'qa'];
  phases.forEach(phase => {
    const badge = document.getElementById(`phase-badge-${phase}`);
    const statusEl = document.getElementById(`phase-status-${phase}`);
    const status = phaseStatus[phase] || 'pending';
    
    // Remove all status classes
    badge.className = 'phase-badge';
    
    // Add appropriate class
    badge.classList.add(`phase-${status}`);
    if (phase === lastActive) {
      badge.classList.add('phase-active');
    }
    
    // Update status icon
    const icons = {
      'pending': '○',
      'in_progress': '⟳',
      'done': '✓',
      'failed': '✗',
      'needs_analysis': '⚠️',
      'skipped': '⊘'
    };
    statusEl.textContent = icons[status] || '○';
  });
}

function updateResumeIndicator(task) {
  const indicator = document.getElementById('resume-indicator');
  const resumePhaseEl = document.getElementById('resume-phase');
  
  if (task.can_resume && task.resume_from_phase) {
    indicator.classList.remove('hidden');
    resumePhaseEl.textContent = task.resume_from_phase.toUpperCase();
  } else {
    indicator.classList.add('hidden');
  }
}

function updatePatchIndicator(task) {
  const indicator = document.getElementById('patch-indicator');
  const currentEl = document.getElementById('patch-current');
  const maxEl = document.getElementById('patch-max');
  
  if (task.patch_count > 0) {
    indicator.classList.remove('hidden');
    currentEl.textContent = task.patch_count;
    maxEl.textContent = task.max_patches || 2;
  } else {
    indicator.classList.add('hidden');
  }
}


// ─── Subtasks Tab ────────────────────────────────────────────────
function renderSubtasks(subtasks) {
  const wrap = document.getElementById('subtasks-list');
  if (!subtasks.length) {
    wrap.innerHTML = '<div style="color:var(--text3);font-size:12px;padding:20px 0">No subtasks yet — run Planning phase first</div>';
    return;
  }

  wrap.innerHTML = subtasks.map((t, i) => {
    const status = t.status || 'pending';
    const currentLoop = t.current_loop || 0;
    const maxLoops = t.max_loops || 3;
    
    // Determine badge and icon
    let badge = '';
    let iconChar = '';
    let iconCls = '';
    
    if (status === 'done') {
      badge = '<span class="st-badge st-done">✓ Done</span>';
      iconChar = '✓';
      iconCls = 'done';
    } else if (status === 'invalid') {
      badge = '<span class="st-badge st-invalid">⊘ Invalid</span>';
      iconChar = '⊘';
      iconCls = 'invalid';
    } else if (status === 'skipped') {
      badge = '<span class="st-badge st-skipped">⊘ Skipped</span>';
      iconChar = '⊘';
      iconCls = 'skipped';
    } else if (status === 'needs_analysis') {
      badge = '<span class="st-badge st-analysis">⚠️ Analysis Needed</span>';
      iconChar = '⚠';
      iconCls = 'failed';
    } else if (status === 'in_progress' && currentLoop > 0) {
      badge = `<span class="st-badge st-loop">⟳ Loop ${currentLoop}/${maxLoops}</span>`;
      iconChar = '◉';
      iconCls = 'active';
    } else if (status === 'in_progress') {
      badge = '<span class="st-badge st-progress">◉ In Progress</span>';
      iconChar = '◉';
      iconCls = 'active';
    } else {
      badge = '<span class="st-badge st-pending">○ Pending</span>';
      iconChar = '';
      iconCls = 'pending';
    }
    
    return `
    <div class="subtask-row" onclick="toggleSubtask(this)">
      <div class="subtask-check ${iconCls}">${iconChar}</div>
      <div class="subtask-content">
        <div class="subtask-header">
          <span class="subtask-num">#${i+1}</span>
          <span class="subtask-title">${esc(t.title)}</span>
          ${badge}
        </div>
        <div class="subtask-detail">
          <div class="subtask-desc">${esc(t.description || '')}</div>
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
let _cacheContents = {};  // rel_path -> content

async function refreshTaskFiles() {
  if (!activeTaskId) return;
  const data = await eel.get_cache_tree(activeTaskId)();
  _cacheContents = data.contents || {};
  // Show ONLY files that have cached content for this task
  const paths = Object.keys(_cacheContents);
  renderFileTree(paths, _cacheContents);
}

function renderFileTree(paths, contents) {
  const container = document.getElementById('file-tree');
  const countEl   = document.getElementById('files-count');
  container.innerHTML = '';

  countEl.textContent = `${paths.length} файлов в кэше`;

  if (!paths.length) {
    container.innerHTML = '<div style="color:var(--text3);font-size:11px;padding:10px 6px">Кэш пуст</div>';
    return;
  }

  // Build tree object; mark which paths have cached content
  const tree = {};
  paths.forEach(p => {
    const parts = p.replace(/\\/g, '/').split('/');
    let node = tree;
    parts.forEach((part, i) => {
      if (i === parts.length - 1) {
        node[part] = null;  // leaf = file
      } else {
        if (!node[part]) node[part] = {};
        node = node[part];
      }
    });
  });

  renderTreeNode(tree, container, '', contents);
}

function renderTreeNode(node, parent, prefix, contents) {
  const entries = Object.entries(node).sort(([a, av], [b, bv]) => {
    const aDir = av !== null;
    const bDir = bv !== null;
    if (aDir !== bDir) return aDir ? -1 : 1;
    return a.localeCompare(b);
  });

  entries.forEach(([name, children]) => {
    const isDir = children !== null;
    const relPath = prefix ? `${prefix}/${name}` : name;

    const el = document.createElement('div');
    el.className = isDir ? 'ftree-dir' : 'ftree-file';

    if (isDir) {
      el.innerHTML = `
        <span class="ftree-arrow">▶</span>
        <span class="ftree-icon">📁</span>
        <span class="ftree-name">${esc(name)}</span>`;
      const childWrap = document.createElement('div');
      childWrap.className = 'ftree-children';
      el.onclick = (e) => {
        e.stopPropagation();
        el.classList.toggle('open');
      };
      renderTreeNode(children, childWrap, relPath, contents);
      parent.appendChild(el);
      parent.appendChild(childWrap);
    } else {
      const icon = fileIcon(name);
      const hasCached = Object.prototype.hasOwnProperty.call(contents, relPath);
      const nameClass = hasCached ? 'ftree-name has-cache' : 'ftree-name';
      el.innerHTML = `
        <span class="ftree-arrow" style="opacity:0">▶</span>
        <span class="ftree-icon">${icon}</span>
        <span class="${nameClass}">${esc(name)}</span>`;
      el.title = relPath;
      el.dataset.path = relPath;
      el.onclick = (e) => {
        e.stopPropagation();
        openFileContent(relPath, name, hasCached, el);
      };
      parent.appendChild(el);
    }
  });
}

function openFileContent(relPath, name, hasCached, el) {
  // Highlight selected
  document.querySelectorAll('.ftree-file.selected').forEach(f => f.classList.remove('selected'));
  el.classList.add('selected');

  const icon    = document.getElementById('file-content-icon');
  const nameEl  = document.getElementById('file-content-name');
  const badge   = document.getElementById('file-content-badge');
  const empty   = document.getElementById('file-content-empty');
  const body    = document.getElementById('file-content-body');

  icon.textContent  = fileIcon(name);
  nameEl.textContent = relPath;

  if (hasCached) {
    const content = _cacheContents[relPath] || '';
    badge.textContent  = 'кэш';
    badge.className    = 'file-content-badge cached';
    empty.classList.add('hidden');
    body.classList.remove('hidden');
    body.textContent   = content || '(пустой файл)';
  } else {
    badge.textContent  = 'нет кэша';
    badge.className    = 'file-content-badge no-cache';
    body.classList.add('hidden');
    empty.classList.remove('hidden');
    document.querySelector('.fce-icon').textContent  = '🔍';
    document.querySelector('.fce-title').textContent = 'Файл не закэширован';
    document.querySelector('.fce-sub').textContent   =
      'Этот файл есть в индексе, но его содержимое\nещё не было прочитано агентом.';
  }
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
  if (tab === 'files') {
    refreshTaskFiles();
  }
  // Scroll logs to bottom when opening
  if (tab === 'logs') {
    setTimeout(scrollLogsToBottom, 50);
  }
}

// ─── Pipeline action buttons ─────────────────────────────────────

function _renderPatchInfo(task) {
  const row  = document.getElementById('ov-patch-row');
  const info = document.getElementById('ov-patch-info');

  const hasPatch     = !!(task.corrections && task.corrections.trim());
  const iteration    = task.current_iteration || 0;
  const maxIter      = task.max_iterations   || 3;
  const isRunning    = activeRunId === task.id;
  const inProgress   = task.column === 'in_progress';

  // Show the row only when there's something meaningful to display
  const hasIterInfo  = iteration > 0;
  if (!hasPatch && !hasIterInfo) {
    row.classList.add('hidden');
    return;
  }
  row.classList.remove('hidden');

  // Build content
  const parts = [];

  if (hasIterInfo) {
    const iterLabel = isRunning && inProgress
      ? `<span class="patch-running">⟳ Running</span> iteration ${iteration} / ${maxIter}`
      : `Iteration ${iteration} / ${maxIter}`;

    // Flow type: patch mode vs fresh planning
    const hasSubs    = (task.subtasks || []).length > 0;
    const modeLabel  = (hasPatch && hasSubs && iteration > 1)
      ? '<span class="patch-badge">patch</span>'
      : '<span class="patch-badge patch-badge--full">full plan</span>';

    parts.push(`${modeLabel} ${iterLabel}`);
  }

  if (hasPatch) {
    const preview = task.corrections.trim().replace(/\n/g, ' ');
    const short   = preview.length > 120 ? preview.slice(0, 120) + '…' : preview;
    parts.push(`<span class="patch-corrections">${_esc(short)}</span>`);
  }

  info.innerHTML = parts.join('<br>');
}

function _esc(s) {
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function _updateTaskButtons(task) {
  const isRunning  = activeRunId === task.id;
  // A task has been started before if it has a task_dir OR subtasks OR logs
  const hasStarted = !!(task.task_dir || (task.subtasks && task.subtasks.length) ||
                        (task.logs && task.logs.length));

  // While running: only Abort visible
  document.getElementById('btn-abort').classList.toggle('hidden', !isRunning);

  if (isRunning) {
    document.getElementById('btn-run').classList.add('hidden');
    document.getElementById('btn-continue').classList.add('hidden');
    document.getElementById('btn-restart').classList.add('hidden');
    return;
  }

  if (hasStarted) {
    // Task was started before → show Continue + Restart
    document.getElementById('btn-run').classList.add('hidden');
    document.getElementById('btn-continue').classList.remove('hidden');
    document.getElementById('btn-restart').classList.remove('hidden');
  } else {
    // Fresh task → show Run only
    document.getElementById('btn-run').classList.remove('hidden');
    document.getElementById('btn-continue').classList.add('hidden');
    document.getElementById('btn-restart').classList.add('hidden');
  }
}

async function runActiveTask() {
  if (!activeTaskId) return;
  const res = await eel.start_task(activeTaskId)();
  if (!res.ok) { alert('Error: ' + res.error); return; }
  activeRunId = activeTaskId;
  const task = await eel.get_task(activeTaskId)();
  if (task) _updateTaskButtons(task);
}

async function continueActiveTask() {
  // Continue = same as Run — pipeline resumes from last state
  await runActiveTask();
}

async function restartActiveTask() {
  if (!activeTaskId) return;
  if (!confirm('Restart will erase all progress, subtasks and logs for this task.\n\nAre you sure?')) return;
  const res = await eel.restart_task(activeTaskId)();
  if (!res.ok) { alert('Error: ' + res.error); return; }
  // Reload task state after reset
  const task = await eel.get_task(activeTaskId)();
  if (task) {
    renderTaskDetail(task);
  }
}

async function saveAndRun() {
  if (!activeTaskId) return;
  const corrections = document.getElementById('corrections-input').value.trim();
  await eel.save_corrections(activeTaskId, corrections)();
  await runActiveTask();
}

async function clearCorrections() {
  if (!activeTaskId) return;
  document.getElementById('corrections-input').value = '';
  await eel.save_corrections(activeTaskId, '')();
}

async function abortActiveTask() {
  if (!activeTaskId) return;
  await eel.abort_task(activeTaskId)();
  activeRunId = null;
  const task = await eel.get_task(activeTaskId)();
  if (task) _updateTaskButtons(task);
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
async function showDirModal() {
  document.getElementById('overlay-dir').classList.add('open');
  // Populate recent dirs list each time the modal opens
  const recent = await eel.get_recent_dirs()();
  const section = document.getElementById('recent-dirs-section');
  const list    = document.getElementById('recent-dirs-list');
  if (recent && recent.length > 0) {
    list.innerHTML = '';
    recent.forEach(dir => {
      const item = document.createElement('div');
      item.className = 'recent-dir-item';
      const name = dir.replace(/\\/g, '/').split('/').pop() || dir;
      item.innerHTML = `
        <span class="recent-dir-icon">📁</span>
        <div class="recent-dir-info">
          <div class="recent-dir-name">${name}</div>
          <div class="recent-dir-path">${dir}</div>
        </div>
        <button class="recent-dir-select" onclick="selectRecentDir(${JSON.stringify(dir)})">Select</button>
      `;
      list.appendChild(item);
    });
    section.classList.remove('hidden');
  } else {
    section.classList.add('hidden');
  }
}

function closeDirModal() {
  document.getElementById('overlay-dir').classList.remove('open');
}

function selectRecentDir(path) {
  document.getElementById('dir-input').value = path;
  applyDir();
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

// ─── Providers Modal ─────────────────────────────────────────────

async function showProvidersModal() {
  await renderProviderList();
  onProviderTypeChange(); // set initial URL placeholder
  document.getElementById('overlay-providers').classList.add('open');
}

function closeProvidersModal() {
  document.getElementById('overlay-providers').classList.remove('open');
}

async function renderProviderList() {
  const providers = await eel.get_providers()();
  const listEl = document.getElementById('prov-list');
  if (!providers.length) {
    listEl.innerHTML = '<div class="prov-empty">No providers configured</div>';
    return;
  }
  const typeLabels = { lmstudio: 'LM Studio', omniroute: 'OmniRoute', gemini: 'Gemini' };
  listEl.innerHTML = providers.map(p => {
    const activeClass = p.is_active ? 'prov-active' : 'prov-inactive';
    const activeLabel = p.is_active ? 'Active' : 'Inactive';
    const typeLabel   = typeLabels[p.type] || p.type;
    const keyInfo = p.api_key_masked
      ? `<span class="prov-key-badge">${esc(p.api_key_masked)}</span>`
      : '';
    const needsKey = (p.type === 'omniroute' || p.type === 'gemini');
    return `
      <div class="prov-item ${activeClass}" data-id="${esc(p.id)}" id="prov-item-${esc(p.id)}">
        <div class="prov-item-view">
          <div class="prov-item-left">
            <div class="prov-item-name">${esc(p.name)}</div>
            <div class="prov-item-meta">
              <span class="prov-type-badge">${esc(typeLabel)}</span>
              <span class="prov-url">${esc(p.base_url)}</span>
              ${keyInfo}
            </div>
          </div>
          <div class="prov-item-actions">
            <span class="prov-status-dot ${activeClass}" title="${activeLabel}"></span>
            <button class="prov-edit-btn" onclick="openProviderEdit('${esc(p.id)}')" title="Edit">✎</button>
            <button class="prov-toggle-btn" onclick="toggleProvider('${esc(p.id)}')" title="${p.is_active ? 'Deactivate' : 'Activate'}">
              ${p.is_active ? 'Deactivate' : 'Activate'}
            </button>
            <button class="prov-remove-btn" onclick="removeProvider('${esc(p.id)}')" title="Remove">✕</button>
          </div>
        </div>
        <div class="prov-item-edit hidden" id="prov-edit-${esc(p.id)}">
          <div class="prov-edit-row">
            <div class="prov-form-field prov-form-field--grow">
              <label class="field-label">Name</label>
              <input type="text" class="inp prov-edit-name" value="${esc(p.name)}" />
            </div>
          </div>
          <div class="prov-edit-row">
            <div class="prov-form-field prov-form-field--grow">
              <label class="field-label">Base URL</label>
              <input type="text" class="inp prov-edit-url" value="${esc(p.base_url)}" />
            </div>
          </div>
          ${needsKey ? `
          <div class="prov-edit-row">
            <div class="prov-form-field prov-form-field--grow">
              <label class="field-label">API Key <span class="prov-key-hint">(leave blank to keep current)</span></label>
              <input type="password" class="inp prov-edit-key" placeholder="sk-… (unchanged)" autocomplete="off" />
            </div>
          </div>` : ''}
          <div class="prov-form-error hidden prov-edit-error"></div>
          <div class="prov-edit-actions">
            <button class="btn-ghost btn-sm" onclick="cancelProviderEdit('${esc(p.id)}')">Cancel</button>
            <button class="btn-primary btn-sm" onclick="saveProviderEdit('${esc(p.id)}')">Save</button>
          </div>
        </div>
      </div>`;
  }).join('');
}

function openProviderEdit(id) {
  const item = document.getElementById('prov-item-' + id);
  if (!item) return;
  item.querySelector('.prov-item-view').classList.add('hidden');
  item.querySelector('.prov-item-edit').classList.remove('hidden');
}

function cancelProviderEdit(id) {
  const item = document.getElementById('prov-item-' + id);
  if (!item) return;
  item.querySelector('.prov-item-view').classList.remove('hidden');
  item.querySelector('.prov-item-edit').classList.add('hidden');
}

async function saveProviderEdit(id) {
  const item = document.getElementById('prov-item-' + id);
  if (!item) return;
  const errEl = item.querySelector('.prov-edit-error');
  errEl.classList.add('hidden');

  const cfg = {
    name:     item.querySelector('.prov-edit-name').value.trim(),
    base_url: item.querySelector('.prov-edit-url').value.trim(),
  };
  const keyInput = item.querySelector('.prov-edit-key');
  if (keyInput) {
    const newKey = keyInput.value.trim();
    if (newKey) cfg.api_key = newKey;  // only send if user typed something
  }

  let res;
  try {
    res = await eel.update_provider(id, cfg)();
  } catch (e) {
    errEl.textContent = 'Error: ' + e;
    errEl.classList.remove('hidden');
    return;
  }
  if (!res || !res.ok) {
    errEl.textContent = (res && res.error) || 'Unknown error';
    errEl.classList.remove('hidden');
    return;
  }
  await _refreshAfterProviderChange();
}

const PROVIDER_DEFAULTS = {
  lmstudio:  { url: 'http://localhost:1234',                                        needsKey: false },
  omniroute: { url: 'https://api.omni-route.com',                                  needsKey: true  },
  gemini:    { url: 'https://generativelanguage.googleapis.com/v1beta/openai',      needsKey: true  },
};

function onProviderTypeChange() {
  const type     = document.getElementById('prov-type').value;
  const keyRow   = document.getElementById('prov-key-row');
  const urlInput = document.getElementById('prov-url');
  const def      = PROVIDER_DEFAULTS[type] || PROVIDER_DEFAULTS.lmstudio;

  if (def.needsKey) {
    keyRow.classList.remove('hidden');
  } else {
    keyRow.classList.add('hidden');
  }

  // Only auto-fill URL if the field is empty or still holds another provider's default
  const currentIsDefault = Object.values(PROVIDER_DEFAULTS).some(d => d.url === urlInput.value);
  if (!urlInput.value || currentIsDefault) {
    urlInput.value = def.url;
  }
  urlInput.placeholder = def.url;
}

async function addProvider() {
  const errEl = document.getElementById('prov-form-error');
  errEl.classList.add('hidden');

  const cfg = {
    type:     document.getElementById('prov-type').value,
    name:     document.getElementById('prov-name').value.trim(),
    base_url: document.getElementById('prov-url').value.trim(),
    api_key:  document.getElementById('prov-key').value.trim(),
  };

  let res;
  try {
    res = await eel.add_provider(cfg)();
  } catch (e) {
    errEl.textContent = 'Error: ' + e;
    errEl.classList.remove('hidden');
    return;
  }
  if (!res || !res.ok) {
    errEl.textContent = (res && res.error) || 'Unknown error';
    errEl.classList.remove('hidden');
    return;
  }

  // Clear form
  document.getElementById('prov-name').value = '';
  document.getElementById('prov-url').value  = '';
  document.getElementById('prov-key').value  = '';

  await _refreshAfterProviderChange();
}

async function toggleProvider(providerId) {
  let res;
  try {
    res = await eel.toggle_provider(providerId)();
  } catch (e) { alert('Error: ' + e); return; }
  if (!res || !res.ok) { alert((res && res.error) || 'Toggle failed'); return; }
  await _refreshAfterProviderChange();
}

async function removeProvider(providerId) {
  if (!confirm('Remove this provider?')) return;
  let res;
  try {
    res = await eel.remove_provider(providerId)();
  } catch (e) { alert('Error: ' + e); return; }
  if (!res || !res.ok) { alert((res && res.error) || 'Remove failed'); return; }
  await _refreshAfterProviderChange();
}

/**
 * After any provider change: reload model cache, re-render provider list,
 * then re-render the board so provider-error styles update on all cards.
 */
async function _refreshAfterProviderChange() {
  // First reload model cache (this can be slow if providers are offline)
  cachedModelsByProvider = await eel.get_models_by_provider()();
  cachedModels = Object.values(cachedModelsByProvider).flatMap(p => p.models);
  populateModelSelects(cachedModelsByProvider);
  // Refresh provider list in modal
  await renderProviderList();
  // Re-render board so provider-error CSS updates on all cards
  const board = await eel.get_board()();
  renderBoard(board);
}

// ─── Misc ────────────────────────────────────────────────────────
async function refreshBoard() {
  const board = await eel.get_board()();
  renderBoard(board);
}

async function reloadModels() {
  cachedModelsByProvider = await eel.get_models_by_provider()();
  cachedModels = Object.values(cachedModelsByProvider).flatMap(p => p.models);
  populateModelSelects(cachedModelsByProvider);
  const board = await eel.get_board()();
  renderBoard(board);
}

/**
 * Populate the three model <select> elements using grouped data from providers.
 * modelsByProvider: {id: {id, name, type, is_active, models: []}}
 * Active providers get their models shown; inactive providers are omitted.
 * Each provider becomes an <optgroup>.
 */
function populateModelSelects(modelsByProvider) {
  const IDS = ['new-planning-model','new-coding-model','new-qa-model'];
  const activeProviders = Object.values(modelsByProvider).filter(p => p.is_active);
  const totalModels = activeProviders.reduce((s, p) => s + p.models.length, 0);

  IDS.forEach(id => {
    const sel = document.getElementById(id);
    if (!sel) return;
    const prev = sel.value;

    if (!totalModels) {
      sel.innerHTML = '<option value="">No models available</option>';
      return;
    }

    let html = '';
    activeProviders.forEach(p => {
      if (!p.models.length) return;
      html += `<optgroup label="${esc(p.name)}">`;
      p.models.forEach(m => {
        html += `<option value="${esc(m)}"${m === prev ? ' selected' : ''}>${esc(m)}</option>`;
      });
      html += '</optgroup>';
    });
    sel.innerHTML = html;
  });
}

/**
 * Called when model selects change — warn if any selected model is from an inactive provider.
 */
function checkNewTaskProviderStatus() {
  const warnEl = document.getElementById('new-task-provider-warn');
  if (!warnEl) return;

  const selected = [
    document.getElementById('new-planning-model')?.value,
    document.getElementById('new-coding-model')?.value,
    document.getElementById('new-qa-model')?.value,
  ].filter(Boolean);

  // Build map of model -> provider for ALL providers (including inactive)
  const allProviders = Object.values(cachedModelsByProvider);
  const activeModels = new Set(
    allProviders.filter(p => p.is_active).flatMap(p => p.models)
  );
  const inactiveIssues = [];
  selected.forEach(m => {
    if (m && !activeModels.has(m)) {
      const owner = allProviders.find(p => p.models.includes(m));
      inactiveIssues.push(owner
        ? `Model '${m}' belongs to inactive provider '${owner.name}'`
        : `Model '${m}' is not available from any active provider`);
    }
  });

  if (inactiveIssues.length) {
    warnEl.textContent = '⚠ ' + inactiveIssues.join(' · ');
    warnEl.classList.remove('hidden');
  } else {
    warnEl.classList.add('hidden');
  }
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
    if (document.getElementById('overlay-msg')?.classList.contains('open')) closeMessageModal();
    else if (document.getElementById('overlay-task').classList.contains('open')) closeTaskModal();
    else if (document.getElementById('overlay-new').classList.contains('open')) closeNewTaskModal();
    else if (document.getElementById('overlay-dir').classList.contains('open')) closeDirModal();
    else if (document.getElementById('overlay-providers').classList.contains('open')) closeProvidersModal();
  }
});

// ─── Boot ────────────────────────────────────────────────────────
init();

// ─── Prompt management (unused in board view but kept for extension) ──
async function loadPromptStep(step) { return await eel.load_prompt_file(step)(); }
async function savePromptStep(step, c) { return await eel.save_prompt_file(step, c)(); }
async function refreshCacheGlobal() { return await eel.refresh_file_cache()(); }

// ─── Git Diff & Merge ─────────────────────────────────────────────
let _diffData = [];

async function showDiff() {
  if (!activeTaskId) return;
  const res = await eel.get_task_workdir_diff(activeTaskId)();
  if (!res.ok) { alert('Error: ' + res.error); return; }

  _diffData = res.files || [];
  document.getElementById('diff-subtitle').textContent =
    `${res.total} file${res.total !== 1 ? 's' : ''} changed`;

  const fileList = document.getElementById('diff-file-list');
  fileList.innerHTML = '';

  if (!_diffData.length) {
    fileList.innerHTML = '<div class="diff-no-changes">No changes — workdir matches project</div>';
    document.getElementById('diff-viewer').innerHTML =
      '<div class="diff-empty">Workdir is identical to project</div>';
  } else {
    _diffData.forEach((f, i) => {
      const item = document.createElement('div');
      item.className = 'diff-file-item' + (i === 0 ? ' active' : '');
      const isNew = f.label.startsWith('new file');
      item.innerHTML =
        `<span class="diff-file-badge ${isNew ? 'badge-new' : 'badge-mod'}">${isNew ? 'NEW' : 'MOD'}</span>` +
        `<span class="diff-file-name">${esc(f.rel)}</span>`;
      item.onclick = () => {
        fileList.querySelectorAll('.diff-file-item').forEach((el, j) =>
          el.classList.toggle('active', j === i));
        renderDiff(_diffData[i].diff);
      };
      fileList.appendChild(item);
    });
    renderDiff(_diffData[0].diff);
  }

  document.getElementById('overlay-diff').classList.add('open');
}

function renderDiff(diffText) {
  const viewer = document.getElementById('diff-viewer');
  if (!diffText) {
    viewer.innerHTML = '<div class="diff-empty">No textual diff available</div>';
    return;
  }
  const lines = diffText.split('\n');
  const html = lines.map(line => {
    let cls = 'diff-line';
    if      (line.startsWith('+') && !line.startsWith('+++')) cls += ' diff-add';
    else if (line.startsWith('-') && !line.startsWith('---')) cls += ' diff-del';
    else if (line.startsWith('@@'))                           cls += ' diff-hunk';
    else if (line.startsWith('+++') || line.startsWith('---')) cls += ' diff-header';
    return `<div class="${cls}">${esc(line) || '\u00a0'}</div>`;
  }).join('');
  viewer.innerHTML = `<div class="diff-content">${html}</div>`;
}

function closeDiffModal() {
  document.getElementById('overlay-diff').classList.remove('open');
}

function showMessageModal(title, body, tone) {
  const t = document.getElementById('msg-modal-title');
  const b = document.getElementById('msg-modal-body');
  if (t) t.textContent = title || 'Notice';
  if (b) {
    b.textContent = body || '';
    b.style.color = tone === 'error'
      ? 'var(--danger, #ff6b6b)'
      : (tone === 'success' ? 'var(--accent, #4ade80)' : 'var(--text1)');
  }
  document.getElementById('overlay-msg').classList.add('open');
}

function closeMessageModal() {
  document.getElementById('overlay-msg').classList.remove('open');
}

async function mergeWorkdir() {
  if (!activeTaskId) return;
  const task = await eel.get_task(activeTaskId)();
  const branch = task?.git_branch || 'main';
  if (!confirm(`Merge workdir into branch "${branch}" and create a commit?`)) return;

  const res = await eel.merge_workdir(activeTaskId)();
  if (res.ok) {
    showMessageModal(
      'Merge successful',
      `✓ Merged ${res.files.length} file(s) into branch "${res.branch}"`,
      'success'
    );
  } else {
    showMessageModal('Merge failed', res.error || 'Unknown error', 'error');
  }
}
