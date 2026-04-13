async function refreshZones() {
    try {
        const data = await window.AppCommon.fetchDashboard();
        const grid = document.getElementById('zonesConfigGrid');
        grid.innerHTML = '';

        data.zones.forEach((zone) => {
            const isOn = zone.valve_status === 'ON';
            const card = document.createElement('div');
            card.className = 'zone-card';
            card.innerHTML = `
                <h3 class="no-margin">Zone ${zone.zone_id}</h3>
                <div class="meta-row"><span>Current Moisture</span><strong>${window.AppCommon.formatValue(zone.moisture, '%')}</strong></div>
                <div class="meta-row"><span>Target Moisture</span><strong>${zone.target_moisture != null ? zone.target_moisture + '%' : '--'}</strong></div>
                <div class="meta-row"><span>Trigger Threshold</span><strong>${zone.threshold_gap != null ? zone.threshold_gap + '%' : '--'}</strong></div>
                <div class="meta-row"><span>Valve</span><strong style="color:${isOn ? '#16a34a' : '#6b7280'}">${isOn ? 'ON' : 'OFF'}</strong></div>
                <div class="zone-controls">
                    <a class="btn secondary" href="/zone/${zone.zone_id}" style="display:inline-block;text-align:center;">Manage Zone</a>
                </div>
            `;
            grid.appendChild(card);
        });
    } catch (err) {
        console.error(err);
    }
}

window.addEventListener('DOMContentLoaded', refreshZones);
