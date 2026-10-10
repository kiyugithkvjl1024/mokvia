const {chromium}=require(process.env.GTD_E2E_PLAYWRIGHT_MODULE||'playwright'),fs=require('fs'),assert=require('assert/strict');
const fixture=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
(async()=>{
 const browser=await chromium.launch({headless:true}),reports=[];
 try{for(const test of fixture.cases){
  const context=await browser.newContext({viewport:{width:test.width,height:1000}}),page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(12000);
  await context.route('**/*',route=>route.request().url().startsWith(test.origin+'/')?route.continue():route.abort());
  await page.goto(test.origin+'/tasks');const panel=page.locator('#notification-settings');await panel.locator('summary').click();await panel.getByRole('status').filter({hasText:'保存済み'}).waitFor();
  assert.equal(await page.evaluate(()=>typeof mutationIsGated),'function');assert.equal(await panel.locator('fieldset').isDisabled(),false);
  let blockedPosts=0;const blocker=r=>{if(r.method()==='POST'&&r.url().endsWith('/api/v1/notifications/settings'))blockedPosts++;};page.on('request',blocker);
  await page.evaluate(()=>{window.notificationOldGate=mutationIsGated;mutationIsGated=()=>true;});await panel.locator('form').evaluate(form=>form.requestSubmit());await panel.getByRole('status').filter({hasText:'操作結果の確認'}).waitFor();assert.equal(blockedPosts,0);await page.evaluate(()=>{mutationIsGated=window.notificationOldGate;delete window.notificationOldGate;});page.off('request',blocker);
  await panel.locator('#notification-quiet').check();await panel.locator('#notification-timezone').fill('Asia/Tokyo');
  await panel.locator('#notification-start').fill('22:00');await panel.locator('#notification-end').fill('22:00');
  let posts=0;page.on('request',r=>{if(r.method()==='POST'&&r.url().endsWith('/api/v1/notifications/settings'))posts++;});
  await panel.getByRole('button',{name:'通知設定を保存',exact:true}).click();await panel.getByRole('status').filter({hasText:'異なる時刻'}).waitFor();assert.equal(posts,0);
  await panel.locator('#notification-end').fill('08:00');
  let release;const paused=new Promise(resolve=>{release=resolve;});await context.route('**/api/v1/notifications/settings',async route=>{if(route.request().method()==='POST'){await paused;await route.continue();}else await route.continue();});
  await panel.locator('form').evaluate(form=>{form.requestSubmit();form.requestSubmit();});await page.waitForFunction(()=>document.querySelector('#notification-settings fieldset').disabled);assert.equal(posts,1);release();
  await panel.getByRole('status').filter({hasText:'保存しました'}).waitFor();assert.equal(posts,1);await context.unroute('**/api/v1/notifications/settings');
  let settings=await page.evaluate(()=>fetch('/api/v1/notifications/settings').then(r=>r.json()));assert.equal(settings.revision,1);assert.equal(settings.quiet_enabled,true);
  await panel.locator('#notification-break_finished').uncheck();
  await context.route('**/api/v1/notifications/settings',async route=>{if(route.request().method()==='POST'){await route.fetch();await route.abort('failed');}else await route.continue();});
  await panel.getByRole('button',{name:'通知設定を保存',exact:true}).click();await panel.getByRole('status').filter({hasText:'照合しました'}).waitFor();assert.equal(posts,2);
  settings=await page.evaluate(()=>fetch('/api/v1/notifications/settings').then(r=>r.json()));assert.equal(settings.revision,2);assert.equal(settings.types.break_finished,false);
  await context.unroute('**/api/v1/notifications/settings');await page.reload();await page.locator('#notification-settings summary').click();await page.locator('#notification-settings').getByRole('status').filter({hasText:'保存済み'}).waitFor();assert.equal(await page.locator('#notification-quiet').isChecked(),true);assert.equal(await page.locator('#notification-break_finished').isChecked(),false);
  // Existing route render refreshes the task list without removing settings.
  await page.getByRole('link',{name:'Inbox',exact:true}).click();await page.getByRole('link',{name:'Focus',exact:true}).click();await page.locator('#notification-settings').waitFor();assert.equal(await page.locator('#notification-settings').count(),1);
  const bounds=await page.locator('#notification-settings').evaluate(e=>({left:e.getBoundingClientRect().left,right:e.getBoundingClientRect().right,width:document.documentElement.clientWidth}));assert(bounds.left>=0&&bounds.right<=bounds.width+1);assert.deepEqual(errors,[]);
  await page.locator('#notification-settings summary').click();await page.locator('#notification-settings form').waitFor();
  await page.screenshot({path:fixture.output+'/notifications-'+test.width+'.png',fullPage:true});reports.push({width:test.width,invalidWrite0:true,doubleClickWrite1:true,unknownReconciled:true,routePreserved:true,overflow:false});await context.close();
 }}finally{await browser.close();}console.log(JSON.stringify(reports));
})().catch(error=>{console.error(error);process.exitCode=1;});
