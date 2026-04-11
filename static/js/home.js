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
    const flowRate = zone.flow_rate_lpm != null ? `${Number(zone.flow_rate_lpm).toFixed(1)} L/min` : '--';
    const modeLabels = { ml: 'ML Prediction', scheduled: 'Scheduled', manual: 'Manual' };
    const activeMode = zone.irr_mode || localStorage.getItem(`irrigation_mode_${zone.zone_id}`) || 'ml';
    const modeName = modeLabels[activeMode] || '--';

    const card = document.createElement('article');
    card.className = 'home-zone-card home-zone-card--clickable';
    card.innerHTML = `
        <div class="home-zone-top">
            <h3 class="zone-title">Zone ${zone.zone_id}</h3>
            <span class="zone-badge ${badgeClass}">${badgeText}</span>
        </div>
        <div class="meta-row"><span>Crop Target</span><strong>${zone.crop_target_name || '--'}</strong></div>
        <div class="meta-row"><span>Moisture</span><strong>${window.AppCommon.formatValue(moisture, '%')}</strong></div>
        <div class="meta-row"><span>Target</span><strong>${zone.target_moisture ?? '--'}%</strong></div>
        <div class="meta-row"><span>Flow Rate</span><strong>${flowRate}</strong></div>
        <div class="meta-row"><span>Irr. Mode</span><strong>${modeName}</strong></div>
        <div class="gauge"><div class="gauge-fill" style="width:${gaugeValue}%; background:${window.AppCommon.moistureColor(moisture)};"></div></div>
        <div class="zone-card-footer">
            <label class="zone-toggle-wrap" title="${isDisabled ? 'Enable zone' : 'Disable zone'}" onclick="event.stopPropagation()">
                <span class="zone-toggle-label">Enabled</span>
                <input type="checkbox" class="zone-toggle-input" data-action="disable" data-zone="${zone.zone_id}" ${isDisabled ? '' : 'checked'}>
                <span class="zone-toggle-slider"></span>
            </label>
        </div>
    `;

    // Clicking the card (not the toggle) navigates to zone detail.
    card.addEventListener('click', () => {
        window.location.href = `/zone/${zone.zone_id}`;
    });

    return card;
}

async function triggerRefresh() {
    const btn  = document.getElementById('refreshBtn');
    const icon = document.getElementById('refreshIcon');
    if (btn.disabled) return;
    btn.disabled = true;
    icon.style.animation = 'spin 0.7s linear infinite';
    await refreshHome();
    icon.style.animation = '';
    btn.disabled = false;
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

        grid.querySelectorAll('input[data-action="disable"]').forEach((toggle) => {
            toggle.addEventListener('change', async () => {
                const zoneId = Number(toggle.dataset.zone);
                const disabled = !toggle.checked;
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
