'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const app = fs.readFileSync('webapp/static/app.js', 'utf8');
const moduleSource = fs.readFileSync('webapp/static/progress.js', 'utf8');
function functionSource(name) {
  const start = app.indexOf('function ' + name + '(');
  let cursor=app.indexOf('{',start), depth=1; cursor++;
  while(depth && cursor<app.length) { const char=app[cursor++]; if(char==='{' )depth++;if(char==='}')depth--; }
  return app.slice(start,cursor);
}
for (const [now, from, to, weeklyFrom, weeklyTo] of [
  ['2026-10-09T14:59:59Z','2026-10-08','2026-10-09','2026-10-05','2026-10-11'],
  ['2026-10-09T15:00:00Z','2026-10-09','2026-10-10','2026-10-05','2026-10-11'],
  ['2026-12-31T15:00:00Z','2026-12-31','2027-01-01','2026-12-28','2027-01-03'],
  ['2028-02-29T15:00:00Z','2028-02-29','2028-03-01','2028-02-28','2028-03-05'],
]) {
  const RealDate = Date;
  class FixedDate extends RealDate { constructor(...args) { super(...(args.length ? args : [now])); } }
  const context = vm.createContext({Date:FixedDate,Intl,activeReviewKind:'daily'});
  context.window = context;
  vm.runInContext(moduleSource + '\n' + ['tokyoToday','defaultReviewDate','progressRange'].map(functionSource).join('\n'), context);
  assert.equal(context.progressRange().from,from, 'Daily includes Tokyo yesterday at '+now);
  assert.equal(context.progressRange().to,to);
  context.activeReviewKind='weekly';
  assert.equal(context.progressRange().from,weeklyFrom);
  assert.equal(context.progressRange().to,weeklyTo);
}
class Element {
  constructor(tag) { this.tagName=tag;this.children=[];this.dataset={};this.textContent=''; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children=children; }
}
const context=vm.createContext({document:{createElement:tag=>new Element(tag)}}); context.window=context;
vm.runInContext(moduleSource,context);
const list=new Element('ul');let edited='';
const edit=item=>{const button=new Element('button');button.click=()=>{edited=item.id;};return button;};
const items=[{id:'late',title:'Yesterday created today',occurred_on:'2026-10-09',created_at:'2026-10-10'},
  {id:'today',title:'Today created earlier',occurred_on:'2026-10-10',created_at:'2026-10-08'},
  {id:'outside',occurred_on:'2026-10-08'}];
assert.equal(context.ProgressUI.render(list,items,'daily',{from:'2026-10-09',to:'2026-10-10'},edit),2);
assert.equal(list.children.length,2);
assert.equal(list.children[0].dataset.progressDate,'2026-10-09');
assert.equal(list.children[0].children[1].children[0].dataset.progressId,'late');
list.children[0].children[1].children[0].children[2].click();assert.equal(edited,'late');
assert.equal(list.children[1].children[1].children[0].dataset.progressId,'today');
context.ProgressUI.render(list,[],'daily',{from:'2026-10-09',to:'2026-10-10'},edit);
assert.match(list.children[0].children[1].children[0].textContent,/昨日.*ありません/);
assert.match(list.children[1].children[1].children[0].textContent,/今日.*ありません/);
context.ProgressUI.render(list,[items[0]],'daily',{from:'2026-10-09',to:'2026-10-10'},edit);
assert.match(list.children[1].children[1].children[0].textContent,/今日.*ありません/);
context.ProgressUI.render(list,[items[0]],'weekly',{from:'2026-10-05',to:'2026-10-11'},edit);
assert.equal(list.children[0].children[2].href,'/reviews/weekly?tab=daily&progress=late');
console.log('Daily date boundaries, occurred_on grouping, empty days, editing and weekly links passed');
