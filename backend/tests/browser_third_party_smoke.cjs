// 隔离本地 API + 实际 Vue 页面；不访问卖家精灵线上服务。
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [], writes = [];
    page.on('pageerror', e => errors.push(e.message));
    const seed = await (await page.request.get('http://127.0.0.1:8014/__test__/seed')).json();
    await page.addInitScript(data => {
      if (!sessionStorage.getItem('third-party-seeded')) {
        localStorage.setItem('keyword-library-active-run-v1', JSON.stringify(data));
        sessionStorage.setItem('third-party-seeded', '1');
      }
    }, seed);
    let hold = false, release, captured;
    await page.route('**/api/v1/**', async route => {
      const req = route.request();
      if (req.method() !== 'GET') writes.push(req.url());
      const response = await route.fetch({ url: req.url().replace('127.0.0.1:5175', '127.0.0.1:8014') });
      if (hold && req.method() === 'GET' && req.url().endsWith('/third-party')) {
        hold = false;
        captured();
        await new Promise(resolve => { release = resolve; });
      }
      await route.fulfill({ response });
    });
    await page.goto('http://127.0.0.1:5175/');
    const panel = page.getByRole('region', { name: '卖家精灵正向 / 反向词表', exact: true });
    await panel.waitFor({ timeout: 15000 });
    await panel.getByLabel('报告开始日').fill('2026-10-01');
    await panel.getByLabel('报告结束日').fill('2026-10-31');
    await panel.getByLabel('来源总行数（不含表头）').fill('23');
    await panel.getByRole('checkbox').check();
    const csv = '\ufeff关键词,关键词翻译,月搜索量,ABA月排名,#1 点击共享,#1转化共享\n' +
      Array.from({ length: 23 }, (_, i) => `cooling blanket ${i},凉毯,${i},20,0.00001,0%`).join('\n');
    async function upload() {
      await panel.locator('input[type=file]').setInputFiles({ name: 'sellersprite.csv', mimeType: 'text/csv', buffer: Buffer.from(csv) });
      await panel.getByRole('button', { name: '上传并预览', exact: true }).click();
      await panel.getByRole('button', { name: '确认映射并保存词表', exact: true }).waitFor();
    }
    async function confirm() {
      await panel.getByRole('button', { name: '确认映射并保存词表', exact: true }).click();
      await panel.getByRole('button', { name: '下载含来源的 CSV', exact: true }).waitFor();
      await panel.getByText('共 23 条 · 第 1 页', { exact: true }).waitFor();
    }
    await upload();
    // 延迟旧预览 GET，确认后的新 GET 先返回；旧响应不得覆盖已确认状态。
    hold = true;
    const snapshot = new Promise(resolve => { captured = resolve; });
    await panel.getByRole('button', { name: '读取导入历史' }).click();
    await snapshot;
    await confirm();
    release();
    await page.waitForLoadState('networkidle');
    assert.equal(await panel.getByRole('button', { name: '确认映射并保存词表', exact: true }).count(), 0);
    assert.equal(await panel.locator('tbody tr').count(), 20);
    await panel.getByRole('button', { name: '下一页', exact: true }).click();
    await panel.getByText('共 23 条 · 第 2 页', { exact: true }).waitFor();
    assert.equal(await panel.locator('tbody tr').count(), 3);
    const event = page.waitForEvent('download');
    await panel.getByRole('button', { name: '下载含来源的 CSV', exact: true }).click();
    const download = await event;
    assert.match(await fs.readFile(await download.path(), 'utf8'), /sellersprite,forward,cooling blanket,US/);
    await panel.getByLabel('查找方向').selectOption('reverse');
    for (const asin of ['B012345678', 'B087654321']) {
      await panel.getByLabel('对应竞对 ASIN').selectOption(asin);
      await upload();
      await confirm();
    }
    await panel.getByText('本周期所需正反向词表均已确认，第五步已完成。', { exact: true }).waitFor();
    const before = writes.length;
    await page.reload();
    await panel.getByText('本周期所需正反向词表均已确认，第五步已完成。', { exact: true }).waitFor();
    await page.waitForLoadState('networkidle');
    assert.equal(writes.length, before);
    await page.addStyleTag({ content: '.topbar{visibility:hidden!important}' });
    await panel.screenshot({ path: '../outputs/third-party-desktop.png' });
    await page.setViewportSize({ width: 390, height: 844 });
    await panel.screenshot({ path: '../outputs/third-party-mobile.png' });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    const competitors = page.getByRole('region', { name: '筛选竞对 ASIN', exact: true });
    await competitors.locator('article.candidate').first().getByLabel('相关性 / 排除理由').fill('再次核对规格一致 ' + Date.now());
    await page.waitForFunction(() => !document.querySelector('.third-party-panel .success'));
    await competitors.getByRole('button', { name: '保存竞对选择', exact: true }).click();
    await competitors.getByRole('button', { name: '竞对选择已保存', exact: true }).waitFor();
    await page.waitForFunction(() => document.querySelector('.third-party-panel article h3')?.textContent.includes('上游已变化'));
    assert.deepEqual(errors, []);
    await page.waitForLoadState('networkidle');
    await page.unrouteAll({ behavior: 'wait' });
    console.log(JSON.stringify({ upload:true, mapping:true, tinyDecimal:true, refreshRace:true, pagination:true, download:true, fullCoverage:true, restoreWithoutWrites:true, staleAfterUpstreamChange:true, mobileNoOverflow:true, pageErrors:errors }));
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
