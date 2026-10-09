/* Synthetic DOM interaction tests; no browser/account/network. */
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
class Element {
  constructor(tag){this.tagName=tag;this.children=[];this.dataset={};this.handlers={};this.disabled=false;this.value='';this.textContent='';this.classList={add:()=>{}};}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=[...nodes];}
  setAttribute(name,value){this[name]=value;}
  addEventListener(name,fn){this.handlers[name]=fn;}
  all(){return this.children.flatMap(c=>[c,...c.all()]);}
  querySelectorAll(selector){const all=this.all();if(selector==='button,input,select')return all.filter(c=>['button','input','select'].includes(c.tagName));if(selector==='[data-unconfirmed="true"]')return all.filter(c=>c.dataset.unconfirmed==='true');return all.filter(c=>c.tagName===selector);}
  querySelector(selector){return this.querySelectorAll(selector)[0];}
  async fire(event){this.handlers[event]?.({preventDefault(){}});await flush();}
}
async function flush(){for(let i=0;i<12;i++)await Promise.resolve();}
(async()=>{
  const document={createElement:tag=>new Element(tag)},window={};
  vm.runInNewContext(fs.readFileSync('webapp/static/timetracker-nx.js','utf8'),{document,window});
  const root=new Element('main'),calls=[];
  const rows=[{reference_id:'task:a',title:'合成A',start:'09:00',end:'10:00',completed:true,provisional:false},{reference_id:'task:b',title:'合成B',start:'10:00',end:'11:00',completed:true,provisional:false},{reference_id:'task:c',title:'未確定',start:'12:00',end:null,completed:false,provisional:true}];
  let unknown=false,block=false;
  const api={listActuals:async()=>rows,registerTask:async data=>{calls.push(['mapping',data]);},previewActuals:async ids=>{calls.push(['preview',ids]);return {token:'token',rows:ids.map(id=>({id,action:block?'blocked':'create',code:block?'unknown':undefined,payload:{workItemId:'145',startTime:'09:00',finishTime:'10:00'}}))};},applyActuals:async(ids,token)=>{calls.push(['apply',ids,token]);return ids.map(id=>({id,status:unknown?'unknown':'synced'}));},reconcile:async id=>{calls.push(['reconcile',id]);return 'synced';}};
  window.MokviaTimeTrackerNX.mount(root,api);await flush();
  const button=text=>root.all().find(e=>e.tagName==='button'&&e.textContent===text);
  const inputs=root.all().filter(e=>e.tagName==='input'&&e.type==='checkbox');
  assert.equal(inputs.length,3);assert.equal(inputs[2].disabled,true);
  const preview=button('選択した確定実績をプレビュー'),apply=button('プレビューの実績を反映');
  assert.equal(preview.disabled,true);assert.equal(apply.disabled,true);
  inputs[0].checked=true;await inputs[0].fire('change');inputs[1].checked=true;await inputs[1].fire('change');
  const form=root.querySelector('form');root.all().find(e=>e.name==='task_url').value='https://example.invalid/task';root.all().find(e=>e.name==='work_item_id').value='145';root.all().find(e=>e.name==='reference_id').value='task:a';await form.fire('submit');
  assert.equal(calls[0][1].work_item_id,'145');assert.equal(apply.disabled,true);
  await preview.fire('click');assert.equal(apply.disabled,false);assert.equal(calls.filter(c=>c[0]==='apply').length,0);
  await apply.fire('click');assert.equal(calls.filter(c=>c[0]==='apply').length,1);assert.equal(calls.find(c=>c[0]==='apply')[1].length,2);assert.equal(apply.disabled,true);assert.equal(inputs[2].disabled,true);
  unknown=true;await preview.fire('click');await apply.fire('click');const reconcile=button('送信せず照合');assert.ok(reconcile);await reconcile.fire('click');assert.equal(reconcile.disabled,true);assert.equal(calls.filter(c=>c[0]==='apply').length,2);
  block=true;await preview.fire('click');assert.equal(apply.disabled,true);
  inputs[0].checked=false;await inputs[0].fire('change');assert.equal(apply.disabled,true);
  console.log('NX DOM: selection, explicit preview/apply, pending reconciliation, disabled rows and stale preview passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
