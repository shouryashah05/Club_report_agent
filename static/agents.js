// AI Agents page — talks to /api/assistant/* and /api/council/*

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

async function post(url, body = {}) {
    const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (!res.ok) throw new Error(`${url} failed: ${res.status}`);
    return res.json();
}

// ---------------- Tabs + model badge ----------------
document.querySelectorAll('.tab').forEach((tab) => {
    tab.addEventListener('click', () => {
        document.querySelectorAll('.tab, .tab-panel').forEach((el) => el.classList.remove('active'));
        tab.classList.add('active');
        $(tab.dataset.tab).classList.add('active');
    });
});

function refreshBadge() {
    fetch('/api/status').then((r) => r.json()).then((s) => {
        const badge = $('llm-badge');
        badge.className = 'llm-badge ' + (s.llm ? 'live' : 'offline');
        badge.textContent = s.llm ? `● Gemini live · ${s.model}`
            : s.quota_blocked ? `● Gemini quota used up · offline for ${Math.ceil(s.retry_in_seconds / 60)} min`
            : '● Offline mode (no Gemini key)';
    });
}
refreshBadge();

// =====================================================================
// Ask the Agent
// =====================================================================
let sessionId = 'web-' + Math.random().toString(36).slice(2, 8);

const SUGGESTIONS = [
    'Which club performed the best?',
    'Tell me about IoT Club',
    'What about its events?',
    'Remember that IoT Club workshops clashed with mid-semester exams in November',
    'Why is IoT Club ranked last?',
    'How is the score calculated?',
    'Is Robotics Club’s first place reliable?',
];

function renderChips() {
    $('chips').innerHTML = SUGGESTIONS.map((s) => `<button class="chip" type="button">${esc(s)}</button>`).join('');
    $('chips').querySelectorAll('.chip').forEach((chip) => chip.addEventListener('click', () => ask(chip.textContent)));
}

function addBubble(role, text, tools) {
    const div = document.createElement('div');
    div.className = `bubble ${role}`;
    const body = esc(text).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/\n/g, '<br>');
    div.innerHTML = (tools && tools.length ? `<span class="tool-used">⚙ called ${[...new Set(tools)].map((t) => esc(t) + '()').join(', ')}</span>` : '') + body;
    $('chat').appendChild(div);
    $('chat').scrollTop = $('chat').scrollHeight;
    return div;
}

function stepClass(step) {
    if (step.includes('memory')) return 'memory';
    if (step === 'function call') return 'call';
    if (step === 'observation') return 'obs';
    if (step === 'fallback') return 'fallback';
    return '';
}

function renderTrace(trace) {
    $('trace').innerHTML = trace.map((t) => `
        <li><span class="step ${stepClass(t.step.toLowerCase())}">${esc(t.step)}</span>
            <span class="detail">${esc(t.detail)}</span>
            ${t.data && (Array.isArray(t.data) ? t.data.length : true) ? `<pre class="json">${esc(JSON.stringify(t.data, null, 2))}</pre>` : ''}
        </li>`).join('');
}

function renderMemory(stm, ltm, recalledIds = []) {
    if (stm) {
        $('stm').innerHTML = `
            <div class="muted" style="margin-bottom:.35rem">${stm.turns.length} of max ${stm.window_size} turns replayed to the model:</div>
            ${stm.turns.map((t) => `<div class="stm-turn">🧑 ${esc(t.user)} → <code>${esc(t.tools.join(', ') || 'no tool')}</code></div>`).join('') || '<div class="muted">Empty.</div>'}`;
    }
    $('ltm').innerHTML = ltm.length
        ? ltm.map((m) => `<li class="${recalledIds.includes(m.id) ? 'recalled' : ''}">#${m.id} ${esc(m.text)}
            <small>${esc(m.club || 'general')} · saved ${esc(m.saved_at)}${recalledIds.includes(m.id) ? ' · <b>recalled now</b>' : ''}</small></li>`).join('')
        : '<li class="muted">Nothing stored yet.</li>';
}

function renderTools(tools, active = []) {
    $('tools').innerHTML = Object.entries(tools).map(([name, t]) => `
        <li class="${active.includes(name) ? 'active' : ''}"><code>${esc(name)}(${t.parameters.join(', ')})</code> — ${esc(t.description)}</li>`).join('');
}

