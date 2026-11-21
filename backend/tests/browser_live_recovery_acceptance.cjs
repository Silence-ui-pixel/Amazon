// 网络故障恢复验收：复用真实图片任务，仅从页面继续未完成核验。
const fs=require('node:fs'), path=require('node:path'), assert=require('node:assert/strict');
const {chromium}=require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
(async()=>{
 const reportPath=path.resolve(process.argv[2]), folder=path.dirname(reportPath);
 const source=JSON.parse(fs.readFileSync(reportPath,'utf8')), item=source.cases.find(c=>c.mode==='image');
 const priorPath=path.join(folder,'image-recovery.json');
 const prior=fs.existsSync(priorPath)?JSON.parse(fs.readFileSync(priorPath,'utf8')):null;
 const result={mode:'image',runId:item.run.id,requests:[],errors:[],status:'running',priorRecovery:prior};
 const save=()=>fs.writeFileSync(path.join(folder,'image-recovery.json'),JSON.stringify(result,null,2));
 const browser=await chromium.launch({headless:true,channel:'chrome'});
 try {
  const context=await browser.newContext({viewport:{width:1440,height:1100}}), page=await context.newPage();
  await context.addInitScript(value=>localStorage.setItem('keyword-library-active-run-v1',JSON.stringify(value)),{run:item.run,marketplace:'US',asin:'',root:'',product:'',sources:['image'],reference:item.run.input.image_ids[0],verificationId:item.verification.id});
  page.on('pageerror',e=>result.errors.push(e.message));
  page.on('request',r=>{if(r.url().includes('/api/'))result.requests.push({method:r.method(),url:new URL(r.url()).pathname});});
  const api=`http://127.0.0.1:5173/api/v1/projects/${item.run.project_id}/pipeline-runs/${item.run.id}`;
  const read=async()=>{const response=await page.request.get(api+'/root-verifications/'+item.verification.id);assert(response.ok());return response.json();};
  await page.goto('http://127.0.0.1:5173/',{waitUntil:'networkidle'});
  result.before=await read();
  if(result.before.status==='incomplete') {
  assert.equal(result.before.selection_saved,false);
  await page.getByRole('button',{name:'处理后继续',exact:true}).waitFor({timeout:20000});
  await page.screenshot({path:path.join(folder,'image-incomplete-recovery-controls.png'),fullPage:true});
  const resumed=page.waitForResponse(r=>r.url().endsWith('/resume')&&r.request().method()==='POST');
  await page.getByRole('button',{name:'处理后继续',exact:true}).click();
  assert((await resumed).ok());
  } else {
   assert.equal(result.before.status,'completed');
   let recoveryEvidence=prior;
   while(recoveryEvidence && recoveryEvidence.before?.status!=='incomplete') recoveryEvidence=recoveryEvidence.priorRecovery;
   assert(recoveryEvidence?.before?.status==='incomplete');
   assert(recoveryEvidence.requests.some(r=>r.method==='POST'&&r.url.endsWith('/resume')));
  }
  const deadline=Date.now()+10*60*1000;let marker='';
  while(Date.now()<deadline){
   result.verification=await read();save();
   const next=JSON.stringify(result.verification.items.map(i=>[i.text,i.status,i.active_query,i.checked_pages]));
   if(next!==marker){console.log(next);marker=next;}
   if(!['queued','running'].includes(result.verification.status))break;
   await new Promise(r=>setTimeout(r,3000));
  }
  assert.equal(result.verification.status,'completed');assert(result.verification.selection_saved);
  await page.getByRole('button',{name:'词根选择已保存',exact:true}).waitFor({timeout:20000});
  const roots=await (await page.request.get(api+'/root-candidates')).json();
  const selection=await (await page.request.get(api+'/root-selection')).json();
  result.selection=selection;result.savedRoots=roots.candidates.filter(c=>selection.candidate_ids.includes(c.id)).map(c=>c.text);
  assert(result.savedRoots.length>0&&result.savedRoots.every(t=>!t.includes(' ')));
  assert(result.verification.items.filter(i=>i.status==='matched').every(i=>i.matched_asin===source.target));
  await page.locator('.roots-panel').screenshot({path:path.join(folder,'image-recovered-completed.png')});
  const count=result.requests.filter(r=>r.method==='POST').length;
  await page.reload({waitUntil:'networkidle'});
  await page.getByRole('button',{name:'词根选择已保存',exact:true}).waitFor({timeout:20000});
  await page.locator('.workflow-header').click();
  assert.equal(await page.locator('.step-item--done').count(),1);assert.equal(await page.locator('.step-item--waiting').count(),7);
  assert.equal(await page.locator('.root-row input:checked').count(),result.savedRoots.length);
  assert.equal(result.requests.filter(r=>r.method==='POST').length,count);
  assert.deepEqual(result.errors,[]);
  assert(!(await page.locator('.verification-panel').innerText()).includes('失败阶段：'),'成功后不应残留失败阶段提示');
  assert(result.requests.filter(r=>r.method==='POST').every(r=>r.url.endsWith('/resume')),'恢复不应重新识别或生成候选');
  await page.locator('.roots-panel').screenshot({path:path.join(folder,'image-recovered-refreshed.png')});
  result.checks={recoverableFailureDisplayed:true,incompleteNotSaved:true,continuedExistingTask:true,noRepeatIdentificationOrGeneration:true,canonicalRootSaved:true,refreshRestored:true,noDuplicatePostOnRefresh:true,onlyFirstStepComplete:true};
  result.status='passed';save();console.log(JSON.stringify({status:result.status,savedRoots:result.savedRoots,checks:result.checks}));
 }catch(error){result.status='failed';result.error=error.message;save();throw error;}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
