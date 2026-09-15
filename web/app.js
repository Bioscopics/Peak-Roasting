const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const chart = $('#chart');
const ctx = chart.getContext('2d');
let selectedLevel = 'light';
let lastAlertId = 0;
let latestState = null;
let audioContext = null;
let micContext = null;
let suppressMicUntil = 0;
let micPops = [];
let micBaseline = .008;
let selectedOutcome = '';
let lastInstructionId = '';
let commandPulseUntil = 0;
let commandPulseTimer = null;
let setupScope = '';
let setupDirty = false;
let editingQueueId = '';
let setupRevision = 0;
let lastAppendRevision = -1;
let queueMutationPending = false;
let previousFocusMode = false;
let dismissedColorRoastId = '';
const pendingActions = new Set();

function formatTime(seconds) {
  const value = Math.max(0, Math.round(seconds || 0));
  return `${String(Math.floor(value / 60)).padStart(2, '0')}:${String(value % 60).padStart(2, '0')}`;
}

function temp(value) { return value == null ? '—' : Math.round(value); }

function renderTimeline(timeline, dropTargetF = null, instruction = null) {
  const steps = Array.isArray(timeline?.steps) ? timeline.steps : [];
  const root = $('#timeline-steps');
  if (steps.length) {
    root.replaceChildren(...steps.map(step => {
      const item = document.createElement('div');
      const status = ['done', 'current', 'upcoming'].includes(step.status) ? step.status : 'upcoming';
      item.className = `timeline-step ${status}`;
      item.title = step.detail || step.label;
      if (status === 'current') item.setAttribute('aria-current', 'step');
      const dot = Object.assign(document.createElement('span'), {className: 'timeline-dot'});
      const label = document.createElement('span'); label.textContent = step.label;
      item.append(dot, label);
      return item;
    }));
  } else {
    const fallback = Object.assign(document.createElement('span'), {className: 'timeline-fallback', textContent: 'Timeline syncing…'});
    root.replaceChildren(fallback);
  }
  const summary = timeline?.next_summary;
  const parts = summary?.label ? [`Next: ${summary.label}`] : [];
  const signedDumpDelta = timeline?.next_step_id === 'dump' && instruction?.degrees_to_target_f != null ? Number(instruction.degrees_to_target_f) : NaN;
  if (Number.isFinite(signedDumpDelta)) parts.push(signedDumpDelta <= 0 ? `TARGET PASSED ${Math.abs(Math.round(signedDumpDelta))}°F` : `${Math.ceil(signedDumpDelta)}°F to go`);
  else if (summary?.remaining_f != null && Number.isFinite(Number(summary.remaining_f))) parts.push(`${Math.max(0, Math.ceil(summary.remaining_f))}°F to go`);
  if (summary?.remaining_seconds != null && Number.isFinite(Number(summary.remaining_seconds))) parts.push(`${formatTime(Math.max(0, summary.remaining_seconds))} to go`);
  if (timeline?.next_step_id === 'dump' && Number.isFinite(dropTargetF)) parts.push(`TARGET ${Math.round(dropTargetF)}°F`);
  $('#timeline-next').textContent = parts.join(' · ') || (steps.length ? 'Timeline complete' : 'Waiting for live milestones');
}

function isImmediateDanger(instruction) {
  const title = instruction.title.toUpperCase();
  const text = `${instruction.title} ${instruction.detail}`.toUpperCase();
  return title.includes('DUMP NOW') || /\b(?:HARD|ABSOLUTE)\s+(?:TEMPERATURE\s+)?(?:CEILING|LIMIT)\b/.test(text);
}

async function api(path, payload = {}) {
  const response = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Request failed');
  return result;
}

function currentMeta() {
  const desired = $('#desired-drop-f').value;
  return {
    bean: $('#bean').value.trim(), origin: $('#origin').value.trim(), level: selectedLevel,
    batch_g: $('#batch-g').value, desired_drop_f: selectedLevel === 'correction' || !desired ? null : Number(desired),
    teach_mode: selectedLevel !== 'correction' && $('#teach-mode').checked,
    roast_level_label: $('#roast-level-label').value.trim(),
    learned_profile_id: $('#learned-profile').value || null,
  };
}

function updateTeachUI() {
  const enabled = selectedLevel !== 'correction' && $('#teach-mode').checked;
  $('#teach-mode').disabled = selectedLevel === 'correction';
  $('#roast-level-label-field').hidden = !enabled;
  $('#roast-level-label').required = enabled;
}

function formatChargeGuidance(guidance = {}) {
  const sensor = String(guidance.sensor || '').toUpperCase();
  if (sensor === 'BT' && guidance.target_f != null && Number.isFinite(Number(guidance.target_f))) return `LOAD ${Math.round(Number(guidance.target_f))}°F BT`;
  if (sensor === 'ET' && guidance.low_f != null && guidance.high_f != null && Number.isFinite(Number(guidance.low_f)) && Number.isFinite(Number(guidance.high_f))) return `LOAD ${Math.round(Number(guidance.low_f))}–${Math.round(Number(guidance.high_f))}°F ET`;
  return 'LOAD —';
}

function roastDetails(meta = {}, profile = {}, chargeGuidance = {}) {
  const target = meta.desired_drop_f ?? profile.drop_target_f;
  const level = meta.level ? `${meta.level[0].toUpperCase()}${meta.level.slice(1)}` : 'Level —';
  const drop = target == null || !Number.isFinite(Number(target)) ? 'DROP —' : `DROP ${Math.round(Number(target))}°F BT`;
  return `${meta.batch_g == null ? 'Batch —' : `${meta.batch_g} g`} · ${level} · ${formatChargeGuidance(chargeGuidance)} · ${drop}`;
}

function roastName(meta = {}) {
  return [meta.origin, meta.bean].filter(Boolean).join(' · ') || 'Unnamed roast';
}

