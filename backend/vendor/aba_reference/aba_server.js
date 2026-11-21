// ABA 热门搜索词 本地工作台服务
// 启动: node aba_server.js   然后浏览器打开 http://127.0.0.1:3210
const http = require('http');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { execSync } = require('child_process');
const agent = require('./agent');
const agentMemory = require('./agent_memory');
const archive = require('./archive');
const SOPRules = require('./public/sop_rules');
const {
  abaMetricFormulas,
  sellerMinerMetricFormulas,
  sellerReverseMetricLabels,
} = require('./source_metric_formulas');

const PORT = Number(process.env.PORT || 3210);
const AUTH_FILE = path.join(__dirname, 'aba_auth.json');
const LLM_FILE = path.join(__dirname, 'llm.json');
const VISION_FILE = path.join(__dirname, 'vision.json');
const PUBLIC_DIR = path.join(__dirname, 'public');
const CACHE_DIR = path.join(__dirname, 'cache');
const CDP_PORT_FILE = path.join(__dirname, '.aba_cdp_port');
const BASE = 'https://sellercentral.amazon.com/api/brand-analytics/v1/dashboard/top-search-terms';
const REPORT_ID = 'top-search-terms-report-table';

const sleep = ms => new Promise(r => setTimeout(r, ms));
// 本机直连最多试几次才走紫鸟兜底（所有直连通道共用；可用环境变量 DIRECT_TRIES 改，1~6）
const DIRECT_TRIES = Math.min(6, Math.max(1, Number(process.env.DIRECT_TRIES) || 3));

function loadAuth() {
  if (!fs.existsSync(AUTH_FILE)) return null;
  try { return JSON.parse(fs.readFileSync(AUTH_FILE, 'utf8')); } catch { return null; }
}

async function amazonDirect(pathname, body, retry = DIRECT_TRIES - 1) {
  const auth = loadAuth();
  if (!auth) { const e = new Error('缺少 aba_auth.json，请先运行 node aba_cookie.js'); e.code = 'AUTH'; throw e; }
  try {
    const res = await fetch(BASE + pathname, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
        'User-Agent': auth.ua || 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36',
        'Cookie': auth.cookie
      },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(35000)
    });
    const text = await res.text();
    if (res.status === 401 || res.status === 403) {
      // 只有明确指向登录的响应才算登录失效；其余 403 可能是反爬/限流，交给上层换紫鸟通道重试
      if (text.includes('sign_in') || res.status === 401) {
        const e = new Error('登录态失效：点右上「凭证」粘贴新 cookie，或重跑 node aba_cookie.js'); e.code = 'AUTH'; throw e;
      }
      const e = new Error(`接口 HTTP ${res.status}（疑似限流/反爬）`); e.code = 'HTTP'; throw e;
    }
    try { return JSON.parse(text); }
    catch {
      const e = new Error(`接口返回非 JSON（HTTP ${res.status}）`);
      if (res.status === 401) e.code = 'AUTH';
      throw e;
    }
  } catch (e) {
    if (e.code === 'AUTH') throw e;
    if (retry > 0) { await sleep(1200); return amazonDirect(pathname, body, retry - 1); }
    throw new Error('网络请求失败：' + (e.message || e));
  }
}

/* ---------------- 紫鸟 CDP 兜底通道 ---------------- */
let cdpPortCache = null;
let directState = { ok: null, at: 0 };

async function tryCdpPort(p) {
  try {
    const r = await fetch(`http://127.0.0.1:${p}/json/version`, { signal: AbortSignal.timeout(1200) });
    if (!r.ok) return null;
    const j = await r.json();
    return /Chrome\/129/.test(j.Browser || '') ? p : null;
  } catch { return null; }
}

async function discoverCdpPort() {
  if (cdpPortCache && await tryCdpPort(cdpPortCache)) return cdpPortCache;

  const explicit = [];
  if (process.env.CDP_PORT) explicit.push(process.env.CDP_PORT);
  try { if (fs.existsSync(CDP_PORT_FILE)) explicit.push(fs.readFileSync(CDP_PORT_FILE, 'utf8').trim()); } catch {}
  explicit.push('38979');
  for (const p of explicit) {
    const ok = await tryCdpPort(p);
    if (ok) { cdpPortCache = ok; return ok; }
  }

  let ports = [];
  try {
    const out = execSync('netstat -ano -p TCP', { encoding: 'latin1', timeout: 8000 });
    ports = [...new Set([...out.matchAll(/127\.0\.0\.1:(\d+)\s+[^\n]*LISTENING/gi)].map(m => Number(m[1])))];
  } catch {}
  const hits = (await Promise.all(ports.map(async p => ({ p, ok: await tryCdpPort(p) })))).filter(x => x.ok);
  if (hits.length) {
    cdpPortCache = hits[0].p;
    try { fs.writeFileSync(CDP_PORT_FILE, String(hits[0].p)); } catch {}
    console.log('已发现紫鸟调试端口:', hits[0].p);
    return hits[0].p;
  }
  return null;
}

let cdpLock = Promise.resolve();
function withCdpLock(fn) {
  const next = cdpLock.then(fn, fn);
  cdpLock = next.then(() => {}, () => {});
  return next;
}

async function amazonViaCDP(pathname, body) {
  const port = await discoverCdpPort();
  if (!port) throw new Error('直连不通，且未找到紫鸟调试端口。请用 agent 模式打开紫鸟：ziniao.exe browser start --id <storeId> --mode agent');

  const list = await fetch(`http://127.0.0.1:${port}/json/list`).then(r => r.json());
  let pages = list.filter(t => t.type === 'page' && /sellercentral\.amazon\.com/.test(t.url || ''));
  if (!pages.length) {
    // 紫鸟开着但没开卖家后台：自动开一个标签页（登录态浏览器级共享，无需人工操作）
    try {
      const created = await fetch(`http://127.0.0.1:${port}/json/new?${encodeURIComponent('https://sellercentral.amazon.com/')}`, { method: 'PUT', signal: AbortSignal.timeout(5000) }).then(r => r.json());
      if (created && created.id) {
        console.log('[CDP] 自动打开卖家后台标签页:', created.id.slice(0, 8));
        await sleep(9000); // 等页面加载完成
        pages = [created];
      }
    } catch {}
  }
  if (!pages.length) throw new Error('紫鸟里没有打开亚马逊卖家后台，请先打开品牌分析页面（保持标签页不要关）');

  let lastErr;
  for (const page of pages) {
    try { return await callViaPage(port, page, pathname, body); }
    catch (e) {
      if (e.code === 'AUTH') throw e;
      lastErr = e;
      console.log('[CDP] 页面 ' + page.id.slice(0, 8) + ' 失败: ' + e.message);
    }
  }
  throw new Error('紫鸟通道请求失败：' + (lastErr ? lastErr.message : '未知错误'));
}

async function callViaPage(port, page, pathname, body) {
  const ver = await fetch(`http://127.0.0.1:${port}/json/version`).then(r => r.json());
  const wsc = new WebSocket(ver.webSocketDebuggerUrl);
  await new Promise((res, rej) => {
    wsc.addEventListener('open', res, { once: true });
    wsc.addEventListener('error', () => rej(new Error('CDP 连接失败')), { once: true });
  });

  let id = 0; const pend = new Map();
  wsc.addEventListener('message', ev => {
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    if (m.id && pend.has(m.id)) {
      const p = pend.get(m.id); pend.delete(m.id);
      m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result);
    }
  });
  const send = (method, params, sessionId) => new Promise((resolve, reject) => {
    const mid = ++id; pend.set(mid, { resolve, reject });
    const payload = { id: mid, method, params };
    if (sessionId) payload.sessionId = sessionId;
    wsc.send(JSON.stringify(payload));
    setTimeout(() => { if (pend.has(mid)) { pend.delete(mid); reject(new Error('CDP 超时: ' + method)); } }, 60000);
  });

  try {
    const { sessionId } = await send('Target.attachToTarget', { targetId: page.id, flatten: true });
    await send('Runtime.enable', {}, sessionId);

    const expr = `(async () => {
      const r = await fetch(${JSON.stringify(BASE + pathname)}, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
        body: ${JSON.stringify(JSON.stringify(body))}
      });
      return JSON.stringify({ status: r.status, text: await r.text() });
    })()`;

    const res = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true }, sessionId);
    if (!res.result || !res.result.value) throw new Error('紫鸟通道返回空');
    const { status, text } = JSON.parse(res.result.value);
    if (status === 401 || status === 403) { const e = new Error('紫鸟里的登录态失效，请在浏览器里重新登录卖家后台'); e.code = 'AUTH'; throw e; }
    if (status !== 200) {
      const e = new Error(`紫鸟通道 HTTP ${status}` + (text ? '：' + String(text).slice(0, 100) : ''));
      e.status = status;
      throw e;
    }
    try { return JSON.parse(text); }
    catch { throw new Error('紫鸟通道返回非 JSON'); }
  } finally {
    try { wsc.close(); } catch {}
  }
}

async function probeDirect() {
  const now = Date.now();
  if (directState.ok !== null && now - directState.at < 180000) return directState.ok;
  try {
    const r = await fetch('https://sellercentral.amazon.com/favicon.ico', { method: 'GET', signal: AbortSignal.timeout(3500) });
    directState = { ok: r.status < 500, at: Date.now() };
  } catch {
    directState = { ok: false, at: Date.now() };
  }
  return directState.ok;
}

// 统一入口：直连优先，不通自动走紫鸟
let lastAuthHeal = 0;
async function amazon(pathname, body) {
  if (await probeDirect()) {
    try {
      return await amazonDirect(pathname, body);
    } catch (e) {
      if (e.code === 'AUTH') {
        // cookie 过期但紫鸟里其实是登录着的：自动从紫鸟抓一次新 cookie 再试
        if (Date.now() - lastAuthHeal > 60000) {
          lastAuthHeal = Date.now();
          try {
            await grabCookies('aba');
            console.log('[auth] 已从紫鸟自动更新 ABA cookie，重试');
            return await amazonDirect(pathname, body);
          } catch (e2) {
            console.log('[auth] 自动抓 cookie 失败：', e2.message);
          }
        }
        console.log('直连登录态失效，改走紫鸟通道');
      } else {
        directState = { ok: false, at: Date.now() };
        console.log('直连失败，切换紫鸟通道:', e.message);
      }
    }
  }
  return amazonViaCDP(pathname, body);
}

function buildBody(q, page) {
  const ALLOWED_SIZES = [10, 25, 50, 100];
  const pageSize = ALLOWED_SIZES.includes(Number(q.pageSize)) ? Number(q.pageSize) : 25;
  const filters = [
    { id: 'reporting-range', value: q.range, valueType: null }
  ];
  // 依据报告范围挂接时间筛选
  if (q.range === 'weekly' && q.week) filters.push({ id: 'weekly-week', value: q.week, valueType: 'weekly' });
  if (q.range === 'daily' && q.day) filters.push({ id: 'day', value: q.day, valueType: 'daily' });
  if (q.range === 'monthly' && q.monthDate) {
    filters.push({ id: 'monthly-year', value: String(q.year) });
    filters.push({ id: `${q.year}-month`, value: q.monthDate, valueType: 'monthly' });
  }
  if (q.range === 'quarterly' && q.quarter) {
    filters.push({ id: 'quarterly-year', value: String(q.year) });
    filters.push({ id: `${q.year}-quarter`, value: q.quarter, valueType: 'quarterly' });
  }
  if (q.asins) filters.push({ id: 'asins', value: q.asins, valueType: null });
  if (q.brand) filters.push({ id: 'brand-freeform', value: q.brand, valueType: null });
  if (q.category) filters.push({ id: 'category-dropdown', value: q.category, valueType: null });
  if (q.searchTerm) filters.push({ id: 'search-term-freeform', value: q.searchTerm, valueType: null });

  return {
    viewId: 'top-search-terms-default-view',
    filterSelections: filters,
    reportOperations: [{
      reportId: REPORT_ID,
      reportType: 'TABLE',
      pageNumber: page,
      pageSize: pageSize,
      sortByColumnId: q.sortBy || 'st-search-frequency',
      ascending: q.ascending !== false
    }],
    selectedCountries: [q.country || 'us'],
    reportId: REPORT_ID
  };
}

function slimMetadata(j) {
  const view = j.metadata.viewsRoot.views.find(v => v.id === 'top-search-terms-default-view');
  const rangeFilter = view.filters.find(f => f.id === 'reporting-range');
  const ranges = (rangeFilter.values || []).map(r => {
    const c = r.child || {};
    const item = {
      value: r.value, label: r.localizedDisplayValue, default: !!r.defaultSelection,
      child: { id: c.id || '', type: c.type || '', valueType: c.valueType || '' }
    };
    if (c.type === 'DATE_PICKER') { item.child.startDate = c.startDate; item.child.endDate = c.endDate; item.child.defaultValue = c.defaultValue; }
    if (c.type === 'DROPDOWN') {
      item.child.values = (c.values || []).map(v => ({
        label: v.localizedDisplayValue, value: v.value, default: !!v.defaultSelection,
        child: v.child ? { id: v.child.id, valueType: v.child.valueType, values: (v.child.values || []).map(x => ({ label: x.localizedDisplayValue, value: x.value, default: !!x.defaultSelection })) } : null
      }));
    }
    return item;
  });
  const cats = view.filters.find(f => f.id === 'category-dropdown');
  const rep = view.reports[0];
  return {
    title: view.localizedDescription,
    ranges,
    categories: (cats.values || []).map(v => ({ label: v.localizedDisplayValue, value: v.value })),
    pageSizes: rep.allowedPageSizes || [10, 25, 50, 100],
    defaultPageSize: rep.defaultPageSize || 25,
    columns: (rep.columns || []).map(c => ({ id: c.id, label: c.localizedLabel, type: c.type, sub: (c.columns || c.subColumns || []).map(s => s.id) }))
  };
}

const COLS = [
  ['st-search-term', '搜索词'],
  ['st-search-frequency', '搜索频率排名'],
  ['st-top-brands', '点击量最高的品牌'],
  ['st-top-categories', '点击量最高的分类'],
  ['st-top-asin-1', 'ASIN1'], ['st-top-asin-title-1', '商品名1'], ['st-top-asin-click-share-1', '点击占比1'], ['st-top-asin-conversion-share-1', '转化份额1'],
  ['st-top-asin-2', 'ASIN2'], ['st-top-asin-title-2', '商品名2'], ['st-top-asin-click-share-2', '点击占比2'], ['st-top-asin-conversion-share-2', '转化份额2'],
  ['st-top-asin-3', 'ASIN3'], ['st-top-asin-title-3', '商品名3'], ['st-top-asin-click-share-3', '点击占比3'], ['st-top-asin-conversion-share-3', '转化份额3']
];
const esc = v => {
  const s = v === undefined || v === null ? '' : String(v);
  return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
};

function toCSV(rows) {
  const head = COLS.map(c => c[1]).join(',') + ',商品链接1,商品链接2,商品链接3';
  const lines = [head];
  for (const r of rows) {
    const vals = COLS.map(c => {
      if (c[0] === 'st-top-brands') return [r['st-top-brand-1'], r['st-top-brand-2'], r['st-top-brand-3']].filter(Boolean).join(' / ');
      if (c[0] === 'st-top-categories') return [r['st-top-category-1'], r['st-top-category-2'], r['st-top-category-3']].filter(Boolean).join(' / ');
      return r[c[0]];
    });
    const links = [1, 2, 3].map(i => r['st-top-asin-' + i] ? `https://www.amazon.com/dp/${r['st-top-asin-' + i]}` : '');
    lines.push([...vals, ...links].map(esc).join(','));
  }
  return lines.join('\r\n');
}

function readBody(req, maxBytes = 5e6) {
  return new Promise((resolve, reject) => {
    let data = '';
    let tooLarge = false;
    req.on('data', c => {
      if (tooLarge) return;
      data += c;
      if (Buffer.byteLength(data, 'utf8') > maxBytes) { tooLarge = true; data = ''; }
    });
    req.on('end', () => {
      if (tooLarge) return reject(Object.assign(new Error(`请求体超过 ${Math.floor(maxBytes / 1024 / 1024)} MB 上限`), { code: 'BODY_TOO_LARGE' }));
      try { resolve(data ? JSON.parse(data) : {}); } catch { reject(new Error('请求体不是合法 JSON')); }
    });
    req.on('error', reject);
  });
}

function sendJSON(res, code, obj) {
  const s = JSON.stringify(obj);
  res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
  res.end(s);
}

const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png', '.ico': 'image/x-icon' };

/* ---------------- 精准大词分析 ---------------- */
const SPEC_WORDS = new Set(['lb', 'lbs', 'pound', 'pounds', 'kg', 'kgs', 'adjustable', 'hex', 'rubber', 'coated', 'neoprene', 'cast', 'iron', 'chrome', 'grip', 'set', 'sets', 'pair', 'rack', 'stand', 'storage', 'weight', 'weights', 'dumbbells', 'dumbells', '1', '2', '5', '10', '15', '20', '25', '30', '35', '40', '45', '50', '55', '60', '65', '70', '75', '80', '90', '100', '120', '150', '200']);
const SCENE_WORDS = new Set(['home', 'gym', 'women', 'woman', 'men', 'man', 'beginner', 'beginners', 'workout', 'workouts', 'training', 'exercise', 'fitness', 'kids', 'senior', 'seniors']);
const WEIGHT_UNITS = new Set(['lb', 'lbs', 'pound', 'pounds', 'kg', 'kgs', 'g', 'oz', 'gram', 'grams']);

// 归并连续重复字母：dumbbell / dumbell -> dumbel
function normalizeWord(s) {
  return String(s || '').toLowerCase().replace(/(.)\1+/g, '$1');
}

async function fetchAllTerms(root, week, country, maxRows = 1000, asins = '', opts = {}) {
  const range = opts.range === 'monthly' ? 'monthly' : 'weekly';
  const month = opts.month || '';
  const year = month ? String(month).slice(0, 4) : '';
  const rows = [];
  let page = 1, total = 0;
  while (page <= 120 && rows.length < maxRows) {
    // 按月时 ABA 要的是「当月最后一天」的完整日期，例如 2026-09-30
    let monthDate = '';
    if (range === 'monthly' && /^\d{4}-\d{2}$/.test(month)) {
      const y = Number(month.slice(0, 4)), mm = Number(month.slice(5, 7));
      const last = new Date(Date.UTC(y, mm, 0)).getUTCDate();
      monthDate = `${month}-${String(last).padStart(2, '0')}`;
    }
    const j = await amazon('/reports', buildBody({
      range, week, country, searchTerm: root, asins, pageSize: 100,
      monthDate, year
    }, page));
    const rep = (j.reportsV2 || [])[0] || {};
    total = rep.totalItems || 0;
    const batch = rep.rows || [];
    for (const r of batch) rows.push(r);
    if (batch.length < 100 || rows.length >= total || rows.length >= maxRows) break;
    page++;
    await sleep(180);
  }
  return { total, rows, pages: page, truncated: rows.length < total };
}

