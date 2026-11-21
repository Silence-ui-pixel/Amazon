const assert = require('node:assert/strict');
const {chromium} = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
(async () => {
  const browser = await chromium.launch({headless:true});
  const page = await browser.newPage({viewport:{width:1440,height:1000}});
  const errors = [], verificationRequests = [];
  page.on('pageerror',e=>errors.push(e.message));
  try {
    const seed = await (await page.request.get('http://127.0.0.1:8016/__test__/seed')).json();
    await page.addInitScript(seed=>localStorage.setItem('keyword-library-active-run-v1',JSON.stringify(seed)),seed);
    await page.route('**/api/v1/**',async route=>{
      if (route.request().url().endsWith('/root-verifications') && route.request().method()==='POST') verificationRequests.push(route.request().postDataJSON());
      const response = await route.fetch({url:route.request().url().replace('127.0.0.1:5174','127.0.0.1:8016')});
      await route.fulfill({response});
    });
    await page.goto('http://127.0.0.1:5174/');
    await page.getByLabel('商品确认人',{exact:true}).fill('浏览器验收');
    await page.getByLabel('商品确认说明',{exact:true}).fill('商品类型与特征已核对');
    await page.getByRole('button',{name:'确认商品资料',exact:true}).click();
    await page.getByText('商品资料已人工确认 · v1',{exact:true}).waitFor();
    await page.getByRole('button',{name:'生成词根候选 · Qwen 3.8 Max',exact:true}).click();
    const roots = page.locator('.roots-panel');
    await roots.locator('.root-row').nth(1).waitFor();
    assert.equal(await roots.locator('input:checked').count(),0);
    assert.match(await roots.innerText(),/马克杯/);
    assert.match(await roots.innerText(),/咖啡杯/);
    assert.equal(verificationRequests.length,0);
    assert.deepEqual((await (await page.request.get('http://127.0.0.1:8016/__test__/calls')).json()).search,[]);
    assert.equal(await roots.getByRole('button',{name:'验证已选词根（0）',exact:true}).isDisabled(),true);
    await roots.locator('.root-check').first().click();
    const save = roots.getByRole('button',{name:'保存词根选择',exact:true});
    assert.equal(await save.isDisabled(),true);
    await roots.getByRole('button',{name:'验证已选词根（1）',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('.roots-footer button')?.disabled === false);
    assert.equal(verificationRequests.length,1);
    assert.equal(verificationRequests[0].candidate_ids.length,1);
    assert.deepEqual((await (await page.request.get('http://127.0.0.1:8016/__test__/calls')).json()).search,[['mug',1]]);
    assert.equal(await roots.locator('input:checked').count(),1);
    // New choices cannot reuse a previous subset's verification.
    await roots.locator('.root-check').nth(1).click();
    assert.equal(await save.isDisabled(),true);
    await roots.locator('.root-check').nth(1).click();
    assert.equal(await save.isEnabled(),true);
    await save.click();
    await roots.getByRole('button',{name:'词根选择已保存',exact:true}).waitFor();
    assert.equal(verificationRequests.length,1);
    await page.addStyleTag({content:'.topbar{visibility:hidden!important}'});
    await roots.screenshot({path:'../outputs/roots-manual-selection.png'});
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({translations:true,noAutomaticSearch:true,selectedOnly:true,noAutoSelection:true,selectionChangeRequiresVerification:true,manualSave:true,errors}));
  } finally {await page.unrouteAll({behavior:'wait'});await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
