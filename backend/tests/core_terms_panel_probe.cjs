// Execute the actual SFC setup and API client in memory. Only HTTP and mounting are replaced.
// Catches: dropping product_term reports, using legacy candidates for new reports,
// wrong collection payloads, and stale selections overwriting the current analysis.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '../..');
const ts = require(path.join(root, 'frontend/node_modules/typescript'));
const vue = require(path.join(root, 'frontend/node_modules/vue'));
const { parse } = require(path.join(root, 'frontend/node_modules/@vue/compiler-sfc'));
const run = { project_id: 'project', id: 'run', status: 'input_received' };
const report = { id: 'report-new', query_mode: 'product_term', rule_version: 'hot-search-core-v1', week_start: '2026-10-18', week_end: '2026-10-24', roots_snapshot: ['gate wheel'], stale: false, warnings: [], reports: [] };
const item = { candidate_id: 'candidate-new', keyword: 'gate wheel', source_product_terms: ['gate wheel'], search_frequency_rank: 10, brand_category: 'generic', intent_evidence: null, summary: null, comparisons: [], status: 'completed', exclusion_reasons: [] };
const latest = { id: 'analysis-B', report_id: report.id, rule_version: 'hot-search-core-v1', status: 'completed', complete: true, stale: false, source_partial: false, own_brands_snapshot: [], items: [item], recommended_candidate_ids: [item.candidate_id], collections: {} };
const transpile = source => ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText;

function probe(options = {}) {
  const calls = [];
  let collected = false;
  let executed = false;
  const activeReport = options.legacy ? { ...report, query_mode: 'root', rule_version: 'legacy-root-v1' } : report;
  class ApiError extends Error { constructor(message, status) { super(message); this.status = status; } }
  async function request(url, init = {}) {
    const method = init.method || 'GET';
    const body = init.body && JSON.parse(init.body);
    calls.push({ url, method, body });
    const endpoint = url.split('?')[0];
    if (endpoint.endsWith('/root-candidates')) return { rule_version: options.legacy ? 'legacy-root-v1' : 'product-term-v1', candidates: [] };
    if (endpoint.endsWith('/aba-reports') && method === 'POST') { collected = true; return activeReport; }
    if (endpoint.endsWith('/aba-reports')) return options.empty && !collected ? [] : [activeReport];
    if (endpoint.endsWith(`/aba-reports/${report.id}`)) return activeReport;
    if (endpoint.endsWith('/core-selection')) return options.selection || null;
    if (endpoint.endsWith('/hot-search-core-analysis')) return options.noAnalysis || options.legacy ? null : { ...latest, ...(options.stale ? { stale: true, status: 'stale' } : {}), ...(options.status ? { status: options.status } : {}) };
    if (endpoint.endsWith('/hot-search-core-analyses/analysis-A')) return { ...latest, id: 'analysis-A', stale: true, status: 'stale', recommended_candidate_ids: ['candidate-old'] };
    if (endpoint.endsWith('/hot-search-core-analyses')) return { ...latest, status: 'queued', complete: false };
    if (endpoint.endsWith('/hot-search-core-analyses/analysis-B/execute')) { executed = true; return { ...latest, status: 'queued', complete: false }; }
    if (endpoint.endsWith('/hot-search-core-analyses/analysis-B')) return executed ? latest : { ...latest, status: 'queued', complete: false };
    if (endpoint.endsWith('/hot-search-candidates')) return [item];
    if (endpoint.endsWith('/core-candidates')) {
      if (options.legacy) return { total: 1, source_partial: false, items: [{ id: 'legacy-candidate', keyword: 'gate wheel', matched_roots: ['gate wheel'], root_reference: true, search_frequency_rank: 1 }] };
      if (options.allowLegacyEndpoint) return { total: 1, source_partial: false, items: [] };
      throw new ApiError('product_term reports require hot-search-candidates', 409);
    }
    if (endpoint.endsWith('/core-analyses')) return [];
    throw new Error(`Unhandled HTTP fixture: ${method} ${url}`);
  }
  const apiExports = {};
  vm.runInNewContext(transpile(fs.readFileSync(path.join(root, 'frontend/src/api/coreTerms.ts'), 'utf8')), {
    exports: apiExports, require: name => { if (name === './images') return { ApiError, request }; throw new Error(name); },
  });
  const source = parse(fs.readFileSync(path.join(root, 'frontend/src/components/CoreTermsPanel.vue'), 'utf8')).descriptor.scriptSetup.content;
  const context = {
    exports: {}, setTimeout, clearTimeout,
    defineProps: () => ({ run, marketplace: 'US' }), defineEmits: () => () => {}, defineModel: () => vue.ref('2026-10-24'),
    require: name => {
      if (name === 'vue') return { ...vue, onMounted: () => {}, onBeforeUnmount: () => {} };
      if (name === '../api/coreTerms') return apiExports;
      if (name === '../api/images') return { ApiError, request, api: { rootCandidates: () => request('/root-candidates') } };
      if (name === '../panelSync') return { usePanelSync: () => vue.ref(false) };
      if (name === '../reportPeriod') return { latestReportWeek: () => '2026-10-24' };
      if (name === '@lucide/vue') return {};
      throw new Error(name);
    },
  };
  vm.runInNewContext(transpile(`${source}\nglobalThis.panel = {restore, loadReport, collect, analyze, report, analysis, selected, error, busy, reports, pageBy, changePageSize, pageSize, visibleRows, canAnalyze, canSave, executeQueued};`), context);
  return { panel: context.panel, calls };
}
async function settle(panel) {
  for (let i = 0; i < 40 && panel.busy.value; i++) { await vue.nextTick(); await new Promise(resolve => setImmediate(resolve)); }
  assert.equal(panel.busy.value, '', 'operation must settle');
  assert.equal(panel.error.value, '', 'operation must not hide an HTTP error');
}

