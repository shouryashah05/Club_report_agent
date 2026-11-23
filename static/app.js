// ---------------------------------------------------------------------------
// View switcher (segmented control in toolbar)
// ---------------------------------------------------------------------------
document.querySelectorAll('.seg-btn[data-view]').forEach(btn => {
    btn.addEventListener('click', () => {
        if (btn.disabled) return;
        document.querySelectorAll('.seg-btn[data-view]').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        document.querySelectorAll('.view').forEach(v => { v.classList.remove('active'); v.classList.add('hidden'); });
        const target = document.getElementById(btn.dataset.view === 'results' ? 'results-section' : 'upload-section');
        target.classList.remove('hidden');
        target.classList.add('active');
    });
});

// ---------------------------------------------------------------------------
// File mapping: match dropped / selected files to expected names
// ---------------------------------------------------------------------------
const EXPECTED = ['events', 'registrations', 'attendance', 'feedback'];
const collected = {};          // key → File

function matchFile(file) {
    const name = file.name.toLowerCase().replace(/\s+/g, '_');
    for (const key of EXPECTED) {
        if (name.includes(key)) {
            collected[key] = file;
            return key;
        }
    }
    return null;
}

function refreshChecklist() {
    const list = document.getElementById('file-checklist');
    list.classList.remove('hidden');

    for (const key of EXPECTED) {
        const li = list.querySelector(`li[data-key="${key}"]`);
        const span = li.querySelector('.check');
        if (collected[key]) {
            span.textContent = '✓';
            li.classList.add('matched');
        } else {
            span.textContent = '✗';
            li.classList.remove('matched');
        }
    }

    const allReady = EXPECTED.every(k => collected[k]);
    document.getElementById('submit-btn').disabled = !allReady;
}

// ---------------------------------------------------------------------------
// Drop zone
// ---------------------------------------------------------------------------
const dropZone = document.getElementById('drop-zone');
const fileInput = document.getElementById('file-input');

function handleFiles(files) {
    const unmatched = [];
    for (const f of files) {
        if (!matchFile(f)) unmatched.push(f.name);
    }
    refreshChecklist();
    if (unmatched.length) {
        alert('Could not match these files to any expected name:\n' + unmatched.join(', ') +
              '\n\nExpected filenames containing: events, registrations, attendance, feedback');
    }
}

dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('drag-over'); });
dropZone.addEventListener('dragleave', e => { e.preventDefault(); dropZone.classList.remove('drag-over'); });
dropZone.addEventListener('drop', e => {
    e.preventDefault();
    dropZone.classList.remove('drag-over');
    handleFiles(e.dataTransfer.files);
});
fileInput.addEventListener('change', () => handleFiles(fileInput.files));

// ---------------------------------------------------------------------------
// Upload & analyze (custom files)
// ---------------------------------------------------------------------------
document.getElementById('upload-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!EXPECTED.every(k => collected[k])) return;

    const formData = new FormData();
    for (const key of EXPECTED) formData.append(key, collected[key]);

    await runAnalysis('/api/analyze', { method: 'POST', body: formData });
});

// ---------------------------------------------------------------------------
// Sample data (one-click)
// ---------------------------------------------------------------------------
document.getElementById('sample-btn').addEventListener('click', async () => {
    await runAnalysis('/api/analyze-sample', { method: 'POST' }, 'sample');
});

// ---------------------------------------------------------------------------
// Shared analysis runner
// ---------------------------------------------------------------------------
async function runAnalysis(url, fetchOpts, mode = 'upload') {
    const submitBtn  = document.getElementById('submit-btn');
    const sampleBtn  = document.getElementById('sample-btn');
    const spinner    = mode === 'sample'
        ? document.getElementById('sample-spinner')
        : document.getElementById('spinner');
    const btnText    = (mode === 'sample' ? sampleBtn : submitBtn).querySelector('span');
    const prevLabel  = btnText.textContent;

    submitBtn.disabled = true;
    sampleBtn.disabled = true;
    btnText.textContent = 'Processing...';
    spinner.style.display = 'inline-block';

    try {
        const response = await fetch(url, fetchOpts);

        if (!response.ok) {
            const error = await response.text();
            throw new Error(error);
        }

        const data = await response.json();
        renderDashboard(data);

        document.getElementById('upload-section').classList.remove('active');
        document.getElementById('upload-section').classList.add('hidden');
        document.getElementById('results-section').classList.remove('hidden');
        document.getElementById('results-section').classList.add('active');
        // Enable and activate the Results tab in the segmented control
        const resultsBtn = document.querySelector('.seg-btn[data-view="results"]');
        if (resultsBtn) { resultsBtn.disabled = false; resultsBtn.classList.add('active'); }
        const uploadBtn = document.querySelector('.seg-btn[data-view="upload"]');
        if (uploadBtn) uploadBtn.classList.remove('active');
    } catch (err) {
        alert('Error analyzing data: ' + err.message);
        console.error(err);
    } finally {
        submitBtn.disabled = false;
        sampleBtn.disabled = false;
        btnText.textContent = prevLabel;
        spinner.style.display = 'none';
    }
}

