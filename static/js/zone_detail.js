let zoneCache = null;
let zoneId = null;
let mlRecommendedLiters = 0;
let schedCheckInterval = null;
let lastScheduledRun = null;

const SCHEDULE_KEY = () => `irrigation_schedule_${zoneId}`;
const MODE_KEY = () => `irrigation_mode_${zoneId}`;
const MODE_LABELS = { ml: 'ML Prediction', scheduled: 'Scheduled', manual: 'Manual' };
function getActiveMode() { return localStorage.getItem(MODE_KEY()) || 'ml'; }

async function saveModeToDB(mode) {
    try {
        await fetch(`/api/zone/${zoneId}/mode`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode }),
        });
    } catch (e) { /* non-blocking */ }
}

function getSchedule() {
    try { return JSON.parse(localStorage.getItem(SCHEDULE_KEY())); } catch { return null; }
}

function saveSchedule(schedule) {
    if (schedule) {
        localStorage.setItem(SCHEDULE_KEY(), JSON.stringify(schedule));
    } else {
        localStorage.removeItem(SCHEDULE_KEY());
    }
}

function addSchedSlot(time = '06:00', liters = 2) {
    const container = document.getElementById('schedSlots');
    if (!container) return;
    const row = document.createElement('div');
    row.className = 'sched-slot';
    row.innerHTML = `<input type="time" class="slot-time" value="${time}" /><input type="number" class="slot-liters" min="0.1" step="0.1" value="${liters}" /><span class="slot-unit">L</span><button class="btn-rm-slot" type="button" title="Remove">×</button>`;
    row.querySelector('.btn-rm-slot').addEventListener('click', () => {
        if (container.children.length > 1) row.remove();
    });
    container.appendChild(row);
}

function nextScheduleRun(schedule) {
    if (!schedule || !schedule.days || !schedule.days.length || !schedule.slots || !schedule.slots.length) return null;
    const now = new Date();
    // Floor to minute so a slot set for "now" still shows today, not next week
    const nowFloor = new Date(now.getFullYear(), now.getMonth(), now.getDate(), now.getHours(), now.getMinutes(), 0);
    let earliest = null;
    for (const slot of schedule.slots) {
        if (!slot.time) continue;
        const [hh, mm] = slot.time.split(':').map(Number);
        for (let i = 0; i < 8; i++) {
            const candidate = new Date(now.getFullYear(), now.getMonth(), now.getDate() + i, hh, mm, 0);
            if (candidate >= nowFloor && schedule.days.includes(candidate.getDay())) {
                if (!earliest || candidate < earliest) earliest = candidate;
                break;
            }
        }
    }
    return earliest;
}

function updateScheduleStatus() {
    const msg = document.getElementById('schedMsg');
    if (!msg) return;
    const schedule = getSchedule();
    if (!schedule || !schedule.slots || !schedule.slots.length) { msg.textContent = 'No schedule active.'; return; }
    const next = nextScheduleRun(schedule);
    const dayNames = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
    const daysStr = schedule.days.map((d) => dayNames[d]).join(', ');
    const slotsStr = schedule.slots.map((s) => `${s.time} (${s.liters} L)`).join(', ');
    if (next) {
        msg.textContent = `Active on ${daysStr}: ${slotsStr}. Next run: ${next.toLocaleDateString()} ${next.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}.`;
    } else {
        msg.textContent = `Active on ${daysStr}: ${slotsStr}. No upcoming run found.`;
    }
}

function restoreScheduleUI() {
    const schedule = getSchedule();
    const container = document.getElementById('schedSlots');
    if (container) container.innerHTML = '';
    if (!schedule) { addSchedSlot(); updateScheduleStatus(); return; }
    document.querySelectorAll('.dow-check').forEach((cb) => {
        cb.checked = schedule.days.includes(Number(cb.value));
    });
    // Support old single-slot format for backward compat
    const slots = schedule.slots || (schedule.time ? [{ time: schedule.time, liters: schedule.liters }] : []);
    if (slots.length) {
        slots.forEach((s) => addSchedSlot(s.time, s.liters));
    } else {
        addSchedSlot();
    }
    updateScheduleStatus();
}