function analyzeTerms(rows, root, limit = 100, opts = {}) {
  // maxRank=0 表示不限（四级 30w 以外也要进候选），只有 >0 才截断
  const maxRankRaw = Number(opts.maxRank);
  const maxRank = Number.isFinite(maxRankRaw) && maxRankRaw >= 0 ? maxRankRaw : 300000;
  const excludeWords = (opts.excludeWords || [])
    .map(w => String(w).trim().toLowerCase())
    .filter(Boolean);
  const excludeSpec = !!opts.excludeSpec;
  const rootNorm = normalizeWord(root.trim());
  // 词组词根（dumbbell rack）：ABA 的搜索词筛选是模糊匹配，会返回 "dumbbell set with rack" 这类
  // 词组不连写的词。只认连续词组会把它们全判成「不含词根」，所以多词根改成「所有词都出现」即可。
  const rootLower = root.trim().toLowerCase();
  const rootTokens = [...new Set(rootLower.replace(/[^a-z0-9\s]/g, ' ').split(/\s+/)
    .filter(w => w.length > 1 && !STOP_WORDS.has(w)).map(normalizeWord))];

  // 品牌频次（用于识别品牌词 / 竞品品牌词）
  const brandFreq = new Map();
  for (const r of rows) {
    for (let i = 1; i <= 3; i++) {
      const b = (r['st-top-brand-' + i] || '').trim();
      if (b && b !== '-') brandFreq.set(b, (brandFreq.get(b) || 0) + 1);
    }
  }
  const topBrands = [...brandFreq.entries()].sort((a, b) => b[1] - a[1]).slice(0, 20).map(([b]) => b.toLowerCase());

  // SOP 1.5 / 1.7 / 第八步：四级流量分层
  const TIER_LABEL = { L1: '一级词', L2: '二级词', L3: '三级词', L4: '四级词' };  // 与广告架构话术一致
  const tierOf = rank => rank <= 30000 ? 'L1' : rank <= 150000 ? 'L2' : rank <= 300000 ? 'L3' : 'L4';
  const tierStats = { L1: 0, L2: 0, L3: 0, L4: 0 };

  const items = [];
  for (const r of rows) {
    const term = (r['st-search-term'] || '').trim();
    if (!term) continue;
    const lower = term.toLowerCase();
    const tokens = lower.split(/\s+/);
    const words = tokens.length;
    const rank = Number(r['st-search-frequency']) || 999999;
    const tier = tierOf(rank);
    tierStats[tier]++;

    const normTokens = tokens.map(normalizeWord);
    const hitRoot = rootTokens.length > 1
      ? rootTokens.every(t => normTokens.includes(t))          // 多词根：所有词都出现即可
      : (normalizeWord(lower).includes(rootNorm) || normTokens.includes(rootNorm));
    // 略泛词：不含完整词根，但含品类头词（rack/stand...）＋ weight(s)，如 weight rack / weight holder
    // 这类词在客户词库里是「高（略泛）」，放行但单独标，别当高相关
    const relaxHead = rootTokens.length > 1 ? rootTokens[rootTokens.length - 1] : '';
    const RELAX_SYN = { rack: ['rack', 'stand', 'holder', 'storage', 'shelf', 'tower'], stand: ['stand', 'rack', 'holder'] };
    const relaxWords = RELAX_SYN[relaxHead] || (relaxHead ? [relaxHead] : []);
    const hitRelax = !hitRoot && relaxWords.some(w => normTokens.includes(w))
      && normTokens.some(t => t === 'weight' || t === 'weights' || t === 'dumbbell' || t === 'dumbell');
    const hitBrand = topBrands.find(b => b.length >= 3 && lower.includes(b)) || null;

    const click1 = Number(r['st-top-asin-click-share-1']) || 0;
    const conv1 = Number(r['st-top-asin-conversion-share-1']) || 0;
    const conv2 = Number(r['st-top-asin-conversion-share-2']) || 0;
    const conv3 = Number(r['st-top-asin-conversion-share-3']) || 0;
    const convSum3 = conv1 + conv2 + conv3;

    // SOP 2.2.7：购买型词 = 转化份额 / 点击份额 > 60%
    const buyRatio = click1 > 0 ? conv1 / click1 : 0;
    const isBuyWord = click1 > 0 && buyRatio >= 0.6;

    // SOP 2.2.7：垄断词 = top1 转化份额 > 25%，或前 3 名累计 > 35%
    const isMonopoly = conv1 > 25 || convSum3 > 35;

    let kind = 'other';
    if (words === 1) kind = 'root';
    else if (hitBrand) kind = 'brand';
    else if (tokens.some(t => SPEC_WORDS.has(t))) kind = 'spec';
    else if (tokens.some(t => SCENE_WORDS.has(t))) kind = 'scene';

    // 投放优先级：一级 + 购买型 + 非垄断 最优先
    let priority = 'P3';
    if (hitRoot && kind !== 'brand' && !isMonopoly) {
      if (tier === 'L1') priority = isBuyWord ? 'P0' : 'P1';
      else if (tier === 'L2' && isBuyWord) priority = 'P2';
    }

    const extra = tokens.filter(t => !rootTokens.includes(normalizeWord(t)));
    // 纯规格词：除词根外只有数字/重量单位（如 30 lb dumbbell、fixed weight dumbbell 由排除词处理）
    // 只要出现「数字+重量单位」（15 lbs、20lb、50 pound），不管后面跟什么，都算规格词
    const hasWeightSpec = /(^|\s)\d+(\.\d+)?\s*(lb|lbs|kg|kgs|oz|pound|pounds)(\s|$)/i.test(lower);
    const specOnly = words > 1 && (hasWeightSpec || tokens.every(t =>
      /^\d+(\.\d+)?(lb|lbs|kg|kgs|oz|g|pound|pounds)?$/.test(t) || WEIGHT_UNITS.has(t) || rootTokens.includes(normalizeWord(t))
    ));

    items.push({
      term, rank, tier, tierLabel: TIER_LABEL[tier], words, kind, extra, specOnly,
      clickShare: click1,
      convShare: conv1,
      convSum3: Math.round(convSum3 * 100) / 100,
      buyRatio: Math.round(buyRatio * 1000) / 10,
      isBuyWord, isMonopoly, priority,
      hitRoot, hitBrand, hitRelax,
      topBrand: r['st-top-brand-1'] || '',
      topAsin: r['st-top-asin-1'] || '',
      category: r['st-top-category-1'] || '',
      // 前 3 名品牌/分类/商品（导出「ABA正向/反向」原始表要用）
      top: [1, 2, 3].map(i => ({
        brand: r['st-top-brand-' + i] || '',
        category: r['st-top-category-' + i] || '',
        asin: r['st-top-asin-' + i] || '',
        title: r['st-top-asin-title-' + i] || '',
        click: Number(r['st-top-asin-click-share-' + i]) || 0,
        conv: Number(r['st-top-asin-conversion-share-' + i]) || 0
      })),
      score: Math.round((100 - Math.log10(Math.max(rank, 1)) * 10 + (isBuyWord ? 15 : 0) - (isMonopoly ? 20 : 0)) * 10) / 10
    });
  }

  const excluded = { root: 0, brand: 0, offRoot: 0, long: 0, overThreshold: 0, byWord: 0, specOnly: 0 };
  const candidates = [];
  const brandItems = [];
  const dropped = [];
  for (const it of items) {
    // 词根本身不过滤（它就是一级核心词，导出第一行），只在 kind 上标 root
    if (it.term.toLowerCase() === rootLower) { it.kind = 'root'; it.isRoot = true; }
    if (!it.hitRoot && !it.hitRelax) { it.status = 'offroot'; excluded.offRoot++; continue; }
    if (it.hitBrand) { it.status = 'brand'; excluded.brand++; brandItems.push(it); continue; }
    if (it.words > 6) { it.status = 'long'; excluded.long++; continue; }
    if (maxRank > 0 && it.rank > maxRank) { it.status = 'over'; excluded.overThreshold++; continue; }
    if (excludeWords.some(w => it.term.toLowerCase().includes(w))) { it.status = 'word'; excluded.byWord++; dropped.push(it); continue; }
    if (excludeSpec && it.specOnly) { it.status = 'spec'; excluded.specOnly++; dropped.push(it); continue; }
    it.status = 'keep';
    candidates.push(it);
  }

  // 主排序按 ABA 排名（SOP 第七步：排名是流量大小的第一判据），priority 只作标签
  candidates.sort((a, b) => a.rank - b.rank);
  brandItems.sort((a, b) => a.rank - b.rank);

  // 高频修饰词：基于全量词表统计（不受排除词影响——点掉的词仍留在 chips 里，可再点回来）
  // 单复数归并到短形（sets → set），排除时子串匹配本来就会连带
  const modStat = new Map();
  const singular = w => (w.length > 3 && w.endsWith('s') && !w.endsWith('ss')) ? w.slice(0, -1) : w;
  for (const it of items) {
    if (it.kind === 'root' || it.kind === 'brand') continue;
    for (const raw of it.extra) {
      const w = singular(String(raw || '').toLowerCase());
      if (!w || w.length < 2 || /^\d+$/.test(w)) continue;
      if (STOP_WORDS.has(w) || WEIGHT_UNITS.has(w)) continue;
      const cur = modStat.get(w) || { word: w, count: 0, l1: 0, rankSum: 0 };
      cur.count++; cur.rankSum += it.rank;
      if (it.tier === 'L1') cur.l1++;
      modStat.set(w, cur);
    }
  }
  const modifiers = [...modStat.values()]
    .map(m => ({
      word: m.word, count: m.count, l1Count: m.l1, avgRank: Math.round(m.rankSum / m.count),
      // 词频清洗口径（对应 wordcounter 的 Keyword Density）：该修饰词出现在多少比例的词条里
      density: Math.round(m.count / Math.max(1, items.length) * 1000) / 10,
      cls: SCENE_WORDS.has(m.word) ? 'scene' : (SPEC_WORDS.has(m.word) ? 'spec' : 'other')
    }))
    .filter(m => m.count >= 2)
    .sort((a, b) => b.count - a.count || b.l1Count - a.l1Count || a.avgRank - b.avgRank)
    .slice(0, 30);

  return {
    scanned: items.length,
    tierStats,
    excluded,
    eligible: candidates.length,
    maxRank,
    appliedFilters: { maxRank, excludeWords, excludeSpec },
    modifiers,
    items: candidates.slice(0, limit),
    brandItems: brandItems.slice(0, 25),
    dropped: dropped.slice(0, 40),
    rawTotal: items.length,
    rawRows: opts.includeRaw ? items.slice(0, 3000).map(it => ({
      term: it.term, rank: it.rank, tier: it.tier, tierLabel: it.tierLabel,
      status: it.status, words: it.words, specOnly: it.specOnly,
      topBrand: it.topBrand, category: it.category, topAsin: it.topAsin,
      clickShare: it.clickShare, convShare: it.convShare,
      buyRatio: it.buyRatio, isMonopoly: it.isMonopoly,
      top: it.top
    })) : undefined
  };
}

/* ---------------- 前台搜索结果相关性校验 ---------------- */
async function cdpEvaluateOnNewPage(url, expr, waitMs = 7000) {
  const port = await discoverCdpPort();
  if (!port) throw new Error('未找到紫鸟调试端口，请用 agent 模式打开紫鸟');
  const ver = await fetch(`http://127.0.0.1:${port}/json/version`).then(r => r.json());
  const wsc = new WebSocket(ver.webSocketDebuggerUrl);
  await new Promise((res, rej) => {
    wsc.addEventListener('open', res, { once: true });
    wsc.addEventListener('error', () => rej(new Error('CDP 连接失败')), { once: true });
  });
  let id = 0; const pend = new Map();
  wsc.addEventListener('message', ev => {
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    if (m.id && pend.has(m.id)) { const p = pend.get(m.id); pend.delete(m.id); m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result); }
  });
  const send = (method, params, sessionId) => new Promise((resolve, reject) => {
    const mid = ++id; pend.set(mid, { resolve, reject });
    const payload = { id: mid, method, params };
    if (sessionId) payload.sessionId = sessionId;
    wsc.send(JSON.stringify(payload));
    setTimeout(() => { if (pend.has(mid)) { pend.delete(mid); reject(new Error('CDP 超时: ' + method)); } }, 50000);
  });
  let targetId = null;
  try {
    ({ targetId } = await send('Target.createTarget', { url: 'about:blank' }));
    const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });
    await send('Page.enable', {}, sessionId);
    await send('Page.navigate', { url }, sessionId);
    await sleep(waitMs);
    const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true }, sessionId);
    return r.result ? r.result.value : null;
  } finally {
    try { if (targetId) await send('Target.closeTarget', { targetId }); } catch {}
    try { wsc.close(); } catch {}
  }
}

const AMZ_SEARCH_PARSER = `(() => {
  const cards = [...document.querySelectorAll('div[data-component-type="s-search-result"]')];
  const items = cards.slice(0, 60).map(c => {
    const asin = c.getAttribute('data-asin') || '';
    const h2 = c.querySelector('h2');
    const title = h2 ? h2.innerText.trim() : '';
    const pe = c.querySelector('.a-price .a-offscreen');
    const price = pe ? (parseFloat(pe.textContent.replace(/[^0-9.]/g, '')) || 0) : 0;
    const img = c.querySelector('img.s-image');
    const html = c.outerHTML || '';
    const isSponsored = /"searchProductType"\s*:\s*"SPONSORED"/i.test(html);
    const isOrganic = /"searchProductType"\s*:\s*"ORGANIC"/i.test(html);
    const sponsored = isSponsored || (!isOrganic && /(^|>)\s*Sponsored\s*</i.test(html));
    return { asin, title: title.slice(0, 220), price, img: img ? img.getAttribute('src') : '', sponsored };
  });
  return JSON.stringify({ items, captcha: /Enter the characters|robot check/i.test(document.body.innerText || '') });
})()`;

const AMZ_PRODUCT_PARSER = `(() => {
  const t = document.getElementById('productTitle');
  const title = t ? t.innerText.trim() : '';
  const p = document.querySelector('.a-price .a-offscreen') || document.querySelector('#corePrice_feature_div .a-offscreen');
  const price = p ? (parseFloat(p.textContent.replace(/[^0-9.]/g, '')) || 0) : 0;
  const img = document.getElementById('landingImage');
  const m = location.pathname.match(/\\/dp\\/([A-Z0-9]{10})/i) || [];
  return JSON.stringify({ title, price, img: img ? (img.getAttribute('src') || '') : '', asin: m[1] || '' });
})()`;

const SEARCH_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36';
const SEARCH_HEADERS = {
  'User-Agent': SEARCH_UA,
  'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
  'Accept-Language': 'en-US,en;q=0.9',
  'Cookie': 'i18n-prefs=USD; lc-main=en_US'
};

// 解析亚马逊前台搜索 HTML（Node 端，直连用）
function parseAmazonSearchHtml(html) {
  const out = [];
  const re = /<div[^>]*data-component-type="s-search-result"[^>]*>/g;
  const tags = [];
  let m;
  while ((m = re.exec(html))) tags.push({ tag: m[0], start: m.index, end: re.lastIndex });
  if (!tags.length) return out;
  for (let i = 0; i < tags.length && out.length < 60; i++) {
    const endPos = i + 1 < tags.length ? tags[i + 1].start : Math.min(tags[i].end + 14000, html.length);
    const chunk = html.slice(tags[i].start, endPos);
    const asin = (tags[i].tag.match(/data-asin="([A-Z0-9]{10})"/) || [])[1] || '';
    if (!asin) continue;
    const titleM = chunk.match(/<h2[\s\S]{0,700}?<span[^>]*>([\s\S]{10,400}?)<\/span>/);
    const title = titleM ? titleM[1].replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim() : '';
    const priceM = chunk.match(/class="a-offscreen">\s*\$\s*([\d,]+\.?\d*)\s*</);
    const price = priceM ? Number(priceM[1].replace(/,/g, '')) : 0;
    const imgM = chunk.match(/<img[^>]+class="s-image"[^>]*src="([^"]+)"/) || chunk.match(/<img[^>]+src="([^"]+)"[^>]*class="s-image"/);
    // 广告位判定：优先用卡片内嵌的 searchProductType 字段，退回可见的 Sponsored 文本
    const isSponsored = /"searchProductType"\s*:\s*"SPONSORED"/i.test(chunk);
    const isOrganic = /"searchProductType"\s*:\s*"ORGANIC"/i.test(chunk);
    const sponsored = isSponsored || (!isOrganic && /(^|>)\s*Sponsored\s*</i.test(chunk));
    out.push({ asin, title: title.slice(0, 220), price, img: imgM ? imgM[1] : '', sponsored });
  }
  return out;
}

async function searchAmazonDirect(term) {
  const url = 'https://www.amazon.com/s?k=' + encodeURIComponent(term);
  const res = await fetch(url, { headers: SEARCH_HEADERS, signal: AbortSignal.timeout(18000) });
  if (!res.ok) throw new Error('HTTP ' + res.status);
  const html = await res.text();
  if (/Enter the characters|robot check/i.test(html)) throw new Error('触发亚马逊验证码');
  const items = parseAmazonSearchHtml(html);
  if (!items.length) throw new Error('没解析到搜索结果');
  return items;
}

async function searchAmazonViaCDP(term) {
  const url = 'https://www.amazon.com/s?k=' + encodeURIComponent(term);
  const raw = await cdpEvaluateOnNewPage(url, AMZ_SEARCH_PARSER, 7000);
  const parsed = raw ? JSON.parse(raw) : { items: [] };
  if (parsed.captcha) throw new Error('触发亚马逊验证码');
  return parsed.items || [];
}

// 前台搜索：直连优先（最多试 DIRECT_TRIES 次，默认 3），仍失败再兜底走紫鸟后台标签页
async function searchAmazon(term) {
  let last;
  for (let k = 0; k < DIRECT_TRIES; k++) {
    try { return await searchAmazonDirect(term); }
    catch (e) {
      last = e;
      console.log(`[搜索] 直连失败 第${k + 1}/${DIRECT_TRIES}次(${e.message}):`, term);
      if (k < DIRECT_TRIES - 1) await sleep(1500 * (k + 1) + Math.random() * 1000);
    }
  }
  console.log(`[搜索] 直连 ${DIRECT_TRIES} 次失败，兜底走紫鸟:`, term);
  try { return await searchAmazonViaCDP(term); }
  catch (e) { throw new Error(`前台搜索失败（直连：${last && last.message}；紫鸟：${e.message}）`); }
}

/* SOP 第三步方法二：前台搜核心大词，取自然位前 topN，统计在多个词下重复出现的 ASIN 作竞对。
   只有 1 个词时没法算「重复」，直接取自然位前 10 */
async function frontRepeatAsins(terms, topN = 20, limit = 10) {
  const list = [...new Set((terms || []).map(t => String(t || '').trim().toLowerCase()).filter(Boolean))].slice(0, 5);
  const freq = new Map(), errors = [];
  await Promise.all(list.map(async t => {
    try {
      const items = await searchAmazon(t);
      const seen = new Set();
      items.filter(x => x.asin && !x.sponsored).slice(0, topN).forEach(x => {
        if (seen.has(x.asin)) return; seen.add(x.asin);
        freq.set(x.asin, (freq.get(x.asin) || 0) + 1);
      });
    } catch (e) { errors.push(t + '：' + e.message); }
  }));
  const sorted = [...freq.entries()].sort((a, b) => b[1] - a[1]);
  const ok = list.length - errors.length;
  const picked = ok >= 2 ? sorted.filter(x => x[1] >= 2) : sorted;
  return { asins: picked.slice(0, limit).map(x => x[0]), hits: Object.fromEntries(picked.slice(0, limit)), searched: list, errors };
}

async function fetchProductInfo(asin) {
  let last;
  for (let k = 0; k < DIRECT_TRIES; k++) {
    try { return await fetchProductInfoDirect(asin); }
    catch (e) {
      last = e;
      console.log(`[商品] 直连失败 第${k + 1}/${DIRECT_TRIES}次(${e.message}):`, asin);
      if (k < DIRECT_TRIES - 1) await sleep(1500 * (k + 1) + Math.random() * 1000);
    }
  }
  console.log(`[商品] 直连 ${DIRECT_TRIES} 次失败，兜底走紫鸟:`, asin);
  try {
    const raw = await cdpEvaluateOnNewPage('https://www.amazon.com/dp/' + asin, AMZ_PRODUCT_PARSER, 7000);
    return raw ? JSON.parse(raw) : {};
  } catch (e) { throw new Error(`商品页抓取失败（直连：${last && last.message}；紫鸟：${e.message}）`); }
}

async function fetchProductInfoDirect(asin) {
  {
    const res = await fetch('https://www.amazon.com/dp/' + asin, { headers: SEARCH_HEADERS, signal: AbortSignal.timeout(25000) });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const html = await res.text();
    const titleM = html.match(/<span[^>]+id="productTitle"[^>]*>([\s\S]*?)<\/span>/);
    const title = titleM ? titleM[1].replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim() : '';
    const priceM = html.match(/class="a-offscreen">\s*\$\s*([\d,]+\.?\d*)\s*</);
    const price = priceM ? Number(priceM[1].replace(/,/g, '')) : 0;
    // 主图：landingImage 标签里取 data-old-hires / src（属性顺序不固定），兜底取页面第一个 hires
    const tagM = html.match(/<img[^>]*id="landingImage"[^>]*>/);
    const tag = tagM ? tagM[0] : '';
    const img = (tag.match(/data-old-hires="([^"]+)"/) || [])[1]
      || (tag.match(/\ssrc="(https:[^"]+)"/) || [])[1]
      || (html.match(/data-old-hires="(https:[^"]+)"/) || [])[1]
      || (html.match(/"hiRes":"(https:[^"]+)"/) || [])[1] || '';
    if (!title) throw new Error('解析不到标题');
    return { title, price, img };
  }
}

const STOP_WORDS = new Set(['the', 'and', 'for', 'with', 'of', 'to', 'in', 'on', 'a', 'an', 'new', 'pcs', 'pack', 'inch', 'inches']);

function termTokens(s) {
  return String(s || '').toLowerCase().replace(/[^a-z0-9\s]/g, ' ').split(/\s+/)
    .filter(w => w.length > 1 && !STOP_WORDS.has(w))
    .map(w => (w.length > 3 && w.endsWith('s') && !w.endsWith('ss')) ? w.slice(0, -1) : w);
}

// 判定“是不是同一类产品”看的是品类词，不是整条标题：
// 去掉数字/规格词和 tier/adjustable/heavy duty 这类修饰词，取前 3 个品类词（去重）
const TYPE_MODIFIERS = new Set(['tier', 'layer', 'adjustable', 'load', 'storage', 'heavy', 'duty', 'home', 'gym', 'premium',
  'upgraded', 'upgrade', 'compact', 'portable', 'durable', 'steel', 'metal', 'only', 'capacity', 'lb', 'lbs', 'kg',
  'set', 'non', 'slip', 'free', 'black', 'white', 'large', 'small', 'size', 'women', 'men', 'kid', 'fitness', 'exercise',
  'workout', 'training', 'equipment', 'accessory', 'organizer', 'holder', 'professional', 'commercial', 'multi', 'function']);
function coreTokens(refTokens) {
  const out = [];
  for (const t of refTokens) {
    if (/\d/.test(t) || TYPE_MODIFIERS.has(t) || out.includes(t)) continue;
    out.push(t);
    if (out.length >= 3) break;
  }
  return out.length ? out : [...new Set(refTokens)].slice(0, 3);
}

function scoreSimilarity(refTokens, refPrice, item) {
  const it = termTokens(item.title);
  if (!refTokens.length || !it.length) return { cover: 0, coreCover: 0, priceOk: false, score: 0 };
  const set = new Set(it);
  const cover = refTokens.filter(t => set.has(t)).length / refTokens.length;
  const core = coreTokens(refTokens);
  const coreCover = core.length ? core.filter(t => set.has(t)).length / core.length : 0;
  let priceScore = 0, priceOk = false;
  if (refPrice > 0 && item.price > 0) {
    const diff = Math.abs(item.price - refPrice) / refPrice;
    if (diff <= 0.15) { priceScore = 1; priceOk = true; }   // SOP：价格浮动 15% 以内算同款
    else if (diff <= 0.3) priceScore = 0.5;
  } else {
    priceScore = 0.5; // 没给参考价时不做价格判断
  }
  return { cover, coreCover, priceOk, score: coreCover * 0.6 + cover * 0.25 + priceScore * 0.15 };
}

