const {chromium}=require(process.env.GTD_E2E_PLAYWRIGHT_MODULE||'playwright');
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const config=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
(async()=>{
 const browser=await chromium.launch({headless:true}),reports=[];
 try{
  for(const fixture of config.cases){
   const context=await browser.newContext({viewport:{width:fixture.width,height:1000},hasTouch:fixture.width<400});
   for(const mode of ['default','mock']){
    const page=await context.newPage(),origin=fixture[mode],errors=[];page.setDefaultTimeout(10000);page.on('pageerror',e=>errors.push(e.message));
    await page.route('**/*',route=>route.request().url().startsWith(origin+'/')?route.continue():route.abort());
    const get=()=>page.evaluate(()=>fetch('/api/v1/actuals').then(r=>r.json()));
    const call=(route,data)=>page.evaluate(async({route,data})=>{const marker=document.querySelector('meta[name="mutation-marker"]')?.content||'X-GTD-Web';const r=await fetch('/api/v1/'+route,{method:'POST',headers:{'Content-Type':'application/json',[marker]:'1'},body:JSON.stringify(data)});const v=await r.json();if(!r.ok)throw Error(v.error?.code);return v;},{route,data});
    await page.goto(origin+'/calendar');await page.getByRole('heading',{name:'カレンダー',exact:true}).waitFor();
    await page.getByLabel('表示日',{exact:true}).fill('2026-10-09');await page.getByLabel('表示日',{exact:true}).dispatchEvent('change');await page.getByLabel('表示期間',{exact:true}).selectOption('day');
    await page.getByText('連携設定',{exact:true}).click();const settings=page.locator('.integration-settings');await settings.getByLabel('TimeTracker NXを有効にする',{exact:true}).waitFor();
    for(const name of ['Outlook','Google Calendar','TimeTracker NX'])assert.equal(await settings.getByLabel(name+'を有効にする',{exact:true}).isChecked(),true);
    const before=await get();
    if(mode==='default'){
     assert.equal(await settings.locator('.integration-setting-row').filter({hasText:'未設定'}).count(),3);
     const statuses=await page.evaluate(()=>fetch('/api/v1/integrations/status').then(r=>r.json()));assert.ok(statuses.plugins.every(p=>!p.configured));assert.ok(statuses.plugins.every(p=>['unconnected','unavailable'].includes(p.connection_state)));
     await settings.getByLabel('TimeTracker NXを有効にする',{exact:true}).uncheck();await settings.getByRole('status').filter({hasText:'連携設定を保存しました'}).waitFor();assert.deepEqual((await get()).actuals,before.actuals);
     await settings.getByLabel('TimeTracker NXを有効にする',{exact:true}).check();await page.waitForFunction(()=>document.querySelector('.integration-setting-row input[aria-label="TimeTracker NXを有効にする"]').checked&&!document.querySelector('.integration-setting-row input').disabled);
     await settings.scrollIntoViewIfNeeded();await page.screenshot({path:path.join(config.output,'default-'+fixture.width+'.png')});
    }else{
     await page.getByText('TimeTracker NXへ確定実績を反映',{exact:true}).click();const panel=page.locator('.nx-panel');await panel.getByRole('button',{name:'実績を再読み込み',exact:true}).waitFor();
     const records=before.actuals.filter(r=>r.completed), refs=records.map(r=>r.reference_id);assert.equal(records.length,2);
     assert.equal(await panel.locator('.nx-record').filter({hasText:'未確定実績'}).getByRole('checkbox').isDisabled(),true);
     async function select(){for(const title of ['合成実績A','合成実績B'])await panel.locator('.nx-record').filter({hasText:title}).getByRole('checkbox').check();}
     async function reload(){await panel.getByRole('button',{name:'実績を再読み込み',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.nx-panel').getAttribute('aria-busy')==='false');await select();}
     async function preview(){await panel.getByRole('button',{name:'選択した確定実績をプレビュー',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.nx-panel').getAttribute('aria-busy')==='false');}
     async function apply(){await panel.getByRole('button',{name:'プレビューの実績を反映',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.nx-panel').getAttribute('aria-busy')==='false');}
     for(const record of records){await panel.getByLabel('実績と紐づくタスク',{exact:true}).selectOption(record.reference_id);await panel.getByLabel('設定済み社内ホストのタスクURL',{exact:true}).fill('https://example.invalid/task');await panel.getByLabel('確認済みワークアイテムID（URLから推測しません）',{exact:true}).fill('145');await panel.getByRole('button',{name:'タスクURLとIDを登録',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.nx-panel').getAttribute('aria-busy')==='false');}
     await select();await preview();assert.equal(JSON.parse(fs.readFileSync(fixture.stats)).mutations,0);assert.equal(await panel.getByRole('button',{name:'プレビューの実績を反映',exact:true}).isEnabled(),true);
     await panel.scrollIntoViewIfNeeded();await page.screenshot({path:path.join(config.output,'preview-'+fixture.width+'.png')});await apply();assert.equal(JSON.parse(fs.readFileSync(fixture.stats)).mutations,2);
     await preview();assert.equal(await panel.getByRole('button',{name:'プレビューの実績を反映',exact:true}).isDisabled(),true);assert.equal(JSON.parse(fs.readFileSync(fixture.stats)).mutations,2);
     async function extend(end){const record=(await get()).actuals.find(r=>r.title==='合成実績B');const task=await page.evaluate(id=>fetch('/api/v1/entities/tasks/'+id).then(r=>r.json()),record.source_id);const mutation=await call('mutations/preview',{action:'update',kind:'tasks',id:record.source_id,base_hash:task.content_hash,fields:{work_ended_at:'2026-10-09T'+end+':00Z'}});const {preview_hash,...body}=mutation;await call('mutations/apply',{preview:body,preview_hash});}
     await extend('03:30');await reload();await preview();await apply();assert.equal(JSON.parse(fs.readFileSync(fixture.stats)).mutations,3);
     await extend('04:00');fs.writeFileSync(fixture.control,JSON.stringify({fail:'after'}));await reload();await preview();await apply();assert.equal(JSON.parse(fs.readFileSync(fixture.stats)).mutations,4);assert.ok((await get()).reflection.timetracker_nx.some(r=>r.state==='unknown'));
     await panel.getByRole('button',{name:'送信せず照合',exact:true}).click();await page.waitForFunction(()=>document.querySelector('.nx-panel').getAttribute('aria-busy')==='false');assert.ok((await get()).reflection.timetracker_nx.every(r=>r.state!=='unknown'));assert.equal(JSON.parse(fs.readFileSync(fixture.stats)).mutations,4);
     fs.writeFileSync(fixture.control,JSON.stringify({remote_change:true}));await preview();assert.equal(await panel.getByRole('button',{name:'プレビューの実績を反映',exact:true}).isDisabled(),true);assert.ok(await panel.getByText('NX側で変更されています',{exact:true}).count());
     const saved=await get();await settings.getByLabel('TimeTracker NXを有効にする',{exact:true}).uncheck();await page.waitForFunction(()=>document.querySelector('.nx-panel').hidden);assert.deepEqual((await get()).actuals,saved.actuals);assert.ok((await get()).reflection.timetracker_nx.every(r=>r.availability==='disabled'));assert.equal(JSON.parse(fs.readFileSync(fixture.stats)).mutations,4);
     await settings.scrollIntoViewIfNeeded();await page.screenshot({path:path.join(config.output,'disabled-'+fixture.width+'.png')});
    }
    assert.deepEqual(errors,[]);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    reports.push({width:fixture.width,mode,allDefaultOn:true,syntheticOnly:true,fullFlow:mode==='mock',pageErrors:errors,noHorizontalOverflow:true});await page.close();
   }
   await context.close();
  }
 }finally{await browser.close();fs.writeFileSync(path.join(config.output,'browser-result.json'),JSON.stringify(reports,null,2));}
 console.log(JSON.stringify(reports));
})().catch(e=>{console.error(e);process.exitCode=1;});
