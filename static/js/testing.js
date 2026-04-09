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

async function refreshRawReadout() {
    const res = await fetch('/api/diagnostics/raw');
    const data = await res.json();
    document.getElementById('adcValue').textContent = data.adc_value ?? '--';
    document.getElementById('adcVoltage').textContent = data.voltage !== null ? `${data.voltage} V` : '-- V';
    document.getElementById('rawError').textContent = data.error ? `Error: ${data.error}` : '';
}

async function runI2CScan() {
    const res = await fetch('/api/diagnostics/i2c-scan', { method: 'POST' });
    const data = await res.json();
    const target = document.getElementById('i2cResult');
    target.textContent = data.error ? `Error: ${data.error}` : `Detected addresses: ${data.addresses.join(', ') || 'None'}`;
}

async function runRelayTest() {
    const target = document.getElementById('relayResult');
    target.textContent = 'Running test...';
    const res = await fetch('/api/diagnostics/relay-test', { method: 'POST' });
    const data = await res.json();
    target.textContent = data.message || 'Relay test complete.';
}

window.refreshRawReadout = refreshRawReadout;
window.runI2CScan = runI2CScan;
window.runRelayTest = runRelayTest;

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
    await refreshRawReadout();
});