function renderTrain(state, focusMode) {
  const queue = Array.isArray(state.roast_queue) ? state.roast_queue : [];
  const train = $('#roast-train');
  const canRemoveNow = queue.length > 0 && !state.recording && !state.replaying && !state.auto_start_armed && !state.viewing_history && !state.last_saved && state.post_drop_seconds == null;
  $('#queue-remove-now').hidden = !canRemoveNow;
  $('#queue-remove-now').disabled = !canRemoveNow || queueMutationPending;
  if (focusMode && !previousFocusMode) train.open = false;
  $('#train-now').textContent = roastName(state.meta);
  $('#train-now-detail').textContent = roastDetails(state.meta, state.profile, state.charge_guidance);
  const head = queue[0];
  $('#train-next').textContent = head ? roastName(head.meta) : 'Nothing queued';
  $('#train-next-detail').textContent = head ? roastDetails(head.meta, head.profile, head.charge_guidance) : 'Add a roast to the queue';
  const later = Math.max(0, queue.length - 1);
  $('#train-later-count').textContent = later ? `+${later} later` : 'No later roasts';
  $('#train-list').replaceChildren(...(queue.length ? queue.map((entry, index) => {
    const item = document.createElement('li');
    item.dataset.queueId = entry.id;
    if (setupScope === 'edit' && entry.id === editingQueueId) item.classList.add('editing');
    const position = Object.assign(document.createElement('span'), {className:'train-position', textContent:index ? String(index + 1) : 'NEXT'});
    const copy = document.createElement('div');
    copy.append(Object.assign(document.createElement('b'), {textContent:roastName(entry.meta)}), Object.assign(document.createElement('small'), {textContent:roastDetails(entry.meta, entry.profile, entry.charge_guidance)}));
    const actions = Object.assign(document.createElement('div'), {className:'train-actions'});
    for (const [action, label, disabled] of [
      ['edit', index ? `Edit ${index + 1}` : 'Edit NEXT', false], ['up', 'Up', index === 0], ['down', 'Down', index === queue.length - 1], ['remove', 'Remove', false],
    ]) {
      const button = Object.assign(document.createElement('button'), {type:'button', className:'secondary', textContent:label, disabled:disabled || queueMutationPending});
      button.dataset.queueAction = action;
      button.setAttribute('aria-label', `${label} queued roast ${index + 1}: ${roastName(entry.meta)}`);
      actions.append(button);
    }
    item.append(position, copy, actions);
    return item;
  }) : [Object.assign(document.createElement('li'), {className:'train-empty', textContent:'No roasts queued. Use + Add roast to build the queue.'})]));
}

function renderSetup(state, focusMode, force = false) {
  const editor = $('#setup-editor');
  const queue = Array.isArray(state.roast_queue) ? state.roast_queue : [];
  let scope = setupScope || (focusMode ? 'append' : 'current');
  if (focusMode && scope === 'current') scope = 'append';
  let queueEntry = scope === 'edit' ? queue.find(entry => entry.id === editingQueueId) : null;
  if (scope === 'edit' && !queueEntry) { scope = 'append'; editingQueueId = ''; }
  const scopeChanged = setupScope !== scope;
  if (scopeChanged) {
    setupScope = scope; setupDirty = false;
    editor.open = scope === 'current';
  }
  const draft = queueEntry?.meta || state.meta;
  const active = state.meta || {};
  const activeTarget = active.desired_drop_f == null ? '' : ` · ${Math.round(active.desired_drop_f)}°F`;
  const appendLocked = scope === 'append' && lastAppendRevision === setupRevision;
  $('#setup-scope-label').textContent = scope === 'current' ? 'CURRENT ROAST' : scope === 'edit' ? 'EDIT QUEUED ROAST' : 'APPEND ROAST';
  $('#setup-meta-summary').textContent = scope !== 'current'
    ? `Active: ${active.bean || 'unnamed'} · ${active.level || 'light'}${activeTarget}`
    : `${active.bean || 'Unnamed bean'} · ${active.level || 'light'}${activeTarget}`;
  $('#setup-status').textContent = setupDirty ? 'Unsaved edits' : appendLocked ? 'Added · edit draft to add again' : scope === 'edit' ? `Editing position ${queue.indexOf(queueEntry) + 1}` : scope === 'append' ? 'Draft · not queued' : 'Ready to edit';
  $('#save-meta').textContent = scope === 'current' ? 'Save details' : scope === 'edit' ? 'Save queued roast' : 'Add to queue';
  $('#save-meta').disabled = queueMutationPending || appendLocked;
  $$('[data-setup-mode]').forEach(button => button.classList.toggle('active', button.dataset.setupMode === scope));
  $('#setup-current').disabled = focusMode;
  $('#setup-edit').disabled = !editingQueueId;
  const editing = editor.contains(document.activeElement) && document.activeElement.matches('input, select, button');
  if (!force && !scopeChanged && (setupDirty || editing || appendLocked)) return;
  const reusable = (state.learned_profiles || []).filter(item => item.status === 'reusable');
  const learned = $('#learned-profile');
  const signature = JSON.stringify(reusable.map(item => [item.profile_id, item.identity, item.learned_drop_f]));
  if (learned.dataset.signature !== signature) {
    const emptyLabel = reusable.length ? 'No learned profile' : 'No reusable learned profiles';
    learned.replaceChildren(Object.assign(document.createElement('option'), {value:'', textContent:emptyLabel}), ...reusable.map(item => {
      const identity = item.identity || {};
      const label = [identity.origin, identity.bean, identity.roast_level_label, item.learned_drop_f == null ? '' : `${Math.round(item.learned_drop_f)}°F`].filter(Boolean).join(' · ');
      return Object.assign(document.createElement('option'), {value:item.profile_id, textContent:label || item.profile_id});
    }));
    learned.dataset.signature = signature;
  }
  $('#learned-profile-field').hidden = scope === 'current';
  selectedLevel = draft.level || 'light';
  $('#bean').value = draft.bean || '';
  $('#origin').value = draft.origin || '';
  $('#batch-g').value = draft.batch_g ?? '';
  const profile = queueEntry?.profile || state.profile;
  $('#desired-drop-f').value = selectedLevel === 'correction' ? profile?.drop_target_f ?? '' : draft.desired_drop_f ?? profile?.drop_target_f ?? '';
  $('#desired-drop-f').dataset.level = selectedLevel;
  $('#learned-profile').value = draft.learned_profile_id || '';
  $('#teach-mode').checked = Boolean(draft.teach_mode);
  $('#roast-level-label').value = draft.roast_level_label || '';
  $$('[data-level]').forEach(button => button.classList.toggle('active', button.dataset.level === selectedLevel));
  $('#desired-drop-f').disabled = selectedLevel === 'correction';
  updateTeachUI();
}

function canonicalDumpTitle(instruction) {
  const target = instruction?.target_temp_f == null ? NaN : Number(instruction.target_temp_f);
  const delta = instruction?.degrees_to_target_f == null ? NaN : Number(instruction.degrees_to_target_f);
  if (!Number.isFinite(target) || !Number.isFinite(delta)) return instruction?.title || 'PREPARE TO DUMP';
  if (delta > 0) return `DUMP IN ${Math.ceil(delta)}°F · TARGET ${Math.round(target)}°F`;
  return `DUMP NOW · TARGET ${Math.round(target)}°F${delta < 0 ? ' · TARGET PASSED' : ''}`;
}

function renderFuelGuidance(guidance) {
  const node = $('#fuel-guidance');
  const range = Array.isArray(guidance?.range) ? (guidance.range[0] === guidance.range[1] ? `${guidance.range[0]}` : `${guidance.range[0]}–${guidance.range[1]}`) : '';
  const setting = guidance?.pilot_only ? 'PILOT ONLY' : guidance?.band ? `${guidance.label.toUpperCase()} · ${range}${guidance.preferred != null && guidance.range?.[0] !== guidance.range?.[1] ? ` · SET ${guidance.preferred}` : ''}` : 'INACTIVE';
  node.textContent = `FUEL ADVISORY · ${setting}`;
  node.title = `${guidance?.detail || ''} Advisory only; fuel is not sensed or applied.`.trim();
}

