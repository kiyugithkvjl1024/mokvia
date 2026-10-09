const {chromium}=require(process.env.GTD_E2E_PLAYWRIGHT_MODULE||'playwright'),fs=require('fs'),path=require('path'),assert=require('assert/strict');
const fixture=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
(async()=>{const browser=await chromium.launch({headless:true}),reports=[];
try{for(const test of fixture.cases){
 const context=await browser.newContext({viewport:{width:test.width,height:1000},hasTouch:test.width<500}),page=await context.newPage(),errors=[];
 page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(10000);
 await context.route('**/*',r=>r.request().url().startsWith(test.origin+'/')?r.continue():r.abort());
 await page.clock.install({time:new Date('2026-10-09T22:00:00Z')});
 const group=date=>page.locator('[data-progress-date="'+date+'"]'),entry=id=>page.locator('[data-progress-id="'+id+'"]');
 await page.goto(test.origin+'/reviews/weekly?tab=daily');await group('2026-10-09').waitFor();
 assert.equal(await group('2026-10-09').getByText('昨日の合成変化',{exact:true}).count(),1);
 assert.equal(await group('2026-10-10').getByText('今日の合成変化',{exact:true}).count(),1);
 assert.equal(await page.locator('#progress-list').getByText('一昨日の合成変化',{exact:true}).count(),0);
 await entry('progress-20261010-001').getByRole('button',{name:'編集',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('#progress-occurred-on').value==='2026-10-09');
 assert.equal(await page.locator('#progress-benefit').inputValue(),'合成の学び');
 await page.locator('#progress-title').fill('日付を変更した合成変化');await page.locator('#progress-occurred-on').fill('2026-10-10');await page.locator('#preview-progress').click();
 for(let n=0;n<100;n++){if(await page.locator('#confirm-preview').isVisible())await page.locator('#confirm-preview').click();if(await group('2026-10-10').getByText('日付を変更した合成変化',{exact:true}).count())break;await page.waitForTimeout(50);}
 assert.equal(await group('2026-10-09').getByText('昨日の記録はありません。',{exact:true}).count(),1);
 assert.equal(await group('2026-10-10').getByText('日付を変更した合成変化',{exact:true}).count(),1);
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
 await page.screenshot({path:path.join(fixture.output,'daily-review-'+test.width+'.png'),fullPage:true});
 await page.goto(test.origin+'/reviews/weekly?tab=weekly');await page.locator('#progress-list').getByText('一昨日の合成変化',{exact:true}).waitFor();
 assert.equal(await page.locator('#progress-list [data-progress-date]').count(),0);
 await page.locator('#progress-list a').first().click();await page.locator('#progress-entry[open]').waitFor();
 // An empty range shows independent yesterday and today placeholders.
 await page.clock.setFixedTime(new Date('2026-10-12T15:00:00Z'));await page.goto(test.origin+'/reviews/weekly?tab=daily');
 await group('2026-10-12').getByText('昨日の記録はありません。',{exact:true}).waitFor();
 assert.equal(await group('2026-10-13').getByText('今日の記録はありません。',{exact:true}).count(),1);
 assert.deepEqual(errors,[]);
 reports.push({width:test.width,touch:test.width<500,occurredOnGrouping:true,editRefresh:true,separateEmptyDays:true,weeklyPreserved:true,pageErrors:errors});await context.close();
}fs.writeFileSync(path.join(fixture.output,'qa.json'),JSON.stringify(reports,null,2));console.log(JSON.stringify(reports));}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