test('I1 restores a real product_term report without a confirmed selection', async () => {
  const { panel } = probe();
  await panel.restore();
  assert.equal(panel.report.value?.id, 'report-new');
  assert.equal(panel.analysis.value?.id, 'analysis-B');
});
test('I2 routes product_term candidate loading away from the legacy 409 endpoint', async () => {
  const { panel, calls } = probe({ noAnalysis: true });
  await assert.doesNotReject(() => panel.loadReport('report-new'));
  assert.equal(calls.some(x => x.url.includes('/core-candidates')), false);
  assert.equal(panel.visibleRows.value.length, 1);
});
test('I3 collects current product-term rules without week_end then starts the new analysis API', async () => {
  const { panel, calls } = probe({ empty: true, noAnalysis: true, allowLegacyEndpoint: true });
  await panel.restore();
  panel.collect();
  await settle(panel);
  const collection = calls.find(x => x.method === 'POST' && x.url.endsWith('/aba-reports'));
  assert.deepEqual(JSON.parse(JSON.stringify(collection.body)), { query_mode: 'product_term', max_rows: 1000 });
  panel.analyze();
  await settle(panel);
  const analysis = calls.find(x => x.method === 'POST' && x.url.endsWith('/hot-search-core-analyses'));
  assert.deepEqual(JSON.parse(JSON.stringify(analysis?.body)), { report_id: 'report-new', own_brands: [] });
  assert.equal(calls.filter(x => x.method === 'POST' && x.url.endsWith('/execute')).length, 1);
  assert.equal(panel.analysis.value.status, 'completed');
});
test('I4 stale selection A cannot replace current hot analysis B or its recommendations', async () => {
  const { panel } = probe({ allowLegacyEndpoint: true, selection: { id: 'selection-A', rule_version: 'hot-search-core-v1', analysis_id: 'analysis-A', report_id: report.id, stale: true, version: 2, items: [{ id: 'candidate-old' }] } });
  await panel.restore();
  assert.equal(panel.analysis.value?.id, 'analysis-B');
  assert.deepEqual(Array.from(panel.selected.value), ['candidate-new']);
});
test('legacy roots retain their report and collection week contract', async () => {
  const { panel, calls } = probe({ legacy: true });
  await panel.restore();
  assert.equal(panel.report.value.query_mode, 'root');
  assert.equal(panel.visibleRows.value[0].id, 'legacy-candidate');
  panel.collect();
  await settle(panel);
  const collection = calls.find(x => x.method === 'POST' && x.url.endsWith('/aba-reports'));
  assert.deepEqual(JSON.parse(JSON.stringify(collection.body)), { week_end: '2026-10-24', query_mode: 'root', max_rows: 1000 });
  assert.equal(calls.some(x => x.url.includes('/hot-search-candidates')), false);
});
test('product-term pagination remains local when analysis has not started', async () => {
  const { panel, calls } = probe({ noAnalysis: true });
  await panel.loadReport('report-new');
  const before = calls.length;
  panel.pageSize.value = 5;
  panel.changePageSize();
  assert.equal(calls.length, before);
  assert.equal(panel.visibleRows.value.length, 1);
});
test('stale evidence is read-only but permits explicit analysis of changed inputs', async () => {
  const { panel, calls } = probe({ stale: true });
  await panel.restore();
  assert.equal(panel.analysis.value.status, 'stale');
  assert.equal(Boolean(panel.canSave.value), false);
  assert.equal(Boolean(panel.canAnalyze.value), true);
  panel.analyze();
  await settle(panel);
  assert.equal(panel.analysis.value.status, 'completed');
  assert.equal(calls.filter(x => x.method === 'POST' && x.url.endsWith('/execute')).length, 1);
});
test('queued running and unknown terminal evidence cannot create duplicate work on refresh', async () => {
  for (const status of ['queued', 'running', 'failed', 'incomplete']) {
    const { panel, calls } = probe({ status });
    await panel.restore();
    assert.equal(Boolean(panel.canAnalyze.value), false);
    panel.analyze();
    await settle(panel);
    assert.equal(calls.filter(x => x.method === 'POST').length, 0);
  }
});
test('restored queue needs an explicit execute action and no duplicate creation', async () => {
  const { panel, calls } = probe({ status: 'queued' });
  await panel.restore();
  assert.equal(calls.filter(x => x.method === 'POST').length, 0);
  panel.executeQueued();
  await settle(panel);
  assert.equal(panel.analysis.value.status, 'completed');
  assert.equal(calls.filter(x => x.method === 'POST').length, 1);
});
test('loading a smaller unanalysed report resets the local display page', async () => {
  const { panel } = probe({ noAnalysis: true });
  await panel.loadReport('report-new');
  panel.pageBy(1);
  await panel.loadReport('report-new');
  assert.equal(panel.visibleRows.value.length, 1);
});