// SOP 价格口径：和自家产品价格浮动 ≤tol 才算同类（默认 15%，可在侧栏调）；缺价不据此排除，记 unknown
const REL_PRICE_TOL = 0.15;
function priceBand(refPrice, price, tol = REL_PRICE_TOL) {
  if (!(tol > 0)) return { ok: true, unknown: false, diff: null };   // tol=0 → 不限价格
  if (!(refPrice > 0)) return { ok: true, unknown: false, diff: null };
  if (!(price > 0)) return { ok: true, unknown: true, diff: null };
  const diff = Math.abs(price - refPrice) / refPrice;
  return { ok: diff <= tol + 1e-9, unknown: false, diff: Math.round(diff * 1000) / 10 };
}

const REL_DIR = path.join(__dirname, 'relevance');
const REL_TTL = 7 * 24 * 3600 * 1000;

function relFile(key) {
  try { fs.mkdirSync(REL_DIR, { recursive: true }); } catch {}
  return path.join(REL_DIR, crypto.createHash('md5').update(key).digest('hex') + '.json');
}

function readRelCache(key) {
  try {
    const f = relFile(key);
    if (!fs.existsSync(f)) return null;
    const c = JSON.parse(fs.readFileSync(f, 'utf8'));
    if (Date.now() - (c.ts || 0) > REL_TTL) return null;
    return c;
  } catch { return null; }
}

function writeRelCache(key, data) {
  try { fs.writeFileSync(relFile(key), JSON.stringify(data, null, 1)); } catch (e) { console.log('[比对] 落盘失败:', e.message); }
}

async function checkRelevance(terms, refTitle, refPrice, topN, concurrency = 5, opts = {}) {
  const refTokens = termTokens(refTitle);
  const THRESHOLD = 0.6;
  const TOL = opts.priceTol != null && opts.priceTol !== '' ? Number(opts.priceTol) : REL_PRICE_TOL; // 0 = 不限价格

  // 逐条比对：命中的词 / 缺失的词 / 价格是否落在区间内，全部留痕
  const runOne = async (term) => {
    const ck = `rel|v8|${term}|${refTitle}|${refPrice}|${TOL}`;
    const hit = opts.fresh ? null : readRelCache(ck);
    if (hit) return { ...hit.data, cached: true };

    let items;
    try { items = await searchAmazon(term); }
    catch (e) { return { term, error: String(e.message || e) }; }
    if (!items.length) {
      const empty = { term, total: 0, similar: 0, ratio: 0, items: [], trail: null };
      writeRelCache(ck, { ts: Date.now(), data: empty });
      return empty;
    }

    const scored = items.map(it => ({ ...it, ...scoreSimilarity(refTokens, refPrice, it) }));
    const core = coreTokens(refTokens);
    // 词头 = 品类词里的第一个（如 dumbbell），原来取标题前 2 个词（tier adjustable）太严，导致全 0
    const head = core.slice(0, 1);
    const isHead = it => {
      const set = new Set(termTokens(it.title));
      return head.every(t => set.has(t));
    };
    // 电脑端横屏一页 48 个自然坑位（竖屏 16 个），广告位不计；判据取其中前一半 24 个
    const natural = scored.filter(it => !it.sponsored);
    const slots = natural.slice(0, 48);
    const half = slots.slice(0, Math.max(1, Math.ceil(slots.length / 2)));
    // SOP 同类判定 = 同类型（品类词命中）且 价格浮动 ≤15%；前半页面占比决定相关性
    // 阈值用 0.66 而不是 0.67：3 个品类词命中 2 个 = 0.6667，用 0.67 会把「哑铃架」错判成不同类
    const typeOk = it => isHead(it) && (it.coreCover >= 0.66 || it.cover >= THRESHOLD);
    const isSim = it => typeOk(it) && priceBand(refPrice, it.price, TOL).ok;
    const similar = half.filter(isSim).length;
    const prices = slots.map(i => i.price).filter(Boolean).sort((a, b) => a - b);

    // 对比痕迹：每个位次为什么算/不算相似，都能回看
    const hits = half.map((it, i) => {
      const set = new Set(termTokens(it.title));
      const matched = refTokens.filter(t => set.has(t));
      const pb = priceBand(refPrice, it.price, TOL);
      const tOk = typeOk(it);
      return {
        pos: i + 1,
        asin: it.asin || '',
        title: String(it.title || '').slice(0, 120),
        price: it.price || 0,
        cover: Math.round(it.cover * 100),
        coreCover: Math.round(it.coreCover * 100),
        priceOk: pb.ok,
        priceUnknown: pb.unknown,
        priceDiff: pb.diff,
        priceMin: refPrice > 0 ? Math.round(refPrice * (1 - TOL)) : 0,
        priceMax: refPrice > 0 ? Math.round(refPrice * (1 + TOL)) : 0,
        sim: Math.round(it.score * 100),
        headOk: isHead(it),
        typeOk: tOk,
        similar: tOk && pb.ok,
        reason: !tOk ? '不同类' : (pb.ok ? (pb.unknown ? '同类型（价格未知）' : '同类') : `价格超 15%（差 ${pb.diff}%）`),
        matched: matched.slice(0, 8),
        missing: refTokens.filter(t => !set.has(t)).slice(0, 8),
        coreMatched: core.filter(t => set.has(t)),
        coreMissing: core.filter(t => !set.has(t)),
        img: it.img || ''
      };
    });

    const out = {
      term,
      total: slots.length,
      sponsoredCount: items.length - natural.length,
      halfCount: half.length,
      similar,
      ratio: half.length ? Math.round(similar / half.length * 100) : 0,
      verdict: (half.length && similar / half.length >= 0.5) ? 'high' : (similar > 0 ? 'mid' : 'low'),
      priceMin: prices.length ? Math.round(prices[0]) : 0,
      priceMax: prices.length ? Math.round(prices[prices.length - 1]) : 0,
      items: slots.slice(0, 8),
      trail: {
        refTitle,
        refTokens,
        core,
        head,
        refPrice,
        priceTol: TOL,
        threshold: THRESHOLD,
        coreThreshold: 0.67,
        cards: items.length,
        sponsored: items.length - natural.length,
        natural: slots.length,
        judged: half.length,
        similar,
        ratio: half.length ? Math.round(similar / half.length * 100) : 0,
        priceMin: prices.length ? Math.round(prices[0]) : 0,
        priceMax: prices.length ? Math.round(prices[prices.length - 1]) : 0,
        verdict: (half.length && similar / half.length >= 0.5) ? 'high' : (similar > 0 ? 'mid' : 'low'),
        hits,
        at: Date.now()
      }
    };
    writeRelCache(ck, { ts: Date.now(), data: out });
    return out;
  };

  const results = [];
  for (let i = 0; i < terms.length; i += concurrency) {
    const batch = terms.slice(i, i + concurrency);
    results.push(...await Promise.all(batch.map(runOne)));
  }
  return results;
}

/* ---------------- 商机探测器（Product Opportunity Explorer，走紫鸟页面内的 GraphQL） ---------------- */
const POE_GQL = `query getNiches($filter: NicheFilter!, $useNewQuery: Boolean, $searchImprovementsEnabled: Boolean) {
  niches(filter: $filter, useNewQuery: $useNewQuery, searchImprovementsEnabled: $searchImprovementsEnabled) {
    nicheId
    nicheTitle
    obfuscatedMarketplaceId
    currency
    nicheSummary {
      searchVolumeT90 searchVolumeGrowthT90 searchVolumeT360
      avgPriceT360 avgPrice minimumPrice maximumPrice
      productCount salesPotentialScore category nicheType
    }
    topSearchTermMetrics { searchTerm }
  }
}`;

// 把 GraphQL 返回的 niches 转成统一结构
function mapPoeNiches(j) {
  if (j.errors && j.errors.length) throw new Error('商机探测器：' + (j.errors[0].message || '接口报错'));
  const niches = (j.data && j.data.niches) || [];
  return niches.map(n => {
    const m = n.nicheSummary || {};
    return {
      id: n.nicheId, title: n.nicheTitle, currency: n.currency || 'USD',
      search90: Number(m.searchVolumeT90) || 0,
      growth90: Number(m.searchVolumeGrowthT90) || 0,
      search360: Number(m.searchVolumeT360) || 0,
      avgPrice: Number(m.avgPrice) || 0,
      priceMin: Number(m.minimumPrice) || 0,
      priceMax: Number(m.maximumPrice) || 0,
      products: Number(m.productCount) || 0,
      category: m.category || '',
      terms: (n.topSearchTermMetrics || []).map(t => t.searchTerm).filter(Boolean)
    };
  });
}

function poeBody(keyword, marketplaceId) {
  return JSON.stringify({
    query: POE_GQL, operationName: 'getNiches',
    variables: {
      filter: { obfuscatedMarketplaceId: marketplaceId, searchTermsFilter: { searchInput: keyword } },
      useNewQuery: true, searchImprovementsEnabled: true
    }
  });
}

// 本地直连：用 aba_auth.json 的 cookie + csrf 调 ox-api/graphql（和 ABA 报表同一套凭证）
async function poeCallLocal(keyword, marketplaceId = 'ATVPDKIKX0DER') {
  const auth = loadAuth();
  if (!auth || !auth.cookie) throw new Error('没有 ABA 凭证');
  const csrf = oxCsrf(auth);
  if (!csrf) throw new Error('缺 csrf（首次需走紫鸟抄一次）');
  const res = await fetch('https://sellercentral.amazon.com/ox-api/graphql', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Accept': 'application/json',
      'anti-csrftoken-a2z': csrf,
      'User-Agent': auth.ua || SEARCH_UA,
      'Cookie': auth.cookie,
      'Accept-Language': 'zh-CN,en;q=0.9',
      'Origin': 'https://sellercentral.amazon.com',
      'Referer': 'https://sellercentral.amazon.com/opportunity-explorer/explore'
    },
    body: poeBody(keyword, marketplaceId),
    signal: AbortSignal.timeout(12000)
  });
  if (!res.ok) throw new Error('HTTP ' + res.status);
  const txt = await res.text();
  let j;
  try { j = JSON.parse(txt); } catch { throw new Error('返回非 JSON（登录态可能失效）'); }
  // 会话过期时接口回 200 + {"reason":"sign_in"}，不能当空结果，要抛出去走兜底
  if (!j.data && !(j.errors && j.errors.length)) throw new Error('会话失效（' + (j.reason || 'no data') + '），请点「凭证 → 自动抓取」');
  return mapPoeNiches(j);
}

// 商机探测器：默认走本地直连，失败再兜底紫鸟页面内请求
let poeLastVia = '';
async function poeCall(keyword, marketplaceId = 'ATVPDKIKX0DER') {
  const ck = marketplaceId + '|' + keyword;
  const hit = poeCache.get(ck);
  if (hit && Date.now() - hit.ts < 10 * 60 * 1000) { poeLastVia = 'cache'; return hit.items; }

  let last;
  for (let k = 0; k < DIRECT_TRIES; k++) {
    try {
      const items = await poeCallLocal(keyword, marketplaceId);
      console.log('[商机探测器] 本地直连 OK：', keyword, items.length, '个细分市场');
      poeCache.set(ck, { ts: Date.now(), items });
      poeLastVia = 'local';
      return items;
    } catch (e) {
      last = e;
      console.log(`[商机探测器] 本地直连失败 第${k + 1}/${DIRECT_TRIES}次(${e.message}):`, keyword);
      if (/缺 csrf/.test(e.message)) break; // 没令牌重试也没用，直接去紫鸟抄
      if (k < DIRECT_TRIES - 1) await sleep(600 * (k + 1));
    }
  }
  console.log(`[商机探测器] 本地直连失败，兜底紫鸟:`, keyword, '（' + (last && last.message) + '）');
  const items = await poeCallCdp(keyword, marketplaceId);
  poeCache.set(ck, { ts: Date.now(), items });
  poeLastVia = 'cdp';
  return items;
}

// 商机探测器接口必须带 anti-csrftoken-a2z 请求头；cookie 里没有，卖家后台页面 meta 也常常为空。
// 紫鸟通道能从页面真实请求里抄到它 → 存回 aba_auth.json，之后本地直连就能用（不再 403）
let oxCsrfMem = '';
function saveOxCsrf(tok) {
  if (!tok || tok === oxCsrfMem) return;
  oxCsrfMem = tok;
  try {
    const a = loadAuth();
    if (a && a.csrf !== tok) { a.csrf = tok; fs.writeFileSync(AUTH_FILE, JSON.stringify(a)); console.log('[商机探测器] 已保存 csrf，后续走本地直连'); }
  } catch (e) { console.log('[商机探测器] 保存 csrf 失败:', e.message); }
}
function oxCsrf(auth) {
  return (auth && auth.csrf) || oxCsrfMem || (String((auth && auth.cookie) || '').match(/anti-csrftoken-a2z=([^;]+)/) || [])[1] || '';
}

// 在紫鸟里开一个商机探测器页面，用页面内 fetch 调 ox-api/graphql（自动带登录态）
async function poeCallCdp(keyword, marketplaceId = 'ATVPDKIKX0DER') {
  return mapPoeNiches(await oxGraphqlViaCdp(poeBody(keyword, marketplaceId)));
}

async function oxGraphqlViaCdp(body) {
  const port = await discoverCdpPort();
  if (!port) throw new Error('未找到紫鸟调试端口，请用 agent 模式打开紫鸟');
  const ver = await fetch(`http://127.0.0.1:${port}/json/version`).then(r => r.json());
  const wsc = new WebSocket(ver.webSocketDebuggerUrl);
  await new Promise((res, rej) => {
    wsc.addEventListener('open', res, { once: true });
    wsc.addEventListener('error', () => rej(new Error('CDP 连接失败')), { once: true });
  });
  let id = 0; const pend = new Map();
  wsc.addEventListener('message', ev => {
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    if (m.id && pend.has(m.id)) { const p = pend.get(m.id); pend.delete(m.id); m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result); }
  });
  const send = (method, params, sessionId) => new Promise((resolve, reject) => {
    const mid = ++id; pend.set(mid, { resolve, reject });
    const payload = { id: mid, method, params };
    if (sessionId) payload.sessionId = sessionId;
    wsc.send(JSON.stringify(payload));
    setTimeout(() => { if (pend.has(mid)) { pend.delete(mid); reject(new Error('CDP 超时: ' + method)); } }, 60000);
  });
  let targetId = null;
  try {
    ({ targetId } = await send('Target.createTarget', { url: 'https://sellercentral.amazon.com/opportunity-explorer/explore' }));
    const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });
    await send('Page.enable', {}, sessionId);
    // 监听页面自己发的 graphql 请求，把它的请求头（含 anti-csrftoken-a2z）抄下来
    // 这个 token 不能从 cookie 直接拼，必须用页面自己带的那份
    await send('Network.enable', {}, sessionId);
    let realHeaders = null;
    wsc.addEventListener('message', ev => {
      let m; try { m = JSON.parse(ev.data); } catch { return; }
      if (m.method === 'Network.requestWillBeSent' && /ox-api\/graphql/.test(m.params.request.url) && !realHeaders) {
        realHeaders = m.params.request.headers;
      }
    });
    for (let i = 0; i < 40 && !realHeaders; i++) await sleep(400);
    if (!realHeaders) throw new Error('页面没发出 graphql 请求，可能登录态失效，请在紫鸟里重新登录卖家后台');
    const hdr = { ...realHeaders };
    Object.keys(hdr).forEach(k => { if (/^content-length$/i.test(k)) delete hdr[k]; });
    const tokKey = Object.keys(hdr).find(k => /^anti-csrftoken-a2z$/i.test(k));
    if (tokKey) saveOxCsrf(hdr[tokKey]);

    const expr = `(async () => {
      const B = ${JSON.stringify(body)};
      const H = JSON.parse(${JSON.stringify(JSON.stringify(hdr))});
      const r = await fetch('/ox-api/graphql', { method: 'POST', headers: H, body: B });
      return await r.text();
    })()`;
    const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true }, sessionId);
    const txt = r.result ? r.result.value : '';
    let j;
    try { j = JSON.parse(txt); } catch { throw new Error('商机探测器返回非 JSON，可能需要重新登录紫鸟'); }
    return j;
  } finally {
    try { if (targetId) await send('Target.closeTarget', { targetId }); } catch {}
    try { wsc.close(); } catch {}
  }
}

/* ---------------- 细分市场 → 竞对 ASIN（SOP 第三步方法二+三） ---------------- */
// POE 的「商品」tab 是页面内按需加载的 chunk，接口抓不到（多次探测未触发）。
// 这里用等价路径：拿细分市场的热门搜索词 → ABA 里这些词的「点击量最高的商品」top1~3
// 按加权票数排序（top1 记 3 票、top2 记 2 票、top3 记 1 票），再用 POE 的 axAsins 补全商品指标。
async function nicheToAsins(terms, week, opts = {}) {
  const useTerms = (terms || []).map(t => String(t || '').trim().toLowerCase()).filter(Boolean).slice(0, opts.maxTerms || 6);
  if (!useTerms.length) throw new Error('没有可用搜索词');
  const votes = new Map();
  for (const t of useTerms) {
    let rows = [];
    try {
      const ck = `${t}||weekly|${week}|us|120`;
      const cached = readDiskCache(ck);
      rows = cached && cached.rows ? cached.rows : (await fetchAllTerms(t, week, 'us', 120)).rows;
    } catch (e) { console.log('[竞对ASIN] 拉词表失败', t, e.message); continue; }
    const hit = rows.find(r => String(r['st-search-term'] || '').toLowerCase() === t) || rows[0];
    if (!hit) continue;
    for (let i = 1; i <= 3; i++) {
      const asin = hit['st-top-asin-' + i];
      if (!asin) continue;
      const cur = votes.get(asin) || { asin, score: 0, terms: [], rank: i, title: '', img: '', clickShare: 0, convShare: 0 };
      cur.score += (i === 1 ? 3 : i === 2 ? 2 : 1);
      cur.terms.push(t);
      if (i < cur.rank) cur.rank = i;
      if (!cur.title) cur.title = hit['st-top-asin-title-' + i] || '';
      if (!cur.clickShare) cur.clickShare = Number(hit['st-top-asin-click-share-' + i]) || 0;
      if (!cur.convShare) cur.convShare = Number(hit['st-top-asin-conversion-share-' + i]) || 0;
      votes.set(asin, cur);
    }
  }
  const list = [...votes.values()].sort((a, b) => b.score - a.score).slice(0, opts.limit || 40);
  if (!list.length) return [];

  // 用 POE 的 axAsins 补商品指标（品牌/价格/点击量/评分/评分数/BSR/图）
  let metrics = {};
  try {
    const j = await poeAxAsins(list.map(x => x.asin));
    for (const a of (j || [])) metrics[a.asin] = a;
  } catch (e) { console.log('[竞对ASIN] axAsins 失败', e.message); }

  const price = Number(opts.price) || 0;
  const tol = opts.tol === undefined || opts.tol === '' ? 0.15 : Number(opts.tol);
  return list.map(x => {
    const m = metrics[x.asin] || {};
    const p = Number(m.averageSellingPriceT90) || 0;
    const diff = (price > 0 && p > 0) ? Math.abs(p - price) / price : null;
    return {
      asin: x.asin, title: m.productName || x.title, brand: m.brand || '', category: m.category || '',
      img: m.asinImageUrl || '', price: p, clickCount: Number(m.clickCount) || 0,
      rating: Number(m.averageCustomerRating) || 0, reviews: Number(m.totalReviews) || 0,
      bsr: Number(m.averageBestSellersRanking) || 0, launchDate: m.launchDate || '',
      score: x.score, hitTerms: [...new Set(x.terms)], bestRank: x.rank,
      clickShare: x.clickShare, convShare: x.convShare,
      diff: diff == null ? null : Math.round(diff * 1000) / 10,
      priceOk: !(tol > 0 && diff != null && diff > tol)
    };
  });
}

async function poeAxAsins(asins) {
  const ids = (asins || []).filter(a => /^[A-Z0-9]{10}$/.test(a)).slice(0, 60);
  if (!ids.length) return [];
  const Q = `query axGetAsins($filter: AxAsinFilter!) {
    axAsins(filter: $filter) {
      asin productName asinImageUrl brand category launchDate clickCount clickCountT360
      averageSellingPriceT90 averageSellingPriceT360 totalReviews averageCustomerRating
      averageBestSellersRanking isExactMatch
    }
  }`;
  const body = JSON.stringify({ query: Q, operationName: 'axGetAsins', variables: { filter: { obfuscatedMarketplaceId: 'ATVPDKIKX0DER', asinIds: ids } } });
  const auth = loadAuth();
  if (!auth || !auth.cookie) throw new Error('没有 ABA 凭证');
  // 本地直连（需要 csrf）；403/空响应/无 csrf 时兜底紫鸟页面内请求
  let j = null, localErr = '';
  const csrf = oxCsrf(auth);
  for (let k = 0; csrf && k < DIRECT_TRIES && !j; k++) {
    if (k) await sleep(600 * k);
    try {
      const res = await fetch('https://sellercentral.amazon.com/ox-api/graphql', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json', 'Accept': 'application/json',
          'anti-csrftoken-a2z': csrf, 'User-Agent': auth.ua || SEARCH_UA,
          'Cookie': auth.cookie, 'Origin': 'https://sellercentral.amazon.com',
          'Referer': 'https://sellercentral.amazon.com/opportunity-explorer/explore'
        },
        body, signal: AbortSignal.timeout(20000)
      });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const txt = await res.text();
      j = JSON.parse(txt);
      if (!j.data && !(j.errors && j.errors.length)) throw new Error('会话失效');
    } catch (e) { localErr = e.message; j = null; console.log(`[axAsins] 本地直连失败 第${k + 1}/${DIRECT_TRIES}次(${e.message})`); }
  }
  if (!csrf) localErr = '缺 csrf';
  if (!j) {
    console.log('[axAsins] 本地直连都失败（' + localErr + '），兜底紫鸟');
    j = await oxGraphqlViaCdp(body);
  }
  if (j.errors && j.errors.length) throw new Error(j.errors[0].message);
  return (j.data && j.data.axAsins) || [];
}