function startScheduleChecker() {
    if (schedCheckInterval) return;
    schedCheckInterval = setInterval(async () => {
        const schedule = getSchedule();
        if (!schedule || !schedule.slots) return;
        const now = new Date();
        if (!schedule.days.includes(now.getDay())) return;
        const flowRate = zoneCache ? (zoneCache.zone.flow_rate_lpm || 3.0) : 3.0;
        for (const slot of schedule.slots) {
            if (!slot.time) continue;
            const [hh, mm] = slot.time.split(':').map(Number);
            if (now.getHours() !== hh || now.getMinutes() !== mm) continue;
            const runKey = `${now.getFullYear()}-${now.getMonth()}-${now.getDate()}-${hh}-${mm}`;
            if (lastScheduledRun === runKey) continue;
            lastScheduledRun = runKey;
            try {
                await fetch('/api/irrigation/queue', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ zone_id: zoneId, volume_liters: slot.liters }),
                });
            } catch (e) { console.error('Scheduled irrigation failed:', e); }
        }
        updateScheduleStatus();
    }, 60000);
}

function renderHistoryRows(history) {
    const tbody = document.querySelector('#zoneHistoryTable tbody');
    tbody.innerHTML = '';
    if (!history.length) {
        tbody.innerHTML = '<tr><td colspan="2">No history available.</td></tr>';
        return;
    }

    history.forEach((row) => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td>${row.timestamp || '--'}</td>
            <td>${row.moisture ?? '--'}</td>
        `;
        tbody.appendChild(tr);
    });
}

function renderEventRows(events) {
    const tbody = document.querySelector('#zoneEventTable tbody');
    tbody.innerHTML = '';
    if (!events.length) {
        tbody.innerHTML = '<tr><td colspan="4">No control events yet.</td></tr>';
        return;
    }

    events.forEach((row) => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td>${row.timestamp || '--'}</td>
            <td>${row.event_type || '--'}</td>
            <td>${row.source || '--'}</td>
            <td>${row.detail || '--'}</td>
        `;
        tbody.appendChild(tr);
    });
}

function bindZoneSettings(zone, cropTargets, soilBaselines) {
    const baselineSelect = document.getElementById('zoneSoilBaselineSelect');
    baselineSelect.innerHTML = ['<option value="">-- Unassigned --</option>']
        .concat((soilBaselines || []).map((item) => `<option value="${item.id}" ${zone.soil_baseline_id === item.id ? 'selected' : ''}>${item.name}</option>`))
        .join('');

    const cropTargetSelect = document.getElementById('zoneCropTargetSelect');
    cropTargetSelect.innerHTML = ['<option value="">-- Unassigned --</option>']
        .concat(cropTargets.map((item) => `<option value="${item.id}" ${zone.crop_target_id === item.id ? 'selected' : ''}>${item.name}</option>`))
        .join('');

    const thresholdInput = document.getElementById('zoneThresholdGap');
    if (thresholdInput) {
        thresholdInput.value = zone.threshold_gap !== null && zone.threshold_gap !== undefined
            ? Number(zone.threshold_gap).toFixed(1)
            : '5.0';
    }
}

function renderZoneHeader(zone) {
    const moisture = zone.moisture;
    const isOn = zone.valve_status === 'ON';

    document.getElementById('zoneMoisture').textContent =
        moisture === null || moisture === undefined ? '-- %' : `${Number(moisture).toFixed(1)} %`;
    document.getElementById('zoneTarget').textContent =
        zone.target_moisture === null || zone.target_moisture === undefined ? '-- %' : `${Number(zone.target_moisture).toFixed(1)} %`;
    document.getElementById('zoneValveStatus').textContent = isOn ? 'ON' : 'OFF';
    document.getElementById('zoneFlowRate').textContent =
        zone.flow_rate_lpm ? `${Number(zone.flow_rate_lpm).toFixed(1)} L/min` : '-- L/min';
    const threshEl = document.getElementById('zoneTriggerThreshold');
    if (threshEl) threshEl.textContent =
        zone.threshold_gap != null ? `${Number(zone.threshold_gap).toFixed(1)} %` : '-- %';
    document.getElementById('zoneGaugeFill').style.width = `${Math.max(0, Math.min(100, moisture || 0))}%`;
    document.getElementById('zoneGaugeFill').style.background = window.AppCommon.moistureColor(moisture);

    updateManualDurationPreview();
}

