const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { parse, compileTemplate } = require('@vue/compiler-sfc');

function loadModule() {
  const source = fs.readFileSync('src/api/abaExpansion.ts', 'utf8');
  const code = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS },
  }).outputText;
  const mod = { exports: {} };
  new Function('require', 'module', 'exports', code)(
    () => ({ request: async () => ({}) }),
    mod,
    mod.exports,
  );
  return mod.exports;
}

(async () => {
  let requested = '';
  let clicked = false;
  let downloaded = '';
  let fetchCount = 0;
  global.fetch = async (url) => {
    fetchCount++;
    requested = String(url);
    return {
      ok: true,
      headers: {
        get: () =>
          fetchCount === 1
            ? "attachment; filename*=UTF-8''%E7%AC%AC%E5%9B%9B%E6%AD%A5ABA%E6%AD%A3%E5%90%91dumbbell%20rack.xlsx"
            : null,
      },
      blob: async () => new Blob(['xlsx']),
    };
  };
  global.URL.createObjectURL = () => 'blob:xlsx';
  global.URL.revokeObjectURL = () => {};
  global.document = {
    createElement: () => ({
      href: '',
      set download(value) {
        downloaded = value;
      },
      click: () => {
        clicked = true;
      },
    }),
  };

  const report = { id: 'report-1', direction: 'forward', week_end: '2026-10-17' };
  const module = loadModule();
  await module.expansionApi.downloadQueryXlsx(
    { id: 'run-1', project_id: 'project-1' },
    report,
    1,
    'dumbbell rack',
  );

  assert.equal(
    requested,
    '/api/v1/projects/project-1/pipeline-runs/run-1/aba-expansion/report-1/queries/1/export.xlsx',
  );
  assert.equal(downloaded, '第四步ABA正向dumbbell rack.xlsx');
  assert.equal(clicked, true);

  const reverse = { id: 'report-2', direction: 'reverse', week_end: '2026-10-17' };
  await module.expansionApi.downloadQueryXlsx(
    { id: 'run-1', project_id: 'project-1' },
    reverse,
    0,
    'B012345678,B087654321',
  );
  assert.equal(
    requested,
    '/api/v1/projects/project-1/pipeline-runs/run-1/aba-expansion/report-2/queries/0/export.xlsx',
  );
  assert.equal(downloaded, '第四步ABA反向2个ASIN.xlsx');

  const legacyQuery = { status: 'legacy', fetched: 100, error: null };
  assert.equal(
    module.queryDownloadable(
      {
        status: 'partial',
        warnings: ['采集意外中断，请读取已保存进度后显式重试。'],
      },
      legacyQuery,
    ),
    false,
  );
  assert.equal(
    module.queryDownloadable(
      {
        status: 'partial',
        warnings: ['部分查询失败或达到采集边界，当前结果不代表穷尽；请查看各查询原因后显式重试。'],
      },
      legacyQuery,
    ),
    true,
  );

  const panelSource = fs.readFileSync('src/components/AbaExpansionPanel.vue', 'utf8');
  const { descriptor } = parse(panelSource);
  const compiled = compileTemplate({
    source: descriptor.template.content,
    filename: 'AbaExpansionPanel.vue',
    id: 'aba-forward-xlsx',
  });
  assert.equal(compiled.errors.length, 0, String(compiled.errors));
  const render = compiled.code;
  assert.match(render, /下载.*Excel/, '正反向查询都必须显示独立 Excel 下载入口');
  assert.match(render, /downloadQueryXlsx/, '下载入口必须调用逐查询 API');
  assert.match(render, /q\.fetched/, '空结果必须根据实际采集行数禁用下载');
  assert.match(render, /queryDownloadable/, '下载入口必须按逐查询完成状态判断');
  assert.match(panelSource, /queryDownloadable/, '面板必须复用 API 层的完整性判断');
  assert.doesNotMatch(
    panelSource,
    /v-if="selected\.direction === 'forward'"/,
    '反向联合 ASIN 查询也必须显示 Excel 下载入口',
  );
  console.log('ABA forward per-query XLSX API and panel passed');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