function showAllDatalistOptions(input) {
  input.addEventListener('pointerdown', event => {
    if (event.clientX < input.getBoundingClientRect().right - 48 || !input.value.trim()) return;
    input.dataset.valueBeforePicker = input.value;
    input.value = '';
  });
  input.addEventListener('input', () => {
    if (input.value.trim()) delete input.dataset.valueBeforePicker;
  });
  input.addEventListener('blur', () => {
    if (!input.value.trim() && input.dataset.valueBeforePicker) input.value = input.dataset.valueBeforePicker;
    delete input.dataset.valueBeforePicker;
  });
}

function historyLabel(item) {
  const date = String(item.saved_at || '').split('T')[0] || 'Unknown date';
  const name = [item.origin, item.bean].filter(Boolean).join(' · ') || item.id;
  const batch = item.batch_g ? `${item.batch_g} g` : 'batch —';
  const level = item.level ? `${item.level[0].toUpperCase()}${item.level.slice(1)}` : 'level —';
  return `${date} · ${name} · ${batch} · ${level}`;
}

function renderHistoryManager(state, disabled, viewingHistory) {
  const history = state.roast_history || [];
  const from = $('#history-from').value;
  const through = $('#history-through').value;
  const invalidRange = Boolean(from && through && from > through);
  const filtered = invalidRange ? [] : history.filter(item => {
    const date = String(item.saved_at || '').split('T')[0];
    return (!from || date >= from) && (!through || date <= through);
  });
  const select = $('#history-select');
  const selected = select.value;
  const signature = JSON.stringify(filtered.map(item => [item.id, historyLabel(item)]));
  if (select.dataset.signature !== signature) {
    select.replaceChildren(...(filtered.length ? filtered.map(item => Object.assign(document.createElement('option'), {value:item.id, textContent:historyLabel(item)})) : [Object.assign(document.createElement('option'), {value:'', textContent:'No matching roasts'})]));
    if (filtered.some(item => item.id === selected)) select.value = selected;
    select.dataset.signature = signature;
  }
  $('#history-error').textContent = invalidRange ? 'Through date must be on or after From.' : '';
  const savedIds = state.saved_reference_roast_ids || [];
  const names = savedIds.map(id => {
    const item = history.find(roast => roast.id === id);
    return item ? item.bean || item.origin || id : id;
  });
  $('#history-reference-status').textContent = savedIds.length === 2 ? `Average: ${names.join(' + ')}` : savedIds.length === 1 ? `Reference: ${names[0]}` : 'No saved reference';
  const unavailable = disabled || invalidRange || !select.value;
  $('#history-manager').classList.toggle('disabled', disabled);
  if (disabled) $('#history-manager').open = false;
  $('#history-from').disabled = disabled;
  $('#history-through').disabled = disabled;
  select.disabled = disabled;
  $('#history-open').disabled = unavailable;
  $('#history-replay').disabled = unavailable;
  $('#history-reference').disabled = unavailable || viewingHistory;
  $('#history-average').disabled = unavailable || viewingHistory || savedIds.length !== 1 || savedIds[0] === select.value;
}

function showToast(message) {
  const toast = $('#toast');
  toast.textContent = message;
  toast.classList.add('show');
  setTimeout(() => toast.classList.remove('show'), 2300);
}

function sound(kind) {
  try {
    audioContext ||= new (window.AudioContext || window.webkitAudioContext)();
    suppressMicUntil = Date.now() + 1400;
    const patterns = {
      stage: [[660, .09], [880, .11]],
      first_crack: [[620, .07], [820, .08], [1040, .12]],
      warning: [[520, .12], [420, .14]],
      dump_warning: [[880, .12], [880, .12], [1100, .25]],
    };
    let when = audioContext.currentTime;
    for (const [frequency, duration] of (patterns[kind] || patterns.stage)) {
      const oscillator = audioContext.createOscillator();
      const gain = audioContext.createGain();
      oscillator.frequency.value = frequency;
      oscillator.type = 'sine';
      gain.gain.setValueAtTime(.0001, when);
      gain.gain.exponentialRampToValueAtTime(.12, when + .015);
      gain.gain.exponentialRampToValueAtTime(.0001, when + duration);
      oscillator.connect(gain).connect(audioContext.destination);
      oscillator.start(when); oscillator.stop(when + duration + .02);
      when += duration + .06;
    }
  } catch (_) {}
}

function profileView(state) {
  const profile = state.recording || state.replaying || state.auto_start_armed || state.last_saved || state.post_drop_seconds != null ? state.profile : state.meta.level === selectedLevel ? state.profile : state.profiles[selectedLevel] || state.profile;
  const correction = profile.mode === 'correction';
  const preRoast = !state.recording && !state.replaying && !state.last_saved && state.post_drop_seconds == null;
  const armed = state.auto_start_armed && !state.recording && (state.connected || state.replaying);
  const referenceArmed = armed && state.replaying;
  const chargeTarget = formatChargeGuidance(state.charge_guidance).slice(5);
  $('#fc-target-label').textContent = correction ? 'Charge ET' : preRoast ? 'Charge baseline' : 'FC candidate';
  $('#drop-target-label').textContent = correction ? 'Window' : 'Drop / ceiling';
  $('#dev-target-label').textContent = correction ? 'Hard limit' : 'Development';
  $('#fc-target').textContent = correction || preRoast ? chargeTarget : `${Math.round(profile.fc_start_f)}°F`;
  $('#drop-target').textContent = correction ? `${Math.round(profile.drop_target_f)}–${Math.round(profile.drop_ceiling_f)}°F` : `${Math.round(profile.drop_target_f)} / ${Math.round(profile.drop_ceiling_f)}°F`;
  $('#dev-target').textContent = correction ? `${formatTime(profile.max_seconds).replace(/^0/, '')} / ${Math.round(profile.absolute_ceiling_f)}°F` : formatTime(profile.development_seconds);
  $('#chart-caption').textContent = referenceArmed ? 'Reference warm-up and charge drop are playing; time zero will backdate to the peak.' : armed ? 'Pre-charge curve is being captured; roast time starts at Charge.' : correction ? 'Experimental correction · sample from 3:00; manual Dump + save.' : 'Adaptive markers will backfill after recording begins.';
  $$('[data-event]').forEach(button => button.disabled = correction && ['FCs', 'FCe', 'SCs', 'SCe'].includes(button.dataset.event));
  if (correction) {
    $('#advisory-title').textContent = 'Experimental correction · manual dump';
    $('#advisory-body').textContent = 'Retain a control sample. Do not wait for FC. Sample from 3:00 every 20–30s; use manual Dump + save.';
    $('#fc-likelihood').textContent = 'Not used';
    $('#likelihood-explanation').textContent = 'Correction uses time, BT/RoR, color, aroma, and manual sampling.';
  } else if (state.drop_prediction) {
    const seconds = Math.ceil(state.drop_prediction.seconds_to_drop);
    $('#advisory-title').textContent = seconds <= 5 ? 'DUMP WINDOW' : `Estimated dump in ${formatTime(seconds)}`;
    $('#advisory-body').textContent = `Adaptive target: ${Math.round(state.drop_prediction.target_temp_f)}°F, using time since FCs and current RoR.`;
  } else if (state.events.some(event => event.name === 'DROP')) {
    $('#advisory-title').textContent = 'Roast complete';
    $('#advisory-body').textContent = 'Review the inferred events and correct any crack markers you heard differently.';
  } else {
    $('#advisory-title').textContent = `${profile.label} profile ready`;
    $('#advisory-body').textContent = profile.description;
  }
  if (!correction) {
    $('#fc-likelihood').textContent = `${Math.round((state.fc_likelihood?.probability || 0) * 100)}%`;
    $('#likelihood-explanation').textContent = state.fc_likelihood?.explanation || '';
  }
}

