// 缓存回放：仅替换创建任务的响应，识别、候选和确认均访问已存在的真实任务，不产生新模型调用。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {chromium}=require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
(async()=>{
 const run=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage();
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/api/v1/projects',r=>r.fulfill({json:{id:run.project_id}}));
  await page.route('**/pipeline-runs',r=>r.fulfill({json:run}));
  await page.goto('http://127.0.0.1:5173/');
  await page.getByLabel('选择 Amazon 站点').selectOption('US');
  await page.getByRole('checkbox',{name:/产品场景图/}).click();
  await page.locator('input[type=file]').setInputFiles(process.argv[3]);
  await page.getByRole('button',{name:'上传图片并保存输入',exact:true}).click();
  await page.getByRole('button',{name:'识别商品 · Qwen 3.8 Max',exact:true}).click();
  await page.getByRole('button',{name:'生成词根候选 · Qwen 3.8 Max',exact:true}).click();
  await page.getByRole('button',{name:'词根选择已保存',exact:true}).waitFor();
  assert.equal(await page.locator('.root-row input:checked').count(),1);
  await page.getByRole('button',{name:'收起词根',exact:true}).click();
  await page.getByRole('button',{name:'生成词根候选 · Qwen 3.8 Max',exact:true}).click();
  await page.getByRole('button',{name:'词根选择已保存',exact:true}).waitFor();
  assert.equal(await page.locator('.root-row input:checked').count(),1);
  assert.equal(await page.locator('.step-item--done').count(),1);
  assert.equal(await page.locator('.step-item--waiting').count(),7);
  await page.locator('.root-check').first().click();
  assert.equal(await page.getByRole('button',{name:'保存词根选择',exact:true}).isDisabled(),true);
  await page.getByLabel('选择 Amazon 站点').selectOption('DE');
  assert.equal(await page.locator('.roots-panel').count(),0);
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({cachedSelectionRestored:true,onlyFirstStepDone:true,inputChangeCleared:true,pageErrors:errors}));
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
