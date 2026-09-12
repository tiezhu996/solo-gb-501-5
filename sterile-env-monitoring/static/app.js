'use strict';

/* 无菌包装环境监测与批次放行联动 —— 前端逻辑（原生 JS，无构建步骤） */

const PARAM_LABELS = { temperature: '温度', humidity: '湿度', pressure: '压差', particle: '悬浮粒子' };
const PARAM_UNITS = { temperature: '°C', humidity: '%RH', pressure: 'Pa', particle: '个/m³' };
const FILTER_LABELS = { all: '全部', open: '未关闭', closed: '已关闭' };

const state = {
  eventsFilter: 'all',
  selectedEventId: null,
  releaseError: null,   // { message, events } 批次放行被拦截时展示
  readingResult: null,  // 最近一次读数登记结果
};

/* ---------------- 基础工具 ---------------- */

const $ = (sel, el = document) => el.querySelector(sel);

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

async function api(path, { method = 'GET', body } = {}) {
  const res = await fetch(path, {
    method,
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const payload = await res.json().catch(() => null);
  if (!res.ok || !payload || payload.ok === false) {
    const err = new Error((payload && payload.error && payload.error.message) || `请求失败（HTTP ${res.status}）`);
    err.code = payload && payload.error && payload.error.code;
    err.details = payload && payload.error && payload.error.details;
    throw err;
  }
  return payload.data;
}

let toastTimer = null;
function toast(msg, type = 'success') {
  const el = $('#toast');
  el.textContent = msg;
  el.className = `show ${type}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = ''; }, 4200);
}

const fmtVal = v => (v === null || v === undefined ? '—' : String(Math.round(Number(v) * 100) / 100));

/* ---------------- 通用片段 ---------------- */

const eventBadge = status =>
  status === 'open' ? '<span class="badge badge-red">未关闭</span>' : '<span class="badge badge-green">已关闭</span>';

const batchBadge = status =>
  status === 'released' ? '<span class="badge badge-blue">已放行</span>' : '<span class="badge badge-orange">在产</span>';

const judgeBadge = r =>
  r.exceeded
    ? `<span class="badge badge-red">${r.is_retest ? '复测超限' : '超限'}</span>`
    : `<span class="badge badge-green">${r.is_retest ? '复测合格' : '合格'}</span>`;

function eventsTable(events) {
  return `<table class="table"><thead><tr>
    <th>事件号</th><th>监测点</th><th>参数</th><th>触发值</th><th>限值</th><th>关联批次</th><th>状态</th><th>创建时间</th>
  </tr></thead><tbody>
  ${events.map(e => `<tr class="clickable" onclick="goEvent(${e.id})">
    <td class="mono">${esc(e.event_no)}</td>
    <td>${esc(e.point_name)}<div class="sub">${esc(e.point_code)} · ${esc(e.line_name)}</div></td>
    <td>${esc(e.parameter_label)}</td>
    <td class="num danger-text">${fmtVal(e.trigger_value)} ${esc(e.unit)}</td>
    <td class="limit">${esc(e.limit_text)}</td>
    <td>${e.batch_no ? `<span class="mono">${esc(e.batch_no)}</span>` : '<span class="sub">未关联</span>'}</td>
    <td>${eventBadge(e.status)}</td>
    <td class="sub nowrap">${esc(e.created_at)}</td>
  </tr>`).join('')}</tbody></table>`;
}

function readingsTable(readings) {
  if (!readings.length) return '<div class="empty">暂无读数</div>';
  return `<table class="table"><thead><tr>
    <th>时间</th><th>监测点</th><th>参数</th><th>读数</th><th>限值</th><th>类别</th><th>判定</th><th>关联事件</th><th>记录人</th>
  </tr></thead><tbody>
  ${readings.map(r => `<tr class="${r.exceeded ? 'row-danger' : ''}">
    <td class="sub nowrap">${esc(r.recorded_at)}</td>
    <td>${esc(r.point_name)}<div class="sub">${esc(r.point_code)}</div></td>
    <td>${esc(r.parameter_label)}</td>
    <td class="num ${r.exceeded ? 'danger-text' : ''}">${fmtVal(r.value)} ${esc(r.unit)}</td>
    <td class="limit">${esc(r.limit_text)}</td>
    <td>${r.is_retest ? '<span class="badge badge-purple">复测</span>' : '<span class="sub">常规</span>'}</td>
    <td>${judgeBadge(r)}</td>
    <td>${r.event_no ? `<a class="link mono" href="#events" onclick="goEvent(${r.event_id});return false;">${esc(r.event_no)}</a>` : '—'}</td>
    <td>${esc(r.recorded_by)}</td>
  </tr>`).join('')}</tbody></table>`;
}

const infoItem = (label, valueHtml) =>
  `<div class="info-item"><div class="info-label">${label}</div><div>${valueHtml}</div></div>`;

/* ---------------- 页面：总览 ---------------- */

function statCard(label, value, unit, tone = '') {
  return `<div class="card ${tone}">
    <div class="card-value">${value}<span class="card-unit"> ${unit}</span></div>
    <div class="card-label">${label}</div>
  </div>`;
}

async function renderOverview() {
  const ov = await api('/api/overview');
  const c = ov.counts;
  $('#app').innerHTML = `
    <div class="cards">
      ${statCard('监测点', c.points, '个')}
      ${statCard('累计读数', c.readings, '条')}
      ${statCard('未关闭事件', c.open_events, '起', c.open_events > 0 ? 'danger' : 'ok')}
      ${statCard('在产批次', c.batches_in_production, '批')}
      ${statCard('已放行批次', c.batches_released, '批')}
    </div>
    <div class="grid-2">
      <section class="panel">
        <div class="panel-head"><h2>未关闭超限事件</h2><a class="more" href="#events">全部事件 →</a></div>
        ${ov.open_events.length
          ? eventsTable(ov.open_events)
          : '<div class="empty">当前没有未关闭的超限事件 ✅</div>'}
      </section>
      <section class="panel">
        <div class="panel-head"><h2>批次放行状态</h2><a class="more" href="#batches">批次放行 →</a></div>
        ${batchReadinessTable(ov.batches)}
      </section>
    </div>
    <section class="panel">
      <div class="panel-head"><h2>最近事件</h2></div>
      ${ov.recent_events.length ? eventsTable(ov.recent_events) : '<div class="empty">暂无事件</div>'}
    </section>`;
}

function batchReadinessTable(batches) {
  if (!batches.length) return '<div class="empty">暂无批次</div>';
  return `<table class="table"><thead><tr>
    <th>批次号</th><th>产品</th><th>产线</th><th>状态</th><th>未关闭事件</th><th>放行判定</th>
  </tr></thead><tbody>
  ${batches.map(b => `<tr>
    <td class="mono">${esc(b.batch_no)}</td>
    <td>${esc(b.product_name)}</td>
    <td>${esc(b.line_name)}</td>
    <td>${batchBadge(b.status)}</td>
    <td>${b.open_event_count ? `<span class="badge badge-red">${b.open_event_count} 起</span>` : '<span class="sub">无</span>'}</td>
    <td>${b.status === 'released'
      ? `<span class="ok-text">已放行${b.released_by ? ' · ' + esc(b.released_by) : ''}</span>`
      : b.can_release
        ? '<span class="ok-text">✔ 可提交放行</span>'
        : '<span class="danger-text">✖ 被事件拦截</span>'}</td>
  </tr>`).join('')}</tbody></table>`;
}

/* ---------------- 页面：监测点 ---------------- */

async function renderPoints() {
  const [points, lines] = await Promise.all([api('/api/points'), api('/api/lines')]);
  $('#app').innerHTML = `
    <section class="panel">
      <div class="panel-head"><h2>监测点（关联产线与限值）</h2><span class="sub">共 ${points.length} 个</span></div>
      <table class="table"><thead><tr>
        <th>点位编码</th><th>点位名称</th><th>所属产线</th><th>监测参数</th><th>限值</th><th>读数数</th>
      </tr></thead><tbody>
      ${points.map(p => `<tr>
        <td class="mono">${esc(p.code)}</td>
        <td>${esc(p.name)}</td>
        <td>${esc(p.line_name)}<div class="sub">${esc(p.line_code)}</div></td>
        <td>${esc(p.parameter_label)}</td>
        <td><span class="limit">${esc(p.limit_text)}</span></td>
        <td class="num">${p.reading_count}</td>
      </tr>`).join('')}</tbody></table>
    </section>
    <section class="panel">
      <div class="panel-head"><h2>新增监测点</h2></div>
      <form id="pointForm" class="form-grid">
        <label>所属产线
          <select name="line_id" required>
            ${lines.map(l => `<option value="${l.id}">${esc(l.name)}（${esc(l.code)}）</option>`).join('')}
          </select>
        </label>
        <label>点位编码<input name="code" required placeholder="如 P-TEMP-03"></label>
        <label>点位名称<input name="name" required placeholder="如 灌装间温度"></label>
        <label>监测参数
          <select name="parameter" id="paramSelect">
            ${Object.entries(PARAM_LABELS).map(([k, v]) => `<option value="${k}">${v}</option>`).join('')}
          </select>
        </label>
        <label>单位<input name="unit" id="unitInput" required></label>
        <label>下限（可空）<input name="limit_min" type="number" step="any" placeholder="如 18"></label>
        <label>上限（可空）<input name="limit_max" type="number" step="any" placeholder="如 26"></label>
        <div class="form-actions"><button type="submit" class="btn primary">保存监测点</button></div>
      </form>
      <p class="hint">限值至少填写一项：温湿度通常设上下限，压差只设下限（≥），悬浮粒子只设上限（≤）。</p>
    </section>`;

  const paramSelect = $('#paramSelect');
  const unitInput = $('#unitInput');
  const syncUnit = () => { unitInput.value = PARAM_UNITS[paramSelect.value]; };
  paramSelect.addEventListener('change', syncUnit);
  syncUnit();

  $('#pointForm').addEventListener('submit', async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const body = {
      line_id: Number(fd.get('line_id')),
      code: fd.get('code').trim(),
      name: fd.get('name').trim(),
      parameter: fd.get('parameter'),
      unit: fd.get('unit').trim(),
    };
    if (fd.get('limit_min') !== '') body.limit_min = Number(fd.get('limit_min'));
    if (fd.get('limit_max') !== '') body.limit_max = Number(fd.get('limit_max'));
    try {
      await api('/api/points', { method: 'POST', body });
      toast('监测点已保存');
      renderPoints();
    } catch (err) {
      toast(err.message, 'error');
    }
  });
}

/* ---------------- 页面：读数登记 ---------------- */

async function renderReadings() {
  const [points, readings] = await Promise.all([api('/api/points'), api('/api/readings?limit=30')]);
  const result = state.readingResult;
  $('#app').innerHTML = `
    <section class="panel">
      <div class="panel-head"><h2>读数登记</h2><span class="sub">超限读数自动生成事件并拦截关联批次</span></div>
      <form id="readingForm" class="form-grid">
        <label>监测点
          <select name="point_id" id="pointSelect" required>
            ${points.map(p => `<option value="${p.id}" data-limit="${esc(p.limit_text)}">${esc(p.code)} · ${esc(p.name)}（${esc(p.line_name)}）</option>`).join('')}
          </select>
        </label>
        <label>读数值<input name="value" type="number" step="any" required placeholder="输入实测值"></label>
        <label>记录人<input name="recorded_by" required value="张监测"></label>
        <div class="form-actions">
          <span class="limit-hint" id="limitHint"></span>
          <button type="submit" class="btn primary">登记读数</button>
        </div>
      </form>
      <div id="readingResult">${result ? readingResultHtml(result) : ''}</div>
    </section>
    <section class="panel">
      <div class="panel-head"><h2>最近读数</h2></div>
      ${readingsTable(readings)}
    </section>`;

  const pointSelect = $('#pointSelect');
  const syncHint = () => { $('#limitHint').textContent = `限值：${pointSelect.selectedOptions[0].dataset.limit}`; };
  pointSelect.addEventListener('change', syncHint);
  syncHint();

  $('#readingForm').addEventListener('submit', async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    try {
      const data = await api('/api/readings', {
        method: 'POST',
        body: {
          point_id: Number(fd.get('point_id')),
          value: Number(fd.get('value')),
          recorded_by: fd.get('recorded_by').trim(),
        },
      });
      state.readingResult = data;
      if (data.event) toast(`读数超限，已生成事件 ${data.event.event_no}`, 'error');
      else toast('读数合格，已登记');
      renderReadings();
    } catch (err) {
      toast(err.message, 'error');
    }
  });
}

function readingResultHtml(data) {
  const r = data.reading;
  if (data.event) {
    const ev = data.event;
    return `<div class="alert alert-red">
      <strong>读数超限！</strong>${esc(r.point_name)} 实测 <strong>${fmtVal(r.value)} ${esc(r.unit)}</strong>（限值 ${esc(r.limit_text)}）。
      已自动生成事件 <a class="link mono" href="#events" onclick="goEvent(${ev.id});return false;">${esc(ev.event_no)}</a>${ev.batch_no
        ? `，关联批次 <strong class="mono">${esc(ev.batch_no)}</strong>，该批次提交放行将被拦截`
        : '；该产线当前无在产批次，事件未关联批次'}。
    </div>`;
  }
  return `<div class="alert alert-green">
    <strong>读数合格。</strong>${esc(r.point_name)} 实测 ${fmtVal(r.value)} ${esc(r.unit)}（限值 ${esc(r.limit_text)}），未触发事件。
  </div>`;
}

/* ---------------- 页面：超限事件 ---------------- */

async function renderEvents() {
  const q = state.eventsFilter === 'all' ? '' : `?status=${state.eventsFilter}`;
  const events = await api('/api/events' + q);
  let detail = null;
  if (state.selectedEventId) {
    try {
      detail = (await api(`/api/events/${state.selectedEventId}`)).event;
    } catch {
      state.selectedEventId = null;
    }
  }
  $('#app').innerHTML = `
    <section class="panel">
      <div class="panel-head">
        <h2>超限事件</h2>
        <div class="filters">
          ${Object.entries(FILTER_LABELS).map(([k, v]) =>
            `<button class="chip ${state.eventsFilter === k ? 'active' : ''}" data-filter="${k}">${v}</button>`).join('')}
        </div>
      </div>
      ${events.length ? eventsTable(events) : '<div class="empty">没有符合筛选的事件</div>'}
    </section>
    ${detail ? eventDetailHtml(detail) : ''}`;

  document.querySelectorAll('[data-filter]').forEach(btn =>
    btn.addEventListener('click', () => { state.eventsFilter = btn.dataset.filter; renderEvents(); }));

  const closeDetailBtn = $('#closeDetailBtn');
  if (closeDetailBtn) closeDetailBtn.addEventListener('click', () => { state.selectedEventId = null; renderEvents(); });

  if (detail) bindEventDetail(detail);
}

function eventDetailHtml(ev) {
  const retests = ev.retests || [];
  return `<section class="panel detail" id="eventDetail">
    <div class="panel-head">
      <h2>事件 ${esc(ev.event_no)} ${eventBadge(ev.status)}</h2>
      <button class="btn ghost small" id="closeDetailBtn">收起</button>
    </div>
    <div class="info-grid">
      ${infoItem('监测点', `${esc(ev.point_name)}（${esc(ev.point_code)}）`)}
      ${infoItem('所属产线', esc(ev.line_name))}
      ${infoItem('监测参数', esc(ev.parameter_label))}
      ${infoItem('限值', `<span class="limit">${esc(ev.limit_text)}</span>`)}
      ${infoItem('触发读数', `<span class="danger-text">${fmtVal(ev.trigger_value)} ${esc(ev.unit)}</span>`)}
      ${infoItem('触发时间', esc(ev.trigger_at))}
      ${infoItem('关联批次', ev.batch_no ? `<span class="mono">${esc(ev.batch_no)}</span> ${batchBadge(ev.batch_status)}` : '未关联（产线无在产批次）')}
      ${infoItem('创建时间', esc(ev.created_at))}
      ${ev.status === 'closed' ? infoItem('关闭信息', `${esc(ev.closed_at)} · ${esc(ev.closed_by)}`) : ''}
    </div>

    <div class="grid-2">
      <div>
        <h3>原因与纠正措施（质量人员）</h3>
        ${ev.cause
          ? `<div class="quote">
              <div><strong>原因：</strong>${esc(ev.cause)}</div>
              <div><strong>措施：</strong>${esc(ev.measures)}</div>
              <div class="sub">记录：${esc(ev.disposition_by)} · ${esc(ev.disposition_at)}</div>
            </div>`
          : '<div class="empty">尚未记录原因和措施</div>'}
        ${ev.status === 'open' ? `
        <form id="dispositionForm" class="form-stack">
          <label>原因分析<textarea name="cause" rows="2" required placeholder="查明超限的根本原因">${esc(ev.cause || '')}</textarea></label>
          <label>纠正措施<textarea name="measures" rows="2" required placeholder="采取的纠正 / 预防措施">${esc(ev.measures || '')}</textarea></label>
          <label>处理人<input name="operator" required value="王质量"></label>
          <div><button class="btn primary" type="submit">保存原因与措施</button></div>
        </form>` : ''}
      </div>
      <div>
        <h3>复测记录</h3>
        ${retests.length
          ? `<table class="table"><thead><tr><th>时间</th><th>读数</th><th>判定</th><th>记录人</th></tr></thead><tbody>
              ${retests.map(r => `<tr class="${r.exceeded ? 'row-danger' : ''}">
                <td class="sub nowrap">${esc(r.recorded_at)}</td>
                <td class="num">${fmtVal(r.value)} ${esc(ev.unit)}</td>
                <td>${judgeBadge(r)}</td>
                <td>${esc(r.recorded_by)}</td>
              </tr>`).join('')}</tbody></table>`
          : '<div class="empty">尚无复测读数</div>'}
        ${ev.status === 'open' ? `
        <form id="retestForm" class="form-inline">
          <label>复测读数<input name="value" type="number" step="any" required></label>
          <label>记录人<input name="recorded_by" required value="张监测"></label>
          <button class="btn primary" type="submit">登记复测</button>
          <span class="hint">限值：${esc(ev.limit_text)}</span>
        </form>` : ''}
      </div>
    </div>

    ${ev.status === 'open' ? `
    <div class="close-bar">
      ${ev.can_close
        ? '<div class="ok-text">✔ 已记录原因措施且最近一次复测合格，满足关闭条件</div>'
        : `<div><span class="danger-text">暂不可关闭：</span><ul class="blockers">${ev.close_blockers.map(b => `<li>${esc(b)}</li>`).join('')}</ul></div>`}
      <button class="btn ${ev.can_close ? 'primary' : 'disabled'}" id="closeEventBtn">关闭事件</button>
    </div>`
    : `<div class="alert alert-green">事件已于 ${esc(ev.closed_at)} 由 ${esc(ev.closed_by)} 关闭。</div>`}
  </section>`;
}

function bindEventDetail(ev) {
  const dispForm = $('#dispositionForm');
  if (dispForm) {
    dispForm.addEventListener('submit', async e => {
      e.preventDefault();
      const fd = new FormData(dispForm);
      try {
        await api(`/api/events/${ev.id}/disposition`, {
          method: 'POST',
          body: { cause: fd.get('cause').trim(), measures: fd.get('measures').trim(), operator: fd.get('operator').trim() },
        });
        toast('原因与措施已保存');
        renderEvents();
      } catch (err) {
        toast(err.message, 'error');
      }
    });
  }

  const retestForm = $('#retestForm');
  if (retestForm) {
    retestForm.addEventListener('submit', async e => {
      e.preventDefault();
      const fd = new FormData(retestForm);
      try {
        const data = await api('/api/readings', {
          method: 'POST',
          body: {
            point_id: ev.point_id,
            value: Number(fd.get('value')),
            recorded_by: fd.get('recorded_by').trim(),
            event_id: ev.id,
          },
        });
        if (data.retest_passed) toast('复测合格，事件满足关闭条件');
        else toast('复测仍超限，事件继续拦截关联批次', 'error');
        renderEvents();
      } catch (err) {
        toast(err.message, 'error');
      }
    });
  }

  const closeBtn = $('#closeEventBtn');
  if (closeBtn) {
    closeBtn.addEventListener('click', async () => {
      try {
        await api(`/api/events/${ev.id}/close`, { method: 'POST', body: { operator: '王质量' } });
        toast(`事件 ${ev.event_no} 已关闭`);
        renderEvents();
      } catch (err) {
        toast(err.message, 'error');
      }
    });
  }
}

/* ---------------- 页面：批次放行 ---------------- */

async function renderBatches() {
  const [batches, lines] = await Promise.all([api('/api/batches'), api('/api/lines')]);
  const re = state.releaseError;
  $('#app').innerHTML = `
    ${re ? `<div class="alert alert-red">
      <strong>${esc(re.message)}</strong>
      <ul class="blockers">${re.events.map(e => `
        <li><a class="link mono" href="#events" onclick="goEvent(${e.id});return false;">${esc(e.event_no)}</a>
          · ${esc(e.point_name)} · 触发值 ${fmtVal(e.trigger_value)} ${esc(e.unit)}（限值 ${esc(e.limit_text)}）</li>`).join('')}
      </ul>
      <div class="sub">请先在「超限事件」中记录原因措施、复测合格并关闭事件后，再重新提交放行。</div>
      <button class="btn ghost small" id="dismissReleaseError">知道了</button>
    </div>` : ''}
    <section class="panel">
      <div class="panel-head"><h2>批次放行</h2><span class="sub">存在未关闭（含复测仍超限）事件的批次将被拦截</span></div>
      <table class="table"><thead><tr>
        <th>批次号</th><th>产品 / 规格</th><th>产线</th><th>状态</th><th>未关闭事件</th><th>创建时间</th><th>放行信息</th><th>操作</th>
      </tr></thead><tbody>
      ${batches.map(b => `<tr>
        <td class="mono">${esc(b.batch_no)}</td>
        <td>${esc(b.product_name)}<div class="sub">${esc(b.spec)}</div></td>
        <td>${esc(b.line_name)}</td>
        <td>${batchBadge(b.status)}</td>
        <td>${b.open_event_count ? `<span class="badge badge-red">${b.open_event_count} 起</span>` : '<span class="sub">无</span>'}</td>
        <td class="sub nowrap">${esc(b.created_at)}</td>
        <td class="sub">${b.status === 'released' ? `${esc(b.released_at)} · ${esc(b.released_by)}` : '—'}</td>
        <td>${b.status === 'released'
          ? '<span class="sub" title="已放行批次不能补挂事件">已放行</span>'
          : `<button class="btn small ${b.can_release ? 'primary' : 'danger'}" data-release="${b.id}" data-batchno="${esc(b.batch_no)}">
              ${b.can_release ? '提交放行' : `被拦截（${b.open_event_count}）`}
            </button>`}</td>
      </tr>`).join('')}</tbody></table>
      <p class="hint">规则：批次存在未关闭超限事件时禁止提交放行；事件记录原因措施并复测合格后方可关闭；已放行批次不能补挂事件。</p>
    </section>
    <section class="panel">
      <div class="panel-head"><h2>新建批次</h2></div>
      <form id="batchForm" class="form-grid">
        <label>所属产线
          <select name="line_id">${lines.map(l => `<option value="${l.id}">${esc(l.name)}（${esc(l.code)}）</option>`).join('')}</select>
        </label>
        <label>批次号<input name="batch_no" required placeholder="如 B2026-0904"></label>
        <label>产品名称<input name="product_name" required placeholder="如 预灌封注射器无菌包装"></label>
        <label>规格<input name="spec" placeholder="如 1ml×10万支"></label>
        <div class="form-actions"><button class="btn primary" type="submit">创建批次</button></div>
      </form>
    </section>`;

  const dismiss = $('#dismissReleaseError');
  if (dismiss) dismiss.addEventListener('click', () => { state.releaseError = null; renderBatches(); });

  document.querySelectorAll('[data-release]').forEach(btn =>
    btn.addEventListener('click', () => submitRelease(Number(btn.dataset.release), btn.dataset.batchno)));

  $('#batchForm').addEventListener('submit', async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    try {
      await api('/api/batches', {
        method: 'POST',
        body: {
          line_id: Number(fd.get('line_id')),
          batch_no: fd.get('batch_no').trim(),
          product_name: fd.get('product_name').trim(),
          spec: fd.get('spec').trim(),
        },
      });
      toast('批次已创建');
      renderBatches();
    } catch (err) {
      toast(err.message, 'error');
    }
  });
}

async function submitRelease(id, batchNo) {
  if (!confirm(`确认提交批次 ${batchNo} 放行？`)) return;
  try {
    await api(`/api/batches/${id}/submit-release`, { method: 'POST', body: { operator: '王质量' } });
    state.releaseError = null;
    toast(`批次 ${batchNo} 已放行`);
    renderBatches();
  } catch (err) {
    if (err.details && err.details.blocking_events) {
      state.releaseError = { message: err.message, events: err.details.blocking_events };
      renderBatches();
      window.scrollTo({ top: 0, behavior: 'smooth' });
    } else {
      toast(err.message, 'error');
    }
  }
}

/* ---------------- 路由 ---------------- */

const PAGES = {
  overview: renderOverview,
  points: renderPoints,
  readings: renderReadings,
  events: renderEvents,
  batches: renderBatches,
};

function currentTab() {
  const t = location.hash.slice(1);
  return PAGES[t] ? t : 'overview';
}

async function route() {
  const tab = currentTab();
  document.querySelectorAll('#nav a').forEach(a => a.classList.toggle('active', a.dataset.tab === tab));
  $('#app').innerHTML = '<div class="loading">加载中…</div>';
  try {
    await PAGES[tab]();
  } catch (err) {
    $('#app').innerHTML = `<div class="alert alert-red">${esc(err.message)}</div>`;
  }
}

/* 从任意位置跳转到事件详情 */
window.goEvent = id => {
  state.selectedEventId = id;
  if (currentTab() === 'events') renderEvents();
  else location.hash = '#events';
};

window.addEventListener('hashchange', route);
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', route);
else route();
