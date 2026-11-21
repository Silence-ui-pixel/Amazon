// 手动真实联调：会产生一次百炼图片调用，重复 POST 验证结果复用。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    const page = await browser.newPage();
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.goto('http://127.0.0.1:5173/');
    await page.getByLabel('选择 Amazon 站点').selectOption('US');
    await page.getByRole('checkbox',{name:/产品场景图/}).click();
    await page.locator('input[type=file]').setInputFiles(process.argv[2]);
    const saved=page.waitForResponse(r=>r.url().endsWith('/product-materials'));
    await page.getByRole('button',{name:'上传图片并保存输入',exact:true}).click();
    assert.equal((await saved).status(),200);
    const received=page.waitForResponse(r=>r.url().endsWith('/product-understanding') && r.request().method()==='POST',{timeout:150000});
    await page.getByRole('button',{name:'识别商品 · Qwen 3.8 Max',exact:true}).click();
    const response=await received;
    assert.equal(response.status(),200,await response.text());
    const body=await response.json();
    assert.equal(body.model,'qwen3.8-max');
    await page.getByRole('region',{name:'商品识别结果'}).waitFor();
    assert.match(await page.locator('.understanding-result').innerText(),/杯/);
    const repeated=await page.request.post(response.url());
    assert.deepEqual(await repeated.json(),body);
    assert.deepEqual(errors,[]);
    await page.screenshot({path:process.argv[3],fullPage:true});
    console.log(JSON.stringify({model:body.model,product:body.product,reused:true,pageErrors:errors}));
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
