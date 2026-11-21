// 手工浏览器集成验证：真实保存 ASIN、图片、组合输入及资料失败重试。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    for (const mode of ['asin', 'image', 'combined']) {
      const page = await browser.newPage();
      const pageErrors = [];
      page.on('pageerror', error => pageErrors.push(error.message));
      await page.goto('http://127.0.0.1:5173/');
      await page.getByLabel('选择 Amazon 站点').selectOption('US');
      // 只选词根不能提交，避免补充信息替代商品识别依据。
      await page.getByRole('checkbox', { name: /^词根/ }).click();
      await page.getByPlaceholder('例如 dumbbell rack、home gym；多个词可换行输入').fill('storage rack');
      assert.equal(await page.getByRole('button', { name: '保存输入并整理资料', exact: true }).isDisabled(), true);
      await page.getByRole('checkbox', { name: /^词根/ }).click();
      if (mode !== 'image') {
        await page.getByRole('checkbox', { name: /商品 ASIN/ }).click();
        await page.getByPlaceholder('例如 B0XXXXXXXX').fill('b0abcdef12');
      }
      if (mode !== 'asin') {
        await page.getByRole('checkbox', { name: /产品场景图/ }).click();
        await page.locator('input[type=file]').setInputFiles({
          name: 'materials-smoke.png', mimeType: 'image/png', buffer: fs.readFileSync(process.argv[2]),
        });
      }
      const buttonName = mode === 'asin' ? '保存输入并整理资料' : '上传图片并保存输入';
      const materialResponse = page.waitForResponse(response => response.url().endsWith('/product-materials'));
      await page.getByRole('button', { name: buttonName, exact: true }).click();
      const response = await materialResponse;
      assert.equal(response.status(), 200);
      const materials = await response.json();
      await page.getByText('商品资料已整理 · US 站', { exact: true }).waitFor();
      assert.equal(materials.asin, mode === 'image' ? null : 'B0ABCDEF12');
      assert.equal(materials.images.length, mode === 'asin' ? 0 : 1);
      assert.equal(await page.locator('.roots-panel').count(), 0);
      if (mode !== 'asin') {
        const source = await page.locator('.attachment-item img').getAttribute('src');
        assert.equal((await page.request.get('http://127.0.0.1:5173' + source)).status(), 200);
      }
      if (mode === 'asin') {
        // 保存成功后刻意让资料查询失败，重试必须复用已有任务而非再次创建。
        await page.getByPlaceholder('例如 B0XXXXXXXX').fill('B0ABCDEF13');
        let submissions = 0;
        page.on('request', request => {
          if (request.method() === 'POST' && request.url().endsWith('/pipeline-runs')) submissions++;
        });
        await page.route('**/product-materials', route => route.fulfill({
          status: 503, contentType: 'application/json',
          body: JSON.stringify({ error: { message: '资料读取测试失败' } }),
        }));
        await page.getByRole('button', { name: buttonName, exact: true }).click();
        await page.getByText('输入已保存，资料读取失败：资料读取测试失败', { exact: true }).waitFor();
        const before = await page.locator('.upload-receipt').innerText();
        assert.match(before, /任务编号：/);
        await page.unroute('**/product-materials');
        await page.getByRole('button', { name: '重试读取资料', exact: true }).click();
        await page.getByText('商品资料已整理 · US 站', { exact: true }).waitFor();
        assert.equal(submissions, 1);
        console.log('materials-read-retry: kept receipt; no duplicate submit');
      }
      assert.deepEqual(pageErrors, []);
      console.log(JSON.stringify({ mode, runId: materials.run_id, images: materials.images.length, pageErrors }));
      await page.close();
    }
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
