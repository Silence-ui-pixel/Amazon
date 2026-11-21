// 真实前端 + 隔离后端验收；不请求商家后台，不消耗模型额度。
const assert = require('node:assert/strict');
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1000}});
    page.on('console',m=>{if(m.type()==='error')console.log('console',m.text());}); page.on('requestfailed',r=>console.log('failed',r.url(),r.failure())); const errors=[]; page.on('pageerror', e=>errors.push(e.message));
    const seed=await (await page.request.get('http://127.0.0.1:8012/__test__/seed')).json();
    await page.addInitScript(data=>{if(!sessionStorage.getItem('core-test-seeded')) {localStorage.setItem('keyword-library-active-run-v1',JSON.stringify(data));sessionStorage.setItem('core-test-seeded','1');}},seed);
    await page.route('**/api/**',async route=>{
      if (!new URL(route.request().url()).pathname.startsWith('/api/')) return route.continue();
      const response=await route.fetch({url:route.request().url().replace('127.0.0.1:5173','127.0.0.1:8012')});
      await route.fulfill({response});
    });
    await page.goto('http://127.0.0.1:5173/');
    const panel=page.getByRole('region',{name:'确定核心大词',exact:true});
    try { await panel.waitFor({timeout:10000}); } catch(e) { console.log(await page.locator('body').innerText()); console.log(errors); throw e; }
    await panel.getByLabel('ABA 周报截止日期').fill('2026-10-17');
    await panel.getByRole('button',{name:'获取 ABA 周报',exact:true}).click();
    await panel.getByText('cooling blanket',{exact:true}).waitFor();
    await panel.getByLabel('自家品牌（选填）').fill('Acme');
    const pending=page.waitForResponse(r=>r.url().endsWith('/core-analyses') && r.request().method()==='POST');
    await panel.getByRole('button',{name:'分析本页候选',exact:true}).click();
    const response=await pending; assert.equal(response.status(),200);
    await panel.getByLabel('选择 cooling blanket',{exact:true}).waitFor();
    assert.equal(await panel.getByLabel('选择 blanket',{exact:true}).isDisabled(),true);
    assert.equal(await panel.getByLabel('选择 electric blanket',{exact:true}).isDisabled(),true);
    assert.equal(await panel.getByLabel('选择 Acme cooling blanket',{exact:true}).isDisabled(),true);
    assert.equal(await panel.getByLabel('选择 Other cooling blanket',{exact:true}).isDisabled(),true);
    await panel.getByLabel('选择 cooling blanket',{exact:true}).check();
    await panel.getByRole('button',{name:'确认核心大词',exact:true}).click();
    await panel.getByText('已保存核心大词',{exact:true}).waitFor();
    if (!(await page.locator('.workflow-body').count())) await page.locator('.workflow-toggle').click();
    assert.equal(await page.locator('.step-item--done').count(),2);
    const counts=async()=> (await (await page.request.get('http://127.0.0.1:8012/__test__/calls')).json()).model;
    assert.equal(await counts(),1);
    await page.reload();
    await panel.getByText('已保存核心大词',{exact:true}).waitFor();
    assert.equal(await counts(),1);
    assert.equal(await panel.getByLabel('选择 cooling blanket',{exact:true}).isChecked(),true);
    await page.addStyleTag({content:'.topbar { visibility:hidden !important; }'});
    await panel.screenshot({path:'../outputs/core-terms-desktop.png'});
    await page.setViewportSize({width:390,height:844});
    await panel.screenshot({path:'../outputs/core-terms-mobile.png'});
    // 服务错误应显示明确提示，并撤销当前完成态。
    await page.route('**/core-selection', route => route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:{message:'测试：后端暂不可用'}})}));
    await panel.getByRole('button',{name:'读取历史状态',exact:true}).click();
    await panel.getByRole('alert').getByText('测试：后端暂不可用',{exact:true}).waitFor();
    if (!(await page.locator('.workflow-body').count())) await page.locator('.workflow-toggle').click();
    assert.equal(await page.locator('.step-item--done').count(),1);
    assert.equal(await counts(),1);
    // 站点变化应立即撤销旧任务结果和完成状态。
    await page.getByLabel('选择 Amazon 站点').selectOption('DE');
    assert.equal(await panel.count(),0);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({saved:true,refreshRestored:true,modelCalls:1,onlyFirstTwoStepsDone:true,staleInputCleared:true,errorVisible:true,pageErrors:errors}));
  } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exitCode=1;});
