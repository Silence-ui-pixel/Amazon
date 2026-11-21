// 第三步真实 UI + 隔离数据库验收；刷新不得重复采集或调用模型。
const assert = require('node:assert/strict');
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1000}});
    const errors=[]; page.on('pageerror', e=>errors.push(e.message));
    const seed=await (await page.request.get('http://127.0.0.1:8014/__test__/seed')).json();
    await page.addInitScript(data=>{if(!sessionStorage.getItem('competitors-test-seeded')) {localStorage.setItem('keyword-library-active-run-v1',JSON.stringify(data));sessionStorage.setItem('competitors-test-seeded','1');}},seed);
    const writes=[];
    await page.route('**/api/v1/**',async route=>{
      const req=route.request();
      if(req.method()!=='GET') writes.push(req.url());
      const response=await route.fetch({url:req.url().replace('127.0.0.1:5175','127.0.0.1:8014')});
      await route.fulfill({response});
    });
    await page.goto('http://127.0.0.1:5175/');
    const panel=page.getByRole('region',{name:'筛选竞对 ASIN',exact:true});
    await panel.waitFor({timeout:15000});
    await panel.getByLabel('竞对 ASIN（最多 100 个）').fill('b012345678\nB087654321');
    await panel.getByRole('button',{name:'添加人工候选',exact:true}).click();
    await panel.getByText('B012345678',{exact:true}).waitFor();
    const cards=panel.locator('article.candidate');
    await cards.nth(0).getByLabel('人工判断').selectOption('include');
    await cards.nth(0).getByLabel('相关性 / 排除理由').fill('人工核对用途与规格一致');
    const core=page.getByRole('region',{name:'确定核心大词',exact:true});
    const checked=core.getByLabel('选择 cooling blanket',{exact:true});
    await checked.uncheck();
    await checked.check();
    assert.equal(await cards.nth(0).getByLabel('相关性 / 排除理由').inputValue(),'人工核对用途与规格一致');
    await core.getByRole('button',{name:'读取历史状态',exact:true}).click();
    await core.getByRole('button',{name:'已确认保存',exact:true}).waitFor();
    assert.equal(await cards.nth(0).getByLabel('相关性 / 排除理由').inputValue(),'人工核对用途与规格一致');
    await panel.getByRole('button',{name:'保存竞对选择',exact:true}).click();
    await panel.getByRole('alert').getByText('价格未知或超出价格段，保留须填写价格核对说明').waitFor();
    await cards.nth(0).getByLabel('价格核对说明').fill('线下已核对，等待补充价格证据');
    await cards.nth(1).getByLabel('人工判断').selectOption('exclude');
    await cards.nth(1).getByLabel('相关性 / 排除理由').fill('规格不同');
    await panel.getByRole('button',{name:'保存竞对选择',exact:true}).click();
    await panel.getByRole('button',{name:'竞对选择已保存',exact:true}).waitFor();
    if (!(await page.locator('.workflow-body').count())) await page.locator('.workflow-toggle').click();
    assert.equal(await page.locator('.step-item--done').count(),3);
    const before=writes.length;
    await page.reload();
    await panel.getByRole('button',{name:'竞对选择已保存',exact:true}).waitFor();
    assert.equal(writes.length,before);
    assert.equal(await cards.nth(0).getByLabel('人工判断').inputValue(),'include');
    assert.equal(await cards.nth(1).getByLabel('相关性 / 排除理由').inputValue(),'规格不同');
    await page.addStyleTag({content:'.topbar {visibility:hidden !important}'});
    await panel.screenshot({path:'../outputs/competitors-desktop.png'});
    await page.setViewportSize({width:390,height:844});
    await panel.screenshot({path:'../outputs/competitors-mobile.png'});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth),false);
    const csv='ASIN,Product Name,Price,Currency\nB000000003,Cooling blanket,21.50,USD\n';
    await panel.getByLabel('商品 CSV',{exact:true}).setInputFiles({name:'poe-products.csv',mimeType:'text/csv',buffer:Buffer.from(csv)});
    await panel.getByRole('button',{name:'导入商品候选',exact:true}).click();
    await panel.getByText('B000000003',{exact:true}).waitFor();
    assert.equal(await panel.getByRole('button',{name:'保存竞对选择',exact:true}).count(),1);
    assert.equal((await (await page.request.get('http://127.0.0.1:8014/__test__/calls')).json()).model,1);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({manual:true,unknownPriceGuard:true,save:true,restoreWithoutWrites:true,csvImport:true,threeStepsDone:true,mobileNoOverflow:true,pageErrors:errors}));
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});


