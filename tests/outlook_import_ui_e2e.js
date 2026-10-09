const {chromium}=require(process.env.GTD_E2E_PLAYWRIGHT_MODULE || 'playwright');
const fs=require('fs'),assert=require('assert/strict'),path=require('path');
const fixture=JSON.parse(fs.readFileSync(process.argv[2],'utf8')),classic=fixture.classic,graph=fixture.graph;
const output=name=>path.join(fixture.output,name);
(async()=>{
 const browser=await chromium.launch({headless:true}),reports=[];
 try{
 for(const width of [1280,390]){
  const context=await browser.newContext({viewport:{width,height:1000},hasTouch:width===390}),page=await context.newPage(),errors=[];
  page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(12000);
  await context.route('**/*',r=>r.request().url().startsWith(classic+'/')?r.continue():r.abort());
  const get=()=>page.evaluate(()=>fetch('/api/v1/outlook-import/status').then(r=>r.json()));
  const ready=async()=>{await page.locator('.outlook-import-controls button').waitFor();await page.waitForFunction(()=>!document.querySelector('.outlook-import-controls button').disabled);await page.locator('#local-calendar-screen > p[role="status"]').last().filter({hasText:'予定は青'}).waitFor();};
  const wait=async predicate=>{for(let i=0;i<100;i++){const state=await get();if(predicate(state)){await ready();return state;}await page.waitForTimeout(30);}throw Error('Expected state did not arrive');};
  const card=title=>page.locator('.outlook-event').filter({hasText:title});
  const menu=async title=>{const c=card(title),toggle=c.getByRole('button',{name:'会議の操作',exact:true});if(await toggle.getAttribute('aria-expanded')!=='true')await toggle.click();return c;};
  const actual=state=>state.events.find(e=>e.title==='合成会議A').local.actual;
  const undo=async before=>{await page.getByRole('button',{name:'元に戻す',exact:true}).click();await wait(s=>JSON.stringify(actual(s))===JSON.stringify(before));};
  await page.goto(classic+'/calendar');await ready();await page.getByLabel('表示日',{exact:true}).fill('2026-10-09');await page.getByLabel('表示日',{exact:true}).dispatchEvent('change');await ready();await page.getByLabel('取り込み開始日',{exact:true}).fill('2026-10-01');await page.getByLabel('取り込み終了日（この日は含まない）',{exact:true}).fill('2026-11-01');await page.getByRole('button',{name:'Outlookから取り込む',exact:true}).click();await ready();await card('合成会議A').waitFor();assert.equal((await get()).events.length,2);
  for(const title of ['合成会議A','合成会議B']){
   if((await get()).events.find(e=>e.title===title).local.done){await (await menu(title)).getByRole('button',{name:'完了取消',exact:true}).click();await wait(s=>!s.events.find(e=>e.title===title).local.done);}
   const grip=card(title).getByRole('button',{name:'会議をドラッグして完了、または実績を移動',exact:true});await grip.scrollIntoViewIfNeeded();const b=await grip.boundingBox(),x=b.x+b.width/2,y=b.y+b.height/2;
   await page.mouse.move(x,y);await page.mouse.down();await page.mouse.move(x+12,y+12,{steps:3});const dock=page.locator('[data-drag-destination="complete"]');await dock.waitFor();const d=await dock.boundingBox();await page.mouse.move(d.x+d.width/2,d.y+d.height/2,{steps:5});await page.mouse.up();await wait(s=>s.events.find(e=>e.title===title).local.done);
  }
  const sourceBefore=(await get()).events.map(e=>({key:e.key,start:e.start,end:e.end,occurrence:e.occurrence}));
  const drag=async(name,delta,touch=false)=>{
   const handle=card('合成会議A').getByRole('button',{name,exact:true});await handle.scrollIntoViewIfNeeded();await handle.evaluate(e=>{const sc=e.closest('.outlook-time-scroll');sc.scrollIntoView({block:'center',inline:'nearest'});sc.scrollTop=480;});const b=await handle.boundingBox(),x=b.x+b.width/2,y=b.y+b.height/2;
   if(touch){const cdp=await context.newCDPSession(page);await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x,y}]});for(let i=1;i<=5;i++)await cdp.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x,y:y+delta*i/5}]});await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});await cdp.detach();}
   else {await page.mouse.move(x,y);await page.mouse.down();await page.mouse.move(x,y+delta,{steps:10});await page.mouse.up();}
  };
  let before=actual(await get());await drag('実績終了を調整',45);let state=await wait(s=>Date.parse(actual(s).end)!==Date.parse(before.end));assert.equal(Date.parse(actual(state).end)-Date.parse(before.end),45*60000);assert.equal(actual(state).origin,'manual');await undo(before);
  before=actual(await get());await drag('実績開始を調整',-15,width===390);state=await wait(s=>Date.parse(actual(s).start)!==Date.parse(before.start));assert.equal(Date.parse(actual(state).start)-Date.parse(before.start),-15*60000);assert.equal(actual(state).end,before.end);await undo(before);
  before=actual(await get());await drag('会議をドラッグして完了、または実績を移動',30);state=await wait(s=>Date.parse(actual(s).start)!==Date.parse(before.start));assert.equal(Date.parse(actual(state).start)-Date.parse(before.start),30*60000);assert.equal(Date.parse(actual(state).end)-Date.parse(before.end),30*60000);
  const corrected=actual(state),cancelRevision=state.revision;await (await menu('合成会議A')).getByRole('button',{name:'実績時刻',exact:true}).click();await page.keyboard.press('Escape');assert.equal((await get()).revision,cancelRevision);
  fs.writeFileSync(fixture.config,'{"fail":true}');const stable=(await get()).events;await page.getByRole('button',{name:'Outlookから取り込む',exact:true}).click();await page.getByRole('status').filter({hasText:'既存データを保持'}).waitFor();assert.deepEqual((await get()).events,stable);
  fs.writeFileSync(fixture.config,'{}');await page.getByRole('button',{name:'Outlookから取り込む',exact:true}).click();await ready();state=await get();assert.equal(state.events.length,2);assert.deepEqual(actual(state),corrected);assert.deepEqual(state.events.map(e=>({key:e.key,start:e.start,end:e.end,occurrence:e.occurrence})),sourceBefore);
  await page.locator('.calendar-day').filter({hasText:'2026-10-09 (金)'}).scrollIntoViewIfNeeded();await card('合成会議A').evaluate(e=>e.closest('.outlook-time-scroll').scrollIntoView({block:'center',inline:'nearest'}));await page.screenshot({path:output('calendar-'+width+'.png')});assert.deepEqual(errors,[]);
  reports.push({width,consecutiveDragDone:2,endResize45min:true,startResizeMinus15min:true,touchPointer:width===390,wholeMove30min:true,undo:true,keyboardEscapeNoWrite:true,failedAcquisitionPreserves:true,reimportPreservesManualActual:true,sourceUntouched:true,pageErrors:errors});await context.close();
 }
 const context=await browser.newContext(),page=await context.newPage();await context.route('**/*',r=>r.request().url().startsWith(graph+'/')?r.continue():r.abort());await page.goto(graph+'/calendar');await page.getByLabel('表示日',{exact:true}).fill('2026-10-09');await page.getByLabel('表示日',{exact:true}).dispatchEvent('change');await page.getByLabel('取り込み開始日',{exact:true}).fill('2026-10-01');await page.getByLabel('取り込み終了日（この日は含まない）',{exact:true}).fill('2026-11-01');const source=page.locator('select[aria-label="取得方式"]');if(await source.isEnabled())await source.selectOption('graph');assert.equal(await source.inputValue(),'graph');await page.getByRole('button',{name:'Outlookから取り込む',exact:true}).click();await page.waitForFunction(()=>!document.querySelector('.outlook-import-controls button').disabled);await page.locator('.outlook-event').waitFor();const state=await page.evaluate(()=>fetch('/api/v1/outlook-import/status').then(r=>r.json()));assert.equal(state.events.length,1);assert.equal(state.binding.source,'graph');reports.push({source:'graph',mockedAcquisition:true,commonUI:true,repeatNoDuplicate:true});await context.close();fs.writeFileSync(output('browser-result.json'),JSON.stringify(reports,null,2));console.log(JSON.stringify(reports));
 }finally{fs.writeFileSync(fixture.config,'{}');await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