// ---------------------------------------------------------------------------
// Dashboard rendering (unchanged)
// ---------------------------------------------------------------------------
function renderDashboard(data) {
    renderTable(data.ranked);
    renderExplanations(data.explanations, data.ranked);
    renderNotes(data.notes);
    renderCharts(data.metrics, data.ranked);
}

function renderTable(ranked) {
    const tbody = document.querySelector('#rankings-table tbody');
    tbody.innerHTML = '';

    ranked.forEach(row => {
        const tr = document.createElement('tr');
        
        const attRate = row.attendance_rate !== null ? `${(row.attendance_rate * 100).toFixed(0)}%` : 'N/A';
        const fbScore = row.avg_feedback !== null ? `${row.avg_feedback.toFixed(2)}/5` : 'N/A';
        const score = row.performance_score !== null ? row.performance_score.toFixed(2) : '0.00';

        tr.innerHTML = `
            <td><span class="rank-badge">${row.rank}</span></td>
            <td><strong>${row.club_name}</strong></td>
            <td>${row.num_events}</td>
            <td>${attRate}</td>
            <td>${fbScore}</td>
            <td class="score-cell">${score}</td>
        `;
        tbody.appendChild(tr);
    });
}

function renderExplanations(explanations, ranked) {
    const container = document.getElementById('explanations-container');
    container.innerHTML = '';
    
    ranked.forEach(row => {
        const text = explanations[row.club_name] || "No explanation provided.";
        const div = document.createElement('div');
        div.className = 'explanation-item';
        div.innerHTML = `
            <h4><span class="rank-badge">${row.rank}</span> ${row.club_name}</h4>
            <p>${text}</p>
        `;
        container.appendChild(div);
    });
}

function renderNotes(notes) {
    const ul = document.getElementById('quality-notes');
    ul.innerHTML = '';
    
    if (!notes || notes.length === 0) {
        ul.innerHTML = '<li>No data gaps detected — all events had complete registration, attendance, and feedback records.</li>';
        return;
    }
    
    notes.forEach(note => {
        const li = document.createElement('li');
        li.textContent = note;
        ul.appendChild(li);
    });
}

function renderCharts(metrics, ranked) {
    Chart.defaults.color = '#86868B';
    Chart.defaults.font.family = "'Inter', -apple-system, sans-serif";

    // Score Chart (Bar)
    const scoreCtx = document.getElementById('scoreChart').getContext('2d');
    
    // Sort clubs by score for the chart
    const labels = ranked.map(r => r.club_name);
    const scores = ranked.map(r => r.performance_score);
    
    new Chart(scoreCtx, {
        type: 'bar',
        data: {
            labels: labels,
            datasets: [{
                label: 'Composite Score',
                data: scores,
                backgroundColor: 'rgba(10, 132, 255, 0.7)',
                borderColor: 'rgba(10, 132, 255, 1)',
                borderWidth: 1,
                borderRadius: 6
            }]
        },
        options: {
            responsive: true,
            plugins: {
                legend: { display: false }
            },
            scales: {
                y: {
                    beginAtZero: true,
                    grid: { color: 'rgba(255, 255, 255, 0.05)' }
                },
                x: {
                    grid: { display: false }
                }
            }
        }
    });

    // Scatter Chart (Attendance vs Feedback)
    const scatterCtx = document.getElementById('scatterChart').getContext('2d');
    
    // Filter out nulls
    const validMetrics = metrics.filter(m => m.attendance_rate !== null && m.avg_feedback !== null);
    
    const scatterData = validMetrics.map(m => ({
        x: m.attendance_rate * 100,
        y: m.avg_feedback,
        club: m.club_name
    }));

    new Chart(scatterCtx, {
        type: 'scatter',
        data: {
            datasets: [{
                label: 'Clubs',
                data: scatterData,
                backgroundColor: 'rgba(48, 209, 88, 0.7)',
                pointRadius: 8,
                pointHoverRadius: 10
            }]
        },
        options: {
            responsive: true,
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        label: (ctx) => {
                            const dataPoint = ctx.raw;
                            return `${dataPoint.club}: ${dataPoint.x.toFixed(0)}% Att, ${dataPoint.y.toFixed(1)}/5 FB`;
                        }
                    }
                }
            },
            scales: {
                x: {
                    title: { display: true, text: 'Attendance Rate (%)' },
                    grid: { color: 'rgba(255, 255, 255, 0.05)' },
                    min: 0,
                    max: 100
                },
                y: {
                    title: { display: true, text: 'Average Feedback' },
                    grid: { color: 'rgba(255, 255, 255, 0.05)' },
                    min: 0,
                    max: 5
                }
            }
        }
    });
}
