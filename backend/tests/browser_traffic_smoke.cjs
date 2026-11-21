// 本地真实 FastAPI + Vue；第三方来源为隔离样本。
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
(async () => {
  const browser = await chromium.launch({ headless: true });
  let page;
  const errors = [], writes = [];
  try {
    page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    page.on('pageerror', e => { errors.push(e.message); console.error('PAGE ERROR',e.message); });
    const seed = await (await page.request.get('http://127.0.0.1:8014/__test__/seed')).json();
    await page.addInitScript(data => {
      if (!sessionStorage.getItem('cleaning-seeded')) {
        localStorage.setItem('keyword-library-active-run-v1', JSON.stringify(data));
        sessionStorage.setItem('cleaning-seeded', '1');
      }
    }, seed);
    await page.route('**/api/v1/**', async route => {
      if (route.request().method() !== 'GET') writes.push(route.request().url());
      const response = await route.fetch({ url: route.request().url().replace('127.0.0.1:5175', '127.0.0.1:8014') });
      await route.fulfill({ response });
    });
    await page.goto('http://127.0.0.1:5175/');
    const workflowNavigation = page.getByRole('navigation', {name:'八步操作导航',exact:true});
    await workflowNavigation.locator('button[data-step="5"]').click();
    const panel = page.getByRole('region', { name: '词频分析与清洗', exact: true });
    await panel.waitFor();
    await page.waitForLoadState('networkidle');
    await panel.getByLabel('ABA 报告周结束日').fill('2026-10-17');
    await panel.getByRole('checkbox', {name:'确认仅合并关键词文本，各周期排名和份额独立保留，不混算指标。',exact:true}).check();
    await panel.getByRole('button', { name: '汇总、清洗并自动分类', exact: true }).click();
    await panel.getByText('清洗结果与自动分类已保存，第六步已完成。', {exact:true}).waitFor();

    await workflowNavigation.locator('button[data-step="6"]').click();
    const traffic = page.getByRole('region', {name:'筛选流量入口',exact:true});
    await traffic.getByRole('button',{name:'生成流量筛选',exact:true}).click();
    await traffic.getByText('共 27 词 · 已明确审核 0 / 27 词 · 审核版本 0', {exact:true}).waitFor();
    const sourceMetrics = traffic.getByRole('table', {name:'四路来源计算明细',exact:true});
    await sourceMetrics.getByRole('columnheader',{name:'来源',exact:true}).waitFor();
    await sourceMetrics.getByRole('columnheader',{name:'前三转化累计',exact:true}).waitFor();
    await sourceMetrics.getByRole('columnheader',{name:'购买性',exact:true}).waitFor();
    await sourceMetrics.getByRole('columnheader',{name:'垄断性',exact:true}).waitFor();
    assert.ok(await sourceMetrics.getByText('ABA 正向',{exact:true}).count());
    assert.ok(await sourceMetrics.getByText('ABA 反向',{exact:true}).count());
    assert.ok(await sourceMetrics.getByText('卖家精灵 正向',{exact:true}).count());
    assert.ok(await sourceMetrics.getByText('卖家精灵 反向',{exact:true}).count());
    const apiBase = `http://127.0.0.1:8014/api/v1/projects/${seed.run.project_id}/pipeline-runs/${seed.run.id}`;
    const snapshot = (await (await page.request.get(apiBase+'/traffic')).json())[0];
    const url = `${apiBase}/traffic/${snapshot.id}`;
    await traffic.getByRole('button',{name:'下一页',exact:true}).click();
    await traffic.getByText('第 2 页 · 共 27 词',{exact:true}).waitFor();
    await traffic.getByRole('button',{name:'上一页',exact:true}).click();
    await traffic.getByRole('button',{name:'审核 adjustable gym',exact:true}).click();
    await traffic.getByLabel('款式相近').selectOption('yes');
    await traffic.getByLabel('同类价格浮动（%）',{exact:true}).fill('15');
    await traffic.getByLabel('前半页同类占比（%）',{exact:true}).fill('50');
    await traffic.getByLabel('筛选决定').selectOption('keep');
    await traffic.getByLabel('审核人（本地记录）',{exact:true}).fill('浏览器验收');
    await traffic.getByLabel('页面与价格证据',{exact:true}).fill('10个样本5个同类，价格浮动15%');
    await traffic.getByLabel('筛选理由',{exact:true}).fill('适合产品');
    await traffic.getByRole('button',{name:'保存当前关键词审核',exact:true}).click();
    await traffic.getByLabel('关键词审核表单').getByRole('status').filter({hasText:'已保存当前关键词审核'}).waitFor();
    await traffic.getByText(/已保存相关性：高相关/).waitFor();
    let current = await (await page.request.get(url)).json();
    assert.equal(current.version,1);
    const records = await (await page.request.get(url+'/reviews')).json();
    const review = records[0].data;
    await traffic.getByLabel('筛选理由',{exact:true}).fill('我的未保存修改');
    assert.equal((await page.request.put(url+'/review',{data:{...review,expected_version:current.version,reason:'另一个客户端'}})).status(),200);
    assert.equal(await traffic.getByLabel('筛选理由',{exact:true}).inputValue(),'我的未保存修改');
    await traffic.getByRole('button',{name:'保存当前关键词审核',exact:true}).click();
    await traffic.getByLabel('关键词审核表单').getByRole('alert').filter({hasText:'审核已更新，请刷新后再提交'}).waitFor();
    await traffic.getByRole('button',{name:'读取筛选历史（丢弃未保存修改）',exact:true}).click();
    await traffic.getByText(/已保存相关性：高相关/).waitFor();
    const all = (await (await page.request.get(url+'/rows?limit=200')).json()).items;
    current = await (await page.request.get(url)).json();
    for (const r of all) {
      if (r.cleaning_exclusion || r.review) continue;
      const res = await page.request.put(url+'/review',{data:{...review,expected_version:current.version,keyword:r.phrase.keyword}});
      assert.equal(res.status(),200); current=await res.json();
    }
    assert.equal(current.complete,true);
    await traffic.getByRole('button',{name:'读取筛选历史（丢弃未保存修改）',exact:true}).click();
    await traffic.getByText('全部候选已审核，第七步已完成。',{exact:true}).waitFor();
    const event = page.waitForEvent('download');
    await traffic.getByRole('button',{name:'下载流量筛选 CSV',exact:true}).click();
    const csv = await fs.readFile(await (await event).path(),'utf8');
    assert.match(csv, /'=1\+1/); assert.match(csv,/sop-traffic-v3/);
    await traffic.getByRole('button',{name:'查看审核历史',exact:true}).click();
    await traffic.getByText('审核历史：28 次',{exact:true}).waitFor();
    const beforeTrafficWrites = writes.filter((url) => url.includes('/traffic')).length;
    await page.reload();
    await page.getByRole('navigation', {name:'八步操作导航',exact:true}).locator('button[data-step="6"]').click();
    await traffic.getByText('全部候选已审核，第七步已完成。',{exact:true}).waitFor();
    await page.waitForLoadState('networkidle');
    assert.equal(writes.filter((url) => url.includes('/traffic')).length,beforeTrafficWrites);
    await page.addStyleTag({content:'.topbar{visibility:hidden!important}'});
    await traffic.screenshot({path:'../outputs/traffic-desktop.png'});
    await page.setViewportSize({width:390,height:844});
    await traffic.screenshot({path:'../outputs/traffic-mobile.png'});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({realApi:true,sourceMetrics:true,reviewAndRelevance:true,pagination:true,conflictKeepsDraft:true,completion:true,csv:true,restoreWithoutTrafficWrites:true,mobileNoOverflow:true,errors}));
  } catch(e) {
    if(page) {
      await page.screenshot({path:"../outputs/traffic-failure.png",fullPage:true});
      const trafficRegion = page.getByRole('region',{name:'筛选流量入口',exact:true});
      if (await trafficRegion.count()) console.error(await trafficRegion.innerText({timeout:1000}).catch(() => '筛选流量入口当前不可见'));
    }
    console.error('DIAGNOSTIC',JSON.stringify({errors,writes}));
    throw e;
  } finally {
    if (page) await page.unrouteAll({behavior:'wait'});
    await browser.close();
  }
})().catch(e => {console.error(e);process.exitCode=1;});
