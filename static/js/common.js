async function fetchDashboard() {
    const res = await fetch('/api/dashboard');
    if (!res.ok) throw new Error('Failed to load dashboard');
    return res.json();
}

function formatValue(value, unit) {
    return value === null || value === undefined ? `-- ${unit}` : `${Number(value).toFixed(1)} ${unit}`;
}

function moistureColor(value) {
    if (value === null || value === undefined) return '#9ca3af';
    if (value < 30) return '#dc2626';
    if (value < 60) return '#3b82f6';
    return '#1d4ed8';
}

async function toggleValve(valveId, state, timerInputId) {
    const timerInput = document.getElementById(timerInputId);
    const autoClose = Number(timerInput ? timerInput.value : 0);

    const res = await fetch(`/api/valve/${valveId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            state,
            auto_close_minutes: state === 'ON' ? (Number.isNaN(autoClose) ? 0 : autoClose) : 0,
        }),
    });

    if (!res.ok) {
        const data = await res.json();
        throw new Error(data.error || 'Failed to change valve state');
    }
}

window.AppCommon = {
    fetchDashboard,
    formatValue,
    moistureColor,
    toggleValve,
};
