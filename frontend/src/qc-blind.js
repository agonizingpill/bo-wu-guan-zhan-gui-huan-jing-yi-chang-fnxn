/* 盲样巡检：派单 -> 盲态复核 -> 全部签认后揭盲 -> 漏报/误报发现 */
const qcToken = () => localStorage.getItem('museum_token');
const qcEndpoint = '/api/qc';
const qcState = { role: null, rounds: [], cases: [], selectedRound: null, blind: null, findings: null, seq: 0 };

function qcEl(tag, text) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  return node;
}

async function qcRequest(path = '', body, method) {
  const response = await fetch(qcEndpoint + path, {
    method: method || (body ? 'POST' : 'GET'),
    headers: { Authorization: `Bearer ${qcToken() || ''}`, 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `请求失败 (${response.status})`);
  return data;
}

const qcLaunch = qcEl('button', '盲样巡检');
qcLaunch.type = 'button';
qcLaunch.id = 'qc-blind-open';
qcLaunch.hidden = true;
const qcDialog = qcEl('dialog');
qcDialog.id = 'qc-blind';
qcDialog.setAttribute('aria-labelledby', 'qc-blind-title');
const qcHeader = qcEl('header');
const qcTitle = qcEl('h2', '盲样巡检 · 告警质量复核');
qcTitle.id = 'qc-blind-title';
const qcClose = qcEl('button', '关闭');
qcClose.addEventListener('click', () => qcDialog.close());
qcHeader.append(qcTitle, qcClose);
const qcFeedback = qcEl('p');
qcFeedback.setAttribute('role', 'status');
const qcBody = qcEl('section');
qcDialog.append(qcHeader, qcFeedback, qcBody);
document.body.append(qcLaunch, qcDialog);

function qcSession() {
  let signedIn = false;
  try { signedIn = Boolean(qcToken()); } catch { /* ignore */ }
  qcLaunch.hidden = !signedIn;
  if (!signedIn && qcDialog.open) qcDialog.close();
  const user = JSON.parse(localStorage.getItem('museum_user') || 'null');
  qcState.role = user?.role || null;
}
qcSession();
window.addEventListener('storage', qcSession);
setInterval(qcSession, 600);

const qcStatusLabel = { dispatched: '待复核', reviewing: '复核中', unblinded: '已揭盲' };
const qcShiftLabel = (key) => key ? (key.startsWith('night:') ? `夜班 ${key.slice(6)}` : `白班 ${key.slice(4)}`) : '—';
const qcFindingText = {
  missed_alert: '漏报', false_alert: '误报', alert_hit: '告警命中', correct_clear: '判正常正确',
};

function qcNotify(msg) { qcFeedback.textContent = msg; }

async function qcLoadRounds() {
  const seq = ++qcState.seq;
  qcNotify('正在加载巡检轮次…');
  try {
    const tasks = [qcRequest('/rounds')];
    if (qcState.role === 'supervisor' && qcState.cases.length === 0) {
      // display-case registry lives under the main API, not the /qc prefix
      tasks.push(fetch('/api/cases', { headers: { Authorization: `Bearer ${qcToken() || ''}` } })
        .then((r) => (r.ok ? r.json() : []))
        .catch(() => []));
    }
    const [rounds, cases] = await Promise.all(tasks);
    if (seq !== qcState.seq) return;
    qcState.rounds = rounds;
    if (Array.isArray(cases)) qcState.cases = cases.map((c) => ({ id: c.id, code: c.code, artifact_title: c.artifact_title }));
    qcRenderRounds();
    qcNotify('');
  } catch (error) { qcNotify(error.message); }
}

function qcRenderRounds() {
  qcBody.replaceChildren();
  if (qcState.role === 'supervisor') {
    const controls = qcEl('div');
    controls.className = 'qc-controls';
    const caseLabel = qcEl('label', '限定展柜（可选）');
    const caseSelect = qcEl('select');
    caseSelect.add(new Option('全部展柜', ''));
    qcState.cases.forEach((c) => caseSelect.add(new Option(`${c.code} · ${c.artifact_title}`, c.id)));
    caseLabel.append(caseSelect);
    const dispatchBtn = qcEl('button', '发起盲样派单');
    dispatchBtn.className = 'primary';
    dispatchBtn.addEventListener('click', async () => {
      dispatchBtn.disabled = true;
      try {
        const payload = {};
        if (caseSelect.value) payload.case_id = Number(caseSelect.value);
        const r = await qcRequest('/rounds/dispatch', payload);
        qcNotify(r.reused_existing_round ? '该幂等派单已存在，返回原轮次，未重复抽样' : '派单完成，已冻结抽样与原判');
        await qcLoadRounds();
        const target = qcState.rounds.find((x) => x.id === r.round.id);
        if (target) qcOpenRound(target);
      } catch (error) { qcNotify(error.message); }
      finally { dispatchBtn.disabled = false; }
    });
    controls.append(caseLabel, dispatchBtn);
    qcBody.append(controls);
  }

  const list = qcEl('div');
  list.className = 'qc-round-list';
  if (!qcState.rounds.length) {
    list.append(qcEl('div', '暂无巡检轮次。主管发起派单后，复核角色在此进入盲态队列。'));
    list.firstChild.className = 'qc-empty';
  }
  qcState.rounds.forEach((round) => {
    const btn = qcEl('button');
    btn.className = 'qc-round' + (qcState.selectedRound?.id === round.id ? ' selected' : '');
    const no = qcEl('span', round.round_key);
    no.className = 'qc-no';
    const meta = qcEl('span');
    meta.className = 'qc-round-meta';
    meta.textContent = `告警候选 ${round.alert_candidate_count} · 配对 ${round.matched_pair_count} · 缺口 ${round.gap_count} · 样本 ${round.sample_count}`;
    const badge = qcEl('span', qcStatusLabel[round.status] || round.status);
    badge.className = `qc-badge ${round.status}`;
    btn.append(no, meta, badge);
    btn.addEventListener('click', () => qcOpenRound(round));
    list.append(btn);
  });
  qcBody.append(list);
}

async function qcOpenRound(round) {
  qcState.selectedRound = round;
  qcState.blind = null;
  qcState.findings = null;
  qcRenderRounds();
  if (round.status === 'unblinded') {
    await qcLoadFindings(round.id);
  } else {
    await qcLoadBlind(round.id);
  }
}

async function qcLoadBlind(roundId) {
  if (qcState.role === 'viewer') {
    qcNotify('观察员无盲样权限，揭盲后可查看抽检发现');
    return;
  }
  try {
    qcState.blind = await qcRequest(`/rounds/${roundId}/blind-queue`);
    qcRenderBlind();
  } catch (error) { qcNotify(error.message); }
}

function qcRenderBlind() {
  const data = qcState.blind;
  if (!data) return;
  const existing = qcBody.querySelector('.qc-workspace');
  if (existing) existing.remove();
  const ws = qcEl('div');
  ws.className = 'qc-workspace';

  const scope = qcEl('p', data.material_scope);
  scope.className = 'qc-scope';
  const progress = qcEl('div');
  progress.className = 'qc-progress';
  progress.innerHTML = `复核进度 <b>${data.progress.signed} / ${data.progress.total}</b> · 剩余 <b>${data.progress.remaining}</b> · 配样缺口 <b>${data.gaps.length}</b>`;
  ws.append(scope, progress);

  if (data.gaps.length) {
    const gapBox = qcEl('div');
    gapBox.className = 'qc-gaps';
    gapBox.append(qcEl('h4', `配样缺口（${data.gaps.length}）`));
    data.gaps.forEach((g) => {
      const row = qcEl('div', `${g.case_code} · ${qcShiftLabel(g.shift_key)}：${g.reason}`);
      row.className = 'qc-gap';
      gapBox.append(row);
    });
    ws.append(gapBox);
  }

  const cards = qcEl('div');
  cards.className = 'qc-cards';
  data.items.forEach((item) => {
    const card = qcEl('div');
    card.className = 'qc-card' + (item.signature_count ? ' signed' : '');
    const head = qcEl('div');
    head.className = 'qc-card-head';
    const no = qcEl('span', `盲样 #${String(item.dispatch_no).padStart(3, '0')} · ${item.case_code}`);
    no.className = 'qc-no';
    const shift = qcEl('span', `${qcShiftLabel(item.shift_key)} · ${item.gallery} / ${item.zone}`);
    head.append(no, shift);
    const reading = qcEl('div');
    reading.className = 'qc-reading';
    [
      ['采样时间', new Date(item.reading_sampled_at).toLocaleString('zh-CN', { hour12: false })],
      ['温度', `${item.reading_temperature} °C`],
      ['湿度', `${item.reading_humidity} %`],
      ['CO₂', `${item.reading_co2} ppm`],
    ].forEach(([k, v]) => {
      const cell = qcEl('div');
      cell.append(Object.assign(qcEl('small', k)), Object.assign(qcEl('b', v)));
      reading.append(cell);
    });
    const door = qcEl('div');
    door.className = 'qc-context';
    door.textContent = `门磁：${item.reading_door_open ? '开启' : '闭合'} · 目标窗口 温度 ${item.target_temp_min}–${item.target_temp_max}°C / 湿度 ${item.target_rh_min}–${item.target_rh_max}% · 文物 ${item.artifact_code} ${item.artifact_title}`;
    const checks = qcEl('div');
    checks.className = 'qc-context';
    if (item.recent_checks && item.recent_checks.length) {
      checks.textContent = '巡检材料：' + item.recent_checks.map((k) => `${k.check_type}/${k.result}`).join('，');
    } else {
      checks.textContent = '巡检材料：该展柜暂无近期巡检记录';
    }
    card.append(head, reading, door, checks);

    const form = qcEl('form');
    form.className = 'qc-verdict';
    [['abnormal', '判异常'], ['normal', '判正常']].forEach(([value, text]) => {
      const label = qcEl('label');
      const radio = qcEl('input');
      radio.type = 'radio'; radio.name = `verdict-${item.id}`; radio.value = value;
      if (item.latest_verdict === value) radio.checked = true;
      label.append(radio, qcEl('span', text));
      form.append(label);
    });
    const note = qcEl('input');
    note.placeholder = '复核依据（可选）'; note.maxLength = 500;
    const submit = qcEl('button', item.signature_count ? '补签留痕' : '提交复核');
    submit.type = 'submit';
    form.append(note, submit);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const picked = form.querySelector('input:checked');
      if (!picked) { qcNotify('请选择判异常或判正常'); return; }
      submit.disabled = true;
      try {
        await qcRequest(`/samples/${item.id}/reviews`, { verdict: picked.value, note: note.value });
        qcNotify('复核签认已记录');
        await qcLoadBlind(data.round_id);
      } catch (error) { qcNotify(error.message); submit.disabled = false; }
    });
    card.append(form);
    if (item.latest_verdict) {
      const signed = qcEl('div', `已签认：${item.latest_verdict === 'abnormal' ? '判异常' : '判正常'}（共 ${item.signature_count} 次签认）`);
      signed.className = 'qc-signed-note';
      card.append(signed);
    }
    cards.append(card);
  });
  ws.append(cards);

  if (qcState.role === 'supervisor') {
    const unblindBtn = qcEl('button', '全部签认后揭盲');
    unblindBtn.className = 'primary';
    unblindBtn.style.marginTop = '1rem';
    unblindBtn.addEventListener('click', async () => {
      try {
        const r = await qcRequest(`/rounds/${data.round_id}/unblind`, {});
        if (r.unblinded) qcNotify('已揭盲，正在生成抽检发现…');
        const round = qcState.rounds.find((x) => x.id === data.round_id);
        await qcLoadRounds();
        await qcOpenRound({ ...round, status: 'unblinded' });
      } catch (error) { qcNotify(error.message); }
    });
    ws.append(unblindBtn);
  }
  qcBody.append(ws);
}