async function loadZoneData() {
    const dashboard = await window.AppCommon.fetchDashboard();
    const zone = dashboard.zones.find((z) => z.zone_id === zoneId);
    if (!zone) {
        throw new Error(`Zone ${zoneId} not found`);
    }

    zoneCache = {
        zone,
        cropTargets: dashboard.crop_targets || [],
        soilBaselines: dashboard.soil_baselines || [],
    };

    renderZoneHeader(zone);
    bindZoneSettings(zone, zoneCache.cropTargets, zoneCache.soilBaselines);

    const res = await fetch(`/api/zone/${zoneId}/history?limit=30`);
    const payload = await res.json();
    renderHistoryRows(payload.history || []);
    renderEventRows(payload.control_history || []);
}

async function saveZoneProfile() {
    if (!zoneCache) return;

    const cropTargetRaw = document.getElementById('zoneCropTargetSelect').value;
    const baselineRaw   = document.getElementById('zoneSoilBaselineSelect').value;
    const thresholdRaw  = document.getElementById('zoneThresholdGap')?.value;

    const payload = {
        crop_target_id:   cropTargetRaw === '' ? null : Number(cropTargetRaw),
        soil_baseline_id: baselineRaw   === '' ? null : Number(baselineRaw),
        threshold_gap:    thresholdRaw !== undefined && thresholdRaw !== '' ? Number(thresholdRaw) : 5.0,
    };

    const msg = document.getElementById('zoneSaveMsg');
    try {
        const res = await fetch(`/api/zone/${zoneId}/mapping`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });

        if (!res.ok) {
            let errText = 'Failed to save zone assignment.';
            try { errText = (await res.json()).error || errText; } catch (_) {}
            msg.textContent = errText;
            return;
        }

        msg.textContent = 'Zone assignment saved.';
        await loadZoneData();
    } catch (e) {
        msg.textContent = 'Save failed: ' + e.message;
    }
}

async function deleteSelectedBaseline() {}

// ── Irrigation modes ───────────────────────────────────────────────────────

function switchIrrigationMode(mode, persistToServer = false) {
    localStorage.setItem(MODE_KEY(), mode);
    if (persistToServer) saveModeToDB(mode);
    document.querySelectorAll('.irr-tab').forEach((tab) => {
        tab.classList.toggle('active', tab.dataset.mode === mode);
    });
    document.querySelectorAll('.irr-panel').forEach((panel) => {
        panel.style.display = 'none';
    });
    const panelMap = { ml: 'irrPanelMl', scheduled: 'irrPanelScheduled', manual: 'irrPanelManual' };
    const target = document.getElementById(panelMap[mode]);
    if (target) target.style.display = 'block';
    const modeEl = document.getElementById('zoneIrrMode');
    if (modeEl) modeEl.textContent = MODE_LABELS[mode] || mode;
}

async function runMLPrediction() {
    const btn = document.getElementById('mlPredictBtn');
    const msg = document.getElementById('mlMsg');
    btn.disabled = true;
    btn.textContent = 'Calculating…';
    msg.textContent = '';

    try {
        const res = await fetch(`/api/zone/${zoneId}/predict`);
        if (!res.ok) {
            const err = await res.json();
            msg.textContent = err.error || 'Prediction failed.';
            return;
        }
        const data = await res.json();
        mlRecommendedLiters = data.recommended_liters;

        document.getElementById('mlDeficit').textContent = `${data.deficit_pct} %`;
        document.getElementById('mlLiters').textContent = `${data.recommended_liters} L`;
        document.getElementById('mlMinutes').textContent = `${data.estimated_minutes} min`;
        document.getElementById('mlPredictResult').style.display = 'block';

        const modelBadge = data.model_used === 'ml' ? ' (RF model)' : ' (formula estimate — train model first)';
        const execBtn = document.getElementById('mlExecuteBtn');
        if (data.recommended_liters <= 0) {
            msg.textContent = 'Soil moisture is already at or above target. No irrigation needed.';
            execBtn.disabled = true;
        } else {
            msg.textContent = `Recommendation ready${modelBadge}.`;
            execBtn.disabled = false;
        }
    } catch (e) {
        msg.textContent = 'Error fetching prediction.';
    } finally {
        btn.disabled = false;
        btn.textContent = 'Calculate Recommendation';
    }
}