function renderEvents(events) {
  const names = {CHARGE: 'Charge', TP: 'Turning point', DRY_END: 'Dry end', FCs: 'FC start', FCe: 'FC end', SCs: 'SC start', SCe: 'SC end', DROP: 'Drop'};
  $('#event-rail').innerHTML = events.map(event => `<span class="event-chip ${event.confidence < .6 ? 'low' : ''}"><b>${names[event.name]}</b>${formatTime(event.elapsed)} · ${Math.round(event.bt)}° · ${Math.round(event.confidence * 100)}%</span>`).join('');
  $('#stages').innerHTML = Object.entries(names).map(([key, label]) => {
    const event = events.find(item => item.name === key);
    return `<div class="stage-row ${event ? 'done' : ''}"><span class="stage-dot"></span><div><b>${label}</b>${event ? `<em>${event.source}</em>` : ''}</div><small>${event ? formatTime(event.elapsed) : '—'}</small></div>`;
  }).join('');
  const last = events[events.length - 1];
  $('#stage-label').textContent = last ? names[last.name] : (latestState?.recording ? 'Pre-charge' : 'Ready');
}

function drawChart(points, events, reference) {
  const ratio = window.devicePixelRatio || 1;
  const width = chart.clientWidth, height = chart.clientHeight;
  if (chart.width !== width * ratio || chart.height !== height * ratio) { chart.width = width * ratio; chart.height = height * ratio; }
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  const pad = {left: 52, right: 50, top: 25, bottom: 38};
  const innerW = width - pad.left - pad.right, innerH = height - pad.top - pad.bottom;
  const maxT = Math.max(600, ...(points.map(p => p.elapsed)));
  const minY = 100, maxY = 500;
  const x = value => pad.left + value / maxT * innerW;
  const y = value => pad.top + (maxY - value) / (maxY - minY) * innerH;
  ctx.font = '11px -apple-system, sans-serif'; ctx.fillStyle = '#778796'; ctx.strokeStyle = '#31404c'; ctx.lineWidth = 1;
  for (let value = 100; value <= 500; value += 50) { ctx.beginPath(); ctx.moveTo(pad.left, y(value)); ctx.lineTo(width - pad.right, y(value)); ctx.stroke(); ctx.fillText(value, 13, y(value) + 4); }
  for (let value = 0; value <= maxT; value += 60) { ctx.beginPath(); ctx.moveTo(x(value), pad.top); ctx.lineTo(x(value), height - pad.bottom); ctx.stroke(); ctx.fillText(`${Math.round(value/60)}m`, x(value)-7, height-14); }
  function line(key, color, lineWidth) {
    if (!points.length) return;
    ctx.beginPath(); ctx.strokeStyle = color; ctx.lineWidth = lineWidth; ctx.lineJoin = 'round'; ctx.lineCap = 'round';
    points.forEach((point, index) => { const px = x(point.elapsed), py = y(point[key]); index ? ctx.lineTo(px, py) : ctx.moveTo(px, py); }); ctx.stroke();
  }
  line('et', '#ff5d73', 2.2); line('bt', '#32a9ed', 2.7);
  if (reference?.points?.length) {
    const ref = reference.points;
    ctx.beginPath(); ctx.strokeStyle = '#d2dae188'; ctx.lineWidth = 1.4; ctx.setLineDash([7, 6]);
    ref.forEach((point, index) => { const px=x(point.elapsed), py=y(point.bt); index ? ctx.lineTo(px,py) : ctx.moveTo(px,py); }); ctx.stroke(); ctx.setLineDash([]);
  }
  const colors = {CHARGE:'#e9c46a', FCs:'#62d28f', DROP:'#ff9f43'};
  for (const event of events) {
    const color = colors[event.name] || '#91a0ae';
    ctx.strokeStyle = color; ctx.globalAlpha = .65; ctx.setLineDash([4,4]); ctx.beginPath(); ctx.moveTo(x(event.elapsed), pad.top); ctx.lineTo(x(event.elapsed), height-pad.bottom); ctx.stroke(); ctx.setLineDash([]); ctx.globalAlpha = 1; ctx.fillStyle = color; ctx.fillText(event.name, x(event.elapsed)+4, pad.top+13);
  }
}

function handleAlerts(alerts) {
  for (const alert of alerts) {
    if (alert.id <= lastAlertId) continue;
    lastAlertId = alert.id;
  }
}

