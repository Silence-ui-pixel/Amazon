// 亚马逊 ABA「热门搜索词」——纯 Node 直连接口抓取（不需要浏览器）
// 前置: 先运行 node aba_cookie.js 导出 aba_auth.json
// 用法: node aba_api.js dumbbell ["dumbbell set" ...]
//       node aba_api.js --week 2026-10-10 --country us --out 结果.csv dumbbell
const fs = require('fs');
const path = require('path');

const AUTH_FILE = path.join(__dirname, 'aba_auth.json');
const BASE = 'https://sellercentral.amazon.com/api/brand-analytics/v1/dashboard/top-search-terms';
const REPORT_ID = 'top-search-terms-report-table';

// ---- 参数 ----
const argv = process.argv.slice(2);
let week = process.env.WEEK || '2026-10-17';
let country = process.env.COUNTRY || 'us';
let pageSize = Number(process.env.PAGE_SIZE || 100);
let outFile = process.env.OUT || 'aba_top_search_terms.csv';
const keywords = [];
for (let i = 0; i < argv.length; i++) {
  const a = argv[i];
  if (a === '--week') week = argv[++i];
  else if (a === '--country') country = argv[++i];
  else if (a === '--page-size') pageSize = Number(argv[++i]);
  else if (a === '--out') outFile = argv[++i];
  else if (a === '-h' || a === '--help') {
    console.log('用法: node aba_api.js [--week 2026-10-17] [--country us] [--page-size 100] [--out 文件.csv] <关键词1> [关键词2] ...');
    console.log('不带关键词则抓全站前 10000 条（会很慢）。');
    process.exit(0);
  } else keywords.push(a);
}
if (!keywords.length) keywords.push('');

if (!fs.existsSync(AUTH_FILE)) {
  console.error('缺少 aba_auth.json，请先运行: node aba_cookie.js');
  process.exit(1);
}
const auth = JSON.parse(fs.readFileSync(AUTH_FILE, 'utf8'));
const UA = auth.ua || 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36';

// ---- 请求 ----
async function callReports(keyword, page) {
  const filters = [
    { id: 'reporting-range', value: 'weekly', valueType: null },
    { id: 'weekly-week', value: week, valueType: 'weekly' }
  ];
  if (keyword) filters.push({ id: 'search-term-freeform', value: keyword, valueType: null });

  const body = {
    viewId: 'top-search-terms-default-view',
    filterSelections: filters,
    reportOperations: [{
      reportId: REPORT_ID, reportType: 'TABLE',
      pageNumber: page, pageSize, sortByColumnId: 'st-search-frequency', ascending: true
    }],
    selectedCountries: [country],
    reportId: REPORT_ID
  };

  const res = await fetch(`${BASE}/reports`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Accept': 'application/json',
      'X-Requested-With': 'XMLHttpRequest',
      'User-Agent': UA,
      'Cookie': auth.cookie
    },
    body: JSON.stringify(body)
  });

  const text = await res.text();
  if (res.status === 401 || res.status === 403) {
    const err = new Error(`HTTP ${res.status}：登录态失效，请重新运行 node aba_cookie.js 导出 cookie`);
    err.auth = true; throw err;
  }
  let json;
  try { json = JSON.parse(text); }
  catch { const e = new Error(`返回非 JSON（HTTP ${res.status}），可能是登录态失效`); e.auth = true; throw e; }
  const rep = (json.reportsV2 || [])[0] || {};
  return { total: rep.totalItems || 0, rows: rep.rows || [] };
}

async function fetchAll(keyword) {
  let page = 1, total = 0;
  const all = [];
  while (page <= 120) {
    const { total: t, rows } = await callReports(keyword, page);
    total = t || total;
    all.push(...rows);
    if (rows.length < pageSize || all.length >= total) break;
    page++;
    await new Promise(r => setTimeout(r, 250));
  }
  return { total, rows: all };
}

// ---- CSV ----
const COLS = [
  ['搜索词', 'st-search-term'],
  ['搜索频率排名', 'st-search-frequency'],
  ['品牌1', 'st-top-brand-1'], ['品牌2', 'st-top-brand-2'], ['品牌3', 'st-top-brand-3'],
  ['分类1', 'st-top-category-1'], ['分类2', 'st-top-category-2'], ['分类3', 'st-top-category-3'],
  ['ASIN1', 'st-top-asin-1'], ['商品名1', 'st-top-asin-title-1'], ['点击占比1', 'st-top-asin-click-share-1'], ['转化份额1', 'st-top-asin-conversion-share-1'],
  ['ASIN2', 'st-top-asin-2'], ['商品名2', 'st-top-asin-title-2'], ['点击占比2', 'st-top-asin-click-share-2'], ['转化份额2', 'st-top-asin-conversion-share-2'],
  ['ASIN3', 'st-top-asin-3'], ['商品名3', 'st-top-asin-title-3'], ['点击占比3', 'st-top-asin-click-share-3'], ['转化份额3', 'st-top-asin-conversion-share-3']
];
const esc = v => {
  const s = v === undefined || v === null ? '' : String(v);
  return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
};

(async () => {
  console.log(`周次 ${week} | 站点 ${country} | 每页 ${pageSize} | 关键词 ${keywords.length} 个`);
  const lines = [['查询词', ...COLS.map(c => c[0]), '商品链接1', '商品链接2', '商品链接3'].map(esc).join(',')];
  let rowsOut = 0;

  for (const kw of keywords) {
    try {
      const { total, rows } = await fetchAll(kw);
      for (const row of rows) {
        rowsOut++;
        const vals = COLS.map(c => row[c[1]]);
        const links = [1, 2, 3].map(i => row['st-top-asin-' + i] ? `https://www.amazon.com/dp/${row['st-top-asin-' + i]}` : '');
        lines.push([kw || '(全部)', ...vals, ...links].map(esc).join(','));
      }
      console.log(`  [OK] ${kw || '(全部)'}: 共 ${total} 条，抓取 ${rows.length} 条${rows.length >= total ? '（完整）' : `（部分）`}`);
    } catch (e) {
      if (e.auth) { console.error('\n' + e.message); process.exit(2); }
      console.error(`  [失败] ${kw}: ${e.message}`);
    }
  }

  const p = path.join(process.cwd(), outFile);
  fs.writeFileSync(p, '\uFEFF' + lines.join('\r\n'), 'utf8');
  console.log(`合计 ${rowsOut} 行 -> ${p}`);
})();