async function ask(question) {
    question = question.trim();
    if (!question) return;
    addBubble('user', question);
    $('question').value = '';
    $('ask-btn').disabled = true;
    const thinking = addBubble('agent thinking', 'Thinking…');
    try {
        const res = await post('/api/assistant/ask', { question, session_id: sessionId });
        thinking.remove();
        addBubble('agent', res.answer, res.tools_used);
        renderTrace(res.trace);
        renderMemory(res.short_term_memory, res.long_term_memory, res.recalled_ids);
        renderTools(res.tools, res.tools_used);
        refreshBadge();
    } catch (err) {
        thinking.remove();
        addBubble('agent', 'Error: ' + err.message);
    }
    $('ask-btn').disabled = false;
}

$('ask-form').addEventListener('submit', (e) => { e.preventDefault(); ask($('question').value); });

$('new-session').addEventListener('click', async () => {
    await post('/api/assistant/reset', { question: '', session_id: sessionId });
    sessionId = 'web-' + Math.random().toString(36).slice(2, 8);
    $('chat').innerHTML = '';
    $('trace').innerHTML = '<li class="muted">New session: short-term memory is empty, long-term memory is kept.</li>';
    $('stm').innerHTML = '<div class="muted">Empty.</div>';
});

$('clear-ltm').addEventListener('click', async () => {
    await post('/api/assistant/memory/clear');
    renderMemory(null, []);
});

renderChips();
fetch('/api/assistant/memory').then((r) => r.json()).then((res) => {
    renderTools(res.tools);
    renderMemory(null, res.memories);
});

// =====================================================================
// Agent Council
// =====================================================================
const AGENT_COLORS = {
    'Coordinator': '#60a5fa', 'DataAgent': '#34d399', 'AnalystAgent': '#fbbf24',
    'AuditorAgent': '#f87171', 'WriterAgent': '#c084fc', 'Faculty Coordinator': '#f472b6',
};
const initials = (n) => n === 'Faculty Coordinator' ? '👤' : n.replace('Agent', '').slice(0, 2).toUpperCase();
let currentRun = null;

function renderAgents(agents) {
    $('agents').innerHTML = agents.map((a) => `
        <div class="agent" id="agent-${a.name.replace(/\s/g, '')}" style="--agent-color:${AGENT_COLORS[a.name]}">
            <div class="avatar">${initials(a.name)}</div>
            <div class="name">${esc(a.name)}</div>
            <div class="role">${esc(a.role)}</div>
            <div class="goal">${esc(a.goal)}</div>
            <div class="agent-tools">${a.tools.length ? a.tools.map((t) => `<span>${esc(t)}</span>`).join('') : '<span>no tools</span>'}</div>
            ${a.brain && a.brain !== 'rules' && a.brain !== 'human' ? `<div class="brain">🧠 ${esc(a.brain)}</div>` : ''}
        </div>`).join('');
}

function highlight(name) {
    document.querySelectorAll('.agent').forEach((a) => a.classList.remove('speaking'));
    if (name) $('agent-' + name.replace(/\s/g, ''))?.classList.add('speaking');
}

function appendMessage(m) {
    highlight(m.sender);
    $('messages').insertAdjacentHTML('beforeend', `<div class="msg" style="--agent-color:${AGENT_COLORS[m.sender] || '#94a3b8'}">
        <div class="msg-head"><span class="from">${esc(m.sender)}</span><span class="to">→ ${esc(m.receiver)}</span>
            <span class="perf ${esc(m.performative)}">${esc(m.performative)}</span><span class="time">${esc(m.time)}</span></div>
        <div class="msg-body">${esc(m.content)}</div></div>`);
    $('messages').scrollTop = $('messages').scrollHeight;
}

function renderFindings(findings) {
    $('findings').innerHTML = findings.map((f) => {
        const cls = /sensitive|unreliable|drops|moves/i.test(f) ? 'sensitive' : /robust|holds|stable/i.test(f) ? 'robust' : '';
        return `<li class="${cls}">${esc(f)}</li>`;
    }).join('') || '<li>No concerns raised.</li>';
}