async function executeMLIrrigation() {
    if (!zoneCache || mlRecommendedLiters <= 0) return;
    const flowRate = zoneCache.zone.flow_rate_lpm || 3.0;
    const msg = document.getElementById('mlMsg');

    try {
        const res = await fetch('/api/irrigation/queue', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ zone_id: zoneId, volume_liters: mlRecommendedLiters, source: 'ml' }),
        });
        if (!res.ok) {
            const err = await res.json();
            msg.textContent = err.error || 'Failed to queue irrigation.';
            return;
        }
        msg.textContent = `Queued: ${mlRecommendedLiters} L (~${(mlRecommendedLiters / flowRate).toFixed(1)} min). Check Testing page for status.`;
    } catch (e) {
        msg.textContent = 'Failed to start irrigation.';
    }
}

async function scheduleIrrigation() {
    const msg = document.getElementById('schedMsg');
    const days = Array.from(document.querySelectorAll('.dow-check:checked')).map((cb) => Number(cb.value));
    if (!days.length) { msg.textContent = 'Select at least one day.'; return; }
    const slotRows = document.querySelectorAll('#schedSlots .sched-slot');
    const slots = [];
    for (const row of slotRows) {
        const time = row.querySelector('.slot-time').value;
        const liters = parseFloat(row.querySelector('.slot-liters').value);
        if (!time || !liters || liters <= 0) { msg.textContent = 'Each time slot needs a valid time and volume.'; return; }
        slots.push({ time, liters });
    }
    if (!slots.length) { msg.textContent = 'Add at least one time slot.'; return; }
    const schedule = { days, slots };
    saveSchedule(schedule);
    try {
        const res = await fetch(`/api/zone/${zoneId}/schedule`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(schedule),
        });
        if (!res.ok) { msg.textContent = 'Saved locally but failed to sync to server.'; return; }
    } catch (e) { msg.textContent = 'Saved locally but server unreachable.'; return; }
    updateScheduleStatus();
}

async function clearSchedule() {
    saveSchedule(null);
    document.querySelectorAll('.dow-check').forEach((cb) => { cb.checked = false; });
    const container = document.getElementById('schedSlots');
    if (container) { container.innerHTML = ''; addSchedSlot(); }
    document.getElementById('schedMsg').textContent = 'Schedule cleared.';
    try { await fetch(`/api/zone/${zoneId}/schedule`, { method: 'DELETE' }); } catch (e) { /* ignore */ }
}

async function runManualLiters() {
    const liters = parseFloat(document.getElementById('manualLiters').value);
    const msg = document.getElementById('manualMsg');

    if (!liters || liters <= 0) { msg.textContent = 'Enter a valid volume.'; return; }
    if (!zoneCache) return;

    const btn = document.getElementById('manualStartBtn');
    btn.disabled = true;
    msg.textContent = 'Adding to queue…';

    try {
        const res = await fetch('/api/irrigation/queue', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ zone_id: zoneId, volume_liters: liters }),
        });
        const data = await res.json();
        if (!res.ok) {
            msg.textContent = data.error || 'Failed to queue irrigation.';
            return;
        }
        const flowRate = zoneCache.zone.flow_rate_lpm || 3.0;
        msg.textContent = `Queued: ${liters} L (~${(liters / flowRate).toFixed(1)} min). Check the Testing page for queue status.`;
    } catch (e) {
        msg.textContent = 'Failed to queue irrigation.';
    } finally {
        btn.disabled = false;
    }
}

function updateManualDurationPreview() {
    const input = document.getElementById('manualLiters');
    const preview = document.getElementById('manualDurationPreview');
    if (!input || !preview) return;
    const liters = parseFloat(input.value);
    if (!liters || liters <= 0 || !zoneCache) { preview.textContent = ''; return; }
    const flowRate = zoneCache.zone.flow_rate_lpm || 3.0;
    preview.textContent = `\u2248 ${(liters / flowRate).toFixed(1)} min at ${flowRate} L/min`;
}

// ── History minimize ───────────────────────────────────────────────────────

