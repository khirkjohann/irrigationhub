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

        document.getElementById('relayGpioAvailable').textContent = boolText(Boolean(relayStatus.available));
        document.getElementById('relayInitialized').textContent = boolText(Boolean(relayStatus.initialized));
        document.getElementById('relayMessage').textContent = relayStatus.message || '--';

        const fanStatus = data.fan_status || {};
        document.getElementById('fanTempValue').textContent =
            fanStatus.temp_c != null ? `${fanStatus.temp_c} °C` : '--';
        document.getElementById('fanDutyValue').textContent =
            fanStatus.duty   != null ? `${fanStatus.duty} %`   : '--';
        document.getElementById('fanMessage').textContent = fanStatus.message || '--';

        renderExpectedInputs(sensorStatus);
    } catch (err) {
        document.getElementById('lastErrorValue').textContent = `Status fetch failed: ${err}`;
    }
}

async function queryCpuTemp() {
    const valEl = document.getElementById('cpuTempValue');
    const errEl = document.getElementById('cpuTempError');
    valEl.textContent = '…';
    errEl.textContent = '';
    try {
        const data = await (await fetch('/api/diagnostics/cpu-temp')).json();
        if (data.error) throw new Error(data.error);
        valEl.textContent = `${data.temp_c} °C`;
        valEl.className = data.temp_c >= 80 ? 'status-missing' : data.temp_c >= 70 ? 'status-warn' : 'status-ok';
        errEl.textContent = data.temp_c >= 80 ? 'Warning: CPU very hot — check ventilation.'
                          : data.temp_c >= 70 ? 'Elevated temperature — monitor closely.' : '';
    } catch (err) {
        valEl.textContent = '--';
        errEl.textContent = `Failed: ${err.message || err}`;
    }
}

window.queryCpuTemp = queryCpuTemp;

async function runRelayTest() {
    const target = document.getElementById('relayResult');
    target.textContent = 'Running test...';
    const res = await fetch('/api/diagnostics/relay-test', { method: 'POST' });
    const data = await res.json();
    target.textContent = data.message || 'Relay test complete.';
}

window.runI2CScan = runI2CScan;
window.runRelayTest = runRelayTest;
window.refreshHardwareStatus = refreshHardwareStatus;

async function refreshThesisStatus() {
    try {
        const res = await fetch('/api/thesis/status');
        const data = await res.json();
        const btn = document.getElementById('thesisToggleBtn');
        const msg = document.getElementById('thesisMessage');
        if (!btn) return;
        if (data.running) {
            btn.textContent = 'Stop Thesis Dashboard';
            btn.className = 'btn secondary';
            msg.textContent = `Running (PID ${data.pid}). Access at port 5002.`;
        } else {
            btn.textContent = 'Start Thesis Dashboard';
            btn.className = 'btn';
            msg.textContent = 'Thesis dashboard is not running.';
        }
        btn.disabled = false;
    } catch (err) {
        const msg = document.getElementById('thesisMessage');
        if (msg) msg.textContent = `Status error: ${err}`;
    }
}

async function toggleThesisDashboard() {
    const btn = document.getElementById('thesisToggleBtn');
    const msg = document.getElementById('thesisMessage');
    btn.disabled = true;
    try {
        const statusRes = await fetch('/api/thesis/status');
        const statusData = await statusRes.json();
        const endpoint = statusData.running ? '/api/thesis/stop' : '/api/thesis/start';
        msg.textContent = statusData.running ? 'Stopping...' : 'Starting...';
        const res = await fetch(endpoint, { method: 'POST' });
        const data = await res.json();
        msg.textContent = data.message || data.error || 'Done.';
    } catch (err) {
        msg.textContent = `Error: ${err}`;
    }
    await refreshThesisStatus();
}