// Minimal markdown -> HTML for the generated report (headings, tables, bullets, bold, italics)
function md(text) {
    let html = '', inTable = false, inList = false;
    const inline = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/(^|\s)_(.+?)_(?=\s|$)/g, '$1<em>$2</em>');
    for (const line of text.split('\n')) {
        if (line.startsWith('|')) {
            if (/^\|[-|\s]+\|$/.test(line)) continue;
            const cells = line.split('|').slice(1, -1).map((c) => c.trim());
            if (!inTable) { html += '<table><tr>' + cells.map((c) => `<th>${inline(c)}</th>`).join('') + '</tr>'; inTable = true; }
            else html += '<tr>' + cells.map((c) => `<td>${inline(c)}</td>`).join('') + '</tr>';
            continue;
        }
        if (inTable) { html += '</table>'; inTable = false; }
        if (line.startsWith('- ')) { if (!inList) { html += '<ul>'; inList = true; } html += `<li>${inline(line.slice(2))}</li>`; continue; }
        if (inList) { html += '</ul>'; inList = false; }
        if (line.startsWith('## ')) html += `<h2>${inline(line.slice(3))}</h2>`;
        else if (line.startsWith('# ')) html += `<h1>${inline(line.slice(2))}</h1>`;
        else if (line.trim()) html += `<p>${inline(line)}</p>`;
    }
    return html + (inTable ? '</table>' : '') + (inList ? '</ul>' : '');
}

// Reads the NDJSON stream so each agent message appears the moment it is sent
async function streamCouncil(onEvent) {
    const res = await fetch('/api/council/run', { method: 'POST' });
    if (!res.ok) throw new Error(`council failed: ${res.status}`);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let nl;
        while ((nl = buffer.indexOf('\n')) >= 0) {
            const line = buffer.slice(0, nl).trim();
            buffer = buffer.slice(nl + 1);
            if (line) onEvent(JSON.parse(line));
        }
    }
}

$('run-council').addEventListener('click', async () => {
    const btn = $('run-council');
    btn.disabled = true; btn.textContent = 'Agents working…';
    ['approval-card', 'findings-card', 'report-card'].forEach((id) => $(id).classList.add('hidden'));
    $('approval-status').classList.add('hidden');
    $('messages').innerHTML = '';
    currentRun = null;
    try {
        await streamCouncil((ev) => {
            if (ev.type === 'message') appendMessage(ev.message);
            else if (ev.type === 'error') throw new Error(ev.error);
            else if (ev.type === 'result') {
                currentRun = ev.run_id;
                renderAgents(ev.agents);
                renderFindings(ev.findings);
                $('report').innerHTML = md(ev.report_md);
                ['approval-card', 'findings-card', 'report-card'].forEach((id) => $(id).classList.remove('hidden'));
                $('approve').disabled = $('reject').disabled = false;
            }
        });
    } catch (err) {
        $('messages').insertAdjacentHTML('beforeend', `<p class="muted">Error: ${esc(err.message)}</p>`);
    }
    highlight(null);
    refreshBadge();
    btn.disabled = false; btn.textContent = '↻ Run Again';
});

async function decide(approve) {
    if (!currentRun) return;
    $('approve').disabled = $('reject').disabled = true;
    const res = await post('/api/council/decision', { run_id: currentRun, approve });
    currentRun = null;
    res.messages.forEach(appendMessage);
    highlight(null);
    const pill = $('approval-status');
    pill.className = `status-pill ${res.status}`;
    pill.textContent = {
        published: '✔ Approved: report published to output/approved_report.md',
        rejected: '✖ Rejected: publication blocked, nothing written',
        expired: '⚠ This run expired (server restarted?). Run the council again.',
    }[res.status] || res.status;
}

$('approve').addEventListener('click', () => decide(true));
$('reject').addEventListener('click', () => decide(false));

// Deep links: agents.html?q=First+question|Follow-up  or  agents.html?run=council
window.addEventListener('load', async () => {
    const params = new URLSearchParams(location.search);
    if (params.get('run') === 'council') {
        document.querySelector('[data-tab=council]').click();
        $('run-council').click();
    } else if (params.get('q')) {
        for (const q of params.get('q').split('|')) await ask(q);
    }
});

// Show the team before the first run
renderAgents([
    { name: 'Coordinator', role: 'Manager', goal: 'Decompose the request and deliver an approved report', tools: ['dispatch', 'approval_gate'] },
    { name: 'DataAgent', role: 'Data Engineer', goal: 'Collect and validate all event data', tools: ['collect_data', 'validate_and_clean'] },
    { name: 'AnalystAgent', role: 'Data Analyst', goal: 'Score and rank every club fairly', tools: ['calculate_metrics', 'rank_clubs'] },
    { name: 'AuditorAgent', role: 'Critic / Reviewer', goal: 'Challenge rankings that rest on weak data', tools: [], brain: 'Gemini' },
    { name: 'WriterAgent', role: 'Technical Writer', goal: 'Explain the results in a clear report', tools: ['explain_rankings', 'generate_report'], brain: 'Gemini' },
    { name: 'Faculty Coordinator', role: 'Human-in-the-loop', goal: 'Approve or reject publication', tools: [] },
]);