function render(state) {
  latestState = state;
  const completed = !state.recording && (state.last_saved || state.post_drop_seconds != null);
  const armed = state.auto_start_armed && !state.recording && (state.connected || state.replaying);
  const referenceArmed = armed && state.replaying;
  const physicalArmed = armed && !state.replaying && !completed;
  const focusMode = state.recording || state.replaying || completed || armed;
  renderSetup(state, focusMode);
  renderTrain(state, focusMode);
  previousFocusMode = focusMode;
  const dropInput = $('#desired-drop-f');
  const enteringFocusMode = focusMode && !document.body.classList.contains('focus-mode');
  document.body.classList.toggle('focus-mode', focusMode);
  if (enteringFocusMode) window.scrollTo(0, 0);
  const connection = $('#connection');
  connection.className = `connection ${state.connected ? 'online' : 'offline'}`;
  connection.querySelector('b').textContent = state.connection_message;
  $('#et-value').textContent = temp(state.temperatures.et);
  $('#bt-value').textContent = temp(state.temperatures.bt);
  $('#ror-value').textContent = state.points.length > 3 ? Math.round(state.ror) : '—';
  $('#timer-value').textContent = armed ? '00:00' : formatTime(state.elapsed);
  $('#start').disabled = state.recording || state.replaying || completed || state.auto_start_armed;
  const stop = $('#stop');
  stop.disabled = !state.recording && !state.replaying;
  stop.textContent = state.replaying ? '← Go back' : 'Dump + save';
  stop.classList.toggle('danger', !state.replaying);
  stop.classList.toggle('secondary', state.replaying);
  $('#replay').disabled = state.recording || state.replaying || completed;
  const canPromote = !state.recording && !state.replaying && !armed && Boolean(state.next_meta || completed);
  $('#new-roast').hidden = !canPromote;
  $('#new-roast').disabled = !canPromote;
  const historyDisabled = state.recording || state.replaying || armed;
  const viewingHistory = Boolean(state.viewing_history || (completed && state.last_saved && state.post_drop_seconds == null));
  dropInput.disabled = selectedLevel === 'correction';
  const effectiveDropTargetF = state.profile?.mode === 'correction' ? null : Number(state.profile?.drop_target_f);
  const canCorrectDrop = completed && !state.replaying && !armed && !viewingHistory && Boolean(state.last_saved) && state.profile?.mode !== 'correction' && Boolean(state.drop_result);
  const dropResult = state.drop_result;
  $('#drop-result').hidden = !canCorrectDrop;
  $('#missed-drop-target').hidden = !canCorrectDrop;
  if (!canCorrectDrop) $('#drop-correction').hidden = true;
  if (canCorrectDrop) {
    const recorded = dropResult.recorded_drop_f == null ? '—' : `${Math.round(dropResult.recorded_drop_f)}°F`;
    const target = dropResult.target_f == null ? '' : ` · target ${Math.round(dropResult.target_f)}°F`;
    $('#drop-result').textContent = dropResult.missed_target ? `Missed target · recorded ${recorded}${target}` : dropResult.within_1f ? `On target · ${recorded}${target}` : `Review needed · recorded ${recorded}${target}`;
  }
  const pendingColorId = state.teach_session?.pending_color_roast_id || '';
  $('#color-confirmation').hidden = !pendingColorId || pendingColorId === dismissedColorRoastId;
  $('#color-confirmation').dataset.roastId = pendingColorId;
  const beans = state.bean_suggestions || [];
  const beanList = $('#bean-suggestions');
  const beanSignature = JSON.stringify(beans);
  if (beanList.dataset.signature !== beanSignature) {
    beanList.replaceChildren(...beans.map(value => Object.assign(document.createElement('option'), {value})));
    beanList.dataset.signature = beanSignature;
  }
  const origins = state.origin_suggestions;
  const originList = $('#origin-suggestions');
  const originSignature = JSON.stringify(origins);
  if (originList.dataset.signature !== originSignature) {
    originList.replaceChildren(...origins.map(value => Object.assign(document.createElement('option'), {value})));
    originList.dataset.signature = originSignature;
  }
  renderHistoryManager(state, historyDisabled, viewingHistory);
  const batchGuidance = state.batch_guidance;
  const showBatchGuidance = batchGuidance.mode === 'experimental_underload';
  $('#batch-guidance').hidden = !showBatchGuidance;
  $('#batch-guidance-title').textContent = showBatchGuidance ? batchGuidance.title : '';
  $('#batch-guidance-message').textContent = showBatchGuidance ? batchGuidance.message : '';
  const instruction = state.next_instruction;
  renderTimeline(state.timeline, effectiveDropTargetF, instruction);
  const timeline = state.timeline;
  const summary = timeline?.next_summary;
  const label = summary?.label || '';
  const remainingF = summary?.remaining_f != null && Number.isFinite(Number(summary.remaining_f)) ? Math.max(0, Math.ceil(summary.remaining_f)) : null;
  const remainingSeconds = summary?.remaining_seconds != null && Number.isFinite(Number(summary.remaining_seconds)) ? Math.max(0, Math.ceil(summary.remaining_seconds)) : null;
  const instructionDelta = instruction?.degrees_to_target_f == null ? NaN : Number(instruction.degrees_to_target_f);
  const dumpTimeline = timeline?.next_step_id === 'dump' && Number.isFinite(instructionDelta);
  const finalTemperature = dumpTimeline ? instructionDelta <= 5 : remainingF != null && remainingF >= 1 && remainingF <= 5;
  const finalSeconds = remainingSeconds != null && remainingSeconds >= 1 && remainingSeconds <= 5;
  const finalFive = finalTemperature || finalSeconds;
  const showTemperature = remainingF != null && (finalTemperature || !finalSeconds);
  const timeValue = remainingSeconds == null ? '' : finalSeconds ? `${remainingSeconds} ${remainingSeconds === 1 ? 'SECOND' : 'SECONDS'}` : formatTime(remainingSeconds);
  let approachTitle = label ? `Next: ${label}` : 'Roast timeline syncing';
  let approachDetail = label ? 'Waiting for the live curve to predict this stage.' : 'Follow the live milestone strip above.';
  if (label && (remainingF != null || remainingSeconds != null)) {
    approachTitle = `${label.toUpperCase()} IN ${showTemperature ? `${remainingF}°F` : timeValue}`;
    if (remainingF != null && remainingSeconds != null) {
      approachDetail = showTemperature ? `Or ${timeValue} by time — whichever comes first.` : `Or ${remainingF}°F by bean temperature — whichever comes first.`;
    } else {
      approachDetail = remainingF != null ? 'Target is approaching by bean temperature.' : 'Target is approaching by time.';
    }
  }
  const targetSuffix = Number.isFinite(effectiveDropTargetF) ? ` · TARGET ${Math.round(effectiveDropTargetF)}°F` : '';
  if (dumpTimeline && instructionDelta <= 15) {
    approachTitle = canonicalDumpTitle(instruction);
    approachDetail = instruction.detail;
  } else if (timeline?.next_step_id === 'dump' && targetSuffix && !approachTitle.includes('TARGET')) approachTitle += targetSuffix;
  const banner = $('#action-banner');
  const bannerEyebrow = banner.firstElementChild.querySelector('span');
  bannerEyebrow.textContent = 'NEXT ACTION';
  const activeSession = state.recording || (state.replaying && !referenceArmed);
  const instructionChanged = lastInstructionId && lastInstructionId !== instruction.id;
  const dangerCommand = isImmediateDanger(instruction);
  const firstCrackPending = activeSession && state.profile?.mode !== 'correction' && timeline?.next_step_id === 'fcs';
  const dumpRemainingF = firstCrackPending && Number.isFinite(instructionDelta) ? Math.ceil(instructionDelta) : null;
  const dumpImminent = dumpRemainingF != null && dumpRemainingF <= 5;
  if (activeSession && instructionChanged && (dangerCommand || instruction.urgency !== 'prepare')) {
    commandPulseUntil = Date.now() + 8000;
    clearTimeout(commandPulseTimer);
    commandPulseTimer = setTimeout(() => latestState && render(latestState), 8000);
    showToast(instruction.title.includes('DUMP') ? canonicalDumpTitle(instruction) : instruction.title);
    sound(dangerCommand ? 'dump_warning' : 'stage');
  } else if (activeSession && instructionChanged) {
    commandPulseUntil = 0;
    clearTimeout(commandPulseTimer);
  } else if (!activeSession) {
    commandPulseUntil = 0;
  }
  const commandPulse = activeSession && Date.now() < commandPulseUntil;
  if (referenceArmed) {
    banner.className = 'action-banner card ready';
    $('#action-title').textContent = 'REFERENCE ARMED — TESTING CHARGE DROP';
    $('#action-detail').textContent = 'The stored warm-up and charge drop are being played; time zero will backdate to the BT peak.';
    $('#action-state').textContent = 'REFERENCE';
  } else if (physicalArmed) {
    const guidance = state.charge_guidance || {};
    banner.className = 'action-banner card ready';
    $('#action-title').textContent = formatChargeGuidance(guidance).replace(/^LOAD /, 'LOAD GREEN BEANS NOW · ');
    $('#action-detail').textContent = `Auto Charge detection is active. Manual fallback: Beans in now.${guidance.experimental_underload && !guidance.batch_adjustment_applied ? ' This underload value is an unvalidated starting reference; no batch adjustment was applied.' : ''}`;
    $('#action-state').textContent = 'ARMED';
  } else if (commandPulse) {
    const conciseAction = instruction.title.replace(/\bNOW\b/gi, '').split('·')[0].replace(/^NEXT:\s*/i, '').replace(/^MOVE AIR\s*→\s*50\s*\/\s*50/i, 'SWITCH TO 50/50').replace(/\s+/g, ' ').trim();
    banner.className = `action-banner card command ${dangerCommand ? 'danger' : 'now'}`;
    $('#action-title').textContent = instruction.id.startsWith('hopper_next:') ? instruction.title : instruction.title.includes('DUMP') ? canonicalDumpTitle(instruction) : `${conciseAction || instruction.title} NOW`;
    $('#action-detail').textContent = instruction.detail;
    $('#action-state').textContent = dangerCommand ? 'DO NOW' : 'NOW';
  } else if (dumpImminent) {
    banner.className = `action-banner card ${dumpRemainingF <= 0 ? 'command danger' : 'summary countdown'}`;
    $('#action-title').textContent = canonicalDumpTitle(instruction);
    $('#action-detail').textContent = 'First Crack is still unconfirmed. Next action: PILOT ONLY + DUMP.';
    $('#action-state').textContent = dumpRemainingF <= 0 ? 'DO NOW' : 'GET READY';
  } else if (firstCrackPending) {
    banner.className = 'action-banner card summary approach';
    bannerEyebrow.textContent = 'NEXT EVENT';
    $('#action-title').textContent = 'FIRST CRACK';
    $('#action-detail').textContent = `Next action: PILOT ONLY + DUMP${dumpRemainingF == null ? targetSuffix : ` · DROP IN ${dumpRemainingF}°F${targetSuffix}`}.`;
    $('#action-state').textContent = 'LISTEN';
  } else if (activeSession) {
    banner.className = `action-banner card summary${finalFive ? instructionDelta <= 0 ? ' danger' : ' countdown' : ' approach'}`;
    $('#action-title').textContent = approachTitle;
    $('#action-detail').textContent = approachDetail;
    $('#action-state').textContent = finalFive ? instructionDelta <= 0 ? 'DO NOW' : 'GET READY' : 'UP NEXT';
  } else if (!completed && !state.replaying && !viewingHistory) {
    const target = state.meta?.desired_drop_f ?? state.profile?.drop_target_f;
    const drop = target == null || !Number.isFinite(Number(target)) ? 'DROP —' : `DROP ${Math.round(Number(target))}°F BT`;
    banner.className = 'action-banner card ready';
    $('#action-title').textContent = `${formatChargeGuidance(state.charge_guidance).replace(/^LOAD /, 'PREHEAT TO ')} · THEN LOAD GREEN BEANS`;
    $('#action-detail').textContent = `${instruction.detail} Eventual roast target: ${drop}.`;
    $('#action-state').textContent = 'READY';
  } else {
    banner.className = 'action-banner card ready';
    $('#action-title').textContent = instruction.title;
    $('#action-detail').textContent = instruction.detail;
    $('#action-state').textContent = completed ? 'COMPLETE' : 'READY';
  }
  renderFuelGuidance(state.fuel_guidance);
  const actions = physicalArmed ? [{id: 'manual_charge', label: 'Beans in now', mark: 'CHARGE'}, {id: 'cancel_arm', label: 'Cancel arm', cancelArm: true}] : instruction.confirm_actions;
  const actionIds = new Set(actions.map(action => action.id));
  for (const action of pendingActions) if (!actionIds.has(action)) pendingActions.delete(action);
  $('#optional-actions').hidden = actions.length === 0;
  $('#optional-actions > span').textContent = physicalArmed ? 'MANUAL FALLBACK' : 'OPTIONAL ACTION LOG';
  $('#action-buttons').replaceChildren(...actions.map(action => {
    const button = document.createElement('button');
    button.className = action.mark ? 'primary' : 'secondary';
    button.dataset.action = action.id;
    if (action.mark) button.dataset.mark = action.mark;
    if (action.cancelArm) button.dataset.cancelArm = 'true';
    button.textContent = action.label;
    button.disabled = pendingActions.has(action.id);
    return button;
  }));
  lastInstructionId = instruction.id;
  const chartPoints = armed ? state.precharge_points || [] : state.points;
  profileView(state); renderEvents(state.events);
  if (armed) $('#stage-label').textContent = `Capturing ${formatTime(state.capture_elapsed)}`;
  drawChart(chartPoints, state.events, state.reference_overlay); handleAlerts(state.alerts);
  const select = $('#reference-select');
  const signature = state.references.map(item => item.id).join('|');
  if (select.dataset.signature !== signature) {
    select.innerHTML = state.references.map(item => `<option value="${item.id}">${item.name} · ${item.machine}</option>`).join('');
    select.dataset.signature = signature;
  }
  select.value = state.active_reference_id || '';
  const airflow = state.airflow;
  const airflowLabels = {cooling:'Cooling bin', half:'50 / 50', drum:'Roast drum', unknown:'Not logged'};
  $('#airflow-title').textContent = `${airflowLabels[airflow.recommended.position]} recommended`;
  $('#airflow-reason').textContent = airflow.recommended.reason;
  $('#airflow-current').textContent = `Optional airflow log: ${airflowLabels[airflow.current]}`;
  $('#airflow-card').classList.toggle('attention', airflow.current !== airflow.recommended.position);
  $$('[data-airflow]').forEach(button => {
    button.classList.toggle('current', button.dataset.airflow === airflow.current);
    button.classList.toggle('recommended', button.dataset.airflow === airflow.recommended.position);
  });
}

