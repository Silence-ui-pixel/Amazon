// 离线接口夹具：验证生成前主图指定、自动核验和刷新恢复，不产生模型费用。
const assert=require('node:assert/strict');
const {chromium}=require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const project='11111111-1111-4111-8111-111111111111';
const run={id:'22222222-2222-4222-8222-222222222222',project_id:project,status:'input_received'};
const image={id:'33333333-3333-4333-8333-333333333333',project_id:project,original_name:'main.png',content_url:'/offline-main.png',width:200,height:200,size_bytes:1024};
const id='44444444-4444-4444-8444-444444444444';
const roots={candidates:[{id:'55555555-5555-4555-8555-555555555555',text:'coffee mug',translation:'咖啡杯',reason:'直接相关'}],warnings:[],model:'qwen3.8-max',language:'en',run_id:run.id,project_id:project};
const selection={candidate_ids:[roots.candidates[0].id],source:'automatic',verification_id:id,confirmed_at:new Date().toISOString()};
const task={id,status:'completed',region:'United States',final:true,selection_saved:true,message:'核验完成；并列最优全部保留',best_candidate_ids:selection.candidate_ids,items:[{candidate_id:selection.candidate_ids[0],text:'coffee mug',translation:'咖啡杯',status:'matched',page:1,position:3,checked_pages:1,matched_asin:'B0ABCDEF12',match_method:'main_image',search_url:'https://www.amazon.com/s?k=coffee+mug',evidence_url:'',region:'United States',message:'主图匹配商品'}]};
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage({viewport:{width:1440,height:1100}});
  const errors=[]; page.on('pageerror',e=>errors.push(e.message));
  let created=0,rootCalls=0,completed=false;
  await page.route('**/api/v1/**',async route=>{
   const url=new URL(route.request().url()).pathname;
   const method=route.request().method();
   let data;
   if(url.endsWith('/projects')) data={id:project};
   else if(url.endsWith('/images')) data=image;
   else if(url.endsWith('/pipeline-runs')) data=run;
   else if(url.endsWith('/product-materials')) data={marketplace:'US',asin:null,images:[{source:'uploaded',asset:image,url:image.content_url}],warnings:[],asin_resolution:'not_requested',model_status:'succeeded'};
   else if(url.endsWith('/product-understanding')) data={project_id:project,run_id:run.id,provider:'bailian',model:'qwen3.8-max',product:{product_name:'杯子',product_type:'马克杯',features:[],uncertainties:[]},warnings:[]};
   else if(url.endsWith('/root-candidates')) { if(method==='POST') rootCalls++;data=roots; }
   else if(url.endsWith('/root-selection')) {
    if(!completed){await route.fulfill({status:404,json:{error:{message:'尚未确认'}}});return;}
    data=selection;
   }
   else if(url.endsWith('/root-verifications')) {
    created++;
    assert.equal(route.request().postDataJSON().reference_image_id,image.id);
    data={...task,status:'queued',final:false,selection_saved:false};
   }
   else if(url.endsWith('/root-verifications/'+id)) {completed=true;data=task;}
   else {await route.fulfill({status:404,json:{error:{message:'offline fixture'}}});return;}
   await route.fulfill({status:200,json:data});
  });
  await page.goto('http://127.0.0.1:5173/');
  await page.evaluate(()=>localStorage.clear());
  await page.reload();
  await page.getByLabel('选择 Amazon 站点').selectOption('US');
  await page.getByRole('checkbox',{name:/产品场景图/}).click();
  await page.locator('input[type=file]').setInputFiles(process.argv[2]);
  await page.getByRole('button',{name:'上传图片并保存输入',exact:true}).click();
  await page.getByRole('button',{name:'识别商品 · Qwen 3.8 Max',exact:true}).click();
  await page.getByLabel('指定商品主图（用于搜索页比对）').selectOption(image.id,{timeout:3000});
  await page.getByRole('button',{name:'生成词根候选 · Qwen 3.8 Max',exact:true}).click();
  await page.getByText('最终最优',{exact:true}).waitFor();
  assert.equal(created,1);
  assert.equal(await page.locator('.root-row input:checked').count(),1);
  await page.reload();
  await page.getByText('最终最优',{exact:true}).waitFor({timeout:5000});
  assert.equal(created,1,'刷新只能读取原任务，不能新建核验');
  assert.equal(rootCalls,1,'刷新不能重新调用候选生成');
  assert.deepEqual(errors,[]);
  await page.locator('.roots-panel').screenshot({path:process.argv[3]});
  await page.getByLabel('选择 Amazon 站点').selectOption('DE');
  assert.equal(await page.locator('.roots-panel').count(),0);
  console.log(JSON.stringify({preGenerationReference:true,automaticVerification:true,refreshRestored:true,noDuplicateCalls:true,staleInputCleared:true,pageErrors:errors}));
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
