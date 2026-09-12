/* 页面渲染校验：以最小 DOM 桩加载真实的 static/app.js，对运行中的服务渲染
   全部页面并断言接口数据真正出现在页面上。
   用法：EMR_BASE=http://127.0.0.1:8000 node verify_pages.js */
'use strict';
const vm = require('vm');
const fs = require('fs');
const path = require('path');

const BASE = process.env.EMR_BASE || 'http://127.0.0.1:8000';

function makeEl() {
  return {
    innerHTML: '', textContent: '', className: '', value: '',
    dataset: {},
    classList: { toggle() {}, add() {}, remove() {} },
    addEventListener() {},
    selectedOptions: [{ dataset: { limit: '' } }],
  };
}

const appEl = makeEl();
const sandbox = {
  console, setTimeout, clearTimeout,
  fetch: (p, opts) => fetch(BASE + p, opts),
  document: {
    readyState: 'loading', // 阻止加载时自动 route()，改为逐页显式调用，保证顺序确定
    querySelector(sel) { return sel === '#app' ? appEl : makeEl(); },
    querySelectorAll() { return []; },
    addEventListener() {},
  },
  location: { hash: '' },
  window: { addEventListener() {}, scrollTo() {} },
  confirm: () => true,
  FormData: class {},
};
vm.createContext(sandbox);
vm.runInContext(
  fs.readFileSync(path.join(__dirname, 'static', 'app.js'), 'utf8'),
  sandbox,
  { filename: 'app.js' }
);

const sleep = ms => new Promise(r => setTimeout(r, ms));
let failed = 0;

function expectPage(name, expects) {
  const missing = expects.filter(s => !appEl.innerHTML.includes(s));
  if (missing.length) {
    failed++;
    console.log(`  FAIL  ${name} 缺少内容: ${missing.join(' / ')}`);
  } else {
    console.log(`  PASS  ${name}（${expects.length} 项内容断言）`);
  }
}

(async () => {
  const tabs = [
    ['#overview', '总览页', ['未关闭超限事件', 'EV-0002', 'B2026-0901', '被事件拦截']],
    ['#points', '监测点页', ['P-TEMP-01', '灌装间温度', '18 ~ 26 °C', '≥ 10 Pa', '≤ 3520 个/m³', '新增监测点']],
    ['#readings', '读数登记页', ['读数登记', '最近读数', 'P-TEMP-01', '复测', '超限']],
    ['#events', '超限事件页', ['EV-0001', 'EV-0002', '未关闭', '已关闭']],
    ['#batches', '批次放行页', ['B2026-0901', '被拦截', 'B2026-0831', '已放行', '新建批次']],
  ];
  for (const [hash, name, expects] of tabs) {
    sandbox.location.hash = hash;
    await sandbox.route();
    expectPage(name, expects);
  }

  // 事件详情联动：从事件列表进入 EV-0002（种子数据中 id=2）详情
  sandbox.location.hash = '#events';
  await sandbox.route();
  sandbox.window.goEvent(2);
  await sleep(400);
  expectPage('事件详情页（EV-0002）', ['事件 EV-0002', '原因与纠正措施', '复测记录', '27.1', '暂不可关闭', '关闭事件']);

  console.log(failed ? `\n${failed} 个页面断言失败` : '\n全部页面渲染正常');
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error('页面校验执行异常:', e); process.exit(1); });
