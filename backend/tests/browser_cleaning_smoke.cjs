// 本地真实 FastAPI + Vue；第三方来源为隔离样本。
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
(async () => {
  const browser = await chromium.launch({ headless: true });
  let page;
  try {
    page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [], writes = [];
    page.on('pageerror', e => errors.push(e.message));
    const seed = await (await page.request.get('http://127.0.0.1:8014/__test__/seed')).json();
    await page.addInitScript(data => {
      if (!sessionStorage.getItem('cleaning-seeded')) {
        localStorage.setItem('keyword-library-active-run-v1', JSON.stringify(data));
        sessionStorage.setItem('cleaning-seeded', '1');
      }
    }, seed);
    await page.route('**/api/v1/**', async route => {
      if (route.request().method() !== 'GET') writes.push(route.request().url());
      const response = await route.fetch({ url: route.request().url().replace('127.0.0.1:5175', '127.0.0.1:8014') });
      await route.fulfill({ response });
    });
    await page.goto('http://127.0.0.1:5175/');
    const stepSix = page.getByRole('button', {name:/词频分析与清洗/});
    if (await stepSix.count()) await stepSix.click();
    const panel = page.getByRole('region', { name: '词频分析与清洗', exact: true });
    await panel.waitFor();
    await panel.getByLabel('ABA 报告周结束日').fill('2026-10-17');
    await panel.getByRole('checkbox', {name:'确认仅合并关键词文本，各周期排名和份额独立保留，不混算指标。',exact:true}).check();
    await panel.getByRole('button', { name: '汇总、清洗并自动分类', exact: true }).click();
    await panel.getByText('清洗结果与自动分类已保存，第六步已完成。', {exact:true}).waitFor();
    await panel.getByText('场景属性', {exact:true}).first().waitFor();
    await panel.getByRole('button', { name: '下一页', exact: true }).click();
    await panel.getByText(/第 2 页/).waitFor();
    await panel.getByLabel('查看内容').selectOption('phrases');
    await panel.locator('tbody tr').filter({hasText: '=1+1'}).first().waitFor();
    const event = page.waitForEvent('download');
    await panel.getByRole('button', {name:'下载汇总、清洗与分类 CSV',exact:true}).click();
    const download = await event;
    const csv = await fs.readFile(await download.path(), 'utf8');
    assert.match(csv, /'=1\+1/);
    assert.match(csv, /portable/);
    const before = writes.filter(url => url.endsWith('/classify')).length;
    await page.reload();
    const restoredStepSix = page.getByRole('button', {name:/词频分析与清洗/});
    if (await restoredStepSix.count()) await restoredStepSix.click();
    await panel.getByText('清洗结果与自动分类已保存，第六步已完成。', {exact:true}).waitFor();
    await page.waitForLoadState('networkidle');
    assert.equal(writes.filter(url => url.endsWith('/classify')).length, before);
    await page.addStyleTag({content:'.topbar{visibility:hidden!important}'});
    await panel.screenshot({path:'../outputs/cleaning-desktop.png'});
    await page.setViewportSize({width:390,height:844});
    await panel.screenshot({path:'../outputs/cleaning-mobile.png'});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert.deepEqual(errors, []);
    await page.unrouteAll({behavior:'wait'});
    console.log(JSON.stringify({fourSources:true, automaticClassification:true, pagination:true, download:true, restoreWithoutWrites:true, mobileNoOverflow:true, errors}));
  } catch (error) {
    console.error(error);
    throw error;
  } finally {
    if (page) await page.unrouteAll({behavior:'wait'});
    await browser.close();
  }
})().catch(e => {console.error(e);process.exitCode=1;});
