// =====================================================================
// Robotpos Call Center - Admin Panel Application
// =====================================================================

// ===== AUTH HELPER =====
async function apiFetch(url, opts = {}) {
    const res = await fetch(url, opts);
    if (res.status === 401) { window.location.href = '/login'; return null; }
    return res;
}

// ===== NAVIGATION =====
function navigate(page) {
    document.querySelectorAll('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.page === page));
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.getElementById('page-' + page).classList.add('active');
    if (page === 'dashboard') { loadStats(); loadRecentCalls(); loadHourlyChart(); }
    if (page === 'calls') loadCalls();
    if (page === 'settings') loadPrompt();
    if (page === 'users') loadUsers();
}

async function doLogout() {
    await fetch('/api/auth/logout', { method: 'POST' });
    window.location.href = '/login';
}

// ===== UTILS =====
function esc(s) { if (!s) return ''; const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
function formatDate(iso) {
    if (!iso) return '-';
    const d = new Date(iso);
    return d.toLocaleDateString('tr-TR') + ' ' + d.toLocaleTimeString('tr-TR', { hour: '2-digit', minute: '2-digit' });
}
function formatTime(iso) {
    if (!iso) return '-';
    return new Date(iso).toLocaleTimeString('tr-TR', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}
function formatDuration(sec) {
    if (!sec) return '-';
    const m = Math.floor(sec / 60), s = sec % 60;
    return m > 0 ? `${m}dk ${s}sn` : `${s}sn`;
}
function statusLabel(s) { return { completed: 'Tamamlandi', in_progress: 'Devam Ediyor', failed: 'Basarisiz' }[s] || s; }
function sentimentLabel(s) { return { pozitif: 'Pozitif', negatif: 'Negatif', notr: 'Notr' }[s] || 'Notr'; }
function today() { return new Date().toISOString().split('T')[0]; }

// ===== DASHBOARD =====
async function loadStats() {
    try {
        const res = await apiFetch('/api/stats');
        if (!res) return;
        const s = await res.json();
        document.getElementById('wTotal').textContent = s.total_calls;
        document.getElementById('wToday').textContent = s.today_calls;
        document.getElementById('wActive').textContent = s.active_calls;
        document.getElementById('wAvgDur').textContent = s.avg_duration_today || s.avg_duration;
        document.getElementById('activeCount').textContent = s.active_calls;

        // Sentiments
        const sent = s.sentiments || { pozitif: 0, negatif: 0, notr: 0 };
        const sentTotal = (sent.pozitif + sent.negatif + sent.notr) || 1;
        document.getElementById('sPozitif').textContent = sent.pozitif;
        document.getElementById('sNotr').textContent = sent.notr;
        document.getElementById('sNegatif').textContent = sent.negatif;
        document.getElementById('sPozitifBar').style.width = (sent.pozitif / sentTotal * 100) + '%';
        document.getElementById('sNotrBar').style.width = (sent.notr / sentTotal * 100) + '%';
        document.getElementById('sNegatifBar').style.width = (sent.negatif / sentTotal * 100) + '%';
    } catch (e) { console.error('Stats error:', e); }
}

async function loadRecentCalls() {
    try {
        const t = today();
        const res = await apiFetch(`/api/calls?page=1&limit=8&date_from=${t}&date_to=${t}`);
        if (!res) return;
        const data = await res.json();
        const tbody = document.getElementById('recentBody');
        if (!data.calls || !data.calls.length) {
            tbody.innerHTML = '<tr><td colspan="5" class="no-data">Bugun arama yok</td></tr>';
            return;
        }
        tbody.innerHTML = data.calls.map(c => `
            <tr onclick="showDetail('${c.call_sid}')">
                <td>${esc(c.caller_number) || '-'}</td>
                <td>${formatTime(c.start_time)}</td>
                <td>${formatDuration(c.duration_seconds)}</td>
                <td><span class="badge badge-${c.sentiment || 'notr'}">${sentimentLabel(c.sentiment)}</span></td>
                <td>${esc((c.summary || '').substring(0, 60))}${(c.summary || '').length > 60 ? '...' : ''}</td>
            </tr>
        `).join('');
    } catch (e) { console.error('Recent calls error:', e); }
}

// ===== HOURLY CHART =====
async function loadHourlyChart() {
    const df = document.getElementById('chartDateFrom').value;
    const dt = document.getElementById('chartDateTo').value;
    try {
        const res = await apiFetch(`/api/stats/hourly?date_from=${df}&date_to=${dt}`);
        if (!res) return;
        const data = await res.json();
        drawHourlyChart(data);
    } catch (e) { console.error('Hourly chart error:', e); }
}

function drawHourlyChart(data) {
    const canvas = document.getElementById('hourlyChart');
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.parentElement.getBoundingClientRect();
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    const ctx = canvas.getContext('2d');
    ctx.scale(dpr, dpr);
    const W = rect.width, H = rect.height;

    ctx.clearRect(0, 0, W, H);

    const padL = 44, padR = 16, padT = 16, padB = 36;
    const chartW = W - padL - padR;
    const chartH = H - padT - padB;

    const counts = data.map(d => d.count);
    const maxVal = Math.max(...counts, 1);
    // Nice Y scale
    const step = maxVal <= 5 ? 1 : maxVal <= 20 ? 5 : Math.ceil(maxVal / 5 / 5) * 5;
    const yMax = Math.ceil(maxVal / step) * step || step;

    // Grid lines
    ctx.strokeStyle = '#1e293b';
    ctx.lineWidth = 1;
    ctx.fillStyle = '#64748b';
    ctx.font = '11px -apple-system, BlinkMacSystemFont, sans-serif';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    const gridLines = Math.min(6, yMax / step);
    for (let i = 0; i <= gridLines; i++) {
        const val = Math.round(i * step);
        if (val > yMax) break;
        const y = padT + chartH - (val / yMax) * chartH;
        ctx.beginPath();
        ctx.moveTo(padL, y);
        ctx.lineTo(W - padR, y);
        ctx.stroke();
        ctx.fillText(val.toString(), padL - 8, y);
    }

    // Bars
    const barGap = 4;
    const barW = (chartW - barGap * 24) / 24;
    const gradient = ctx.createLinearGradient(0, padT, 0, padT + chartH);
    gradient.addColorStop(0, '#38bdf8');
    gradient.addColorStop(1, '#2563eb');

    // Hour labels
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    ctx.fillStyle = '#64748b';
    ctx.font = '10px -apple-system, BlinkMacSystemFont, sans-serif';

    for (let i = 0; i < 24; i++) {
        const x = padL + i * (barW + barGap) + barGap / 2;
        const val = counts[i] || 0;
        const barH = (val / yMax) * chartH;

        // Bar
        const radius = Math.min(4, barW / 2);
        const bx = x, by = padT + chartH - barH, bw = barW, bh = barH;
        ctx.fillStyle = gradient;
        if (bh > 0) {
            ctx.beginPath();
            ctx.moveTo(bx + radius, by);
            ctx.lineTo(bx + bw - radius, by);
            ctx.quadraticCurveTo(bx + bw, by, bx + bw, by + radius);
            ctx.lineTo(bx + bw, by + bh);
            ctx.lineTo(bx, by + bh);
            ctx.lineTo(bx, by + radius);
            ctx.quadraticCurveTo(bx, by, bx + radius, by);
            ctx.fill();
        }

        // Value on top of bar
        if (val > 0) {
            ctx.fillStyle = '#e2e8f0';
            ctx.font = '10px -apple-system, BlinkMacSystemFont, sans-serif';
            ctx.textBaseline = 'bottom';
            ctx.fillText(val.toString(), x + barW / 2, by - 2);
        }

        // Hour label
        ctx.fillStyle = '#64748b';
        ctx.font = '10px -apple-system, BlinkMacSystemFont, sans-serif';
        ctx.textBaseline = 'top';
        ctx.fillText(data[i].hour, x + barW / 2, padT + chartH + 8);
    }
}

// Redraw chart on resize
let _chartData = null;
const _origLoadHourlyChart = loadHourlyChart;
loadHourlyChart = async function() {
    const df = document.getElementById('chartDateFrom').value;
    const dt = document.getElementById('chartDateTo').value;
    try {
        const res = await apiFetch(`/api/stats/hourly?date_from=${df}&date_to=${dt}`);
        if (!res) return;
        _chartData = await res.json();
        drawHourlyChart(_chartData);
    } catch (e) { console.error('Hourly chart error:', e); }
};
window.addEventListener('resize', () => { if (_chartData) drawHourlyChart(_chartData); });

// ===== CALLS PAGE =====
let currentPage = 1;

async function loadCalls(page = 1) {
    currentPage = page;
    const params = new URLSearchParams({ page, limit: 20 });
    const df = document.getElementById('filterDateFrom').value;
    const dt = document.getElementById('filterDateTo').value;
    const num = document.getElementById('filterNumber').value;
    const st = document.getElementById('filterStatus').value;
    const se = document.getElementById('filterSentiment').value;
    if (df) params.set('date_from', df);
    if (dt) params.set('date_to', dt);
    if (num) params.set('number', num);
    if (st) params.set('status', st);
    if (se) params.set('sentiment', se);
    try {
        const res = await apiFetch(`/api/calls?${params}`);
        if (!res) return;
        const data = await res.json();
        renderTable(data.calls);
        renderPagination(data.page, data.pages);
    } catch (e) { console.error('Calls error:', e); }
}

function renderTable(calls) {
    const tbody = document.getElementById('callsBody');
    if (!calls || !calls.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="no-data">Arama bulunamadi</td></tr>';
        return;
    }
    tbody.innerHTML = calls.map(c => `
        <tr onclick="showDetail('${c.call_sid}')">
            <td>${esc(c.caller_number) || '-'}</td>
            <td>${formatDate(c.start_time)}</td>
            <td>${formatDuration(c.duration_seconds)}</td>
            <td><span class="badge badge-${c.status}">${statusLabel(c.status)}</span></td>
            <td><span class="badge badge-${c.sentiment || 'notr'}">${sentimentLabel(c.sentiment)}</span></td>
            <td>${esc((c.summary || '').substring(0, 80))}${(c.summary || '').length > 80 ? '...' : ''}</td>
        </tr>
    `).join('');
}

function renderPagination(page, pages) {
    const el = document.getElementById('pagination');
    if (pages <= 1) { el.innerHTML = ''; return; }
    let h = `<button ${page <= 1 ? 'disabled' : ''} onclick="loadCalls(${page - 1})">&laquo;</button>`;
    const start = Math.max(1, page - 3), end = Math.min(pages, page + 3);
    for (let i = start; i <= end; i++)
        h += `<button class="${i === page ? 'active' : ''}" onclick="loadCalls(${i})">${i}</button>`;
    h += `<button ${page >= pages ? 'disabled' : ''} onclick="loadCalls(${page + 1})">&raquo;</button>`;
    el.innerHTML = h;
}

// ===== CALL DETAIL MODAL =====
async function showDetail(callSid) {
    try {
        const res = await apiFetch(`/api/calls/${callSid}`);
        if (!res) return;
        const c = await res.json();
        let tHtml = '<div class="no-data">Transkript yok</div>';
        if (c.transcript && c.transcript.length) {
            tHtml = c.transcript.map(t => `
                <div class="t-msg ${t.role}">
                    <div class="t-role">${t.role === 'user' ? 'Musteri' : 'Asistan'}</div>
                    ${esc(t.text)}
                </div>`).join('');
        }
        document.getElementById('modalBody').innerHTML = `
            <div class="detail-grid">
                <div class="detail-item"><div class="dlabel">Arayan Numara</div><div class="dvalue">${esc(c.caller_number) || '-'}</div></div>
                <div class="detail-item"><div class="dlabel">Durum</div><div class="dvalue"><span class="badge badge-${c.status}">${statusLabel(c.status)}</span></div></div>
                <div class="detail-item"><div class="dlabel">Baslangic</div><div class="dvalue">${formatDate(c.start_time)}</div></div>
                <div class="detail-item"><div class="dlabel">Sure</div><div class="dvalue">${formatDuration(c.duration_seconds)}</div></div>
                <div class="detail-item"><div class="dlabel">Duygu Durumu</div><div class="dvalue"><span class="badge badge-${c.sentiment || 'notr'}">${sentimentLabel(c.sentiment)}</span></div></div>
                <div class="detail-item"><div class="dlabel">Ticket ID</div><div class="dvalue">${c.ticket_id || '-'}</div></div>
            </div>
            ${c.summary ? `<div class="section-title">Sorun Ozeti</div><p style="font-size:13px;line-height:1.7;margin-bottom:12px;color:#cbd5e1">${esc(c.summary)}</p>` : ''}
            ${c.recording_path ? `<div class="section-title">Ses Kaydi</div><audio controls src="/api/recordings/${c.call_sid}"></audio>` : ''}
            <div class="section-title">Konusma Metni</div>
            <div class="transcript-list">${tHtml}</div>`;
        document.getElementById('modalOverlay').classList.add('open');
    } catch (e) { console.error('Detail error:', e); }
}

function closeModal(event) {
    if (!event || event.target === document.getElementById('modalOverlay'))
        document.getElementById('modalOverlay').classList.remove('open');
}

// ===== SETTINGS =====
let origSettings = {};

async function loadPrompt() {
    try {
        const res = await apiFetch('/api/settings/prompt');
        if (!res) return;
        const data = await res.json();
        document.getElementById('promptEditor').value = data.prompt;
        document.getElementById('greetingEditor').value = data.greeting || '';
        document.getElementById('maxDurationEditor').value = data.max_call_duration || 90;
        document.getElementById('endPhraseEditor').value = data.end_call_phrase || 'iyi gunler dilerim';
        document.getElementById('webhookUrlEditor').value = data.webhook_url || '';
        origSettings = { ...data };
    } catch (e) { console.error('Load prompt error:', e); }
}

async function savePrompt() {
    const payload = {
        prompt: document.getElementById('promptEditor').value.trim(),
        greeting: document.getElementById('greetingEditor').value.trim(),
        max_call_duration: parseInt(document.getElementById('maxDurationEditor').value) || 90,
        end_call_phrase: document.getElementById('endPhraseEditor').value.trim(),
        webhook_url: document.getElementById('webhookUrlEditor').value.trim(),
    };
    try {
        const res = await apiFetch('/api/settings/prompt', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (res.ok) {
            origSettings = { ...payload };
            const ind = document.getElementById('saveIndicator');
            ind.classList.add('show');
            setTimeout(() => ind.classList.remove('show'), 2500);
        }
    } catch (e) { console.error('Save prompt error:', e); }
}

function resetPrompt() {
    document.getElementById('promptEditor').value = origSettings.prompt || '';
    document.getElementById('greetingEditor').value = origSettings.greeting || '';
    document.getElementById('maxDurationEditor').value = origSettings.max_call_duration || 90;
    document.getElementById('endPhraseEditor').value = origSettings.end_call_phrase || 'iyi gunler dilerim';
    document.getElementById('webhookUrlEditor').value = origSettings.webhook_url || '';
}

// ===== VOICE TEST =====
let testWs = null, testStream = null, testConnected = false;
let testAudioCtx = null, testScriptProc = null;
let testPlaybackCtx = null, testAnalyser = null, testNextPlayTime = 0, testAnimFrame = null;

const VIS_COUNT = 28;
const visEl = document.getElementById('testVisualizer');
for (let i = 0; i < VIS_COUNT; i++) { const b = document.createElement('div'); b.className = 'vbar'; visEl.appendChild(b); }
const vbars = visEl.querySelectorAll('.vbar');

function setTestStatus(text, cls = '') { const el = document.getElementById('testStatus'); el.textContent = text; el.className = 'test-status ' + cls; }

function addTestTranscript(role, text) {
    const el = document.getElementById('testTranscript');
    const div = document.createElement('div'); div.className = 't-msg ' + role;
    const r = document.createElement('div'); r.className = 't-role'; r.textContent = role === 'user' ? 'Siz' : 'Asistan';
    div.appendChild(r); div.appendChild(document.createTextNode(text)); el.appendChild(div); el.scrollTop = el.scrollHeight;
}

function updateTestVis() {
    if (!testAnalyser) { vbars.forEach(b => b.style.height = '5px'); return; }
    const data = new Uint8Array(testAnalyser.frequencyBinCount);
    testAnalyser.getByteFrequencyData(data);
    const step = Math.floor(data.length / VIS_COUNT);
    for (let i = 0; i < VIS_COUNT; i++) vbars[i].style.height = Math.max(4, (data[i * step] / 255) * 40) + 'px';
    testAnimFrame = requestAnimationFrame(updateTestVis);
}

function float32ToPcm16B64(f32) {
    const pcm = new Int16Array(f32.length);
    for (let i = 0; i < f32.length; i++) { let s = Math.max(-1, Math.min(1, f32[i])); pcm[i] = s < 0 ? s * 0x8000 : s * 0x7FFF; }
    const bytes = new Uint8Array(pcm.buffer); let bin = '';
    for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin);
}

function playTestAudio(b64) {
    if (!testPlaybackCtx) {
        testPlaybackCtx = new AudioContext({ sampleRate: 24000 });
        testAnalyser = testPlaybackCtx.createAnalyser(); testAnalyser.fftSize = 256;
        testAnalyser.connect(testPlaybackCtx.destination); updateTestVis();
    }
    try {
        const bin = atob(b64); const bytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
        const pcm16 = new Int16Array(bytes.buffer); const f32 = new Float32Array(pcm16.length);
        for (let i = 0; i < pcm16.length; i++) f32[i] = pcm16[i] / 32768.0;
        const buf = testPlaybackCtx.createBuffer(1, f32.length, 24000); buf.getChannelData(0).set(f32);
        const src = testPlaybackCtx.createBufferSource(); src.buffer = buf; src.connect(testAnalyser);
        const now = testPlaybackCtx.currentTime;
        if (testNextPlayTime < now) testNextPlayTime = now;
        src.start(testNextPlayTime); testNextPlayTime += buf.duration;
    } catch (e) {}
}

async function toggleTestCall() { if (testConnected) stopTestCall(); else await startTestCall(); }

async function startTestCall() {
    const btn = document.getElementById('testCallBtn');
    btn.classList.add('connecting'); setTestStatus('Baglaniyor...');
    document.getElementById('testTranscript').innerHTML = '';
    try {
        testStream = await navigator.mediaDevices.getUserMedia({ audio: { sampleRate: 16000, channelCount: 1, echoCancellation: true, noiseSuppression: true } });
        testAudioCtx = new AudioContext({ sampleRate: 16000 });
        const source = testAudioCtx.createMediaStreamSource(testStream);
        testScriptProc = testAudioCtx.createScriptProcessor(4096, 1, 1);
        let canSend = false;
        testScriptProc.onaudioprocess = (e) => {
            if (!canSend || !testWs || testWs.readyState !== WebSocket.OPEN) return;
            testWs.send(JSON.stringify({ type: 'audio', data: float32ToPcm16B64(e.inputBuffer.getChannelData(0)) }));
        };
        source.connect(testScriptProc); testScriptProc.connect(testAudioCtx.destination);
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        testWs = new WebSocket(`${proto}//${location.host}/ws`);
        testWs.onmessage = (e) => {
            const msg = JSON.parse(e.data);
            switch (msg.type) {
                case 'setup_complete': canSend = true; testConnected = true; btn.classList.remove('connecting'); btn.classList.add('active'); btn.innerHTML = '&#x1F534;'; setTestStatus('Baglandi - Konusabilirsiniz', 'connected'); break;
                case 'audio': playTestAudio(msg.data); break;
                case 'text': if (msg.text && msg.text.trim()) addTestTranscript(msg.role || 'assistant', msg.text.trim()); break;
                case 'turn_complete': testNextPlayTime = 0; break;
                case 'call_ended': setTestStatus(msg.reason === 'max_duration' ? 'Sure doldu' : 'Agent gorusmeyi sonlandirdi'); stopTestCall(); loadStats(); loadRecentCalls(); break;
                case 'error': setTestStatus('Hata: ' + msg.message, 'error'); break;
            }
        };
        testWs.onerror = () => setTestStatus('Baglanti hatasi', 'error');
        testWs.onclose = () => { if (testConnected) { stopTestCall(); setTestStatus('Baglanti kapandi'); } };
    } catch (err) { btn.classList.remove('connecting'); setTestStatus('Hata: ' + err.message, 'error'); stopTestCall(); }
}

function stopTestCall() {
    if (testWs) { try { testWs.send(JSON.stringify({ type: 'end' })); } catch (e) {} testWs.close(); testWs = null; }
    if (testScriptProc) { testScriptProc.disconnect(); testScriptProc = null; }
    if (testAudioCtx) { testAudioCtx.close(); testAudioCtx = null; }
    if (testStream) { testStream.getTracks().forEach(t => t.stop()); testStream = null; }
    if (testPlaybackCtx) { testPlaybackCtx.close(); testPlaybackCtx = null; }
    if (testAnimFrame) { cancelAnimationFrame(testAnimFrame); testAnimFrame = null; }
    testAnalyser = null; testNextPlayTime = 0;
    vbars.forEach(b => b.style.height = '5px');
    testConnected = false;
    const btn = document.getElementById('testCallBtn'); btn.classList.remove('active', 'connecting'); btn.innerHTML = '&#x1F4DE;';
    setTestStatus('Arama sonlandirildi');
}

// ===== USERS =====
async function loadUsers() {
    try {
        const res = await apiFetch('/api/users');
        if (!res) return;
        const users = await res.json();
        const tbody = document.getElementById('usersBody');
        if (!users.length) { tbody.innerHTML = '<tr><td colspan="4" class="no-data">Kullanici yok</td></tr>'; return; }
        tbody.innerHTML = users.map(u => `
            <tr>
                <td>${u.id}</td>
                <td>${esc(u.username)}</td>
                <td>${formatDate(u.created_at)}</td>
                <td class="user-actions">
                    <button class="btn btn-sm btn-outline" onclick="changePassword(${u.id}, '${esc(u.username)}')">Parola Degistir</button>
                    <button class="btn btn-sm btn-danger" onclick="removeUser(${u.id}, '${esc(u.username)}')">Sil</button>
                </td>
            </tr>
        `).join('');
    } catch (e) { console.error('Load users error:', e); }
}

async function addUser() {
    const username = document.getElementById('newUsername').value.trim();
    const password = document.getElementById('newPassword').value;
    if (!username || !password) return alert('Kullanici adi ve parola gerekli');
    if (password.length < 4) return alert('Parola en az 4 karakter olmali');
    try {
        const res = await apiFetch('/api/users', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password }),
        });
        if (!res) return;
        if (res.ok) {
            document.getElementById('newUsername').value = '';
            document.getElementById('newPassword').value = '';
            loadUsers();
        } else {
            const data = await res.json();
            alert(data.error || 'Hata');
        }
    } catch (e) { alert('Hata: ' + e.message); }
}

