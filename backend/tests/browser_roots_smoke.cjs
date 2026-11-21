// 手动真实联调：产生一次图片识别与一次词根推荐调用，不在自动测试中运行。
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
    const generated = page.waitForResponse(r=>r.url().endsWith('/root-candidates') && r.request().method()==='POST',{timeout:150000});
    await page.getByRole('button',{name:'生成词根候选 · Qwen 3.8 Max',exact:true}).click();
    const rootResponse=await generated;
    assert.equal(rootResponse.status(),200,await rootResponse.text());
    const roots=await rootResponse.json();
    await page.locator('.root-row').first().waitFor();
    assert.equal(await page.locator('.root-row input:checked').count(),0);
    assert.ok(roots.candidates.length>0);
    assert.ok(roots.candidates.every(r=>r.translation && r.reason));
    const save=page.getByRole('button',{name:'保存词根选择',exact:true});
    assert.equal(await save.isDisabled(),true);
    await page.locator('.root-check').first().click();
    const persisted=page.waitForResponse(r=>r.url().endsWith('/root-selection') && r.request().method()==='PUT');
    await save.click();
    const selection=await persisted;
    assert.equal(selection.status(),200,await selection.text());
    await page.getByRole('button',{name:'词根选择已保存',exact:true}).waitFor();
    assert.deepEqual((await (await page.request.get(selection.url())).json()).candidate_ids,[roots.candidates[0].id]);
    assert.equal(await page.locator('.step-item--done').count(),1);
    assert.equal(await page.locator('.step-item--waiting').count(),7);
    assert.equal(await page.locator('.step-item--running').count(),0);
    const cached=await page.request.post(rootResponse.url());
    assert.deepEqual(await cached.json(),roots);
    await page.screenshot({path:process.argv[3],fullPage:true});
    // 收起后重新读取缓存时恢复已保存选择，不重复调用模型。
    await page.getByRole('button',{name:'收起词根',exact:true}).click();
    await page.getByRole('button',{name:'生成词根候选 · Qwen 3.8 Max',exact:true}).click();
    await page.getByRole('button',{name:'词根选择已保存',exact:true}).waitFor();
    assert.equal(await page.locator('.root-row input:checked').count(),1);
    // 改选立即撤销本地“已保存”状态；输入变化清除旧候选。
    await page.locator('.root-check').first().click();
    assert.equal(await page.getByRole('button',{name:'保存词根选择',exact:true}).isDisabled(),true);
    await page.getByLabel('选择 Amazon 站点').selectOption('DE');
    assert.equal(await page.locator('.roots-panel').count(),0);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({rootCount:roots.candidates.length,roots:roots.candidates,selectionSaved:true,onlyFirstStepDone:true,staleResultsCleared:true}));
    console.log(JSON.stringify({model:body.model,product:body.product,reused:true,pageErrors:errors}));
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