window.addEventListener('DOMContentLoaded', async () => {
    const safeShutdownBtn = document.getElementById('safeShutdownBtn');
    if (safeShutdownBtn) safeShutdownBtn.addEventListener('click', requestSafeShutdown);

    const thesisBtn = document.getElementById('thesisToggleBtn');
    if (thesisBtn) thesisBtn.addEventListener('click', toggleThesisDashboard);

    // ── WiFi config panel ──────────────────────────────────────────────────
    document.getElementById('wifiScanBtn')?.addEventListener('click', scanWifi);
    document.getElementById('wifiHotspotBtn')?.addEventListener('click', useHotspot);
    document.getElementById('wifiConnectBtn')?.addEventListener('click', connectWifi);
    document.getElementById('wifiShowToggle')?.addEventListener('click', () => {
        const pw = document.getElementById('wifiPassword');
        const btn = document.getElementById('wifiShowToggle');
        if (!pw) return;
        const hide = pw.type === 'password';
        pw.type = hide ? 'text' : 'password';
        btn.textContent = hide ? 'Hide' : 'Show';
    });

    await refreshWifiStatus();
    await loadWifiProfiles();

    await refreshHardwareStatus();
    await runI2CScan();
    await refreshThesisStatus();
    setInterval(refreshHardwareStatus, 8000);
    setInterval(refreshThesisStatus, 8000);
    setInterval(refreshWifiStatus, 15000);
});

// ── WiFi helpers ─────────────────────────────────────────────────────────────

function signalBars(signal) {
    if (signal >= 70) return '▂▄▆█';
    if (signal >= 50) return '▂▄▆_';
    if (signal >= 30) return '▂▄__';
    return '▂___';
}

async function refreshWifiStatus() {
    try {
        const data = await (await fetch('/api/network/wifi-status')).json();
        const connEl = document.getElementById('wifiActiveConn');
        const ipEl   = document.getElementById('wifiActiveIp');
        if (connEl) connEl.textContent = data.active || 'None';
        if (ipEl)   ipEl.textContent   = data.ip    || '--';
    } catch (e) { /* non-fatal */ }
}

async function scanWifi() {
    const btn       = document.getElementById('wifiScanBtn');
    const resultsEl = document.getElementById('wifiScanResults');
    const listEl    = document.getElementById('wifiNetworkList');
    const msg       = document.getElementById('wifiMsg');

    btn.disabled    = true;
    btn.textContent = 'Scanning…';
    msg.textContent = '';
    if (resultsEl) resultsEl.style.display = 'none';

    try {
        const data     = await (await fetch('/api/network/wifi-scan')).json();
        const networks = data.networks || [];
        if (!networks.length) {
            msg.textContent = 'No networks found. Try again.';
            return;
        }
        listEl.innerHTML = networks.map(n => `
            <div class="wifi-network-row" data-ssid="${n.ssid}" data-open="${n.open}"
                 style="display:flex;align-items:center;gap:10px;padding:7px 10px;margin-bottom:4px;
                        border:1px solid #dde;border-radius:6px;cursor:pointer;background:#f8faff;">
                <span style="flex:1;font-weight:${n.in_use ? 700 : 400};">${n.ssid}${n.in_use ? ' ✓' : ''}</span>
                <span class="small-text" style="font-family:monospace;">${signalBars(n.signal)}</span>
                <span class="small-text" style="color:#888;">${n.open ? 'Open' : 'WPA'}</span>
            </div>
        `).join('');
        listEl.querySelectorAll('.wifi-network-row').forEach(row => {
            row.addEventListener('click', () => {
                const ssid = row.dataset.ssid;
                document.getElementById('wifiSsid').value = ssid;
                if (row.dataset.open === 'true') {
                    document.getElementById('wifiPassword').value = '';
                }
                listEl.querySelectorAll('.wifi-network-row').forEach(r =>
                    r.style.background = r === row ? '#e8f4fd' : '#f8faff'
                );
                msg.textContent = `Selected: ${ssid}. ${row.dataset.open === 'true' ? 'No password needed.' : 'Enter password below.'}`;
            });
        });
        resultsEl.style.display = 'block';
    } catch (e) {
        msg.textContent = `Scan failed: ${e.message || e}`;
    } finally {
        btn.disabled    = false;
        btn.textContent = 'Scan Networks';
    }
}

