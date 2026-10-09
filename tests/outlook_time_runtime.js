const assert=require('node:assert/strict');const {shiftActual}=require('../webapp/static/outlook-import.js');
const first={start:'2026-10-09T01:00:00Z',end:'2026-10-09T02:00:00Z'};
assert.deepEqual(shiftActual(first,'end',45),{start:'2026-10-09T01:00:00.000Z',end:'2026-10-09T02:45:00.000Z'});
assert.deepEqual(shiftActual(first,'start',-15),{start:'2026-10-09T00:45:00.000Z',end:'2026-10-09T02:00:00.000Z'});
assert.deepEqual(shiftActual(first,'move',1440),{start:'2026-10-10T01:00:00.000Z',end:'2026-10-10T02:00:00.000Z'});
assert.throws(()=>shiftActual(first,'start',60));assert.throws(()=>shiftActual(first,'end',-90));
console.log('Outlook actual time move/resize/cross-day/invalid interval: passed');

const {intervalLanes}=require('../webapp/static/outlook-import.js');
assert.deepEqual(intervalLanes([{start:0,end:60},{start:30,end:90},{start:90,end:120}]),[{lane:0,columns:2},{lane:1,columns:2},{lane:0,columns:1}]);
assert.deepEqual(intervalLanes([{start:30,end:45},{start:0,end:60},{start:15,end:30}]),[{lane:1,columns:2},{lane:0,columns:2},{lane:1,columns:2}]);