/* ---------------- 抓取结果落盘 ---------------- */
function cacheFile(key) {
  try { fs.mkdirSync(CACHE_DIR, { recursive: true }); } catch {}
  return path.join(CACHE_DIR, crypto.createHash('md5').update(key).digest('hex') + '.json');
}

function readDiskCache(key, maxAgeMs = 7 * 24 * 3600 * 1000) {
  try {
    const f = cacheFile(key);
    if (!fs.existsSync(f)) return null;
    const c = JSON.parse(fs.readFileSync(f, 'utf8'));
    if (!c.rows || !c.rows.length) return null;
    if (Date.now() - (c.ts || 0) > maxAgeMs) return null;
    return c;
  } catch { return null; }
}

function writeDiskCache(key, cache) {
  try { fs.writeFileSync(cacheFile(key), JSON.stringify(cache)); } catch (e) { console.log('[缓存] 写入失败:', e.message); }
}

/* ---------------- 导出「关键词库」实操表（对齐模板：新版关键词库） ---------------- */
let _exceljs;
function loadExcelJS() {
  if (_exceljs) return _exceljs;
  try { _exceljs = require('exceljs'); }
  catch {
    _exceljs = require(path.join(require('os').homedir(), '.workbuddy', 'binaries', 'node', 'workspace', 'node_modules', 'exceljs'));
  }
  return _exceljs;
}

const TIER_XLSX = {
  L1: ['一级：3w以内', '一级词'],
  L2: ['二级：3-15w', '二级词'],
  L3: ['三级：15-30w', '三级词'],
  L4: ['四级：30w以外', '四级词']
};
const REL_XLSX = { high: '高', mid: '中等', low: '低' };

/* ---------------- 大模型（OpenAI 兼容） ---------------- */
function loadLLM() {
  try {
    const c = JSON.parse(fs.readFileSync(LLM_FILE, 'utf8'));
    if (c && c.key && !/把这里换成/.test(c.key)) return c;
  } catch {}
  return null;
}

function loadVisionLLM() {
  const llm = loadLLM();
  return llm && llm.key && llm.model && llm.baseUrl ? llm : null;
}

function saveLLM(cfg) {
  fs.writeFileSync(LLM_FILE, JSON.stringify(cfg, null, 2));
}
function maskKey(k) {
  const v = String(k || '');
  if (v.length <= 8) return v ? '****' : '';
  return v.slice(0, 5) + '****' + v.slice(-4);
}

/* 有些网关按协议要求模型名带前缀（OpenAI 协议要 openai/xxx、Anthropic 要 anthropic/xxx）。
   400 报文里会直接写出正确名字（Use 'openai/xxx'），拿到就自动改一次并记住，省得每次手改配置 */
function suggestModel(j) {
  try {
    const msg = String((j && (j.error && (j.error.message || j.error)) || (j && j.message) || ''));
    const m = msg.match(/Use\s+'([^']+)'/i);
    return m ? m[1].trim() : '';
  } catch { return ''; }
}
function rememberModel(model) {
  try {
    const c = loadLLM();
    if (!c || c.model === model) return;
    c.model = model;
    saveLLM(c);
    console.log('[模型] 已按网关提示自动改用', model);
  } catch (e) { console.log('[模型] 保存失败:', e.message); }
}

async function callLLM(cfg, messages, { maxTokens = 1200, temperature = 0.2, timeout = 120000 } = {}) {
  const base = String(cfg.baseUrl || 'https://api.openai.com/v1').replace(/\/+$/, '');
  const model = cfg.model || (cfg.models && cfg.models[0] && cfg.models[0].id) || 'gpt-4o-mini';
  const proto = cfg.protocol || 'chat';

  // Anthropic /messages
  if (proto === 'messages') {
    const sys = messages.filter(m => m.role === 'system').map(m => m.content).join('\n');
    const rest = messages.filter(m => m.role !== 'system').map(m => ({ role: m.role === 'assistant' ? 'assistant' : 'user', content: m.content }));
    const r = await fetch(base + '/messages', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'x-api-key': cfg.key,
        'anthropic-version': '2023-06-01'
      },
      body: JSON.stringify({ model, max_tokens: maxTokens, temperature, system: sys || undefined, messages: rest }),
      signal: AbortSignal.timeout(timeout)
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error('模型返回 HTTP ' + r.status + '：' + JSON.stringify(j).slice(0, 180));
    const txt = (j.content || []).map(x => x.text || '').join('');
    return String(txt || '').trim();
  }

  // OpenAI /responses
  if (proto === 'responses') {
    const input = messages.map(m => ({ role: m.role === 'assistant' ? 'assistant' : 'user', content: [{ type: m.role === 'assistant' ? 'output_text' : 'input_text', text: String(m.content || '') }] }));
    const r = await fetch(base + '/responses', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + cfg.key },
      body: JSON.stringify({ model, input, max_output_tokens: maxTokens, temperature }),
      signal: AbortSignal.timeout(timeout)
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error('模型返回 HTTP ' + r.status + '：' + JSON.stringify(j).slice(0, 180));
    let txt = j.output_text;
    if (!txt && Array.isArray(j.output)) {
      txt = j.output.flatMap(o => (o.content || []).map(c => c.text || '')).join('');
    }
    return String(txt || '').trim();
  }

  // 默认 OpenAI 兼容 /chat/completions
  const ask = async (m) => {
    const r = await fetch(base + '/chat/completions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + cfg.key },
      body: JSON.stringify({ model: m, temperature, max_tokens: maxTokens, messages }),
      signal: AbortSignal.timeout(timeout)
    });
    return { r, j: await r.json().catch(() => ({})) };
  };
  let { r, j } = await ask(model);
  if (!r.ok) {
    const hint = suggestModel(j);
    if (hint && hint !== model) {
      ({ r, j } = await ask(hint));
      if (r.ok) rememberModel(hint);
    }
  }
  if (!r.ok) throw new Error('模型返回 HTTP ' + r.status + '：' + JSON.stringify(j).slice(0, 180));
  let txt = j.choices && j.choices[0] && j.choices[0].message && j.choices[0].message.content;
  if (Array.isArray(txt)) txt = txt.map(x => x.text || '').join('');
  return String(txt || '').trim();
}

function pickJSON(txt) {
  const t = String(txt || '').replace(/```json|```/g, '').trim();
  const m = t.match(/\{[\s\S]*\}/);
  if (!m) return null;
  try { return JSON.parse(m[0]); } catch { return null; }
}

const LIB_TPL = path.join(__dirname, 'templates', 'keyword-library.xlsx');

/* 用用户模板做底：保留原配色/列宽/合并/边框，只换数据 */
async function buildKwWorkbookTpl(meta, rows) {
  const ExcelJS = loadExcelJS();
  const wb = new ExcelJS.Workbook();
  await wb.xlsx.readFile(LIB_TPL);
  const ws = wb.worksheets[0];
  const COLS = 24;

  // 记下第 12 行的样式，作为数据行样板
  const sample = [];
  for (let c = 1; c <= COLS; c++) {
    const cell = ws.getCell(12, c);
    sample.push({ font: cell.font, alignment: cell.alignment, border: cell.border, fill: cell.fill, numFmt: cell.numFmt });
  }
  // 清掉模板里的旧数据行：整段 splice 在 exceljs 4.4 里会静默失效（旧数据留在下面，
  // 看起来就是「没筛选、混进别人的词」），所以逐行从下往上删
  for (let r = ws.rowCount; r >= 12; r--) ws.spliceRows(r, 1);

  // 清空并重填元信息（B2:X9）
  for (let r = 2; r <= 9; r++) for (let c = 2; c <= COLS; c++) ws.getCell(r, c).value = null;
  const metaVals = [
    ['词根', [meta.root || '']],
    ['品牌词', [meta.brand || '']],
    ['竞·品牌词', [meta.compBrand || '']],
    ['核心大词', meta.coreWords || []],
    ['竞对ASIN', meta.asins || []],
    ['卖点词根', meta.sellWords || []],
    ['场景属性', meta.sceneWords || []],
    ['偏好属性', meta.preferWords || []]
  ];
  metaVals.forEach(([label, values], i) => {
    const r = i + 2;
    const arr = (Array.isArray(values) ? values : [values]).filter(v => v != null && v !== '');
    arr.slice(0, COLS - 1).forEach((v, j) => { ws.getCell(r, j + 2).value = v; });
    if (ws.getCell(r, 1).value !== label) ws.getCell(r, 1).value = label;
  });

  const dateCn = (w) => {
    const m = String(w || '').match(/^(\d{4})-(\d{2})-(\d{2})$/);
    return m ? `${m[1]}年${parseInt(m[2], 10)}月${parseInt(m[3], 10)}号` : (w || '');
  };
  const hit = (term, list) => (list || []).filter(w => w && term.toLowerCase().includes(String(w).toLowerCase()));

  rows.forEach((r, i) => {
    const rn = 12 + i;
    const term = String(r.term || '');
    let tierPair;
    if (r.asinTier) tierPair = [r.asinTier, r.asinLabel || ''];
    else if (r.tier && TIER_XLSX[r.tier]) tierPair = TIER_XLSX[r.tier];
    else tierPair = ['三方词：卖家精灵', '三方词'];

    const vals = [
      term,
      dateCn(meta.week),
      r.rank == null ? null : (Number(r.rank) || null),
      tierPair[0], tierPair[1],
      r.asinTier ? null : '待使用',
      r.asinTier ? null : (hit(term, meta.sellWords).join(' ') || null),
      r.asinTier ? null : (hit(term, meta.sceneWords).join(' ') || null),
      r.asinTier ? null : (hit(term, meta.preferWords).join(' ') || null),
      r.asinTier ? null : (REL_XLSX[r.rel] || r.relText || null),
      r.convSum3 ? Number(r.convSum3) : null,
      (r.buyRatio > 0 ? Number((r.buyRatio / 100).toFixed(4)) : null),
      r.asinTier ? null : (r.priority || null)
    ];
    vals.forEach((v, j) => {
      const cell = ws.getCell(rn, j + 1);
      cell.value = v;
      cell.font = sample[j] ? sample[j].font : cell.font;
      cell.alignment = { vertical: 'middle' };
      cell.border = sample[j] ? sample[j].border : cell.border;
    });
    // 大模型分析：否词列表（第 17 列）、情况说明（第 23 列）
    if (!r.asinTier && r.negWords) ws.getCell(rn, 17).value = r.negWords;
    if (!r.asinTier && r.analysis) {
      const c = ws.getCell(rn, 23);
      c.value = r.analysis;
      c.font = sample[22] ? sample[22].font : c.font;
      c.border = sample[22] ? sample[22].border : c.border;
      c.alignment = { vertical: 'middle', wrapText: true };
    }
    ws.getCell(rn, 1).alignment = { vertical: 'middle' };
    ws.getCell(rn, 3).alignment = { vertical: 'middle', horizontal: 'left' };
    ws.getCell(rn, 11).numFmt = '0.00_ ';
    ws.getCell(rn, 12).numFmt = '0.00%';
    ws.getRow(rn).height = 30;
  });

  addBuildSheet(wb, meta, rows);
  addKwSourceSheets(wb, meta);
  addCollectionStatusSheet(wb, meta);
  return wb.xlsx.writeBuffer();
}

/* 「搭建关键词库」页（层级分组 + 关键词翻译 + 场景/偏好词），模板导出和默认导出共用 */
function addBuildSheet(wb, meta, rows) {
  const thin = { style: 'thin', color: { argb: 'FF000000' } };
  const BORDER = { top: thin, left: thin, bottom: thin, right: thin };
  const hit = (term, list) => (list || []).filter(w => w && term.toLowerCase().includes(String(w).toLowerCase()));
  // ---------- Sheet 2：搭建关键词库（老版布局：层级分组 + 翻译 + 场景/偏好词） ----------
  const kwRows = rows.filter(r => !r.asinTier && r.rank != null);
  const TIER_CN2 = { L1: '一级词', L2: '二级词', L3: '三级词', L4: '四级词' };
  const TIER_SZ2 = { L1: '一级：3w以内', L2: '二级：3-15w', L3: '三级：15-30w', L4: '四级：30w以外' };
  const ws2 = wb.addWorksheet('搭建关键词库');
  const W2 = [9, 30, 16, 10, 14, 18, 16, 12, 12, 16];
  W2.forEach((w, i) => { ws2.getColumn(i + 1).width = w; });
  // 标题
  ws2.mergeCells(1, 1, 1, 10);
  const t2 = ws2.getCell(1, 1);
  t2.value = '关键词库';
  t2.font = { bold: true, size: 30, name: '等线' };
  ws2.getRow(1).height = 45;
  // 元信息
  const metaRows2 = [
    ['词根', meta.root || ''],
    ['品牌词', meta.brand || ''],
    ['竞·品牌词', meta.compBrand || ''],
    ['一级词', meta.coreWords || []],
    ['竞对ASIN', meta.asins || []],
    ['场景属性', meta.sceneWords || []],
    ['偏好属性', meta.preferWords || []]
  ];
  metaRows2.forEach(([label, values], i) => {
    const r = i + 2;
    const a = ws2.getCell(r, 1);
    a.value = label;
    a.font = { bold: true, size: 12, name: '等线', color: { argb: 'FFFFFFFF' } };
    a.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: 'FF133C9A' } };
    a.alignment = { horizontal: 'center', vertical: 'middle' };
    const vals = Array.isArray(values) ? values : [values];
    vals.filter(v => v !== '' && v != null).slice(0, 9).forEach((v, j) => { ws2.getCell(r, j + 2).value = v; });
    for (let c = 1; c <= Math.max(2, Math.min(vals.filter(v => v != null && v !== '').length + 1, 10)); c++) ws2.getCell(r, c).border = BORDER;
  });
  // 表头（第 9 行）
  const H2 = ['项目', '关键词/ASIN', '关键词翻译', 'ABA排名', '流量大小',
    '场景词:在哪里用 / 给谁用（使用环境、适用场合）',
    '偏好词:产品长什么样、什么材质、什么规格、有什么功能（买家挑选产品的特征要求）',
    '相关性:搜索词 和 你的 Listing 产品本身的匹配程度', '购买能力', '垄断程度（TOP3）'];
  H2.forEach((h, i) => {
    const c = ws2.getCell(9, i + 1);
    c.value = h;
    c.font = { bold: true, size: 12, name: '等线', color: { argb: 'FFFFFFFF' } };
    c.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: 'FF133C9A' } };
    c.alignment = { horizontal: 'center', vertical: 'middle', wrapText: true };
  });
  ws2.getRow(9).height = 63;
  for (let c = 1; c <= 10; c++) ws2.getCell(9, c).border = BORDER;
  // 按层级分组填充
  const groups = ['L1', 'L2', 'L3', 'L4'];
  let rn2 = 10;
  const byTier = {};
  kwRows.forEach(r => { (byTier[r.tier] = byTier[r.tier] || []).push(r); });
  for (const g of groups) {
    const list = byTier[g] || [];
    if (!list.length) continue;
    const start = rn2;
    for (const r of list) {
      const term = String(r.term || '');
      const scene = hit(term, meta.sceneWords).join(' ') || null;
      const prefer = [...hit(term, meta.sellWords), ...hit(term, meta.preferWords)].join(' ') || null;
      const rowVals = [
        TIER_CN2[g], term, r.ssCn || null,
        Number(r.rank) || null, TIER_SZ2[g],
        scene, prefer,
        REL_XLSX[r.rel] || r.relText || null,
        r.buyRatio > 0 ? Number((r.buyRatio / 100).toFixed(4)) : 0,
        r.convSum3 ? Number((r.convSum3 / 100).toFixed(4)) : 0
      ];
      rowVals.forEach((v, j) => {
        const c = ws2.getCell(rn2, j + 1);
        c.value = v;
        c.alignment = { vertical: 'middle' };
        if (j === 0) c.alignment = { horizontal: 'center', vertical: 'middle' };
      });
      ws2.getCell(rn2, 9).numFmt = '0.0000';
      ws2.getCell(rn2, 10).numFmt = '0.0000';
      ws2.getRow(rn2).height = 17.25;
      for (let c = 1; c <= 10; c++) ws2.getCell(rn2, c).border = BORDER;
      rn2++;
    }
    if (rn2 - 1 > start) ws2.mergeCells(start, 1, rn2 - 1, 1);
  }

}

