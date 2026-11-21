// 真实页面验收：不拦截接口，不替换模型或搜索结果；两个独立浏览器上下文。
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const folder = path.resolve('data', 'frontend-acceptance-' + new Date().toISOString().replace(/[:.]/g, '-'));
fs.mkdirSync(folder, {recursive:true});
const report = {target:'B0CGV33WDY', marketplace:'US', mocked:false, cases:[]};
const save = () => fs.writeFileSync(path.join(folder,'report.json'),JSON.stringify(report,null,2));
const pause = ms => new Promise(resolve=>setTimeout(resolve,ms));
(async()=>{
 console.log('START '+folder);
 const browser = await chromium.launch({headless:true,channel:'chrome'});
 try {
  for(const mode of ['asin','image']) {
   const context=await browser.newContext({viewport:{width:1440,height:1100}});
   const page=await context.newPage();
   const item={mode,status:'running',stage:'input',requests:[],pageErrors:[],consoleErrors:[],checks:{}};
   report.cases.push(item); save();
   page.on('pageerror',e=>{item.pageErrors.push(e.message);save();});
   page.on('console',m=>{if(m.type()==='error') item.consoleErrors.push(m.text());});
   const pending = new Set();
   page.on('response',response=>{
    if(!response.url().includes('/api/v1/'))return;
    const job=(async()=>{
     const url=new URL(response.url()).pathname;
     const method=response.request().method();
     item.requests.push({url,method,status:response.status()});
     const data=await response.json().catch(()=>null);
     if(response.ok()&&method==='POST'&&url.endsWith('/pipeline-runs')) {
      item.run=data; item.inputPayload=response.request().postDataJSON();
     }
     if(response.ok()&&url.endsWith('/root-candidates'))item.roots=data;
     if(response.ok()&&url.endsWith('/product-understanding'))item.understanding=data;
     if(response.ok()&&url.includes('/root-verifications')&&data?.items)item.verification=data;
     if(response.ok()&&url.endsWith('/root-selection'))item.selection=data;
     save();
    })();pending.add(job);job.finally(()=>pending.delete(job));
   });
   try {
    await page.goto('http://127.0.0.1:5173/',{waitUntil:'networkidle'});
    const submit=page.getByRole('button',{name:mode==='asin'?'保存输入并整理资料':'上传图片并保存输入',exact:true});
    for(const box of await page.getByRole('checkbox').all()) {
     if(await box.getAttribute('aria-checked')==='true')await box.click();
    }
    if(mode==='asin') {
     await page.getByRole('checkbox',{name:/ASIN/}).click();
     await page.getByPlaceholder('例如 B0XXXXXXXX').fill('B0CGV33WDY');
    } else {
     await page.getByRole('checkbox',{name:/产品场景图/}).click();
     await page.locator('input[type=file]').setInputFiles(path.resolve('data/asin-B0CGV33WDY-20261024-085422/reference.jpg'));
    }
    assert(await submit.isDisabled(),'未选站点应禁止提交');item.checks.marketplaceRequired=true;
    await page.getByLabel('选择 Amazon 站点').selectOption('US');
    await submit.click();
    await page.locator('.upload-receipt').getByText('商品输入已保存',{exact:true}).waitFor({timeout:30000});
    item.checks.inputSaved=true;item.stage='understanding';save();console.log(mode+' INPUT SAVED');
    await page.getByRole('button',{name:'识别商品 · Qwen 3.8 Max',exact:true}).click();
    await page.getByLabel('商品识别结果',{exact:true}).waitFor({timeout:180000});
    item.checks.productIdentified=true;
    const generate=page.getByRole('button',{name:'生成词根候选 · Qwen 3.8 Max',exact:true});
    if(mode==='image') {
     assert(await generate.isDisabled(),'图片分支应先指定参考主图');
     const picker=page.getByLabel('指定商品主图（用于搜索页比对）');
     const value=await picker.locator('option').nth(1).getAttribute('value');
     await picker.selectOption(value);item.checks.referenceRequired=true;
    }
    item.stage='verification';save();
    await generate.click();console.log(mode+' VERIFYING');
    const deadline=Date.now()+15*60*1000;
    let marker='';
    while(Date.now()<deadline) {
     const task=item.verification;
     const next=task?JSON.stringify(task.items.map(i=>[i.text,i.status,i.active_query,i.checked_pages])):'waiting';
     if(next!==marker){marker=next;console.log(mode+' '+next);}
     if(task&&!['queued','running'].includes(task.status))break;
     if(await page.locator('.upload-error:visible').count()) {
      const errors=await page.locator('.upload-error:visible').allTextContents();
      throw new Error(errors.join('; '));
     }
     await pause(1000);
    }
    assert(item.verification?.status==='completed','核验未完成：'+item.verification?.status);
    assert(item.verification.selection_saved,'未自动保存');
    await page.getByRole('button',{name:'词根选择已保存',exact:true}).waitFor({timeout:15000});
    await Promise.allSettled([...pending]);
    const selected=item.roots.candidates.filter(c=>item.selection.candidate_ids.includes(c.id)).map(c=>c.text);
    item.savedRoots=selected;
    assert(selected.length>0&&selected.every(t=>!t.includes(' ')),'应保存最小单词词根');
    assert(item.verification.items.filter(i=>i.status==='matched').every(i=>i.matched_asin==='B0CGV33WDY'),'匹配到了其他商品');
    assert((await page.locator('.verification-panel').innerText()).includes('实际搜索词与自然位置'));
    item.checks.automaticSave=true;item.checks.canonicalRootSaved=true;item.checks.expansionEvidenceDisplayed=true;
    await page.locator('.roots-panel').screenshot({path:path.join(folder,mode+'-completed.png')});
    const before=item.requests.filter(r=>r.method==='POST').length;
    item.stage='refresh';save();
    await page.reload({waitUntil:'networkidle'});
    await page.getByRole('button',{name:'词根选择已保存',exact:true}).waitFor({timeout:15000});
    await page.getByText('最终最优',{exact:true}).first().waitFor({timeout:10000});
    assert.equal(await page.locator('.root-row input:checked').count(),selected.length);
    assert.equal(item.requests.filter(r=>r.method==='POST').length,before,'刷新重复触发付费流程');
    // 流程图默认折叠，展开后再检查真实节点状态。
    await page.locator('.workflow-header').click();
    assert.equal(await page.locator('.step-item--done').count(),1,'仅第一步应完成');
    assert.equal(await page.locator('.step-item--waiting').count(),7,'后续七步应保持待执行');
    assert.deepEqual(item.pageErrors,[]);
    await page.locator('.roots-panel').screenshot({path:path.join(folder,mode+'-refreshed.png')});
    item.checks.refreshRestored=true;item.checks.noDuplicatePostOnRefresh=true;item.checks.onlyStepOneComplete=true;
    item.status='passed';item.stage='finished';
   } catch(error) {
    item.status='failed';item.error=error.message;
    await page.screenshot({path:path.join(folder,mode+'-failed.png'),fullPage:true}).catch(()=>{});
    console.log(mode+' FAILED '+error.message);
   } finally {
    await Promise.allSettled([...pending]);save();await context.close();
   }
   console.log(mode+' '+item.status+' '+JSON.stringify(item.savedRoots||[]));
  }
 } finally {await browser.close();save();}
 console.log('REPORT '+path.join(folder,'report.json'));
 if(report.cases.some(c=>c.status!=='passed'))process.exitCode=1;
})().catch(error=>{console.error(error);process.exitCode=1;});

