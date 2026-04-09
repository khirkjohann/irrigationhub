let zoneCache = null;
let zoneId = null;

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

function bindZoneSettings(zone, baselines, cropTargets) {
    const baselineSelect = document.getElementById('zoneBaselineSelect');
    const cropTargetSelect = document.getElementById('zoneCropTargetSelect');

    // Default to the first saved baseline when the zone has none assigned,
    // so the calibration page's saved baseline is always picked up automatically.
    const effectiveBaselineId = zone.soil_baseline_id ?? (baselines.length > 0 ? baselines[0].id : null);

    baselineSelect.innerHTML = ['<option value="">-- Unassigned --</option>']
        .concat(baselines.map((item) => `<option value="${item.id}" ${effectiveBaselineId === item.id ? 'selected' : ''}>${item.name}</option>`))
        .join('');

    // Enable the delete button only when a real baseline is selected.
    const deleteBtn = document.getElementById('deleteBaselineBtn');
    const updateDeleteBtn = () => {
        deleteBtn.disabled = baselineSelect.value === '';
    };
    updateDeleteBtn();
    baselineSelect.addEventListener('change', updateDeleteBtn);

    cropTargetSelect.innerHTML = ['<option value="">-- Unassigned --</option>']
        .concat(cropTargets.map((item) => `<option value="${item.id}" ${zone.crop_target_id === item.id ? 'selected' : ''}>${item.name}</option>`))
        .join('');
}

function renderZoneHeader(zone) {
    const moisture = zone.moisture;
    const isOn = zone.valve_status === 'ON';

    document.getElementById('zoneMoisture').textContent = moisture === null || moisture === undefined ? '-- %' : `${Number(moisture).toFixed(1)} %`;
    document.getElementById('zoneTarget').textContent = zone.target_moisture === null || zone.target_moisture === undefined ? '-- %' : `${Number(zone.target_moisture).toFixed(1)} %`;
    document.getElementById('zoneValveStatus').textContent = isOn ? 'ON' : 'OFF';
    document.getElementById('zoneGaugeFill').style.width = `${Math.max(0, Math.min(100, moisture || 0))}%`;
    document.getElementById('zoneGaugeFill').style.background = window.AppCommon.moistureColor(moisture);

    const toggleBtn = document.getElementById('zoneToggleBtn');
    toggleBtn.textContent = isOn ? 'Valve ON (Tap to OFF)' : 'Valve OFF (Tap to ON)';
    toggleBtn.className = `toggle-btn ${isOn ? 'toggle-on' : 'toggle-off'}`;

    document.getElementById('zoneTimerMsg').textContent = zone.manual_until
        ? `Auto-close at ${new Date(zone.manual_until).toLocaleTimeString()}`
        : 'No active timer';
}

async function loadZoneData() {
    const dashboard = await window.AppCommon.fetchDashboard();
    const zone = dashboard.zones.find((z) => z.zone_id === zoneId);
    if (!zone) {
        throw new Error(`Zone ${zoneId} not found`);
    }

    zoneCache = {
        zone,
        baselines: dashboard.soil_baselines || [],
        cropTargets: dashboard.crop_targets || [],
    };

    renderZoneHeader(zone);
    bindZoneSettings(zone, zoneCache.baselines, zoneCache.cropTargets);

    const res = await fetch(`/api/zone/${zoneId}/history?limit=30`);
    const payload = await res.json();
    renderHistoryRows(payload.history || []);
    renderEventRows(payload.control_history || []);
}

async function saveZoneProfile() {
    if (!zoneCache) return;

    const baselineRaw = document.getElementById('zoneBaselineSelect').value;
    const cropTargetRaw = document.getElementById('zoneCropTargetSelect').value;

    const payload = {
        soil_baseline_id: baselineRaw === '' ? null : Number(baselineRaw),
        crop_target_id: cropTargetRaw === '' ? null : Number(cropTargetRaw),
    };

    const res = await fetch(`/api/zone/${zoneId}/mapping`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });

    if (!res.ok) {
        const err = await res.json();
        document.getElementById('zoneSaveMsg').textContent = err.error || 'Failed to save zone assignment.';
        return;
    }

    document.getElementById('zoneSaveMsg').textContent = 'Zone assignment saved.';
    await loadZoneData();
}

async function deleteSelectedBaseline() {
    const select = document.getElementById('zoneBaselineSelect');
    const id = Number(select.value);
    const name = select.options[select.selectedIndex]?.text || 'this baseline';
    if (!id) return;
    if (!confirm(`Delete baseline "${name}"?\n\nAny zones using it will be unassigned.`)) return;

    const res = await fetch(`/api/calibration/baseline/${id}`, { method: 'DELETE' });
    if (!res.ok) {
        const err = await res.json();
        document.getElementById('zoneSaveMsg').textContent = err.error || 'Delete failed.';
        return;
    }
    document.getElementById('zoneSaveMsg').textContent = `Baseline "${name}" deleted.`;
    await loadZoneData();
}

async function toggleZoneValve() {
    if (!zoneCache) return;
    const desiredState = zoneCache.zone.valve_status === 'ON' ? 'OFF' : 'ON';
    await window.AppCommon.toggleValve(zoneId, desiredState, 'zoneTimerInput');
    await loadZoneData();
}

window.addEventListener('DOMContentLoaded', async () => {
    const root = document.getElementById('zoneDetailRoot');
    zoneId = Number(root ? root.dataset.zoneId : NaN);
    if (!Number.isInteger(zoneId) || zoneId < 1 || zoneId > 4) {
        document.getElementById('zoneSaveMsg').textContent = 'Invalid zone page.';
        return;
    }

    document.getElementById('saveZoneProfileBtn').addEventListener('click', saveZoneProfile);
    document.getElementById('zoneToggleBtn').addEventListener('click', toggleZoneValve);

    await loadZoneData();
    setInterval(loadZoneData, 7000);
});