function toggleHistory(wrapId, btnId) {
    const wrap = document.getElementById(wrapId);
    const btn = document.getElementById(btnId);
    if (!wrap || !btn) return;
    const hidden = wrap.style.display === 'none';
    wrap.style.display = hidden ? '' : 'none';
    btn.innerHTML = hidden ? '&#9660;' : '&#9654;';
}

window.addEventListener('DOMContentLoaded', async () => {
    const root = document.getElementById('zoneDetailRoot');
    zoneId = Number(root ? root.dataset.zoneId : NaN);
    if (!Number.isInteger(zoneId) || zoneId < 1 || zoneId > 4) {
        document.getElementById('zoneSaveMsg').textContent = 'Invalid zone page.';
        return;
    }

    document.getElementById('saveZoneProfileBtn').addEventListener('click', saveZoneProfile);
    document.getElementById('mlPredictBtn').addEventListener('click', runMLPrediction);
    document.getElementById('mlExecuteBtn').addEventListener('click', executeMLIrrigation);
    document.getElementById('addSlotBtn').addEventListener('click', () => addSchedSlot());
    document.getElementById('schedSaveBtn').addEventListener('click', scheduleIrrigation);
    document.getElementById('schedClearBtn').addEventListener('click', clearSchedule);
    document.getElementById('manualStartBtn').addEventListener('click', runManualLiters);
    document.getElementById('manualLiters').addEventListener('input', updateManualDurationPreview);
    document.getElementById('minimizeMoistureBtn').addEventListener('click', () =>
        toggleHistory('zoneHistoryWrap', 'minimizeMoistureBtn'));
    document.getElementById('minimizeEventsBtn').addEventListener('click', () =>
        toggleHistory('zoneEventsWrap', 'minimizeEventsBtn'));
    document.querySelectorAll('.irr-tab').forEach((tab) => {
        tab.addEventListener('click', () => {
            if (tab.dataset.mode === 'ml' && tab.dataset.mlDisabled === 'true') return;
            switchIrrigationMode(tab.dataset.mode, true);
        });
    });

    // Check ML model availability — disable the ML tab if no model is trained yet.
    let mlAvailable = false;
    try {
        const mlRes  = await fetch('/api/ml/model-status');
        const mlData = await mlRes.json();
        mlAvailable  = mlData.model_available === true;
    } catch (e) { /* non-blocking */ }
    const mlTab     = document.querySelector('.irr-tab[data-mode="ml"]');
    const mlPanel   = document.getElementById('irrPanelMl');
    const mlNoModel = document.getElementById('mlNoModelMsg');
    if (!mlAvailable) {
        if (mlTab) {
            mlTab.dataset.mlDisabled = 'true';
            mlTab.style.opacity      = '0.45';
            mlTab.style.cursor       = 'not-allowed';
            mlTab.title              = 'No trained model yet. Collect data with Panel 6 on the thesis dashboard, then run cron_retrain.py.';
        }
        if (mlNoModel) mlNoModel.style.display = 'block';
    }

    // Restore irrigation mode from server (source of truth)
    try {
        const modeRes = await fetch(`/api/zone/${zoneId}/mode`);
        if (modeRes.ok) {
            const modeData = await modeRes.json();
            let serverMode = modeData.irr_mode || 'manual';
            // If server stored 'ml' but model is no longer available, fall back to manual
            if (serverMode === 'ml' && !mlAvailable) serverMode = 'manual';
            localStorage.setItem(MODE_KEY(), serverMode);
        }
    } catch (e) { /* non-blocking */ }
    // Never switch to ml mode when model is unavailable
    const activeMode = getActiveMode();
    switchIrrigationMode((!mlAvailable && activeMode === 'ml') ? 'manual' : activeMode);

    restoreScheduleUI();
    // Restore schedule from server (source of truth) and sync to server if localStorage has one
    try {
        const sr = await fetch(`/api/zone/${zoneId}/schedule`);
        const sd = await sr.json();
        if (sd.schedule) {
            saveSchedule(sd.schedule);
            restoreScheduleUI();
        } else {
            // Push any locally-saved schedule to server on first load
            const local = getSchedule();
            if (local) {
                await fetch(`/api/zone/${zoneId}/schedule`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(local),
                });
            }
        }
    } catch (e) { /* non-blocking */ }
    await loadZoneData();
    setInterval(loadZoneData, 7000);
});