async function connectWifi() {
    const ssid     = (document.getElementById('wifiSsid').value || '').trim();
    const password = (document.getElementById('wifiPassword').value || '').trim();
    const msg      = document.getElementById('wifiMsg');
    const btn      = document.getElementById('wifiConnectBtn');

    if (!ssid) { msg.textContent = 'Select or type an SSID first.'; return; }

    btn.disabled    = true;
    msg.textContent = `Connecting to "${ssid}"… (up to 30 s)`;

    try {
        const res  = await fetch('/api/network/wifi-connect', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ssid, password }),
        });
        const data = await res.json();
        if (!res.ok) {
            msg.textContent = `Error: ${data.error}`;
        } else {
            msg.textContent = `${data.message} IP: ${data.ip}`;
            document.getElementById('wifiSsid').value     = '';
            document.getElementById('wifiPassword').value = '';
        }
        await refreshWifiStatus();
        await loadWifiProfiles();
    } catch (e) {
        msg.textContent = `Connect failed: ${e.message || e}`;
    } finally {
        btn.disabled = false;
    }
}

async function useHotspot() {
    const msg = document.getElementById('wifiMsg');
    const btn = document.getElementById('wifiHotspotBtn');
    btn.disabled    = true;
    msg.textContent = 'Switching to Hotspot…';
    try {
        const res  = await fetch('/api/network/use-hotspot', { method: 'POST' });
        const data = await res.json();
        msg.textContent = data.message || data.error || 'Done.';
        await refreshWifiStatus();
    } catch (e) {
        msg.textContent = `Error: ${e.message || e}`;
    } finally {
        btn.disabled = false;
    }
}

async function loadWifiProfiles() {
    const listEl = document.getElementById('wifiProfilesList');
    try {
        const data     = await (await fetch('/api/network/wifi-profiles')).json();
        const profiles = data.profiles || [];
        if (!profiles.length) {
            listEl.innerHTML = '<span class="small-text">No saved profiles yet. Connect to a network above to save one.</span>';
            return;
        }
        listEl.innerHTML = profiles.map(p => `
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">
                <span class="small-text" style="flex:1;">${p.replace(/^irr-wifi-/, '')}</span>
                <button class="btn" style="font-size:0.78rem;padding:3px 10px;"
                        onclick="connectWifiProfile('${p}')">Connect</button>
                <button class="btn" style="font-size:0.78rem;padding:3px 10px;background:#e74c3c;"
                        onclick="deleteWifiProfile('${p}')">Delete</button>
            </div>
        `).join('');
    } catch (e) {
        listEl.innerHTML = `<span class="small-text" style="color:#c0392b;">Failed to load: ${e.message || e}</span>`;
    }
}

async function connectWifiProfile(profile) {
    const msg = document.getElementById('wifiMsg');
    const ssid = profile.replace(/^irr-wifi-/, '');
    msg.textContent = `Connecting to "${ssid}"… (up to 30 s)`;
    try {
        const res  = await fetch('/api/network/wifi-activate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ profile }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || `Connect failed (${res.status})`);
        msg.textContent = `${data.message} IP: ${data.ip}`;
        await refreshWifiStatus();
    } catch (e) {
        msg.textContent = `Connect failed: ${e.message || e}`;
    }
}

async function deleteWifiProfile(name) {
    if (!window.confirm(`Delete profile "${name}"?`)) return;
    const msg = document.getElementById('wifiMsg');
    try {
        const res  = await fetch('/api/network/wifi-delete', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ profile: name }),
        });
        const data = await res.json();
        msg.textContent = data.message || data.error || 'Done.';
        await loadWifiProfiles();
        await refreshWifiStatus();
    } catch (e) {
        msg.textContent = `Delete failed: ${e.message || e}`;
    }
}