function addKwSourceSheets(wb, meta) {
  const usedNames = new Set(wb.worksheets.map(w => w.title));
  const sheetName = (base) => {
    let n = String(base).replace(/[:\\/?*\[\]]/g, '-').slice(0, 31);
    if (!n) n = 'Sheet';
    if (!usedNames.has(n)) { usedNames.add(n); return n; }
    for (let i = 2; i < 99; i++) {
      const cand = (n.slice(0, 28) + '(' + i + ')');
      if (!usedNames.has(cand)) { usedNames.add(cand); return cand; }
    }
    return n;
  };
  const isoWeek = (ds) => {
    const d = new Date(String(ds || '').slice(0, 10) + 'T00:00:00Z');
    if (isNaN(d)) return '';
    const t = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
    const day = (t.getUTCDay() + 6) % 7;
    t.setUTCDate(t.getUTCDate() - day + 3);
    const first = new Date(Date.UTC(t.getUTCFullYear(), 0, 4));
    const fd = (first.getUTCDay() + 6) % 7;
    first.setUTCDate(first.getUTCDate() - fd + 3);
    return String(1 + Math.round((t - first) / (7 * 864e5)));
  };
  const isMonthlyExport = /^\d{4}-\d{2}$/.test(String(meta.week || ''));
  const weekLabel = (() => {
    const w = String(meta.week || '');
    if (isMonthlyExport) return `月份 ${w.slice(0, 4)}年${Number(w.slice(5, 7))}月`;
    const wk = isoWeek(meta.week);
    if (!wk) return '';
    const d = new Date(String(meta.week).slice(0, 10) + 'T00:00:00Z');
    const end = new Date(d); const startD = new Date(d.getTime() - 6 * 864e5);
    const f = x => x.toISOString().slice(0, 10);
    return `周 ${wk} | ${f(startD)} - ${f(end)} ${d.getUTCFullYear()}`;
  })();

  const H_EN = ['Search Frequency Rank', 'Search Term', '购买比', '前三转化份额累计',
    'Top Clicked Brand #1', 'Top Clicked Brands #2', 'Top Clicked Brands #3',
    'Top Clicked Category #1', 'Top Clicked Category #2', 'Top Clicked Category #3',
    'Top Clicked Product #1: ASIN', 'Top Clicked Product #1: Product Title', 'Top Clicked Product #1: Click Share', 'Top Clicked Product #1: Conversion Share',
    'Top Clicked Product #2: ASIN', 'Top Clicked Product #2: Product Title', 'Top Clicked Product #2: Click Share', 'Top Clicked Product #2: Conversion Share',
    'Top Clicked Product #3: ASIN', 'Top Clicked Product #3: Product Title', 'Top Clicked Product #3: Click Share', 'Top Clicked Product #3: Conversion Share',
    'Reporting Date', '购买性判断（SOP）', '垄断性判断（SOP）'];
  const H_CN = ['搜索频率排名', '搜索词', '购买比', '前三转化份额累计',
    '点击量第1的品牌', '点击量第2的品牌', '点击量第3的品牌',
    '点击量最高的分类', '点击量第二的分类', '点击量第 3 的分类',
    '点击量第1的商品：ASIN', '点击量第1的商品: 商品标题', '点击量最高的商品：点击份额', '点击量第1的商品：转化贡献占比',
    '点击量第2的商品：ASIN', '点击量第2的商品：商品标题', '点击量第二的商品：点击份额', '热门点击商品第2名：转化贡献占比',
    '点击量第三的商品：ASIN', '点击量第3的商品：商品标题', '点击量第三的商品：点击份额', '点击量第3的商品：转化贡献占比',
    '报告日期', '购买性判断（SOP）', '垄断性判断（SOP）'];
  const ABA_W = [14, 30, 14, 16, 16, 16, 16, 16, 16, 16, 15, 34, 12, 14, 15, 34, 12, 14, 15, 34, 12, 14, 12, 15, 17];

  // ABA 正向 / 反向：结构相同，只是表头和第一行筛选说明不同
  const addAbaSheet = (src) => {
    const isReverse = !src.root && src.asins;
    const title = isReverse ? `ABA反向-${String(src.asins || '').split(/[\s,]+/)[0]}` : `ABA正向-${src.root}`;
    const ws = wb.addWorksheet(sheetName(title));
    ABA_W.forEach((w, i) => { ws.getColumn(i + 1).width = w; });
    // R1 筛选说明（对齐模板）
    ws.getCell(1, 1).value = isMonthlyExport ? '报告范围=["每月"]' : '报告范围=["每周"]';
    ws.getCell(1, 2).value = weekLabel ? `选择周=["${weekLabel}"]` : '';
    if (isReverse) {
      ws.getCell(1, 3).value = '';
      ws.getCell(1, 4).value = '';
      ws.getCell(1, 5).value = `点击量最高的商品=["${String(src.asins || '').trim()}"]`;
    } else {
      ws.getCell(1, 3).value = '购买能力(> 60%)';
      ws.getCell(1, 4).value = '垄断程度（TOP3）(累计 > 35%)';
      ws.getCell(1, 5).value = `搜索词=["${src.root}"]`;
    }
    ws.getCell(1, 3).fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: 'FFFFC000' } };
    ws.getCell(1, 4).fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: 'FFFFC000' } };
    // R2 表头
    const H = isReverse ? H_CN : H_EN;
    H.forEach((h, i) => {
      const c = ws.getCell(2, i + 1);
      c.value = h || null;
      c.font = { bold: true, size: 10, name: '宋体' };
      c.alignment = { horizontal: 'center', vertical: 'middle', wrapText: true };
      c.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: 'FFF2F2F2' } };
    });
    ws.getRow(2).height = 42;
    // R3+ 数据
    const list = (src.rows || []).slice(0, 5000);
    list.forEach((r, i) => {
      const rn = 3 + i;
      const metric = abaMetricFormulas(rn);
      const top = Array.isArray(r.top) && r.top.length === 3 ? r.top : [1, 2, 3].map(k => ({ brand: k === 1 ? r.topBrand : '', category: k === 1 ? r.category : '', asin: k === 1 ? r.topAsin : '', title: '', click: k === 1 ? r.clickShare : null, conv: k === 1 ? r.convShare : null }));
      const v = [
        r.rank || null, r.term || '',
        { f: metric.purchaseRatio }, { f: metric.conversionSum3 },
        top[0].brand || '', top[1].brand || '', top[2].brand || '',
        top[0].category || '', top[1].category || '', top[2].category || '',
        top[0].asin || '', top[0].title || '', top[0].click != null ? top[0].click : null, top[0].conv != null ? top[0].conv : null,
        top[1].asin || '', top[1].title || '', top[1].click != null ? top[1].click : null, top[1].conv != null ? top[1].conv : null,
        top[2].asin || '', top[2].title || '', top[2].click != null ? top[2].click : null, top[2].conv != null ? top[2].conv : null,
        r.reportDate ? new Date(r.reportDate) : (meta.week ? new Date(isMonthlyExport ? meta.week + '-01' : meta.week) : null),
        { f: metric.purchaseLabel }, { f: metric.monopolyLabel }
      ];
      v.forEach((x, j) => {
        const c = ws.getCell(rn, j + 1);
        if (x && typeof x === 'object' && x.f) c.value = { formula: x.f };
        else c.value = x == null || x === '' ? null : x;
      });
      ws.getCell(rn, 3).numFmt = '0.00%';
      ws.getCell(rn, 4).numFmt = '0.00%';
      [13, 14, 17, 18, 21, 22].forEach(cc => { ws.getCell(rn, cc).numFmt = '0.00'; });
      ws.getCell(rn, 23).numFmt = 'yyyy-mm-dd';
    });
    if (list.length) ws.autoFilter = { from: { row: 2, column: 1 }, to: { row: 2 + list.length, column: H.length } };
    ws.freezePanes = { xSplit: 2, ySplit: 2 };
    return list.length;
  };

  // 卖家精灵正向（列对齐卖家精灵自己的导出）
  const SS_M_HEAD = ['关键词', '关键词翻译', '购买比', '前三转化份额累计', 'AC推荐词', '相关度', 'ABA月排名', 'ABA周排名', '月搜索量',
    '搜索量环比增长率', '搜索量同比增长率', '月购买量', '购买率', '展示量', '点击量', 'SPR', '标题密度', '商品数', '需供比', '广告竞品数',
    '点击总占比', '转化总占比', 'PPC竞价', '建议竞价范围', '点击前三中位价', '点击前三评分数', '点击前三评分值', '所属类目',
    '#1 前三ASIN', '#1 点击共享', '#1转化共享', '#2 前三ASIN', '#2 点击共享', '#2 转化共享', '#3 前三ASIN', '#3 点击共享', '#3 转化共享', '前十ASIN',
    '购买性判断（SOP）', '垄断性判断（SOP）'];
  const SS_M_W = [26, 14, 10, 12, 10, 9, 10, 10, 11, 14, 14, 10, 9, 11, 10, 7, 9, 9, 9, 11, 11, 11, 10, 14, 13, 13, 13, 20, 14, 10, 10, 14, 10, 10, 14, 10, 10, 30, 15, 17];
  const addSsMinerSheet = (src) => {
    const ws = wb.addWorksheet(sheetName(`卖家精灵正向-${src.root || ''}`));
    SS_M_W.forEach((w, i) => { ws.getColumn(i + 1).width = w; });
    SS_M_HEAD.forEach((h, i) => {
      const c = ws.getCell(1, i + 1);
      c.value = h; c.font = { bold: true, size: 10, name: '宋体' };
      c.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: 'FFF2F2F2' } };
    });
    const items = (src.items || []).slice(0, 5000);
    items.forEach((it, i) => {
      const rn = i + 2;
      const metric = sellerMinerMetricFormulas(rn);
      const ms = Array.isArray(it.monopolyAsinDtos) ? it.monopolyAsinDtos : [];
      const at = k => ms[k] || {};
      const v = [
        it.keyword || '', it.keywordCn || '', { f: metric.purchaseRatio }, { f: metric.conversionSum3 },
        it.amazonChoice ? 'Y' : 'N', it.relevancy != null ? it.relevancy : null,
        it.searchRank != null ? it.searchRank : null, it.searchWeeklyRank != null ? it.searchWeeklyRank : null,
        it.searches != null ? it.searches : null, it.searchesGrowth != null ? it.searchesGrowth : null,
        it.yearlyGrowthRate != null ? it.yearlyGrowthRate : null, it.purchases != null ? it.purchases : null,
        it.purchaseRate != null ? it.purchaseRate : null, it.impressions != null ? it.impressions : null,
        it.clicks != null ? it.clicks : null, it.spr != null ? it.spr : null, it.titleDensity != null ? it.titleDensity : null,
        it.products != null ? it.products : null, it.supplyDemandRatio != null ? it.supplyDemandRatio : null,
        Array.isArray(it.adProducts) ? it.adProducts.length : (it.adProducts != null ? it.adProducts : null),
        it.monopolyClickRate != null ? it.monopolyClickRate : null, it.cvsShareRate != null ? it.cvsShareRate : null,
        it.bid != null ? '$' + it.bid : '', (it.bidMin || it.bidMax) ? `$${it.bidMin || '?'}-$${it.bidMax || '?'}` : '',
        it.avgPrice != null ? '$' + it.avgPrice : '', it.avgReviews != null ? it.avgReviews : null, it.avgRating != null ? it.avgRating : null,
        Array.isArray(it.departments) ? it.departments.filter(x => typeof x === 'string').join(' / ') : (typeof it.departments === 'string' ? it.departments : ''),
        at(0).asin || '', at(0).clickRate != null ? at(0).clickRate : null, at(0).conversionShareRate != null ? at(0).conversionShareRate : null,
        at(1).asin || '', at(1).clickRate != null ? at(1).clickRate : null, at(1).conversionShareRate != null ? at(1).conversionShareRate : null,
        at(2).asin || '', at(2).clickRate != null ? at(2).clickRate : null, at(2).conversionShareRate != null ? at(2).conversionShareRate : null,
        ms.map(x => x.asin).filter(Boolean).join(','),
        { f: metric.purchaseLabel }, { f: metric.monopolyLabel }
      ];
      v.forEach((x, j) => {
        const c = ws.getCell(rn, j + 1);
        c.value = (x && typeof x === 'object' && x.f) ? { formula: x.f } : (x === '' || x == null ? null : x);
      });
      [3, 4, 10, 11, 13, 21, 22, 30, 31, 34, 37].forEach(cc => { ws.getCell(rn, cc).numFmt = '0.00%'; });
    });
    if (items.length) ws.autoFilter = { from: { row: 1, column: 1 }, to: { row: 1, column: SS_M_HEAD.length } };
    ws.freezePanes = { ySplit: 1 };
    return items.length;
  };

  // 卖家精灵反向（按 ASIN 反查流量词）
  const SS_R_HEAD = ['关键词', '关键词翻译', '流量占比', '预估周曝光量', '关键词类型', '转化效果', '流量词类型', '自然流量占比', '广告流量占比',
    '自然排名', '自然排名页码', '更新时间', '广告排名', '广告排名页码', '更新时间', 'ABA周排名', '月搜索量', '搜索量环比增长率', '搜索量同比增长率',
    'SPR', '标题密度', '购买量', '购买率', '展示量', '点击量', '商品数', '需供比', '近7天广告竞品数', '点击总占比', '转化总占比', 'PPC价格', '建议竞价范围', '前十ASIN',
    '购买性判断（SOP）', '垄断性判断（SOP）'];
  const SS_R_W = [26, 14, 10, 13, 12, 10, 24, 11, 11, 9, 14, 20, 9, 14, 20, 10, 11, 14, 14, 7, 9, 9, 9, 11, 10, 9, 9, 12, 11, 11, 10, 14, 30, 15, 20];
  const addSsReverseSheet = (src) => {
    const title = src.mode === 'extend'
      ? `卖家精灵拓展-${(src.asins || [src.asin || '']).slice(0, 3).join('_')}${(src.asins || []).length > 3 ? '等' + (src.asins || []).length + '个' : ''}`
      : `卖家精灵反向-${src.asin || ''}`;
    const ws = wb.addWorksheet(sheetName(title));
    SS_R_W.forEach((w, i) => { ws.getColumn(i + 1).width = w; });
    SS_R_HEAD.forEach((h, i) => {
      const c = ws.getCell(1, i + 1);
      c.value = h; c.font = { bold: true, size: 10, name: '宋体' };
      c.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: 'FFF2F2F2' } };
    });
    const items = (src.items || []).slice(0, 5000);
    // 反查返回的排名/更新时间是 JSON 串或时间戳，拆成「排名 / 页码 / 时间」三列
    const posOf = v => {
      if (v == null) return { pos: null, page: '', time: '' };
      let o = v;
      if (typeof v === 'string') { try { o = JSON.parse(v); } catch { return { pos: null, page: '', time: '' }; } }
      if (typeof o !== 'object') return { pos: null, page: '', time: '' };
      const page = o.page ? `第${o.page}页,${o.index || '?'}/${o.pageSize || '?'}` : '';
      const t = o.updatedTime ? new Date(o.updatedTime) : null;
      const time = t && !isNaN(t) ? `${String(t.getMonth() + 1).padStart(2, '0')}.${String(t.getDate()).padStart(2, '0')} ${String(t.getHours()).padStart(2, '0')}:${String(t.getMinutes()).padStart(2, '0')}` : '';
      return { pos: o.position != null ? o.position : null, page, time };
    };
    items.forEach((it, i) => {
      const rn = i + 2;
      const nat = posOf(it.rankPosition), ad = posOf(it.adPosition);
      const metricLabels = sellerReverseMetricLabels(it.top3ConversionRate);
      const v = [
        it.keywords || it.keyword || '', it.keywordCn || '',
        it.trafficPercentage != null ? it.trafficPercentage : null,
        it.calculatedWeeklySearches != null ? Math.round(it.calculatedWeeklySearches) : null,
        '', '', it.position || '', it.naturalRatio != null ? it.naturalRatio : null, it.adRatio != null ? it.adRatio : null,
        nat.pos, nat.page, nat.time,
        ad.pos, ad.page, ad.time,
        it.searchesRank != null ? it.searchesRank : null,
        it.searches != null ? it.searches : null,
        it.searchesGrowth != null ? it.searchesGrowth : null,
        it.yearlyGrowthRate != null ? it.yearlyGrowthRate : null,
        it.cprExact != null ? it.cprExact : null, it.titleDensityExact != null ? it.titleDensityExact : null,
        it.purchases != null ? it.purchases : null, it.purchaseRate != null ? it.purchaseRate : null,
        it.impressions != null ? it.impressions : null, it.clicks != null ? it.clicks : null,
        it.products != null ? it.products : null, it.supplyDemandRatio != null ? it.supplyDemandRatio : null,
        it.latest7daysAds != null ? it.latest7daysAds : null,
        it.monopolyClickRate != null ? it.monopolyClickRate : null,
        it.top3ConversionRate != null ? it.top3ConversionRate : null,
        it.bid != null ? '$' + it.bid : ((it.exactPpc != null) ? '$' + it.exactPpc : ''),
        (it.bidMin || it.bidMax) ? `$${it.bidMin || '?'}-$${it.bidMax || '?'}` : '',
        String(it.top10Asin || (Array.isArray(it.top10Asin) ? it.top10Asin.join(',') : '') || ''),
        metricLabels.purchaseLabel, metricLabels.monopolyLabel
      ];
      v.forEach((x, j) => { ws.getCell(rn, j + 1).value = x === '' || x == null ? null : x; });
      [3, 8, 9, 18, 19, 23, 29, 30].forEach(cc => { ws.getCell(rn, cc).numFmt = '0.00%'; });
    });
    if (items.length) ws.autoFilter = { from: { row: 1, column: 1 }, to: { row: 1, column: SS_R_HEAD.length } };
    ws.freezePanes = { ySplit: 1 };
    return items.length;
  };

  if (Array.isArray(meta.sources) && meta.sources.length) {
    for (const src of meta.sources) {
      try {
        if (src.kind === 'aba' && Array.isArray(src.rows) && src.rows.length) {
          const n = addAbaSheet(src);
          console.log('[导出]', src.root ? `ABA正向-${src.root}` : `ABA反向-${src.asins}`, n, '行');
        } else if (src.kind === 'ss' && src.mode === 'miner' && Array.isArray(src.items) && src.items.length) {
          const n = addSsMinerSheet(src);
          console.log('[导出] 卖家精灵正向-', src.root, n, '行');
        } else if (src.kind === 'ss' && src.mode === 'reverse' && Array.isArray(src.items) && src.items.length) {
          const n = addSsReverseSheet(src);
          console.log('[导出] 卖家精灵反向-', src.asin, n, '行');
        } else if (src.kind === 'ss' && src.mode === 'extend' && Array.isArray(src.items) && src.items.length) {
          const n = addSsReverseSheet(src);
          console.log('[导出] 卖家精灵拓展-多ASIN', n, '行');
        }
      } catch (e) { console.log('[导出] 来源表失败:', src.kind, e.message); }
    }
  } else if (Array.isArray(meta.rawRows) && meta.rawRows.length) {
    // 兼容：没有分来源数据时，退回一张汇总表
    addAbaSheet({ root: String(meta.root || '').split(' ')[0] || '全部', rows: meta.rawRows });
  }

}

async function buildKwWorkbook(meta, rows) {
  if (fs.existsSync(LIB_TPL)) {
    try { return await buildKwWorkbookTpl(meta, rows); }
    catch (e) { console.warn('[export] 模板导出失败，回退默认样式：', e.message); }
  }
  return buildKwWorkbookLegacy(meta, rows);
}

function addCollectionStatusSheet(wb, meta) {
  const ws = wb.addWorksheet('数据采集状态');
  const headers = ['来源', '查询条件', '报告范围', '总量', '已取回', '截断', '页数', '单次上限'];
  const widths = [24, 32, 20, 12, 12, 12, 10, 12];
  widths.forEach((width, i) => { ws.getColumn(i + 1).width = width; });
  headers.forEach((header, i) => {
    const cell = ws.getCell(1, i + 1);
    cell.value = header;
    cell.font = { bold: true, size: 11, name: '宋体', color: { argb: 'FFFFFFFF' } };
    cell.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: 'FF133C9A' } };
    cell.alignment = { horizontal: 'center', vertical: 'middle' };
  });
  ws.getRow(1).height = 24;

  const period = /^\d{4}-\d{2}$/.test(String(meta.week || ''))
    ? `每月 ${meta.week}`
    : (meta.week ? `每周 ${meta.week}` : '未记录');
  let sources = Array.isArray(meta.sources) ? meta.sources : [];
  if (!sources.length && Array.isArray(meta.rawRows) && meta.rawRows.length) {
    sources = [{ kind: 'aba', root: meta.root || '', total: meta.rawRows.length, fetched: meta.rawRows.length, truncated: false }];
  }

  const labelOf = source => {
    if (source.kind === 'aba') return source.root ? 'ABA 正向' : 'ABA 反向';
    if (source.kind === 'ss' && source.mode === 'miner') return '卖家精灵正向';
    if (source.kind === 'ss' && source.mode === 'reverse') return '卖家精灵反向';
    if (source.kind === 'ss' && source.mode === 'extend') return '卖家精灵多 ASIN 拓展';
    return String(source.kind || '未知来源');
  };
  const queryOf = source => source.root || source.asins || source.asin || meta.root || '未记录';
  if (!sources.length) {
    ws.addRow(['未记录来源', '无采集来源明细', period, null, null, '未知', null, null]);
  } else {
    sources.forEach(source => {
      const total = Number(source.total);
      const fetched = Number(source.fetched);
      const pages = Number(source.pages);
      const limit = Number(source.limit);
      ws.addRow([
        labelOf(source), queryOf(source), period,
        Number.isFinite(total) ? total : null,
        Number.isFinite(fetched) ? fetched : null,
        source.truncated === true ? '是' : source.truncated === false ? '否' : '未知',
        Number.isFinite(pages) ? pages : null,
        Number.isFinite(limit) ? limit : null
      ]);
    });
  }
  ws.eachRow(row => row.eachCell(cell => { cell.alignment = { vertical: 'middle', wrapText: true }; }));
  ws.autoFilter = { from: { row: 1, column: 1 }, to: { row: ws.rowCount, column: headers.length } };
  ws.freezePanes = { ySplit: 1 };
}

async function buildKwWorkbookLegacy(meta, rows) {
  const ExcelJS = loadExcelJS();
  const wb = new ExcelJS.Workbook();
  const ws = wb.addWorksheet('关键词库', { views: [{ state: 'frozen', xSplit: 1, ySplit: 11 }] });

  const HEADERS = ['关键词&ASIN', '更新日期', '排名', '流量层级', '词流量层级', '使用情况', '卖点词根包含', '场景词根包含', '偏好词根包含',
    '相关性', '垄断性TOP3', '购买性', '投放优先级', '投放时间', '应用产品', '当前状态', '否词列表', 'CTR', 'CVR', '花费',
    '订单', 'ACOS', '情况说明', '父记录', '备注'];
  const WIDTHS = [26.8, 15, 10, 15, 11, 10, 16, 14, 14, 10, 13, 10, 11, 11, 12, 10, 10, 8, 8, 10, 10, 9, 14, 12, 26];
  WIDTHS.forEach((w, i) => { ws.getColumn(i + 1).width = w; });

  const thin = { style: 'thin', color: { argb: 'FF000000' } };
  const BORDER = { top: thin, left: thin, bottom: thin, right: thin };
  const borderBlock = (r1, r2, c1, c2) => {
    for (let r = r1; r <= r2; r++) for (let c = c1; c <= c2; c++) ws.getCell(r, c).border = BORDER;
  };

  // 标题
  ws.mergeCells(1, 1, 1, HEADERS.length);
  const title = ws.getCell(1, 1);
  title.value = '关键词库';
  title.font = { bold: true, size: 26, name: '等线' };
  title.alignment = { vertical: 'middle' };
  ws.getRow(1).height = 40;

  // 元信息块（第 2-9 行）
  const metaRows = [
    ['词根', meta.root || ''],
    ['品牌词', meta.brand || ''],
    ['竞·品牌词', meta.compBrand || ''],
    ['核心大词', meta.coreWords || []],
    ['竞对ASIN', meta.asins || []],
    ['卖点词根', meta.sellWords || []],
    ['场景属性', meta.sceneWords || []],
    ['偏好属性', meta.preferWords || []]
  ];
  metaRows.forEach(([label, values], i) => {
    const r = i + 2;
    ws.getRow(r).height = 40;
    const a = ws.getCell(r, 1);
    a.value = label;
    a.font = { bold: true, size: 12, name: '等线', color: { argb: 'FFFFFFFF' } };
    a.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: 'FF133C9A' } };
    a.alignment = { horizontal: 'center', vertical: 'middle' };
    const vals = Array.isArray(values) ? values : [values];
    vals.filter(v => v !== '' && v != null).slice(0, HEADERS.length - 1).forEach((v, j) => {
      const c = ws.getCell(r, j + 2);
      c.value = v;
      c.alignment = { vertical: 'middle' };
    });
    borderBlock(r, r, 1, Math.max(2, Math.min(vals.filter(v => v !== '' && v != null).length + 1, HEADERS.length)));
  });

  // 空行
  ws.mergeCells(10, 1, 10, 11);
  ws.getRow(10).height = 12;

  // 表头
  const hr = ws.getRow(11);
  hr.height = 27;
  HEADERS.forEach((h, i) => {
    const c = ws.getCell(11, i + 1);
    c.value = h;
    c.font = { bold: true, size: 10, name: '宋体' };
    c.alignment = { horizontal: 'center', vertical: 'middle', wrapText: true };
  });
  borderBlock(11, 11, 1, HEADERS.length);

  // 数据行
  const dateCn = (w) => {
    const m = String(w || '').match(/^(\d{4})-(\d{2})-(\d{2})$/);
    return m ? `${m[1]}年${parseInt(m[2], 10)}月${parseInt(m[3], 10)}号` : (w || '');
  };
  const hit = (term, list) => (list || []).filter(w => w && term.toLowerCase().includes(String(w).toLowerCase()));

  rows.forEach((r, i) => {
    const rn = 12 + i;
    ws.getRow(rn).height = 30;
    const term = String(r.term || '');
    let tierPair;
    if (r.asinTier) tierPair = [r.asinTier, r.asinLabel || ''];
    else if (r.tier && TIER_XLSX[r.tier]) tierPair = TIER_XLSX[r.tier];
    else tierPair = ['三方词：卖家精灵', '三方词'];
    let noteParts = [];
    if (r.rank == null && !r.asinTier) {
      if (r.ssSearches != null && r.ssSearches > 0) noteParts.push('SS月搜 ' + r.ssSearches);
      if (r.ssClick3 > 0) noteParts.push('点击集中度 ' + (r.ssClick3 * 100).toFixed(1) + '%');
      if (r.ssBid > 0) noteParts.push('竞价 $' + r.ssBid);
      if (r.ssRelevancy > 0) noteParts.push('SS相关度 ' + r.ssRelevancy);
    }
    if (r.ssCn) noteParts.push('翻译:' + r.ssCn);
    if (r.note) noteParts.push(r.note);
    const vals = [
      term,
      r.asinTier ? dateCn(meta.week) : dateCn(meta.week),
      r.rank == null ? null : (Number(r.rank) || null),
      tierPair[0], tierPair[1],
      r.asinTier ? null : '待使用',
      r.asinTier ? null : (hit(term, meta.sellWords).join(' ') || null),
      r.asinTier ? null : (hit(term, meta.sceneWords).join(' ') || null),
      r.asinTier ? null : (hit(term, meta.preferWords).join(' ') || null),
      r.asinTier ? null : (REL_XLSX[r.rel] || r.relText || null),
      r.convSum3 ? Number(r.convSum3) : null,
      // 购买性 = #1 转化份额÷#1 点击份额；卖家精灵词用它返回的 #1 点击/转化共享按同一公式算（不用购买率）
      ((r.buyRatio > 0 ? Number((r.buyRatio / 100).toFixed(4)) : null)),
      r.asinTier ? null : (r.priority || null)
    ];
    vals.forEach((v, j) => {
      const c = ws.getCell(rn, j + 1);
      c.value = v;
      c.alignment = { vertical: 'middle' };
    });
    if (noteParts.length) ws.getCell(rn, HEADERS.indexOf('备注') + 1).value = noteParts.join('；');
    if (!r.asinTier && r.negWords) ws.getCell(rn, HEADERS.indexOf('否词列表') + 1).value = r.negWords;
    if (!r.asinTier && r.analysis) {
      const c = ws.getCell(rn, HEADERS.indexOf('情况说明') + 1);
      c.value = r.analysis;
      c.alignment = { vertical: 'middle', wrapText: true };
    }
    ws.getCell(rn, 11).numFmt = '0.00_ ';   // 垄断性TOP3：27.20
    ws.getCell(rn, 12).numFmt = '0.00%';    // 购买性：存 1.0404，显示 104.04%（对齐模板）
  });
  if (rows.length) borderBlock(12, 11 + rows.length, 1, HEADERS.length);

  addBuildSheet(wb, meta, rows);

  // ---------- Sheet 3+：按来源分表（对应模板里的 ABA正向-* / ABA反向 / 卖家精灵正向 / 卖家精灵反向-*） ----------
  addKwSourceSheets(wb, meta);
  addCollectionStatusSheet(wb, meta);

  // 下拉校验（对齐模板）
  const last = 11 + Math.max(rows.length, 1);
  const dvList = (words) => {
    const f = `"${(words || []).join(',')}"`;
    return f.length <= 255 ? f : null;
  };
  const addDV = (range, formula) => { if (formula) ws.dataValidations.add(range, { type: 'list', allowBlank: true, formulae: [formula] }); };
  addDV(`D12:D${last}`, '"一级：3w以内,二级：3-15w,三级：15-30w,四级：30w以外,ASIN：TOP30,ASIN：ROS30,ASIN：直接竞对"');
  addDV(`F12:F${last}`, '"待使用,已使用,使用中"');
  addDV(`G12:G${last}`, dvList(meta.sellWords));
  addDV(`H12:H${last}`, dvList(meta.sceneWords));
  addDV(`I12:I${last}`, dvList(meta.preferWords));
  addDV(`P12:P${last}`, '"已开启,已关闭,待开启,已暂停"');
  addDV(`Q12:Q${last}`, '"精否,词否"');

  return wb.xlsx.writeBuffer();
}

