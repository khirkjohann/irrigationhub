const calibrationState = {
    activeChannel: 'A0',
};

const VALID_CALIBRATION_CHANNELS = new Set(['A0', 'A1', 'A2', 'A3']);

function getActiveCalibrationChannel() {
    return VALID_CALIBRATION_CHANNELS.has(calibrationState.activeChannel)
        ? calibrationState.activeChannel
        : 'A0';
}

async function captureLiveVoltage() {
    const channel = getActiveCalibrationChannel();
    const res = await fetch('/api/calibration/capture-live', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ channel }),
    });
    const data = await res.json();
    if (!res.ok) {
        throw new Error(data.error || 'Failed to capture live voltage');
    }
    return data.averaged_voltage;
}

function renderCropTargetTable(rows) {
    const tbody = document.querySelector('#cropTargetTable tbody');
    tbody.innerHTML = '';
    if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="3">No crop targets saved.</td></tr>';
        return;
    }

    rows.forEach((row) => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td>${row.name}</td>
            <td>${Number(row.target_voltage).toFixed(4)}</td>
            <td><button class="btn secondary" style="padding:2px 8px;font-size:.8em" onclick="deleteCropTarget(${row.id}, '${row.name.replace(/'/g, "\\'")}')">Delete</button></td>
        `;
        tbody.appendChild(tr);
    });
}

async function refreshCalibrationCatalog() {
    const data = await window.AppCommon.fetchDashboard();
    renderCropTargetTable(data.crop_targets || []);
}

async function deleteCropTarget(id, name) {
    if (!confirm(`Delete crop target "${name}"?\n\nAny zones using it will be unassigned.`)) return;
    const res = await fetch(`/api/calibration/crop-target/${id}`, { method: 'DELETE' });
    if (!res.ok) {
        const err = await res.json();
        document.getElementById('cropTargetMsg').textContent = err.error || 'Delete failed.';
        return;
    }
    document.getElementById('cropTargetMsg').textContent = `Deleted: ${name}`;
    await refreshCalibrationCatalog();
}

async function saveCropTarget() {
    const msg = document.getElementById('cropTargetMsg');
    const name = document.getElementById('cropTargetName').value.trim();
    const targetVoltage = Number(document.getElementById('cropTargetVoltage').value);

    if (!name) {
        msg.textContent = 'Crop target name is required.';
        return;
    }
    if (Number.isNaN(targetVoltage)) {
        msg.textContent = 'Target voltage is required.';
        return;
    }

    const res = await fetch('/api/calibration/crop-target', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            name,
            target_voltage: targetVoltage,
        }),
    });

    const data = await res.json();
    if (!res.ok) {
        msg.textContent = data.error || 'Failed to save crop target.';
        return;
    }

    msg.textContent = `Saved crop target: ${data.crop_target.name}`;
    await refreshCalibrationCatalog();
}

async function handleCapture(targetInputId, messageId) {
    const msg = document.getElementById(messageId);
    try {
        msg.textContent = 'Capturing live data...';
        const voltage = await captureLiveVoltage();
        document.getElementById(targetInputId).value = voltage;
        msg.textContent = `Captured: ${Number(voltage).toFixed(4)} V`;
    } catch (err) {
        msg.textContent = err.message;
    }
}

window.addEventListener('DOMContentLoaded', async () => {
    const activeSensorSelect = document.getElementById('activeCalibrationSensor');
    if (activeSensorSelect) {
        if (VALID_CALIBRATION_CHANNELS.has(activeSensorSelect.value)) {
            calibrationState.activeChannel = activeSensorSelect.value;
        }
        activeSensorSelect.addEventListener('change', (event) => {
            const selected = String(event.target.value || '').toUpperCase();
            calibrationState.activeChannel = VALID_CALIBRATION_CHANNELS.has(selected) ? selected : 'A0';
        });
    }

    document.getElementById('captureTargetBtn').addEventListener('click', () => handleCapture('cropTargetVoltage', 'cropTargetMsg'));
    document.getElementById('saveCropTargetBtn').addEventListener('click', saveCropTarget);

    await refreshCalibrationCatalog();
    await refreshIrrigationQueue();
    setInterval(refreshIrrigationQueue, 5000);
});

// ── Irrigation queue ───────────────────────────────────────────────────────

function fmtMoisture(v) {
    return v == null ? '--' : `${v} %`;
}

function fmtTime(iso) {
    if (!iso) return '--';
    const d = new Date(iso);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

async function removeQueueItem(id) {
    await fetch(`/api/irrigation/queue/${id}`, { method: 'DELETE' });
    await refreshIrrigationQueue();
}

function renderQueueTable(queue, active) {
    const tbody = document.querySelector('#irrQueueTable tbody');
    const rows = [];
    if (active) rows.push(active);
    rows.push(...queue);

    if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="8" class="muted-cell">No irrigations queued.</td></tr>';
        return;
    }
    tbody.innerHTML = rows.map((item) => {
        const isRunning = item.status === 'running';
        const rowClass = isRunning ? ' class="irr-row-running"' : '';
        const statusBadge = isRunning
            ? '<span class="zone-badge on" style="font-size:0.75rem;">RUNNING</span>'
            : '<span class="zone-badge off" style="font-size:0.75rem;">QUEUED</span>';
        const removeBtn = isRunning
            ? '&mdash;'
            : `<button class="btn secondary" style="padding:2px 8px;font-size:.8em" onclick="removeQueueItem(${item.id})">Remove</button>`;
        return `<tr${rowClass}>
            <td>Zone ${item.zone_id}</td>
            <td>${fmtTime(item.added_at)}</td>
            <td>${fmtMoisture(item.initial_moisture)}</td>
            <td>${item.volume_liters}</td>
            <td>${item.duration_minutes}</td>
            <td>${fmtTime(item.est_complete)}</td>
            <td>${statusBadge}</td>
            <td>${removeBtn}</td>
        </tr>`;
    }).join('');
}

function renderCompletedTable(completed) {
    const tbody = document.querySelector('#irrCompletedTable tbody');
    if (!completed.length) {
        tbody.innerHTML = '<tr><td colspan="14" class="muted-cell">No completed irrigations yet.</td></tr>';
        return;
    }
    tbody.innerHTML = completed.map((item) => {
        const actualDur = item.actual_duration_minutes != null ? item.actual_duration_minutes : '--';
        const estDur    = item.duration_minutes ?? item.est_duration_minutes ?? '--';
        return `<tr>
            <td>Zone ${item.zone_id}</td>
            <td>${item.source || 'manual'}</td>
            <td>${fmtTime(item.added_at)}</td>
            <td>${fmtTime(item.started_at)}</td>
            <td>${fmtTime(item.completed_at)}</td>
            <td>${item.volume_liters ?? '--'}</td>
            <td>${estDur}</td>
            <td><strong>${actualDur}</strong></td>
            <td>${fmtMoisture(item.initial_moisture)}</td>
            <td><strong>${fmtMoisture(item.post_moisture)}</strong></td>
            <td>${item.temperature != null ? item.temperature : '--'}</td>
            <td>${item.humidity != null ? item.humidity : '--'}</td>
            <td>${item.crop_target_name || '--'}</td>
            <td>${item.target_moisture != null ? item.target_moisture + ' %' : '--'}</td>
        </tr>`;
    }).join('');
}

async function refreshIrrigationQueue() {
    try {
        const res = await fetch('/api/irrigation/queue');
        if (!res.ok) return;
        const data = await res.json();
        renderQueueTable(data.queue || [], data.active || null);
        renderCompletedTable(data.completed || []);
    } catch (e) {
        // silently ignore
    }
}
