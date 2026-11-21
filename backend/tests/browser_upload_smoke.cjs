// 手工浏览器冒烟脚本：验证前端 FormData、代理、预览与输入任务关联。
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
(async () => {
 const browser = await chromium.launch({ headless: true });
 const page = await browser.newPage();
 const errors = [];
 page.on('pageerror', error => errors.push(error.message));
 await page.goto('http://127.0.0.1:5173/');
 await page.getByLabel('选择 Amazon 站点').selectOption('US');
 await page.getByRole('checkbox', { name: /产品场景图/ }).click();
 // 使用测试生成的完整 PNG，避免依赖网络图片。
 await page.locator('input[type=file]').setInputFiles({
   name: 'browser-smoke.png', mimeType: 'image/png',
   buffer: require('fs').readFileSync(process.argv[2])
 });
 await page.getByRole('button', { name: '上传图片并保存输入' }).click();
 await page.locator('.upload-receipt').waitFor({ timeout: 15000 });
 const text = await page.locator('.upload-receipt').innerText();
 const imageSource = await page.locator('.attachment-item img').getAttribute('src');
 const preview = await page.request.get('http://127.0.0.1:5173' + imageSource);
 if (preview.status() !== 200) throw new Error('Preview failed');
 if (await page.locator('.roots-panel').count()) throw new Error('Unexpected mock root suggestions');
 if (errors.length) throw new Error(errors.join('\n'));
 console.log(JSON.stringify({ receipt: text, preview: preview.status(), source: imageSource, pageErrors: errors }, null, 2));
 // 验证错误保留在输入区域，不能用伪造扩展名绕过内容校验。
 await page.locator('input[type=file]').setInputFiles({
   name: 'fake.png', mimeType: 'image/png', buffer: Buffer.from('not a real image')
 });
 await page.getByRole('button', { name: '上传图片并保存输入' }).click();
 await page.locator('.upload-error').waitFor();
 console.log('invalid-image-rejected:', await page.locator('.upload-error').innerText());
 await browser.close();
})().catch(error => { console.error(error); process.exitCode = 1; });
