const assert=require('node:assert/strict');
const {shouldNotify}=require('../webapp/static/local-notifications.js');
assert.equal(shouldNotify({id:'1',text:'one',native_active:false},'', 'granted'),true);
assert.equal(shouldNotify({id:'1',text:'one',native_active:true},'', 'granted'),false);
assert.equal(shouldNotify({id:'1',text:'one',native_active:false},'1', 'granted'),false);
assert.equal(shouldNotify({id:'1',text:'one',native_active:false},'', 'denied'),false);
assert.equal(shouldNotify({id:null,text:null,native_active:false},'', 'granted'),false);
console.log('notification fallback decisions passed');
