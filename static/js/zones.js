const cropOptions = ['Corn', 'Cassava', 'Peanuts', 'Custom'];

async function saveZoneProfile(zoneId, cropTargets) {
    const crop = document.getElementById(`crop-${zoneId}`).value;
    let target = cropTargets[crop];

    if (crop === 'Custom') {
        const customVal = Number(document.getElementById(`custom-target-${zoneId}`).value);
        if (Number.isNaN(customVal) || customVal < 0 || customVal > 100) return;
        target = customVal;
    }

    await fetch(`/api/zone/${zoneId}/profile`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ crop, target_moisture: target }),
    });

    await refreshZones();
}

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
                <div class="zone-controls">
                    <label>Crop Profile</label>
                    <select id="crop-${zone.zone_id}">
                        ${cropOptions.map((c) => `<option value="${c}" ${zone.crop === c ? 'selected' : ''}>${c}</option>`).join('')}
                    </select>
                    <div id="custom-wrap-${zone.zone_id}" style="display:${zone.crop === 'Custom' ? 'block' : 'none'};">
                        <label>Custom Target Moisture (%)</label>
                        <input type="number" id="custom-target-${zone.zone_id}" min="0" max="100" value="${zone.target_moisture ?? ''}">
                    </div>
                    <div class="small-text">Target Moisture: ${zone.target_moisture ?? '--'}%</div>
                    <button class="btn secondary save-profile" data-zone="${zone.zone_id}">Save Zone Profile</button>
                    <hr>
                    <button class="toggle-btn ${isOn ? 'toggle-on' : 'toggle-off'} valve-toggle" data-zone="${zone.zone_id}" data-state="${isOn ? 'OFF' : 'ON'}">
                        ${isOn ? 'Valve ON (Tap to OFF)' : 'Valve OFF (Tap to ON)'}
                    </button>
                    <label>Failsafe timer (minutes)</label>
                    <input type="number" id="zones-timer-${zone.zone_id}" min="0" value="10">
                </div>
            `;
            grid.appendChild(card);

            const selectEl = document.getElementById(`crop-${zone.zone_id}`);
            const wrap = document.getElementById(`custom-wrap-${zone.zone_id}`);
            selectEl.addEventListener('change', () => {
                wrap.style.display = selectEl.value === 'Custom' ? 'block' : 'none';
            });
        });

        grid.querySelectorAll('.save-profile').forEach((btn) => {
            btn.addEventListener('click', async () => {
                const zoneId = Number(btn.dataset.zone);
                await saveZoneProfile(zoneId, data.crop_targets);
            });
        });

        grid.querySelectorAll('.valve-toggle').forEach((btn) => {
            btn.addEventListener('click', async () => {
                const zoneId = Number(btn.dataset.zone);
                await window.AppCommon.toggleValve(zoneId, btn.dataset.state, `zones-timer-${zoneId}`);
                await refreshZones();
            });
        });
    } catch (err) {
        console.error(err);
    }
}

window.addEventListener('DOMContentLoaded', refreshZones);
