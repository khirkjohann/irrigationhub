async function setZoneDisabled(zoneId, disabled) {
    const res = await fetch(`/api/zone/${zoneId}/disable`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ disabled }),
    });

    if (!res.ok) {
        const data = await res.json();
        throw new Error(data.error || 'Failed to update zone disable state');
    }
}

function renderSensorSummary(sensorStatus) {
    const missing = sensorStatus.missing_inputs || [];
    const pill = document.getElementById('sensorInputsStatus');
    const help = document.getElementById('sensorInputsError');

    if (missing.length) {
        pill.className = 'hero-status-pill warn';
        pill.textContent = 'Inputs Missing';
        help.textContent = `Not found: ${missing.join(', ')}`;
        return;
    }

    pill.className = 'hero-status-pill ok';
    pill.textContent = 'Inputs Healthy';
    help.textContent = sensorStatus.last_error || 'All expected sensors detected.';
}

function renderZoneCard(zone) {
    const isOn = zone.valve_status === 'ON';
    const isDisabled = Boolean(zone.disabled);
    const moisture = zone.moisture;
    const badgeClass = isDisabled ? 'disabled' : (isOn ? 'on' : 'off');
    const badgeText = isDisabled ? 'DISABLED' : (isOn ? 'WATERING' : 'IDLE');
    const gaugeValue = isDisabled ? 0 : Math.max(0, Math.min(100, moisture || 0));

    const card = document.createElement('article');
    card.className = 'home-zone-card';
    card.innerHTML = `
        <div class="home-zone-top">
            <h3 class="zone-title">Zone ${zone.zone_id}</h3>
            <span class="zone-badge ${badgeClass}">${badgeText}</span>
        </div>
        <div class="meta-row"><span>Soil Baseline</span><strong>${zone.soil_baseline_name || '--'}</strong></div>
        <div class="meta-row"><span>Crop Target</span><strong>${zone.crop_target_name || '--'}</strong></div>
        <div class="meta-row"><span>Moisture</span><strong>${window.AppCommon.formatValue(moisture, '%')}</strong></div>
        <div class="meta-row"><span>Target</span><strong>${zone.target_moisture ?? '--'}%</strong></div>
        <div class="gauge"><div class="gauge-fill" style="width:${gaugeValue}%; background:${window.AppCommon.moistureColor(moisture)};"></div></div>
        <div class="zone-controls compact-controls">
            <p class="zone-mini-label">Valve timer (minutes)</p>
            <input class="zone-mini-input" type="number" id="home-timer-${zone.zone_id}" min="0" value="10" ${isDisabled ? 'disabled' : ''}>
            <div class="zone-quick-grid">
                <button class="toggle-btn ${isOn ? 'toggle-on' : 'toggle-off'}" data-action="valve" data-zone="${zone.zone_id}" data-state="${isOn ? 'OFF' : 'ON'}" ${isDisabled ? 'disabled' : ''}>
                    ${isOn ? 'Turn OFF' : 'Turn ON'}
                </button>
                <button class="btn secondary" data-action="disable" data-zone="${zone.zone_id}" data-disabled="${isDisabled ? 'false' : 'true'}">
                    ${isDisabled ? 'Enable Zone' : 'Disable Zone'}
                </button>
            </div>
            <a href="/zone/${zone.zone_id}"><button class="btn" type="button">Open Zone</button></a>
        </div>
    `;
    return card;
}

async function refreshHome() {
    try {
        const data = await window.AppCommon.fetchDashboard();
        document.getElementById('tempValue').textContent = window.AppCommon.formatValue(data.environment.temperature, 'C');
        document.getElementById('humidityValue').textContent = window.AppCommon.formatValue(data.environment.humidity, '%');
        document.getElementById('hotspotStatus').textContent = data.system_health.hotspot_status || '--';
        document.getElementById('dbUptime').textContent = data.system_health.db_uptime || '--';

        renderSensorSummary(data.system_health.sensor_status || {});

        const grid = document.getElementById('homeZonesGrid');
        grid.innerHTML = '';
        data.zones.forEach((zone) => {
            grid.appendChild(renderZoneCard(zone));
        });

        grid.querySelectorAll('button[data-action="valve"]').forEach((btn) => {
            btn.addEventListener('click', async () => {
                const zoneId = Number(btn.dataset.zone);
                const state = btn.dataset.state;
                await window.AppCommon.toggleValve(zoneId, state, `home-timer-${zoneId}`);
                await refreshHome();
            });
        });

        grid.querySelectorAll('button[data-action="disable"]').forEach((btn) => {
            btn.addEventListener('click', async () => {
                const zoneId = Number(btn.dataset.zone);
                const disabled = btn.dataset.disabled === 'true';
                await setZoneDisabled(zoneId, disabled);
                await refreshHome();
            });
        });
    } catch (err) {
        console.error(err);
    }
}

window.addEventListener('DOMContentLoaded', async () => {
    await refreshHome();
    setInterval(refreshHome, 5000);
});
