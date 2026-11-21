// 对真实验收任务补验刷新；只读取现有任务，禁止任何 POST，避免重复模型费用。
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
(async()=>{
 const reportPath=path.resolve(process.argv[2]);
 const report=JSON.parse(fs.readFileSync(reportPath,'utf8'));
 const results=[];
 const browser=await chromium.launch({headless:true,channel:'chrome'});
 try {
  for(const item of report.cases.filter(c=>c.verification?.status==='completed')) {
   const context=await browser.newContext({viewport:{width:1440,height:1100}});
   const page=await context.newPage();
   const posts=[],errors=[];
   page.on('request',r=>{if(r.url().includes('/api/')&&r.method()==='POST')posts.push(r.url());});
   page.on('pageerror',e=>errors.push(e.message));
   const snapshot={run:item.run,marketplace:'US',asin:item.mode==='asin'?report.target:'',root:'',product:'',sources:[item.mode],reference:item.mode==='image'?item.run.input.image_ids[0]:'',verificationId:item.verification.id};
   // 恢复上一浏览器上下文的本地任务指针；所有页面数据仍通过真实后端 GET 获取。
   await context.addInitScript(value=>localStorage.setItem('keyword-library-active-run-v1',JSON.stringify(value)),snapshot);
   await page.goto('http://127.0.0.1:5173/',{waitUntil:'networkidle'});
   for(let round=0;round<2;round++) {
    if(round)await page.reload({waitUntil:'networkidle'});
    await page.getByRole('button',{name:'词根选择已保存',exact:true}).waitFor({timeout:20000});
    await page.getByText('最终最优',{exact:true}).first().waitFor();
    await page.locator('.workflow-header').click();
    assert.equal(await page.locator('.step-item--done').count(),1);
    assert.equal(await page.locator('.step-item--waiting').count(),7);
    assert.equal(await page.locator('.root-row input:checked').count(),item.selection.candidate_ids.length);
    assert.equal(posts.length,0,'恢复或刷新触发了 POST');
    assert.deepEqual(errors,[]);
   }
   const evidence=await page.locator('.verification-panel').innerText();
   assert(evidence.includes('实际搜索词与自然位置'));
   await page.locator('.roots-panel').screenshot({path:path.join(path.dirname(reportPath),item.mode+'-refreshed-verified.png')});
   results.push({mode:item.mode,status:'passed',runId:item.run.id,checks:{refreshRestored:true,noDuplicatePostOnRefresh:true,onlyStepOneComplete:true,remainingSevenWaiting:true,selectionRestored:true},posts,errors});
   await context.close();
  }
 }finally{await browser.close();}
 assert(results.length>0);
 fs.writeFileSync(path.join(path.dirname(reportPath),'refresh-checks.json'),JSON.stringify(results,null,2));
 console.log(JSON.stringify(results,null,2));
})().catch(e=>{console.error(e);process.exitCode=1;});
