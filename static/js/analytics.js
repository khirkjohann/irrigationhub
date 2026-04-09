let trendChart;

function compactLabel(timestamp) {
    if (!timestamp) return '--';
    // Expected format: YYYY-MM-DD HH:MM:SS
    return timestamp.length >= 16 ? timestamp.slice(5, 16) : timestamp;
}

async function loadTrendData(hours = 24) {
    const res = await fetch(`/api/trends?hours=${hours}`);
    const payload = await res.json();
    const labels = payload.data.map((item) => compactLabel(item.timestamp));

    const datasets = [1, 2, 3, 4].map((zone) => ({
        label: `Zone ${zone}`,
        data: payload.data.map((item) => item[`soil_moisture_${zone}`]),
        borderWidth: 2,
        fill: false,
        pointRadius: 0,
        tension: 0.2,
    }));

    if (!trendChart) {
        const ctx = document.getElementById('trendChart').getContext('2d');
        trendChart = new Chart(ctx, {
            type: 'line',
            data: { labels, datasets },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
                scales: {
                    y: { min: 0, max: 100, title: { display: true, text: 'Moisture %' } },
                    x: { ticks: { maxTicksLimit: 8, maxRotation: 0 } },
                },
            },
        });
    } else {
        trendChart.data.labels = labels;
        trendChart.data.datasets = datasets;
        trendChart.update();
    }
}

window.loadTrendData = loadTrendData;
window.addEventListener('DOMContentLoaded', async () => {
    await loadTrendData(24);
});