async function poll() {
  try { render(await (await fetch('/api/state', {cache: 'no-store'})).json()); }
  catch (_) { const node = $('#connection'); node.className = 'connection offline'; node.querySelector('b').textContent = 'Peak Roasting server stopped'; }
}

function markSetupDirty() {
  setupDirty = true;
  setupRevision += 1;
  $('#setup-status').textContent = 'Unsaved edits';
  $('#save-meta').disabled = queueMutationPending;
}

$$('[data-level]').forEach(button => button.addEventListener('click', () => {
  selectedLevel = button.dataset.level; $$('[data-level]').forEach(item => item.classList.toggle('active', item === button));
  if (latestState) {
    const profile = latestState.profiles[selectedLevel];
    $('#desired-drop-f').value = profile.drop_target_f;
    $('#desired-drop-f').dataset.level = selectedLevel;
    $('#desired-drop-f').disabled = selectedLevel === 'correction';
    if (selectedLevel === 'correction') $('#teach-mode').checked = false;
    markSetupDirty(); updateTeachUI();
    profileView(latestState);
  }
}));
showAllDatalistOptions($('#origin'));
showAllDatalistOptions($('#bean'));
$$('#setup-editor input').forEach(input => input.addEventListener('input', markSetupDirty));
$('#teach-mode').addEventListener('change', updateTeachUI);
$('#batch-full').addEventListener('click', () => { $('#batch-g').value = '5000'; markSetupDirty(); });
$$('[data-setup-mode]').forEach(button => button.addEventListener('click', () => {
  if (!latestState || button.disabled || (button.dataset.setupMode === 'edit' && !editingQueueId)) return;
  setupScope = button.dataset.setupMode;
  setupDirty = false;
  $('#setup-editor').open = true;
  renderSetup(latestState, document.body.classList.contains('focus-mode'), true);
}));
$('#queue-add').addEventListener('click', event => {
  event.preventDefault(); event.stopPropagation();
  if (!latestState || queueMutationPending) return;
  setupScope = 'append'; editingQueueId = ''; setupDirty = false; setupRevision += 1;
  $('#setup-editor').open = true;
  renderSetup(latestState, document.body.classList.contains('focus-mode'), true);
  $('#setup-editor').scrollIntoView({block:'nearest'}); $('#bean').focus();
});
$('#queue-remove-now').addEventListener('click', event => {
  event.preventDefault(); event.stopPropagation();
  if (!event.currentTarget.disabled) $('#new-roast').click();
});
$('#learned-profile').addEventListener('change', async event => {
  if (!event.target.value) { markSetupDirty(); return; }
  try {
    const state = await api('/api/learned-profile/select', {profile_id:event.target.value});
    const head = Array.isArray(state.roast_queue) ? state.roast_queue[0] : null;
    editingQueueId = head?.id || '';
    setupScope = head ? 'edit' : 'append';
    document.activeElement?.blur(); setupDirty = false; setupRevision += 1; render(state); showToast('Learned profile queued at NEXT');
  }
  catch (error) { showToast(error.message); }
});
$('#save-meta').addEventListener('click', async () => {
  if (!$('#roast-level-label').reportValidity() || queueMutationPending) return;
  try {
    const mode = setupScope;
    const queueId = editingQueueId;
    const meta = currentMeta();
    if (mode === 'append' && lastAppendRevision === setupRevision) return;
    queueMutationPending = true; $('#save-meta').disabled = true;
    const state = mode === 'current'
      ? await api('/api/meta', meta)
      : mode === 'edit'
        ? await api('/api/queue/update', {id:queueId, meta})
        : await api('/api/queue/add', meta);
    if (mode === 'append') lastAppendRevision = setupRevision;
    document.activeElement?.blur(); setupDirty = false; queueMutationPending = false; render(state);
    showToast(mode === 'current' ? 'Bean details saved' : mode === 'edit' ? 'Queued roast updated' : 'Roast added to queue');
  } catch (error) { queueMutationPending = false; setupDirty = true; $('#save-meta').disabled = false; $('#setup-status').textContent = 'Unsaved edits'; showToast(error.message); }
});
$('#train-list').addEventListener('click', async event => {
  const button = event.target.closest('[data-queue-action]');
  const item = button?.closest('[data-queue-id]');
  if (!button || !item || button.disabled || queueMutationPending) return;
  const id = item.dataset.queueId;
  const action = button.dataset.queueAction;
  if (action === 'edit') {
    editingQueueId = id; setupScope = 'edit'; setupDirty = false;
    $('#setup-editor').open = true;
    renderSetup(latestState, document.body.classList.contains('focus-mode'), true);
    renderTrain(latestState, document.body.classList.contains('focus-mode'));
    $('#setup-editor').scrollIntoView({block:'nearest'});
    return;
  }
  queueMutationPending = true; renderTrain(latestState, document.body.classList.contains('focus-mode'));
  try {
    const state = action === 'remove'
      ? await api('/api/queue/remove', {id})
      : await api('/api/queue/move', {id, direction:action === 'up' ? -1 : 1});
    if (action === 'remove' && editingQueueId === id) { editingQueueId = ''; setupScope = 'append'; setupDirty = false; }
    queueMutationPending = false; render(state);
    showToast(action === 'remove' ? 'Queued roast removed' : `Queued roast moved ${action}`);
  } catch (error) { queueMutationPending = false; renderTrain(latestState, document.body.classList.contains('focus-mode')); showToast(error.message); }
});
$('#missed-drop-target').addEventListener('click', () => { $('#drop-correction').hidden = false; $('#actual-drop-f').focus(); });
$('#save-drop-correction').addEventListener('click', async () => {
  const value = $('#actual-drop-f').value.trim();
  const payload = {missed: true};
  if (value) payload.actual_drop_f = Number(value);
  try {
    render(await api('/api/drop-result', payload));
    $('#drop-correction').hidden = true; $('#actual-drop-f').value = '';
    showToast(value ? `Drop corrected to ${Math.round(Number(value))}°F` : 'Missed drop target saved for review');
  } catch (error) { showToast(error.message); }
});
$('#new-roast').addEventListener('click', async () => { try { const state = await api('/api/new-roast'); $('#saved-message').textContent = ''; lastAlertId = 0; lastInstructionId = ''; commandPulseUntil = 0; clearTimeout(commandPulseTimer); pendingActions.clear(); setupScope = 'current'; editingQueueId = ''; setupDirty = false; render(state); window.scrollTo(0, 0); showToast('Ready for next roast'); } catch (error) { showToast(error.message); } });
$('#start').addEventListener('click', async () => { try { audioContext ||= new (window.AudioContext || window.webkitAudioContext)(); render(await api('/api/start', currentMeta())); lastAlertId = 0; showToast('Auto-start armed — load beans when ready'); } catch (error) { showToast(error.message); } });
$('#stop').addEventListener('click', async () => {
  try {
    if (latestState?.replaying) {
      const state = await api('/api/replay/cancel');
      $('#saved-message').textContent = '';
      lastAlertId = 0; lastInstructionId = ''; commandPulseUntil = 0;
      clearTimeout(commandPulseTimer); pendingActions.clear(); render(state); window.scrollTo(0, 0);
      showToast('Replay cancelled');
      return;
    }
    const result = await api('/api/stop'); $('#saved-message').textContent = `Saved ${result.roast_id}`; showToast('Roast saved');
  } catch (error) { showToast(error.message); }
});
$('#replay').addEventListener('click', async () => { try { audioContext ||= new (window.AudioContext || window.webkitAudioContext)(); await api('/api/replay', {...currentMeta(), speed: 5}); lastAlertId = 0; showToast('Replaying the confirmed roast at 5×'); } catch (error) { showToast(error.message); } });
$('#history-open').addEventListener('click', async () => {
  const roastId = $('#history-select').value;
  if (!roastId) return showToast('Choose a saved roast');
  try {
    const state = await api('/api/history/view', {id: roastId});
    lastAlertId = 0; lastInstructionId = ''; commandPulseUntil = 0;
    clearTimeout(commandPulseTimer); pendingActions.clear(); render(state); window.scrollTo(0, 0);
    showToast(`Opened ${state.meta?.bean || roastId}`); $('#history-manager').open = false;
  } catch (error) { showToast(error.message); }
});
$('#history-replay').addEventListener('click', async () => {
  const roastId = $('#history-select').value;
  if (!roastId) return showToast('Choose a saved roast');
  try { audioContext ||= new (window.AudioContext || window.webkitAudioContext)(); render(await api('/api/replay', {roast_id:roastId, speed:5})); lastAlertId = 0; $('#history-manager').open = false; showToast('Replaying selected roast at 5×'); }
  catch (error) { showToast(error.message); }
});
$('#history-reference').addEventListener('click', async () => {
  const roastId = $('#history-select').value;
  if (!roastId) return showToast('Choose a saved roast');
  try { render(await api('/api/reference/saved', {roast_ids:[roastId]})); showToast('Saved roast reference active'); }
  catch (error) { showToast(error.message); }
});
$('#history-average').addEventListener('click', async () => {
  const roastId = $('#history-select').value;
  const current = latestState?.saved_reference_roast_ids || [];
  if (!roastId || current.length !== 1 || current[0] === roastId) return showToast('Choose a different second roast');
  try { render(await api('/api/reference/saved', {roast_ids:[current[0], roastId]})); showToast('Averaged reference active'); }
  catch (error) { showToast(error.message); }
});
$('#history-from').addEventListener('input', () => latestState && render(latestState));
$('#history-through').addEventListener('input', () => latestState && render(latestState));
$('#history-select').addEventListener('change', () => latestState && render(latestState));
$('#action-buttons').addEventListener('click', async event => {
  const button = event.target.closest('[data-action]');
  if (!button) return;
  const action = button.dataset.action;
  const mark = button.dataset.mark;
  const cancelArm = button.dataset.cancelArm === 'true';
  if (!action || button.disabled) return;
  const label = button.textContent;
  pendingActions.add(action);
  button.disabled = true;
  try {
    if (cancelArm) { const state = await api('/api/arm/cancel'); pendingActions.clear(); render(state); showToast('Arm cancelled'); }
    else if (mark) { await api('/api/mark', {name: mark}); showToast('Charge marked'); }
    else { await api('/api/action', {action}); showToast(`${label.replace(/^Log /, '')} logged`); }
  }
  catch (error) { pendingActions.delete(action); button.disabled = false; showToast(error.message); }
});
$$('[data-event]').forEach(button => button.addEventListener('click', async () => { try { await api('/api/mark', {name: button.dataset.event}); showToast(`${button.textContent} marked`); } catch (error) { showToast(error.message); } }));
$('#undo-mark').addEventListener('click', async () => { try { const result=await api('/api/undo'); showToast(`${result.name} mark undone`); } catch(error) { showToast(error.message); } });
$('#reference-select').addEventListener('change', async event => { try { render(await api('/api/reference/active', {id:event.target.value})); } catch (error) { showToast(error.message); } });
$('#import-reference').addEventListener('click', async () => {
  const file = $('#reference-file').files[0];
  if (!file) return showToast('Choose an Artisan .alog first');
  try {
    const result = await api('/api/reference/import', {filename:file.name, content:await file.text(), machine:$('#reference-machine').value.trim()});
    render(result.state); showToast(`Imported ${result.reference.name}`);
  } catch (error) { showToast(error.message); }
});
$('#enable-mic').addEventListener('click', async () => {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:false, noiseSuppression:false, autoGainControl:false}});
    micContext = new (window.AudioContext || window.webkitAudioContext)();
    const source = micContext.createMediaStreamSource(stream);
    const analyser = micContext.createAnalyser(); analyser.fftSize = 2048; analyser.smoothingTimeConstant = .15; source.connect(analyser);
    const waveform = new Float32Array(analyser.fftSize); const spectrum = new Uint8Array(analyser.frequencyBinCount);
    $('#mic-title').textContent = 'Listening for crack clusters'; $('#mic-copy').textContent = 'Curve gating is active; isolated roaster noises will not create an event.'; $('#enable-mic').disabled = true;
    let lastPop = 0, lastReport = 0;
    function sampleMic() {
      analyser.getFloatTimeDomainData(waveform); analyser.getByteFrequencyData(spectrum);
      let sum = 0, peak = 0; for (const value of waveform) { sum += value*value; peak = Math.max(peak, Math.abs(value)); }
      const rms = Math.sqrt(sum/waveform.length); let high=0,total=0; for(let i=0;i<spectrum.length;i++){total+=spectrum[i]; if(i>spectrum.length*.18) high+=spectrum[i];}
      const sharpness = total ? high/total : 0; const now=Date.now();
      if (now > suppressMicUntil) micBaseline = micBaseline*.995 + Math.min(rms,micBaseline*2)*.005;
      const isPop = now>suppressMicUntil && now-lastPop>90 && peak>.08 && rms>Math.max(.018,micBaseline*2.8) && sharpness>.32;
      if (isPop) { micPops.push(now); lastPop=now; }
      micPops = micPops.filter(value => now-value<=8000); $('#mic-level').style.width = `${Math.min(100,rms/.12*100)}%`;
      if (now-lastReport>1000 && latestState?.recording) {
        lastReport=now; api('/api/audio/features',{elapsed:latestState.elapsed,rms,baseline:micBaseline,pops_8s:micPops.length,sharpness}).catch(()=>{});
      }
      requestAnimationFrame(sampleMic);
    }
    sampleMic();
  } catch (error) { showToast(`Microphone unavailable: ${error.message}`); }
});
$$('[data-outcome]').forEach(button => button.addEventListener('click', () => {
  selectedOutcome=button.dataset.outcome; $$('[data-outcome]').forEach(item=>item.classList.toggle('active',item===button));
}));
$('#save-feedback').addEventListener('click', async () => {
  try { await api('/api/feedback',{outcome:selectedOutcome,notes:$('#feedback-notes').value}); showToast('Cup feedback saved for calibration'); }
  catch(error) { showToast(error.message); }
});
$$('[data-color-state]').forEach(button => button.addEventListener('click', async () => {
  const roastId = $('#color-confirmation').dataset.roastId;
  try {
    const state = await api('/api/learning/color-confirmation', {roast_id:roastId, state:button.dataset.colorState, notes:$('#color-notes').value});
    $('#color-notes').value = ''; render(state); showToast(button.dataset.colorState === 'hit' ? 'Color confirmed · profile reusable' : 'Color miss recorded');
  } catch (error) { showToast(error.message); }
}));
$('#color-later').addEventListener('click', () => { dismissedColorRoastId = $('#color-confirmation').dataset.roastId; $('#color-confirmation').hidden = true; showToast('Color check saved for later'); });
$$('[data-airflow]').forEach(button => button.addEventListener('click', async () => {
  try { await api('/api/airflow',{position:button.dataset.airflow}); showToast(`Optional airflow log saved: ${button.textContent}`); }
  catch(error) { showToast(error.message); }
}));
window.addEventListener('resize', () => latestState && drawChart(latestState.auto_start_armed && !latestState.recording && (latestState.connected || latestState.replaying) ? latestState.precharge_points || [] : latestState.points, latestState.events, latestState.reference_overlay));
poll(); setInterval(poll, 700);