async function qcLoadFindings(roundId) {
  try {
    qcState.findings = await qcRequest(`/rounds/${roundId}/findings`);
    qcRenderFindings();
  } catch (error) { qcNotify(error.message); }
}

function qcRenderFindings() {
  const data = qcState.findings;
  const existing = qcBody.querySelector('.qc-workspace');
  if (existing) existing.remove();
  const ws = qcEl('div');
  ws.className = 'qc-workspace qc-findings';
  ws.append(qcEl('h3', `抽检发现 · ${data.round.round_key}`));

  const summary = qcEl('div');
  summary.className = 'qc-summary';
  [
    ['missed_alert', '漏报'], ['false_alert', '误报'],
    ['alert_hit', '告警命中'], ['correct_clear', '判正常正确'],
  ].forEach(([key, text]) => {
    const cell = qcEl('div');
    cell.append(Object.assign(qcEl('b', String(data.summary[key])), {}), qcEl('small', text));
    summary.append(cell);
  });
  ws.append(summary);

  if (data.gaps.length) {
    const gapBox = qcEl('div');
    gapBox.className = 'qc-gaps';
    gapBox.append(qcEl('h4', `配样缺口（${data.gaps.length}）`));
    data.gaps.forEach((g) => {
      const row = qcEl('div', `${g.case_code} · 告警 #${g.event_id} · ${qcShiftLabel(g.shift_key)}：${g.reason}`);
      row.className = 'qc-gap';
      gapBox.append(row);
    });
    ws.append(gapBox);
  }

  const table = qcEl('table');
  const thead = table.createTHead().insertRow();
  ['盲样号', '展柜', '采样时间', '温/湿/CO₂', '门磁', '原判角色', '复核判定', '结论', '复核人'].forEach((t) => thead.append(qcEl('th', t)));
  const body = table.createTBody();
  data.items.forEach((it) => {
    const tr = body.insertRow();
    const tag = qcEl('span', qcFindingText[it.finding]);
    tag.className = `qc-tag ${it.finding}`;
    [
      `#${String(it.dispatch_no).padStart(3, '0')}`,
      it.case_code,
      new Date(it.reading_sampled_at).toLocaleString('zh-CN', { hour12: false }),
      `${it.reading_temperature}°C / ${it.reading_humidity}% / ${it.reading_co2}`,
      it.reading_door_open ? '开' : '合',
      it.frozen_role === 'alert' ? '告警点' : '未告警',
      it.verdict === 'abnormal' ? '判异常' : '判正常',
    ].forEach((v) => tr.insertCell().textContent = v);
    tr.insertCell().append(tag);
    tr.insertCell().textContent = it.reviewer_name || '—';
  });
  ws.append(table);
  const legend = qcEl('p');
  legend.className = 'qc-context';
  legend.style.marginTop = '.8rem';
  legend.textContent = `口径：${data.finding_labels.missed_alert}；${data.finding_labels.false_alert}。`;
  ws.append(legend);
  qcBody.append(ws);
}

qcLaunch.addEventListener('click', async () => {
  qcDialog.showModal();
  qcState.selectedRound = null;
  await qcLoadRounds();
});