async function changePassword(id, username) {
    const newPw = prompt(`"${username}" icin yeni parola:`);
    if (!newPw) return;
    if (newPw.length < 4) return alert('Parola en az 4 karakter olmali');
    try {
        const res = await apiFetch(`/api/users/${id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ password: newPw }),
        });
        if (res && res.ok) alert('Parola guncellendi');
        else { const d = await res.json(); alert(d.error || 'Hata'); }
    } catch (e) { alert('Hata: ' + e.message); }
}

async function removeUser(id, username) {
    if (!confirm(`"${username}" kullanicisini silmek istediginize emin misiniz?`)) return;
    try {
        const res = await apiFetch(`/api/users/${id}`, { method: 'DELETE' });
        if (res && res.ok) loadUsers();
        else { const d = await res.json(); alert(d.error || 'Hata'); }
    } catch (e) { alert('Hata: ' + e.message); }
}

// ===== KEYBOARD =====
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

// ===== INIT =====
const t = today();
document.getElementById('filterDateFrom').value = t;
document.getElementById('filterDateTo').value = t;
document.getElementById('dashDate').textContent = new Date().toLocaleDateString('tr-TR', { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });

document.getElementById('chartDateFrom').value = t;
document.getElementById('chartDateTo').value = t;

loadStats();
loadRecentCalls();
loadHourlyChart();
setInterval(() => { loadStats(); loadRecentCalls(); }, 10000);
