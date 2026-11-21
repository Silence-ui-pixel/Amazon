// Verify compact input without writing any project data.
const assert = require('node:assert/strict');
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  try {
    await page.addInitScript(() => localStorage.clear());
    await page.goto('http://127.0.0.1:5174/');
    const panel = page.getByRole('region', { name: '添加研究商品', exact: true });
    const save = panel.getByRole('button', { name: '保存商品', exact: true });
    await panel.waitFor();
    assert.equal(await save.isDisabled(), true);
    assert.equal(await panel.getByLabel('词根或核心词', { exact: true }).isVisible(), false);
    await panel.getByLabel('选择 Amazon 站点').selectOption('US');
    await panel.getByLabel('商品 ASIN', { exact: true }).fill('b012345678');
    assert.equal(await save.isEnabled(), true);
    await panel.getByLabel('商品 ASIN', { exact: true }).fill('bad');
    assert.equal(await save.isDisabled(), true);
    await panel.getByLabel('商品 ASIN', { exact: true }).fill('');
    await panel.locator('input[type=file]').setInputFiles({ name:'product.png', mimeType:'image/png', buffer:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jX1kAAAAASUVORK5CYII=','base64') });
    assert.equal(await save.isEnabled(), true);
    await panel.getByRole('button', {name:'移除 product.png',exact:true}).click();
    assert.equal(await save.isDisabled(), true);
    await panel.getByText('补充信息', {exact:false}).click();
    await panel.getByLabel('词根或核心词', {exact:true}).fill('cooling blanket');
    assert.equal(await save.isDisabled(), true);
    await panel.getByLabel('商品 ASIN', {exact:true}).fill('B012345678');
    assert.equal(await save.isEnabled(), true);
    await panel.getByText('补充信息', {exact:false}).click();
    await panel.getByLabel('商品 ASIN', {exact:true}).fill('');
    await page.addStyleTag({content:'.topbar{visibility:hidden!important}'});
    await panel.screenshot({path:'../outputs/input-compact-desktop.png'});
    await page.setViewportSize({width:390,height:844});
    await panel.screenshot({path:'../outputs/input-compact-mobile.png'});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({asinValidation:true,imageOnly:true,rootOptional:true,collapsedExtras:true,mobileNoOverflow:true,writes:0,errors}));
  } finally { await browser.close(); }
})().catch(e => {console.error(e);process.exitCode=1;});