/* ---------------- 卖家精灵通道：纯本地直连（cookie 一次性导出到 ss_auth.json） ---------------- */
const SS_AUTH_FILE = path.join(__dirname, 'ss_auth.json');

function loadSSAuth() {
  if (!fs.existsSync(SS_AUTH_FILE)) return null;
  try { return JSON.parse(fs.readFileSync(SS_AUTH_FILE, 'utf8')); } catch { return null; }
}

async function ssCall(apiPath, bodyObj, retry = 1) {
  const auth = loadSSAuth();
  if (!auth) throw new Error('缺少 ss_auth.json，请先运行 node ss_cookie.js 导出卖家精灵 cookie');
  try {
    const res = await fetch(apiPath, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/json, text/plain, */*',
        'X-Requested-With': 'XMLHttpRequest',
        'User-Agent': auth.ua || 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36',
        'Cookie': auth.cookie,
        'Referer': 'https://www.sellersprite.com/v3/keyword-miner',
        'Origin': 'https://www.sellersprite.com'
      },
      body: JSON.stringify(bodyObj),
      signal: AbortSignal.timeout(35000)
    });
    const text = await res.text();
    if (res.status === 401 || res.status === 403) {
      throw Object.assign(new Error('卖家精灵登录态失效，请重新运行 node ss_cookie.js'), { code: 'AUTH' });
    }
    try { return JSON.parse(text); }
    catch { throw new Error(`卖家精灵返回非 JSON（HTTP ${res.status}）: ` + String(text).slice(0, 120)); }
  } catch (e) {
    if (e.code === 'AUTH') throw e;
    if (retry > 0) { await sleep(1200); return ssCall(apiPath, bodyObj, retry - 1); }
    throw new Error('卖家精灵请求失败：' + (e.message || e));
  }
}

async function ssMiner(keyword, pageNum = 1, pageSize = 50) {
  const j = await ssCall('https://www.sellersprite.com/v3/api/keyword-miner', {
    keyword, market: 1, pageNum, pageSize, historyDate: '', orderBy: 21, desc: true,
    filterRootWord: 0, matchType: 0, amazonChoice: false, keywordBidMatchType: 'exact'
  });
  if (j.code && j.code !== 'OK') throw new Error('卖家精灵：' + (j.message || j.code));
  const d = j.data || {};
  const items = d.items || d.list || [];
  const total = Number(d.total) || items.length;
  const fetchedBefore = (Math.max(1, Number(pageNum) || 1) - 1) * (Math.max(1, Number(pageSize) || 50)) + items.length;
  return { total, items, fetched: items.length, pages: 1, limit: Number(pageSize) || 50, truncated: fetchedBefore < total };
}

async function ssMinerAll(keyword, opts = {}) {
  const pageSize = Math.min(100, Math.max(1, Number(opts.pageSize) || 100));
  return SOPRules.collectPages(async (page, size) => {
    const j = await ssCall('https://www.sellersprite.com/v3/api/keyword-miner', {
      keyword, market: 1, pageNum: page, pageSize: size, historyDate: '', orderBy: 21, desc: true,
      filterRootWord: 0, matchType: 0, amazonChoice: false, keywordBidMatchType: 'exact'
    });
    if (j.code && j.code !== 'OK') throw new Error('卖家精灵：' + (j.message || j.code));
    const d = j.data || {};
    return { total: d.total, items: d.items || d.list || [] };
  }, { pageSize, limit: opts.limit, delayMs: 500 });
}

async function ssReverse(asin, skip = 0, limit = 50) {
  const j = await ssCall('https://www.sellersprite.com/v3/api/relation/reversing?market=COM', {
    asin, limit, skip, month: '', badges: [], conversionKeywordTypes: [], trafficKeywordTypes: [],
    order: 12, desc: true, exactly: false, ac: false, keywordBidMatchType: 'exact', filterDeletedKeywords: false
  });
  if (j.code && j.code !== 'OK') throw new Error('卖家精灵：' + (j.message || j.code));
  const d = j.data || {};
  const items = d.items || [];
  const total = Number(d.total) || 0;
  return { total, items, fetched: items.length, pages: 1, limit: Number(limit) || 50, truncated: Number(skip || 0) + items.length < total };
}

async function ssReverseAll(asin, opts = {}) {
  const pageSize = Math.min(100, Math.max(1, Number(opts.pageSize) || 100));
  return SOPRules.collectPages(async (page, size) => {
    const skip = (page - 1) * size;
    const j = await ssCall('https://www.sellersprite.com/v3/api/relation/reversing?market=COM', {
      asin, limit: size, skip, month: '', badges: [], conversionKeywordTypes: [], trafficKeywordTypes: [],
      order: 12, desc: true, exactly: false, ac: false, keywordBidMatchType: 'exact', filterDeletedKeywords: false
    });
    if (j.code && j.code !== 'OK') throw new Error('卖家精灵：' + (j.message || j.code));
    const d = j.data || {};
    return { total: d.total, items: d.items || [] };
  }, { pageSize, limit: opts.limit, delayMs: 500 });
}

// 批量拓展流量词：多个 ASIN 一起拓词（对应卖家精灵「拓展流量词」，最多 20 个）
async function ssExtend(asins, opts = {}) {
  const parsed = SOPRules.validateAsins(asins || [], { max: 20 });
  const uniq = parsed.asins;
  if (!uniq.length) throw new Error('没有有效 ASIN（最多 20 个）');
  const wantVariants = opts.variants !== false && opts.variants !== 'false';

  let counts = null;
  try {
    const p = await ssCall('https://www.sellersprite.com/v3/api/traffic/extend/prepare', { month: '', market: 1, asinList: uniq });
    const d = p.data || {};
    counts = { base: d.results || 0, withVariants: d.variationResults || 0 };
  } catch (e) { console.log('[拓展词] prepare 失败', e.message); }

  const size = Math.min(100, Number(opts.size) || 100);
  const limit = Math.min(1000, Number(opts.limit) || 1000);
  const result = await SOPRules.collectPages(async (page, pageSize) => {
    const j = await ssCall('https://www.sellersprite.com/v3/api/traffic/extend/asin', {
      queryVariations: wantVariants,
      asinList: uniq, originAsinList: uniq,
      market: 1, page, month: '', size: pageSize,
      orderColumn: 12, desc: true, exactly: false, ac: false,
      keywordBidMatchType: 'exact', filterDeletedKeywords: false,
      badges: [], conversionKeywordTypes: [], trafficKeywordTypes: []
    });
    if (j.code && j.code !== 'OK') throw new Error('卖家精灵：' + (j.message || j.code));
    const d = j.data || {};
    return { total: d.total, items: d.items || [] };
  }, { pageSize: size, limit, delayMs: 500 });
  return {
    ...result,
    variants: wantVariants,
    counts,
    asins: uniq,
    note: (counts ? `全部变体 ${counts.withVariants} 词 / 仅这些 ASIN ${counts.base} 词；` : '')
      + `已取 ${result.fetched}/${result.total}${result.truncated ? `，达到单次 ${result.limit} 条上限` : ''}`
  };
}

/* ---------------- 卖家精灵通道结束 ---------------- */

let analyzeCache = { key: '', rows: [], total: 0, truncated: false, ts: 0 };
const asinPriceCache = new Map();
const poeCache = new Map();   // 商机探测器结果缓存 10 分钟   // ASIN → {ts, price}，价格段筛选用，24h 有效

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://127.0.0.1:${PORT}`);

  try {
    // 智能体导出的文件
    if (req.method === 'GET' && url.pathname.startsWith('/exports/')) {
      const name = path.basename(url.pathname);
      const file = path.join(__dirname, 'exports', name);
      if (!file.startsWith(path.join(__dirname, 'exports')) || !fs.existsSync(file)) { res.writeHead(404); return res.end('Not found'); }
      res.writeHead(200, {
        'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'Content-Disposition': `attachment; filename="${encodeURIComponent(name)}"`
      });
      return res.end(fs.readFileSync(file));
    }

    // 静态资源
    if (req.method === 'GET' && !url.pathname.startsWith('/api/')) {
      let rel = url.pathname === '/' ? '/index.html' : url.pathname;
      const file = path.join(PUBLIC_DIR, rel);
      if (!file.startsWith(PUBLIC_DIR) || !fs.existsSync(file)) { res.writeHead(404); return res.end('Not found'); }
      res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-cache, no-store, must-revalidate' });
      return res.end(fs.readFileSync(file));
    }

    if (url.pathname === '/api/bootstrap' && req.method === 'GET') {
      const j = await amazon('/metadata', { selectedCountries: ['us'] });
      return sendJSON(res, 200, { ok: true, data: slimMetadata(j) });
    }

    if (url.pathname === '/api/reports' && req.method === 'POST') {
      const q = await readBody(req);
      const j = await amazon('/reports', buildBody(q, q.pageNumber || 1));
      const rep = (j.reportsV2 || [])[0] || {};
      return sendJSON(res, 200, {
        ok: true,
        total: rep.totalItems || 0,
        pageNumber: rep.pageNumber || 1,
        pageSize: rep.pageSize || 25,
        rows: rep.rows || []
      });
    }

    if (url.pathname === '/api/analyze' && req.method === 'POST') {
      const q = await readBody(req);
      const root = String(q.root || '').trim();
      let asins = String(q.asins || '').trim();
      try {
        const parsed = SOPRules.validateAsins(asins, { max: 100 });
        asins = parsed.asins.join(',');
      } catch (e) {
        return sendJSON(res, 400, { ok: false, error: e.message });
      }
      if (!root && !asins) return sendJSON(res, 400, { ok: false, error: '请填写词根或竞对 ASIN' });
      const isMonthly = q.range === 'monthly';
      if (isMonthly ? !q.month : !q.week) {
        return sendJSON(res, 400, { ok: false, error: isMonthly ? '缺少月份（month）' : '缺少周次（week），请刷新页面重新加载周期下拉' });
      }

      // 缓存原始词表，改阈值/排除词时不用重拉
      const maxRowsParam = Number(q.maxRows);
      const maxRows = maxRowsParam === 0 ? 100000 : (maxRowsParam > 0 ? maxRowsParam : 1000);
      const range = q.range === 'monthly' ? 'monthly' : 'weekly';
      const month = String(q.month || '').slice(0, 7);
      const key = `${root}|${asins}|${range}|${range === 'monthly' ? month : q.week || ''}|${q.country || 'us'}|${maxRows}`;
      let cache = null;
      let fromDisk = false;
      if (analyzeCache.key === key && Date.now() - analyzeCache.ts < 10 * 60 * 1000) {
        cache = analyzeCache;
      } else {
        cache = readDiskCache(key);
        if (cache) fromDisk = true;
      }
      if (!cache) {
        const r = await fetchAllTerms(root, q.week, q.country, maxRows, asins, { range, month });
        cache = { key, rows: r.rows, total: r.total, pages: r.pages, truncated: r.truncated, ts: Date.now() };
        writeDiskCache(key, cache);
      }
      analyzeCache = cache;
      if (!cache.rows.length) return sendJSON(res, 200, {
        ok: true, root, total: 0, analyzed: 0, cached: fromDisk, pages: 0, limit: maxRows,
        scanned: 0, items: [], excluded: { root: 0, brand: 0, offRoot: 0, long: 0, overThreshold: 0, byWord: 0, specOnly: 0 },
        eligible: 0, modifiers: [], brandItems: [], dropped: [],
        hint: range === 'monthly' ? '这个月没有数据，换个词/月次或改用按周' : '这个词没有反向流量数据，换个 ASIN 或周次试试'
      });

      const excludeWords = Array.isArray(q.excludeWords)
        ? q.excludeWords
        : String(q.excludeWords || '').split(/[,，\s]+/).filter(Boolean);

      const result = analyzeTerms(cache.rows, root, Number(q.limit) || 100, {
        maxRank: q.maxRank,
        excludeWords,
        excludeSpec: q.excludeSpec,
        includeRaw: !!q.includeRaw
      });
      if (q.source !== 'agent' && Array.isArray(result.items) && result.items.length) {
        try {
          const a = archive.save({
            roots: [root || ('ASIN:' + asins)],
            week: String(q.week || ''),
            rows: result.items,
            params: { maxRank: q.maxRank, from: 'analyze' },
            source: 'analyze'
          });
          if (a) result.archiveId = a.id;
        } catch (e) { console.error('[archive] analyze 存档失败', e.message); }
      }
      return sendJSON(res, 200, {
        ok: true, root, total: cache.total, analyzed: cache.rows.length,
        pages: Number(cache.pages) || Math.ceil(cache.rows.length / 100), limit: maxRows,
        truncated: cache.truncated, cached: fromDisk,
        debugKeys: q.debugKeys ? Object.keys(cache.rows[0] || {}) : undefined,
        ...result
      });
    }

    if (url.pathname === '/api/product-info' && req.method === 'GET') {
      const asin = String(url.searchParams.get('asin') || '').trim().toUpperCase();
      if (!/^[A-Z0-9]{10}$/.test(asin)) return sendJSON(res, 400, { ok: false, error: 'ASIN 需为 10 位字母数字' });
      try {
        const info = await fetchProductInfo(asin);
        if (!info.title) return sendJSON(res, 200, { ok: false, error: '没抓到标题，可能是页面结构变化或需要登录' });
        return sendJSON(res, 200, { ok: true, asin, ...info });
      } catch (e) {
        return sendJSON(res, 500, { ok: false, error: String(e.message || e) });
      }
    }

    // 调试：返回 ABA 视图的筛选项定义
    if (url.pathname === '/api/debug/filters' && req.method === 'GET') {
      try {
        const j = await amazon('/metadata', { selectedCountries: ['us'] });
        const view = (j.metadata.viewsRoot.views || []).find(v => v.id === 'top-search-terms-default-view') || {};
        return sendJSON(res, 200, { ok: true, filters: view.filters || [] });
      } catch (e) { return sendJSON(res, 500, { ok: false, error: String(e.message || e) }); }
    }

    // ---------- 历史记录：每次分析整份存档，可回看 ----------
    const HIST_DIR = path.join(__dirname, 'history');
    const histFiles = () => {
      try { fs.mkdirSync(HIST_DIR, { recursive: true }); return fs.readdirSync(HIST_DIR).filter(f => f.endsWith('.json')); } catch { return []; }
    };
    const histMeta = (f) => {
      try {
        const j = JSON.parse(fs.readFileSync(path.join(HIST_DIR, f), 'utf8'));
        return { id: f, at: j.at, week: j.week, roots: j.roots, count: (j.rows || []).length, mode: j.mode || '' };
      } catch { return null; }
    };

    if (url.pathname === '/api/history' && req.method === 'GET') {
      const list = histFiles().map(histMeta).filter(Boolean).sort((a, b) => (b.at || 0) - (a.at || 0)).slice(0, 100);
      return sendJSON(res, 200, { ok: true, items: list, dir: HIST_DIR });
    }

    if (url.pathname === '/api/history' && req.method === 'POST') {
      const q = await readBody(req);
      const rows = Array.isArray(q.rows) ? q.rows.slice(0, 3000) : [];
      if (!rows.length) return sendJSON(res, 400, { ok: false, error: '没有可存的词' });
      const at = Date.now();
      const d = new Date(at);
      const stamp = `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, '0')}${String(d.getDate()).padStart(2, '0')}-${String(d.getHours()).padStart(2, '0')}${String(d.getMinutes()).padStart(2, '0')}${String(d.getSeconds()).padStart(2, '0')}`;
      const f = `${stamp}.json`;
      try {
        fs.mkdirSync(HIST_DIR, { recursive: true });
        fs.writeFileSync(path.join(HIST_DIR, f), JSON.stringify({ at, week: q.week || '', roots: q.roots || [], mode: q.mode || '', rows }));
        console.log('[历史] 已存档', f, rows.length, '行');
        return sendJSON(res, 200, { ok: true, id: f, at });
      } catch (e) { return sendJSON(res, 500, { ok: false, error: String(e.message || e) }); }
    }

    if (url.pathname.startsWith('/api/history/')) {
      const id = decodeURIComponent(url.pathname.slice('/api/history/'.length));
      if (!/^[0-9\-]+\.json$/.test(id)) return sendJSON(res, 400, { ok: false, error: '非法记录名' });
      const full = path.join(HIST_DIR, id);
      if (req.method === 'DELETE') {
        try { fs.unlinkSync(full); return sendJSON(res, 200, { ok: true }); } catch (e) { return sendJSON(res, 500, { ok: false, error: '删除失败' }); }
      }
      try {
        const j = JSON.parse(fs.readFileSync(full, 'utf8'));
        return sendJSON(res, 200, { ok: true, ...j, id });
      } catch { return sendJSON(res, 404, { ok: false, error: '记录不存在' }); }
    }

    // 三方工具 · 批量拓展流量词（多个 ASIN）
    if (url.pathname === '/api/ss/extend' && req.method === 'POST') {
      const q = await readBody(req);
      const asins = Array.isArray(q.asins) ? q.asins : String(q.asins || '').split(/[\s,，]+/);
      try { SOPRules.validateAsins(asins, { max: 20, label: '卖家精灵批量拓展 ASIN' }); }
      catch (e) { return sendJSON(res, 400, { ok: false, error: e.message }); }
      try {
        const r = await ssExtend(asins, { variants: q.variants, page: q.page, size: q.size, limit: q.limit });
        return sendJSON(res, 200, { ok: true, ...r });
      } catch (e) { return sendJSON(res, 500, { ok: false, error: String(e.message || e) }); }
    }

    // 细分市场 → 竞对 ASIN 候选（SOP 第三步）
    if (url.pathname === '/api/poe/asins' && req.method === 'POST') {
      const q = await readBody(req);
      const terms = Array.isArray(q.terms) ? q.terms : String(q.terms || '').split(',');
      if (!terms.length) return sendJSON(res, 400, { ok: false, error: '缺少搜索词' });
      try {
        const items = await nicheToAsins(terms, q.week || '', { price: q.price, tol: q.tol, limit: 60 });
        return sendJSON(res, 200, { ok: true, items, total: items.length });
      } catch (e) { return sendJSON(res, 500, { ok: false, error: String(e.message || e) }); }
    }

    // 商机探测器：按关键词查细分市场（SOP 第三步方法三）
    if (url.pathname === '/api/poe' && req.method === 'POST') {
      const q = await readBody(req);
      const kw = String(q.keyword || '').trim();
      if (!kw) return sendJSON(res, 400, { ok: false, error: '缺少关键词' });
      try {
        const items = await poeCall(kw);
        // getNiches 没有分页参数，一次返回全量（实测上限 500 条），这里把是否触顶如实带出去
        return sendJSON(res, 200, {
          ok: true, keyword: kw, items, via: poeLastVia,
          total: items.length, capped: items.length >= 500
        });
      } catch (e) {
        return sendJSON(res, 500, { ok: false, error: String(e.message || e) });
      }
    }

    // 竞对 ASIN 价格段筛选（SOP 第四步：竞对ASIN反向拓展需要价格筛选）
    if (url.pathname === '/api/asins-price' && req.method === 'POST') {
      const q = await readBody(req);
      const list = (Array.isArray(q.asins) ? q.asins : String(q.asins || '').split(/[\s,，]+/))
        .map(s => String(s || '').trim().toUpperCase())
        .filter(a => /^[A-Z0-9]{10}$/.test(a));
      const asins = [...new Set(list)].slice(0, 60);
      if (!asins.length) return sendJSON(res, 400, { ok: false, error: '没有有效 ASIN' });
      const price = Number(q.price) || 0;
      const tol = q.tol === undefined || q.tol === '' ? 0.15 : Number(q.tol);
      const out = [];
      for (let i = 0; i < asins.length; i += 4) {
        const batch = asins.slice(i, i + 4);
        await Promise.all(batch.map(async a => {
          let c = asinPriceCache.get(a);
          if (!c || Date.now() - c.ts > 864e5) {
            let p = 0;
            try { const info = await fetchProductInfo(a); p = Number(info.price) || 0; } catch {}
            c = { ts: Date.now(), price: p };
            asinPriceCache.set(a, c);
          }
          const diff = (price > 0 && c.price > 0) ? Math.abs(c.price - price) / price : null;
          out.push({
            asin: a, price: c.price,
            diff: diff == null ? null : Math.round(diff * 1000) / 10,
            ok: !(tol > 0 && diff != null && diff > tol)
          });
        }));
      }
      const order = new Map(asins.map((a, i) => [a, i]));
      out.sort((x, y) => order.get(x.asin) - order.get(y.asin));
      return sendJSON(res, 200, { ok: true, price, tol, items: out, kept: out.filter(x => x.ok).length });
    }

    if (url.pathname === '/api/check-relevance' && req.method === 'POST') {
      const q = await readBody(req);
      const terms = (Array.isArray(q.terms) ? q.terms : [q.terms])
        .map(t => String(t || '').trim()).filter(Boolean).slice(0, 20);
      if (!terms.length) return sendJSON(res, 400, { ok: false, error: '请提供要校验的搜索词' });
      const refTitle = String(q.refTitle || '').trim();
      if (!refTitle) return sendJSON(res, 400, { ok: false, error: '请填写参考产品标题或核心描述' });
      const refPrice = Number(q.refPrice) || 0;
      const results = await checkRelevance(terms, refTitle, refPrice, Number(q.topN) || 24, 2, {
        fresh: !!q.fresh,
        priceTol: q.priceTol === undefined || q.priceTol === null || q.priceTol === '' ? undefined : Number(q.priceTol)
      });
      return sendJSON(res, 200, {
        ok: true, refTitle, refPrice, results,
        cachedCount: results.filter(r => r.cached).length
      });
    }

    // SOP 第三步方法二：前台搜核心大词，取重复出现的自然位 ASIN 作竞对
    if (url.pathname === '/api/front-asins' && req.method === 'POST') {
      const q = await readBody(req);
      const terms = (Array.isArray(q.terms) ? q.terms : [q.terms]).map(t => String(t || '').trim()).filter(Boolean);
      if (!terms.length) return sendJSON(res, 400, { ok: false, error: '请提供核心大词' });
      const r = await frontRepeatAsins(terms, Number(q.topN) || 20, Number(q.limit) || 10);
      return sendJSON(res, 200, { ok: true, ...r });
    }

    if (url.pathname === '/api/ss/miner' && req.method === 'POST') {
      const q = await readBody(req);
      const kw = String(q.keyword || '').trim();
      if (!kw) return sendJSON(res, 400, { ok: false, error: '缺少关键词' });
      const r = q.all
        ? await ssMinerAll(kw, { pageSize: q.pageSize, limit: q.limit })
        : await ssMiner(kw, Number(q.pageNum) || 1, Math.min(Number(q.pageSize) || 50, 100));
      return sendJSON(res, 200, { ok: true, ...r });
    }

    if (url.pathname === '/api/ss/reverse' && req.method === 'POST') {
      const q = await readBody(req);
      const asin = String(q.asin || '').trim().toUpperCase();
      if (!/^[A-Z0-9]{10}$/.test(asin)) return sendJSON(res, 400, { ok: false, error: 'ASIN 需为 10 位字母数字' });
      const r = q.all
        ? await ssReverseAll(asin, { pageSize: q.pageSize, limit: q.limit })
        : await ssReverse(asin, Math.max(0, Number(q.skip) || 0), Math.min(Number(q.limit) || 50, 100));
      return sendJSON(res, 200, { ok: true, ...r });
    }

/* ---------------- 从浏览器一键抓 cookie（不用开 F12） ---------------- */
function cdpConnect(port) {
  return fetch(`http://127.0.0.1:${port}/json/version`, { signal: AbortSignal.timeout(3000) })
    .then(r => r.json())
    .then(ver => new Promise((resolve, reject) => {
      const ws = new WebSocket(ver.webSocketDebuggerUrl);
      let id = 0; const pend = new Map();
      ws.addEventListener('message', ev => {
        let m; try { m = JSON.parse(ev.data); } catch { return; }
        if (m.id && pend.has(m.id)) { const p = pend.get(m.id); pend.delete(m.id); m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result); }
      });
      ws.addEventListener('open', () => resolve({
        send: (method, params, sessionId) => new Promise((res2, rej2) => {
          const mid = ++id; pend.set(mid, { resolve: res2, reject: rej2 });
          const msg = { id: mid, method, params };
          if (sessionId) msg.sessionId = sessionId;
          ws.send(JSON.stringify(msg));
          setTimeout(() => { if (pend.has(mid)) { pend.delete(mid); rej2(new Error('timeout ' + method)); } }, 20000);
        }),
        close: () => { try { ws.close(); } catch {} }
      }));
      ws.addEventListener('error', () => reject(new Error('CDP 连接失败')));
    }));
}

async function ssFindBrowser() {
  const candidates = [9222];
  try {
    const out = execSync('netstat -ano -p TCP', { encoding: 'latin1', timeout: 8000 });
    candidates.push(...[...new Set([...out.matchAll(/127\.0\.0\.1:(\d+)\s+[^\n]*LISTENING/gi)].map(m => Number(m[1])))]);
  } catch {}
  for (const p of [...new Set(candidates)]) {
    if (!p || p < 1024) continue;
    try {
      const list = await fetch(`http://127.0.0.1:${p}/json/list`, { signal: AbortSignal.timeout(1200) }).then(r => r.json()).catch(() => null);
      if (!Array.isArray(list)) continue;
      const tab = list.find(t => t.type === 'page' && /sellersprite\.com/.test(t.url || ''));
      if (tab) return { port: p, tab };
    } catch {}
  }
  // 主力 Chrome 开着但没开卖家精灵页：cookie 是浏览器级的，照样能抓
  try {
    const ver = await fetch('http://127.0.0.1:9222/json/version', { signal: AbortSignal.timeout(1200) }).then(r => r.json());
    if (ver && ver.Browser) return { port: 9222, tab: null };
  } catch {}
  return null;
}

const ZINIAO_CLI = process.env.ZINIAO_CLI || 'D:\\Software\\ziniao\\resources\\app.asar.unpacked\\envkit\\cli\\win32\\x64\\ziniao.exe';

async function ziniaoAutoStart() {
  if (!fs.existsSync(ZINIAO_CLI)) return false;
  try {
    const out = execSync(`"${ZINIAO_CLI}" browser list`, { encoding: 'utf8', timeout: 30000 });
    const line = String(out).split(/\r?\n/).find(l => /\d{10,}/.test(l));
    if (!line) return false;
    const storeId = line.trim().split(/\t\s*/)[0];
    if (!/^\d{10,}$/.test(storeId)) return false;
    console.log('[紫鸟] 自动启动店铺:', storeId);
    execSync(`"${ZINIAO_CLI}" browser start --id ${storeId} --mode agent`, { encoding: 'utf8', timeout: 180000, stdio: 'ignore' });
    await sleep(6000);
    return true;
  } catch (e) { console.log('[紫鸟] 自动启动失败:', (e.message || e).slice(0, 80)); return false; }
}

async function grabCookies(which) {
  if (which === 'aba') {
    let port = await discoverCdpPort();
    if (!port) {
      await ziniaoAutoStart();
      port = await discoverCdpPort();
    }
    if (!port) throw new Error('紫鸟无法自动启动，请手动打开紫鸟并进入店铺后再试');
    const cdp = await cdpConnect(port);
    try {
      const { cookies } = await cdp.send('Storage.getCookies', {});
      const amazon = cookies.filter(c => /(^|\.)amazon\.com$/.test(c.domain));
      if (!amazon.length) throw new Error('紫鸟里没有 amazon.com 的 cookie，请先登录卖家后台');
      let csrf = '';
      try {
        const list = await fetch(`http://127.0.0.1:${port}/json/list`).then(r => r.json());
        const page = list.find(t => t.type === 'page' && /sellercentral/.test(t.url || ''));
        if (page) {
          const { sessionId } = await cdp.send('Target.attachToTarget', { targetId: page.id, flatten: true });
          const r = await cdp.send('Runtime.evaluate', { expression: `(() => { const m = document.querySelector('meta[name="anti-csrftoken-a2z"]'); return m ? m.content : ''; })()`, returnByValue: true }, sessionId);
          csrf = (r.result && r.result.value) || '';
        }
      } catch {}
      const prev = loadAuth();
      fs.writeFileSync(AUTH_FILE, JSON.stringify({
        cookie: amazon.map(c => `${c.name}=${c.value}`).join('; '),
        csrf,
        ua: (prev && prev.ua) || 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36',
        cookieCount: amazon.length,
        exportedAt: new Date().toISOString(),
        source: 'grab'
      }));
      return { count: amazon.length, csrf: !!csrf };
    } finally { cdp.close(); }
  }
  if (which === 'ss') {
    const found = await ssFindBrowser();
    if (!found) throw new Error('未找到主力 Chrome 的调试端口（9222）');
    const cdp = await cdpConnect(found.port);
    try {
      const { cookies } = await cdp.send('Storage.getCookies', {});
      const ss = cookies.filter(c => /sellersprite/.test(c.domain));
      if (!ss.length) throw new Error('该浏览器里没有卖家精灵 cookie，请先登录');
      const prev = loadSSAuth();
      fs.writeFileSync(SS_AUTH_FILE, JSON.stringify({
        cookie: ss.map(c => `${c.name}=${c.value}`).join('; '),
        ua: (prev && prev.ua) || 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36',
        count: ss.length,
        exportedAt: new Date().toISOString(),
        source: 'grab'
      }));
      return { count: ss.length };
    } finally { cdp.close(); }
  }
  throw new Error('未知目标');
}

if (url.pathname === '/api/auth/grab' && req.method === 'POST') {
  const q = await readBody(req);
  try {
    const r = await grabCookies(String(q.which || ''));
    return sendJSON(res, 200, { ok: true, ...r });
  } catch (e) {
    return sendJSON(res, 400, { ok: false, error: String(e.message || e) });
  }
}

if (url.pathname === '/api/auth/status' && req.method === 'GET') {
      const a = loadAuth(); const s = loadSSAuth();
      return sendJSON(res, 200, {
        ok: true,
        aba: a ? { exportedAt: a.exportedAt, source: a.source || 'script', ua: String(a.ua || '').slice(0, 50) } : null,
        ss: s ? { exportedAt: s.exportedAt, source: s.source || 'script', count: s.count } : null
      });
    }

    if (url.pathname === '/api/auth/aba' && req.method === 'POST') {
      const q = await readBody(req);
      let cookie = String(q.cookie || '').trim(), ua = String(q.ua || '').trim();
      if (cookie.startsWith('{')) {
        try { const j = JSON.parse(cookie); cookie = String(j.cookie || '').trim(); ua = ua || String(j.ua || '').trim(); } catch {}
      }
      if (!cookie) return sendJSON(res, 400, { ok: false, error: 'cookie 为空' });
      if (!/x-main|at-main|session-id/i.test(cookie)) {
        return sendJSON(res, 400, { ok: false, error: '没识别到亚马逊登录字段（x-main / at-main / session-id），检查粘贴内容' });
      }
      fs.writeFileSync(AUTH_FILE, JSON.stringify({
        ua: ua || 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36',
        cookie, exportedAt: new Date().toISOString(), source: 'manual'
      }));
      return sendJSON(res, 200, { ok: true });
    }

    if (url.pathname === '/api/auth/ss' && req.method === 'POST') {
      const q = await readBody(req);
      let cookie = String(q.cookie || '').trim(), ua = String(q.ua || '').trim();
      if (cookie.startsWith('{')) {
        try { const j = JSON.parse(cookie); cookie = String(j.cookie || '').trim(); ua = ua || String(j.ua || '').trim(); } catch {}
      }
      if (!cookie) return sendJSON(res, 400, { ok: false, error: 'cookie 为空' });
      if (!/=/i.test(cookie)) return sendJSON(res, 400, { ok: false, error: '格式不对，应为 k=v; k=v 的 cookie 串' });
      fs.writeFileSync(SS_AUTH_FILE, JSON.stringify({
        ua: ua || 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36',
        cookie, exportedAt: new Date().toISOString(), source: 'manual'
      }));
      return sendJSON(res, 200, { ok: true });
    }

    if (url.pathname === '/api/auth/verify' && req.method === 'POST') {
      const q = await readBody(req);
      const which = String(q.which || '');
      try {
        if (which === 'aba') {
          await amazon('/metadata', { selectedCountries: ['us'] });
          return sendJSON(res, 200, { ok: true, message: 'ABA 凭证有效' });
        }
        if (which === 'ss') {
          const auth = loadSSAuth();
          if (!auth) return sendJSON(res, 400, { ok: false, error: '还没有卖家精灵 cookie' });
          const r = await fetch('https://www.sellersprite.com/v3/api/common/keyword/department/list/1', {
            headers: { 'Cookie': auth.cookie, 'User-Agent': auth.ua, 'X-Requested-With': 'XMLHttpRequest' },
            signal: AbortSignal.timeout(15000)
          });
          if (r.status === 200) return sendJSON(res, 200, { ok: true, message: '卖家精灵凭证有效' });
          if (r.status === 401 || r.status === 403) return sendJSON(res, 200, { ok: false, error: '卖家精灵 cookie 已失效（HTTP ' + r.status + '）' });
          return sendJSON(res, 200, { ok: false, error: '卖家精灵返回 HTTP ' + r.status });
        }
        return sendJSON(res, 400, { ok: false, error: '未知目标' });
      } catch (e) {
        return sendJSON(res, 200, { ok: false, error: String(e.message || e) });
      }
    }

    // 大模型配置：读写 / 连接测试 / 模型增删
    if (url.pathname === '/api/vision/config' && req.method === 'GET') {
      const llm = loadLLM() || {};
      return sendJSON(res, 200, {
        ok: true,
        baseUrl: llm.baseUrl || '',
        protocol: llm.protocol || 'chat',
        model: llm.model || '',
        modalities: ['text', 'image'],
        keyMask: maskKey(llm.key),
        hasKey: !!llm.key,
        configured: !!(llm.model && llm.key && llm.baseUrl)
      });
    }
    if (url.pathname === '/api/vision/config' && req.method === 'POST') {
      return sendJSON(res, 410, { ok: false, error: '图片分析已改为复用当前模型配置，请保存「模型设置」中的模型、接口地址和 API Key' });
    }
    if (url.pathname === '/api/llm/config' && req.method === 'GET') {
      const c = loadLLM() || {};
      return sendJSON(res, 200, {
        ok: true,
        baseUrl: c.baseUrl || '',
        protocol: c.protocol || 'chat',
        model: c.model || '',
        models: Array.isArray(c.models) ? c.models : [],
        keyMask: maskKey(c.key),
        hasKey: !!c.key
      });
    }
    if (url.pathname === '/api/llm/config' && req.method === 'POST') {
      const q = await readBody(req);
      const c = loadLLM() || {};
      if (q.baseUrl !== undefined) c.baseUrl = String(q.baseUrl || '').trim();
      if (q.protocol !== undefined) c.protocol = ['chat', 'messages', 'responses'].includes(q.protocol) ? q.protocol : 'chat';
      if (q.model !== undefined) c.model = String(q.model || '').trim();
      if (q.key) c.key = String(q.key).trim();      // 留空表示不改
      saveLLM(c);
      return sendJSON(res, 200, { ok: true, baseUrl: c.baseUrl, protocol: c.protocol, model: c.model, models: c.models || [], keyMask: maskKey(c.key), hasKey: !!c.key });
    }
    if (url.pathname === '/api/llm/test' && req.method === 'POST') {
      const q = await readBody(req);
      const c = loadLLM() || {};
      const base = String(q.baseUrl || c.baseUrl || '').replace(/\/+$/, '');
      const key = String(q.key || c.key || '');
      if (!base) return sendJSON(res, 200, { ok: false, error: '先填接口地址' });
      try {
        const r = await fetch(base + '/models', {
          headers: { 'Authorization': 'Bearer ' + key, 'x-api-key': key, 'anthropic-version': '2023-06-01' },
          signal: AbortSignal.timeout(25000)
        });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) return sendJSON(res, 200, { ok: false, error: '接口返回 HTTP ' + r.status + '：' + JSON.stringify(j).slice(0, 120) });
        const models = (j.data || j.models || []).map(x => {
          const number = (...values) => values.find(v => Number.isFinite(Number(v)) && Number(v) > 0);
          const context = number(x.context_length, x.context_window, x.max_context_length, x.max_input_tokens);
          const output = number(x.max_output_tokens, x.output_limit, x.max_completion_tokens);
          return {
            id: String(x.id || x.name || ''), name: String(x.name || x.id || ''),
            context: context ? String(context) : '', output: output ? String(output) : '',
            contextProvidedByApi: context !== undefined, outputProvidedByApi: output !== undefined,
            modalities: ['text', 'image']
          };
        }).filter(x => x.id);
        const ids = models.map(x => x.id);
        return sendJSON(res, 200, { ok: true, count: ids.length, ids: ids.slice(0, 200), models: models.slice(0, 200) });
      } catch (e) {
        return sendJSON(res, 200, { ok: false, error: '连不上：' + String(e.message || e) });
      }
    }
    if (url.pathname === '/api/llm/models/save' && req.method === 'POST') {
      const q = await readBody(req);
      const c = loadLLM() || {};
      c.models = Array.isArray(c.models) ? c.models : [];
      const id = String(q.id || '').trim();
      if (!id) return sendJSON(res, 400, { ok: false, error: '模型 ID 不能为空' });
      const item = {
        id,
        name: String(q.name || id),
        context: String(q.context || ''),
        output: String(q.output || ''),
        modalities: ['text', 'image'],
        contextProvidedByApi: !!q.contextProvidedByApi,
        outputProvidedByApi: !!q.outputProvidedByApi
      };
      const idx = c.models.findIndex(x => x.id === id);
      if (idx >= 0) c.models[idx] = { ...c.models[idx], ...item }; else c.models.push(item);
      if (!c.model) c.model = id;
      saveLLM(c);
      return sendJSON(res, 200, { ok: true, models: c.models, model: c.model });
    }
    if (url.pathname === '/api/llm/models/delete' && req.method === 'POST') {
      const q = await readBody(req);
      const c = loadLLM() || {};
      c.models = (c.models || []).filter(x => x.id !== String(q.id || ''));
      if (c.model === q.id) c.model = (c.models[0] || {}).id || '';
      saveLLM(c);
      return sendJSON(res, 200, { ok: true, models: c.models, model: c.model });
    }

    // 词库存档：列表 / 取 / 删 / 合并累计 / 手动存
    if (url.pathname === '/api/archive/list' && req.method === 'GET') {
      return sendJSON(res, 200, { ok: true, items: archive.list() });
    }
    if (url.pathname === '/api/archive/get' && req.method === 'GET') {
      const id = String(url.searchParams.get('id') || '');
      const a = archive.get(id);
      if (!a) return sendJSON(res, 404, { ok: false, error: '没有这个存档' });
      return sendJSON(res, 200, { ok: true, ...a });
    }
    if (url.pathname === '/api/archive/delete' && req.method === 'POST') {
      const q = await readBody(req);
      return sendJSON(res, 200, { ok: archive.remove(String(q.id || '')), items: archive.list() });
    }
    if (url.pathname === '/api/archive/merge' && req.method === 'POST') {
      const q = await readBody(req);
      const key = String(q.key || '').toLowerCase();
      const r = archive.merge(key);
      return sendJSON(res, 200, { ok: true, ...r });
    }
    if (url.pathname === '/api/archive/save' && req.method === 'POST') {
      const q = await readBody(req);
      const a = archive.save({ roots: q.roots || [], week: q.week || '', rows: q.rows || [], params: q.params || {}, refAsin: q.refAsin || '', source: q.source || 'manual' });
      return sendJSON(res, 200, { ok: !!a, ...(a || {}), items: archive.list() });
    }

    // 智能体知识库：看 / 手动教 / 删
    if (url.pathname === '/api/agent/memory' && req.method === 'GET') {
      return sendJSON(res, 200, { ok: true, ...agentMemory.list() });
    }
    if (url.pathname === '/api/agent/memory/add' && req.method === 'POST') {
      const q = await readBody(req);
      const e = agentMemory.add({
        kind: q.kind || 'preference',
        scope: q.scope || '',
        text: q.text,
        source: 'manual'
      });
      if (!e) return sendJSON(res, 200, { ok: false, error: '内容太短，写具体一点' });
      agentMemory.save();
      return sendJSON(res, 200, { ok: true, entry: e, ...agentMemory.list() });
    }
    if (url.pathname === '/api/agent/memory/delete' && req.method === 'POST') {
      const q = await readBody(req);
      const ok = agentMemory.remove(String(q.id || ''));
      agentMemory.save();
      return sendJSON(res, 200, { ok, ...agentMemory.list() });
    }
    if (url.pathname === '/api/agent/memory/clear' && req.method === 'POST') {
      agentMemory.clear();
      agentMemory.save();
      return sendJSON(res, 200, { ok: true, ...agentMemory.list() });
    }

    // 对话（流式）：边跑边把「到哪一步、在干什么」推给前端
    if (url.pathname === '/api/agent/stream' && req.method === 'POST') {
      const q = await readBody(req);
      const sid = String(q.sid || 'default');
      const message = String(q.message || '').trim();
      if (!message) return sendJSON(res, 400, { ok: false, error: '没有消息' });

      res.writeHead(200, {
        'Content-Type': 'text/event-stream; charset=utf-8',
        'Cache-Control': 'no-cache, no-transform',
        'Connection': 'keep-alive',
        'X-Accel-Buffering': 'no'
      });
      if (res.flushHeaders) res.flushHeaders();
      const send = (ev, data) => {
        if (res.writableEnded) return;
        try { res.write('event: ' + ev + '\ndata: ' + JSON.stringify(data || {}) + '\n\n'); } catch {}
      };
      const hb = setInterval(() => { if (!res.writableEnded) { try { res.write(': ping\n\n'); } catch {} } }, 15000);

      const cfg = loadLLM();
      if (!cfg) {
        send('error', { error: '未配置大模型', hint: '项目目录建 llm.json（baseUrl / key / model）后重启；纯查词/导出不需要模型，直接说「查 xxx 的关键词」也能跑' });
        clearInterval(hb);
        return res.end();
      }
      try {
        const out = await agent.chat(sid, message, {
          callLLM, pickJSON, buildKwWorkbook, cfg,
          exportDir: path.join(__dirname, 'exports'),
          emit: send
        });
        send('done', out);
      } catch (e) {
        send('error', { error: String(e.message || e), hint: agent.explainError(String(e.message || e)) });
      } finally {
        clearInterval(hb);
        res.end();
      }
      return;
    }

    // 深度解读（单独点击，才花一次模型调用）
    if (url.pathname === '/api/agent/insight' && req.method === 'POST') {
      const q = await readBody(req);
      const cfg = loadLLM();
      if (!cfg) return sendJSON(res, 200, { ok: false, error: '未配置大模型' });
      try {
        const out = await agent.insight(String(q.sid || 'default'), { callLLM, pickJSON, cfg }, String(q.question || ''));
        return sendJSON(res, 200, out);
      } catch (e) {
        return sendJSON(res, 200, { ok: false, error: '解读失败：' + String(e.message || e) });
      }
    }

    // 对话式智能体：自然语言 → 调工具拿数据 → 回话
    if (url.pathname === '/api/agent/chat' && req.method === 'POST') {
      const q = await readBody(req);
      const sid = String(q.sid || 'default');
      const message = String(q.message || '').trim();
      if (!message) return sendJSON(res, 400, { ok: false, error: '没有消息' });
      const cfg = loadLLM();
      if (!cfg) return sendJSON(res, 200, { ok: false, error: '未配置大模型：项目目录建 llm.json 后重启' });
      try {
        const out = await agent.chat(sid, message, {
          callLLM, pickJSON, buildKwWorkbook, cfg,
          exportDir: path.join(__dirname, 'exports')
        });
        return sendJSON(res, 200, out);
      } catch (e) {
        return sendJSON(res, 200, { ok: false, error: '智能体出错：' + String(e.message || e) });
      }
    }

    // 一句话（中文/口语）→ 结构化任务：产品定义 / 英文词根 / 竞品 ASIN
    if (url.pathname === '/api/agent/plan' && req.method === 'POST') {
      const q = await readBody(req);
      const text = String(q.text || '').trim();
      if (!text) return sendJSON(res, 400, { ok: false, error: '没有输入内容' });
      const cfg = loadLLM();
      if (!cfg) return sendJSON(res, 200, { ok: false, error: '未配置大模型：在项目目录建 llm.json（baseUrl / key / model）后重启' });
      const sys = [
        '你是亚马逊选品与关键词专家，服务对象是中国卖家运营。',
        '把用户的一句话需求转成可直接执行的 JSON，只输出 JSON：',
        '{"product":"产品的中文定义，一句话",',
        ' "category":"英文品类名，2~3个单词",',
        ' "roots":["英文词根，1~3个，每个1~3个单词，必须是买家在亚马逊真实会搜的词"],',
        ' "asins":["用户提到的10位ASIN，原样保留；没有就空数组"],',
        ' "exclude":["明显无关的排除词，0~5个"],',
        ' "maxRank":150000,',
        ' "note":"一句话说明你的判断依据"}',
        '规则：roots 只要英文，不要品牌名，不要中文；用户提到具体型号/规格时词根仍取品类词；拿不准就给最通用的品类词。'
      ].join('\n');
      try {
        const txt = await callLLM(cfg, [{ role: 'system', content: sys }, { role: 'user', content: text }], { maxTokens: 600 });
        const j = pickJSON(txt);
        if (!j) return sendJSON(res, 200, { ok: false, error: '模型没返回 JSON：' + txt.slice(0, 160) });
        j.roots = (Array.isArray(j.roots) ? j.roots : []).map(x => String(x).toLowerCase().trim()).filter(Boolean).slice(0, 3);
        j.asins = (Array.isArray(j.asins) ? j.asins : []).map(x => String(x).toUpperCase().trim()).filter(x => /^[A-Z0-9]{10}$/.test(x));
        j.exclude = Array.isArray(j.exclude) ? j.exclude.slice(0, 8) : [];
        return sendJSON(res, 200, { ok: true, result: j, model: cfg.model || '' });
      } catch (e) {
        return sendJSON(res, 200, { ok: false, error: '调用模型失败：' + String(e.message || e) });
      }
    }

    // 词性分配：把关键词分成 场景 / 偏好 / 属性 / 规格 / 其他
    if (url.pathname === '/api/agent/classify' && req.method === 'POST') {
      const q = await readBody(req);
      const terms = (Array.isArray(q.terms) ? q.terms : []).map(x => String(x || '').trim()).filter(Boolean).slice(0, 300);
      if (!terms.length) return sendJSON(res, 400, { ok: false, error: '没有关键词' });
      const cfg = loadLLM();
      if (!cfg) return sendJSON(res, 200, { ok: false, error: '未配置大模型：在项目目录建 llm.json 后重启' });
      const product = String(q.product || q.root || '').trim();
      const sys = [
        '你是亚马逊关键词分类器。只输出 JSON，不解释。',
        '把每个关键词分到下列一类：',
        'scene=使用场景（home gym / travel / outdoor 等）',
        'prefer=偏好/人群（women / beginners / heavy duty 等）',
        'attribute=卖点属性（adjustable / non-slip / compact 等）',
        'spec=纯规格（20lb / 30 kg / set of 2 等）',
        'other=纯品类词或不带修饰的词',
        '输出格式：{"items":[{"term":"原词","kind":"scene|prefer|attribute|spec|other"}]}'
      ].join('\n');
      try {
        const txt = await callLLM(cfg, [
          { role: 'system', content: sys },
          { role: 'user', content: (product ? '产品：' + product + '\n' : '') + '关键词：\n' + terms.join('\n') }
        ], { maxTokens: 4000 });
        const j = pickJSON(txt);
        if (!j || !Array.isArray(j.items)) return sendJSON(res, 200, { ok: false, error: '模型没返回 JSON：' + txt.slice(0, 160) });
        const items = j.items.filter(x => x && x.term).map(x => ({ term: String(x.term).toLowerCase().trim(), kind: ['scene', 'prefer', 'attribute', 'spec', 'other'].includes(x.kind) ? x.kind : 'other' }));
        return sendJSON(res, 200, { ok: true, items });
      } catch (e) {
        return sendJSON(res, 200, { ok: false, error: '调用模型失败：' + String(e.message || e) });
      }
    }

    // 产品图 → 当前选中模型：高召回整理产品词源架构，并保留证据与不确定项
    if (url.pathname === '/api/vision' && req.method === 'POST') {
      const q = await readBody(req, 20 * 1024 * 1024);
      const images = (Array.isArray(q.images) ? q.images : [q.image]).filter(Boolean).map(String).slice(0, 4);
      if (!images.length || images.some(image => !/^data:image\/(?:jpeg|png|webp|gif);base64,/i.test(image))) {
        return sendJSON(res, 400, { ok: false, error: '图片数据无效，请上传 JPEG、PNG、WebP 或 GIF 图片（最多 4 张）' });
      }
      const cfg = loadVisionLLM();
      if (!cfg) {
        return sendJSON(res, 200, { ok: false, error: '请先在「模型设置」里保存当前模型、接口地址和 API Key，图片会随选中模型请求一起发送' });
      }
      // 分两步：先用短提示让模型只回答「这是什么产品」锚定品类，再展开词源架构。
      // 一次性长提示时，模型容易在长 JSON 里把产品认错（实测把哑铃架写成刀架）。
      const hint = String(q.hint || '').trim().slice(0, 80);
      const identifyPrompt = [
        '只判断这是什么产品，不要展开关键词。先在心里确认：图中的开口槽/挂钩/层板是用来放什么的，放进去的东西长什么样。',
        '输出中文一句话产品定义 + 最贴切的英文品类名（用买家在亚马逊搜索时最常用的商品叫法，不要用功能描述）。',
        hint ? `用户给的参考词根：「${hint}」。如果图片与它一致，category 就用这个词根的品类；如果明显不一致，按图片判断，并在 unsure 里说明冲突。` : '',
        '只输出 JSON：{"product":"中文","category":"english category"' + (hint ? ',"unsure":"与参考词是否一致（无则空）"' : '') + '}'
      ].filter(Boolean).join('\n');
      const archPrompt = [
        '你是 Amazon US 搜索词源架构师。产品已在上文确认，下面所有输出都必须围绕它，不得换成别的产品。',
        '整理一份高召回、可直接拿去 ABA 验证的词源架构，目标是穷尽买家的真实叫法、场景和偏好。',
        '只输出一个严格 JSON 对象，不要 Markdown、解释或代码围栏：',
        '{"product":"中文产品定义（沿用确认结果）",',
        ' "category":"英文品类名词短语（沿用确认结果）",',
        ' "roots":["产品本体的英文类目词根 3-8 个，1-3 词，含主叫法与常见同义叫法"],',
        ' "core":["买家会直接搜的核心大词 10-24 个，不含品牌，只要真实有人这么搜的说法"],',
        ' "scene":["场景词根 8-16 个：使用地点 / 活动 / 对象，如 home gym、garage gym、small apartment"],',
        ' "prefer":["偏好词根 8-16 个：需求、体验、解决的问题，如 space saving、heavy duty、easy assembly"],',
        ' "attributes":["图片可见的材质 / 结构 / 形态 / 功能属性 5-14 个，如 a-frame、steel、5 tier、hex"],',
        ' "evidence":["支撑判断的可见线索，中文，最多 6 条"],',
        ' "uncertain":["仅凭图片无法确认、需人工核对的点，中文，最多 6 条"]}',
        '硬性要求：',
        '1. 全小写、自然美式英文、不带品牌名、不带标点。',
        '2. 禁止机械堆砌：不要写 "X for <某个具体型号/尺寸/刀型/用途>" 这类模板短语，不要用 different / easy to 之类反复凑数。',
        '3. 同义或只差一两个词的短语只保留最常搜的一条，不要近义重复。',
        '4. scene / prefer / attributes 每条 1-4 个单词，不要长句。',
        '5. 只写图片能支持的属性和线索；不确定的放 uncertain，不要当事实。'
      ].join('\n');
      try {
        const base = String(cfg.baseUrl).replace(/\/+$/, '');
        const proto = cfg.protocol || 'chat';
        const endpoint = proto === 'messages' ? base + '/messages' : proto === 'responses' ? base + '/responses' : base + '/chat/completions';
        const headers = proto === 'messages'
          ? { 'Content-Type': 'application/json', 'x-api-key': cfg.key, 'anthropic-version': '2023-06-01' }
          : { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + cfg.key };
        // parts: [{text}] | [{image:dataUrl}] → 按协议拼请求体
        const buildPayload = (parts, maxTokens) => {
          if (proto === 'messages') {
            return {
              max_tokens: maxTokens, temperature: 0.2, system: '你只输出 JSON，不输出解释。',
              messages: [{ role: 'user', content: parts.map(p => {
                if (p.text) return { type: 'text', text: p.text };
                const mt = p.image.match(/^data:(image\/(?:jpeg|png|webp|gif));base64,(.+)$/i);
                return { type: 'image', source: { type: 'base64', media_type: mt[1].toLowerCase(), data: mt[2] } };
              }) }]
            };
          }
          if (proto === 'responses') {
            return {
              temperature: 0.2, max_output_tokens: maxTokens,
              input: [
                { role: 'system', content: [{ type: 'input_text', text: '你只输出 JSON，不输出解释。' }] },
                { role: 'user', content: parts.map(p => p.text ? { type: 'input_text', text: p.text } : { type: 'input_image', image_url: p.image }) }
              ]
            };
          }
          return {
            temperature: 0.2, max_tokens: maxTokens,
            messages: [
              { role: 'system', content: '你只输出 JSON，不输出解释。' },
              { role: 'user', content: parts.map(p => p.text ? { type: 'text', text: p.text } : { type: 'image_url', image_url: { url: p.image } }) }
            ]
          };
        };
        const askModel = async (parts, maxTokens) => {
          const call = async (m) => {
            const r = await fetch(endpoint, {
              method: 'POST', headers, body: JSON.stringify({ ...buildPayload(parts, maxTokens), model: m }),
              signal: AbortSignal.timeout(180000)
            });
            return { r, j: await r.json().catch(() => ({})) };
          };
          let out = await call(cfg.model);
          if (!out.r.ok) {
            // 网关要求模型名带协议前缀时（Use 'openai/xxx'），自动换名重试一次并记住
            const hint = suggestModel(out.j);
            if (hint && hint !== cfg.model) {
              out = await call(hint);
              if (out.r.ok) rememberModel(hint);
            }
          }
          return out;
        };
        const textOf = ({ j: jj }) => {
          jj = jj || {};
          let t = proto === 'messages'
            ? (jj.content || []).map(x => x.text || '').join('')
            : proto === 'responses'
              ? (jj.output_text || (Array.isArray(jj.output) ? jj.output.flatMap(o => (o.content || []).map(c => c.text || '')).join('') : ''))
              : (jj.choices && jj.choices[0] && jj.choices[0].message && jj.choices[0].message.content);
          if (Array.isArray(t)) t = t.map(x => x.text || '').join('');
          return String(t || '').replace(/```json|```/g, '').trim();
        };

        const imgParts = images.map(u => ({ image: u }));
        // 上游把图片丢了（多模态没转发到模型）时，模型会说没收到图；识别阶段就要如实报出来
        const VISION_MISS_RE = /(?:no attached image|no image (?:was )?provided|please upload (?:the )?(?:product )?image|image (?:was )?not provided|cannot see (?:the )?image|can't see (?:the )?image|未收到(?:全部)?(?:产品)?图片|请上传(?:全部)?产品图片|暂无法确定产品)/i;
        // 第一步：只认产品（短提示，准确率明显高于长提示）
        let product = '', category = '';
        try {
          const idr = await askModel([{ text: identifyPrompt }, ...imgParts], 300);
          if (idr.r.ok) {
            const it = textOf(idr);
            if (VISION_MISS_RE.test(it)) {
              return sendJSON(res, 200, {
                ok: false,
                code: 'vision_image_not_read',
                error: `上游接口没有读取到这次发送的图片（这不代表模型本身不支持图片）。当前配置模型「${String(cfg.model || '未命名')}」使用图片入参发送；请检查 API 路由的多模态转发和模型映射。`
              });
            }
            const mm = it.match(/\{[\s\S]*\}/);
            const obj = mm ? JSON.parse(mm[0]) : null;
            if (obj) {
              product = String(obj.product || '').trim();
              category = String(obj.category || '').trim();
              if (obj.unsure) console.log('[vision] 识别与参考词的出入：', obj.unsure);
            }
            console.log('[vision] 识别：', product, '/', category, hint ? '（参考词 ' + hint + '）' : '');
          }
        } catch (e) { console.log('[vision] 识别步骤失败，直接展开：', e.message); }
        const anchor = product && category ? `产品已确认：${product}（English: ${category}）。` : '';

        // 第二步：展开词源架构（带上确认结果做锚点）
        const ar = await askModel([{ text: (anchor ? anchor + '\n' : '') + archPrompt }, ...imgParts], 3000);
        const r = ar.r, j = ar.j;
        if (!r.ok) return sendJSON(res, 200, { ok: false, error: '模型返回 HTTP ' + r.status + '：' + JSON.stringify(j).slice(0, 200) });
        let txt = textOf(ar);
        // Some OpenAI-compatible gateways accept the request but silently route it
        // to a text-only model.  Surface that provider-side capability failure
        // instead of misreporting it as a missing keyword JSON response.
        const providerVisionMiss = VISION_MISS_RE.test(txt);
        if (providerVisionMiss) {
          return sendJSON(res, 200, {
            ok: false,
            code: 'vision_image_not_read',
            error: `上游接口没有读取到这次发送的图片（这不代表模型本身不支持图片）。当前配置模型「${String(cfg.model || '未命名')}」使用 OpenAI Chat Completions 图片入参发送；请检查 API 路由的多模态转发和模型映射。`
          });
        }
        const m = txt.match(/\{[\s\S]*\}/);
        if (!m) return sendJSON(res, 200, { ok: false, error: '模型没返回 JSON：' + txt.slice(0, 160) });
        let result;
        try { result = JSON.parse(m[0]); } catch { return sendJSON(res, 200, { ok: false, error: 'JSON 解析失败：' + m[0].slice(0, 160) }); }
        const cleanWords = (value, max) => {
          const words = Array.isArray(value) ? value : (typeof value === 'string' ? value.split(/[\n,;，；]+/) : []);
          return [...new Set(words.map(x => String(x && (x.term || x.word) || x || '').trim().toLowerCase())
            .map(x => x.replace(/^[\s•*-]+|[\s.,;:]+$/g, ' ').trim().replace(/\s+/g, ' '))
            .filter(x => /^[a-z][a-z0-9' -]{0,79}$/i.test(x)))].slice(0, max);
        };
        const cleanNotes = value => (Array.isArray(value) ? value : []).map(x => String(x || '').trim()).filter(Boolean).slice(0, 8);
        result.roots = cleanWords(result.roots, 8);
        result.core = cleanWords(result.core, 30);
        result.attributes = cleanWords(result.attributes, 24);
        result.scene = cleanWords(result.scene, 24);
        result.prefer = cleanWords(result.prefer, 24);
        result.evidence = cleanNotes(result.evidence);
        result.uncertain = cleanNotes(result.uncertain);
        if (!result.roots.length && result.core.length) result.roots = result.core.slice(0, 8);
        if (!result.roots.length) {
          const why = result.error ? String(result.error) : '模型没有给出词根';
          const emptyArchitecture = !result.product && !result.category && !result.core.length && !result.scene.length && !result.prefer.length && !result.attributes.length && !result.evidence.length;
          if (emptyArchitecture || result.message || result.error) {
            const hint = String(result.error || result.message || '').trim().slice(0, 120);
            return sendJSON(res, 200, {
              ok: false,
              code: 'vision_not_analyzed',
              error: `当前 API 接口没有基于图片生成词源架构${hint ? `（模型回复：${hint}）` : ''}。请检查多模态图片是否被 API 路由转发到模型；这不代表模型本身不支持图片。`
            });
          }
          return sendJSON(res, 200, { ok: false, error: '图片识别没有返回有效英文产品词根：' + why });
        }
        result.product = String(result.product || '').trim().slice(0, 300);
        result.category = String(result.category || '').trim().slice(0, 120);
        return sendJSON(res, 200, { ok: true, result });
      } catch (e) {
        return sendJSON(res, 200, { ok: false, error: '调用视觉模型失败：' + String(e.message || e) });
      }
    }

    if (url.pathname === '/api/export-kw' && req.method === 'POST') {
      const q = await readBody(req, 25 * 1024 * 1024);
      const rows = Array.isArray(q.rows) ? q.rows : [];
      if (!rows.length) return sendJSON(res, 400, { ok: false, error: '没有可导出的关键词' });
      const meta = q.meta || {};
      meta.week = q.week;
      const buf = await buildKwWorkbook(meta, rows);
      res.writeHead(200, {
        'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'Content-Disposition': `attachment; filename="keywords.xlsx"`
      });
      return res.end(Buffer.from(buf));
    }

    if (url.pathname === '/api/export' && req.method === 'POST') {
      const q = await readBody(req);
      const all = [];
      let page = 1, total = 0;
      while (page <= 120) {
        const j = await amazon('/reports', buildBody({ ...q, pageSize: 100 }, page));
        const rep = (j.reportsV2 || [])[0] || {};
        total = rep.totalItems || 0;
        const rows = rep.rows || [];
        all.push(...rows);
        if (rows.length < 100 || all.length >= total) break;
        page++;
        await sleep(220);
      }
      const csv = '\uFEFF' + toCSV(all);
      const fn = `aba_${q.searchTerm || 'all'}_${Date.now()}.csv`.replace(/[^\w.\-]/g, '_');
      res.writeHead(200, { 'Content-Type': 'text/csv; charset=utf-8', 'Content-Disposition': `attachment; filename="${fn}"` });
      return res.end(csv);
    }

    res.writeHead(404);
    res.end('Not found');
  } catch (e) {
    const code = e.code === 'AUTH' ? 401 : e.code === 'BODY_TOO_LARGE' ? 413 : 500;
    if (!res.headersSent) sendJSON(res, code, { ok: false, error: e.message || String(e), auth: e.code === 'AUTH' });
  }
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`ABA 工作台已启动: http://127.0.0.1:${PORT}`);
  const a = loadAuth();
  console.log(a ? `凭证: 已加载 (${a.exportedAt})` : '凭证: 未找到，请先运行 node aba_cookie.js');
});
