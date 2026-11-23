document.getElementById('upload-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    
    const eventsFile = document.getElementById('events').files[0];
    const regFile = document.getElementById('registrations').files[0];
    const attFile = document.getElementById('attendance').files[0];
    const fbFile = document.getElementById('feedback').files[0];

    if (!eventsFile || !regFile || !attFile || !fbFile) {
        alert("Please select all 4 CSV files.");
        return;
    }

    const formData = new FormData();
    formData.append('events', eventsFile);
    formData.append('registrations', regFile);
    formData.append('attendance', attFile);
    formData.append('feedback', fbFile);

    const submitBtn = document.getElementById('submit-btn');
    const spinner = document.getElementById('spinner');
    const btnText = submitBtn.querySelector('span');
    
    submitBtn.disabled = true;
    btnText.textContent = 'Processing...';
    spinner.style.display = 'inline-block';

    try {
        const response = await fetch('/api/analyze', {
            method: 'POST',
            body: formData
        });

        if (!response.ok) {
            const error = await response.text();
            throw new Error(error);
        }

        const data = await response.json();
        renderDashboard(data);
        
        document.getElementById('upload-section').classList.remove('active');
        document.getElementById('upload-section').classList.add('hidden');
        document.getElementById('results-section').classList.remove('hidden');
    } catch (err) {
        alert("Error analyzing data: " + err.message);
        console.error(err);
    } finally {
        submitBtn.disabled = false;
        btnText.textContent = 'Analyze Data';
        spinner.style.display = 'none';
    }
});

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
    Chart.defaults.color = '#94a3b8';
    Chart.defaults.font.family = "'Inter', sans-serif";

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
                backgroundColor: 'rgba(59, 130, 246, 0.8)',
                borderColor: 'rgba(59, 130, 246, 1)',
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
                    grid: { color: 'rgba(255, 255, 255, 0.1)' }
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
                backgroundColor: 'rgba(16, 185, 129, 0.8)',
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
                    grid: { color: 'rgba(255, 255, 255, 0.1)' },
                    min: 0,
                    max: 100
                },
                y: {
                    title: { display: true, text: 'Average Feedback' },
                    grid: { color: 'rgba(255, 255, 255, 0.1)' },
                    min: 0,
                    max: 5
                }
            }
        }
    });
}
