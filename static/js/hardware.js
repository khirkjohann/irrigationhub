function fmtTime(ts) {
    if (!ts) return '--';
    try {
        return new Date(ts).toLocaleString();
    } catch (_) {
        return ts;
    }
}

function boolText(v) {
    return v ? 'YES' : 'NO';
}

function statusClass(ok) {
    return ok ? 'status-ok' : 'status-missing';
}

function renderExpectedInputs(sensorStatus) {
    const grid = document.getElementById('expectedInputsGrid');
    grid.innerHTML = '';

    const expected = [
        { id: 'bme280', title: 'BME280 @ 0x76/0x77' },
        { id: 'ads1115_0x48', title: 'ADS1115 @ 0x48' },
    ];

    expected.forEach((item) => {
        const state = sensorStatus[item.id] || { ok: false, message: 'No status yet' };
        const card = document.createElement('div');
        card.className = 'diag-box';
        card.innerHTML = `
            <h3>${item.title}</h3>
            <div class="status-tag ${statusClass(state.ok)}">${state.ok ? 'DETECTED' : 'NOT FOUND'}</div>
            <p class="small-text">${state.message || '--'}</p>
        `;
        grid.appendChild(card);
    });

    const missing = sensorStatus.missing_inputs || [];
    const missingText = document.getElementById('missingInputsText');
    missingText.textContent = missing.length
        ? `Missing inputs: ${missing.join(', ')}`
        : 'All expected sensor inputs are detected.';
}

async function runI2CScan() {
    try {
        const res = await fetch('/api/diagnostics/i2c-scan', { method: 'POST' });
        const data = await res.json();
        document.getElementById('i2cDetected').textContent = (data.addresses || []).join(', ') || 'None';
        document.getElementById('i2cMissing').textContent = (data.missing || []).join(', ') || 'None';
        document.getElementById('i2cErrorText').textContent = data.error || 'I2C scan completed.';
    } catch (err) {
        document.getElementById('i2cErrorText').textContent = `I2C scan failed: ${err}`;
    }
}

async function requestSafeShutdown() {
    const msg = document.getElementById('shutdownMessage');
    const confirmed = window.confirm('Proceed with safe shutdown now? You can unplug power after the device fully turns off.');
    if (!confirmed) {
        if (msg) msg.textContent = 'Shutdown canceled.';
        return;
    }

    if (msg) msg.textContent = 'Sending shutdown request...';

    try {
        const res = await fetch('/api/system/shutdown', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
        });
        const contentType = (res.headers.get('content-type') || '').toLowerCase();
        let data = {};
        if (contentType.includes('application/json')) {
            data = await res.json();
        } else {
            const bodyText = await res.text();
            const shortBody = bodyText.replace(/\s+/g, ' ').trim().slice(0, 120);
            throw new Error(
                `Server returned non-JSON response (${res.status}). ${shortBody}. If you just updated the app, restart the Flask service.`
            );
        }

        if (!res.ok) {
            throw new Error(data.error || `Shutdown request failed (${res.status}).`);
        }

        if (msg) msg.textContent = data.message || 'Shutdown requested. Wait until LEDs stop before unplugging.';
    } catch (err) {
        if (msg) msg.textContent = `Shutdown failed: ${err.message || err}`;
    }
}

async function refreshHardwareStatus() {
    try {
        const res = await fetch('/api/system/status');
        const data = await res.json();

        const sensorStatus = data.sensor_status || {};
        const relayStatus = data.relay_status || {};

        document.getElementById('sensorModeValue').textContent = data.sensor_mode || '--';
        document.getElementById('lastPollValue').textContent = fmtTime(sensorStatus.last_poll);
        document.getElementById('lastSuccessValue').textContent = fmtTime(sensorStatus.last_success);
        document.getElementById('lastErrorValue').textContent = sensorStatus.last_error || 'None';

        document.getElementById('relayGpioAvailable').textContent = boolText(Boolean(relayStatus.gpio_available));
        document.getElementById('relayInitialized').textContent = boolText(Boolean(relayStatus.initialized));
        document.getElementById('relayMessage').textContent = relayStatus.message || '--';

        renderExpectedInputs(sensorStatus);
    } catch (err) {
        document.getElementById('lastErrorValue').textContent = `Status fetch failed: ${err}`;
    }
}

window.runI2CScan = runI2CScan;
window.refreshHardwareStatus = refreshHardwareStatus;

window.addEventListener('DOMContentLoaded', async () => {
    const safeShutdownBtn = document.getElementById('safeShutdownBtn');
    if (safeShutdownBtn) {
        safeShutdownBtn.addEventListener('click', requestSafeShutdown);
    }

    await refreshHardwareStatus();
    await runI2CScan();
    setInterval(refreshHardwareStatus, 8000);
});
