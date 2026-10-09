"""Loopback-only synthetic NX panel for in-app browser QA. Never contacts NX.

python3 tests/run_timetracker_nx_demo.py --port 25187
The temporary journal is deleted on exit. No company host/account is needed.
"""
import argparse
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.test_timetracker_nx import MockNX
from webapp.integration_plugins.timetracker_nx import TimeTrackerNXPlugin
from webapp.integrations.contracts import ActualRecord
from webapp.timetracker_nx import Journal, NXAdapter, NXError, Settings, ResultUnknown

HTML = '''<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>NX 合成QA</title><link rel="stylesheet" href="/assets/timetracker-nx.css"><main id="nx"></main><aside class="nx-panel"><h2>合成QA操作</h2><button id="change">合成実績Bを30分延長</button><button id="timeout">次の送信を結果不明にする</button><button id="remote">合成NX側を変更</button><p id="fixture-status" role="status"></p></aside><script src="/assets/timetracker-nx.js"></script><script>
const request=async(path,data)=>{const r=await fetch('/mock/'+path,{method:data===undefined?'GET':'POST',headers:{'Content-Type':'application/json'},body:data===undefined?undefined:JSON.stringify(data)});const v=await r.json();if(!r.ok){const e=new Error('synthetic');e.code=v.code;throw e;}return v;};
window.panel=MokviaTimeTrackerNX.mount(document.querySelector('#nx'),{listActuals:()=>request('actuals'),registerTask:data=>request('task',data),previewActuals:ids=>request('preview',{ids}),applyActuals:(ids,token)=>request('apply',{ids,token}),reconcile:id=>request('reconcile',{id})});
for(const action of ['change','timeout','remote'])document.getElementById(action).onclick=async()=>{await request('fixture',{action});document.getElementById('fixture-status').textContent='合成状態を変更しました。';};
</script></html>'''


def run(port):
    with tempfile.TemporaryDirectory(prefix='nx-synthetic-') as directory:
        mock = MockNX()
        adapter = NXAdapter(Settings('https://example.invalid/api','Asia/Tokyo',5),Journal(Path(directory)/'nx.json'),mock)
        plugin = TimeTrackerNXPlugin(adapter)
        def make(uid,title,start,end,completed=True):
            return ActualRecord.create(reference_id='task:'+uid, source_kind='task',source_id=uid,source_provider='core',
                title=title,start='2026-10-09T'+start+':00+09:00',end='2026-10-09T'+end+':00+09:00',
                origin='recorded',completed=completed,provisional=False)
        records = [make('qa-a','合成実績A','09:00','10:00'),make('qa-b','合成実績B','10:00','11:00'),make('qa-open','未確定実績','13:00','14:00',False)]
        origin=f'http://127.0.0.1:{port}'
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def reply(self,value,status=200,kind='application/json'):
                body=(json.dumps(value,ensure_ascii=False) if kind=='application/json' else value).encode()
                self.send_response(status);self.send_header('Content-Type',kind+'; charset=utf-8');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            def do_GET(self):
                if self.headers.get('Host')!=f'127.0.0.1:{port}':return self.reply({'code':'invalid_data'},403)
                if self.path=='/':return self.reply(HTML,kind='text/html')
                if self.path=='/mock/actuals':return self.reply([asdict(row) for row in records])
                if self.path in ['/assets/timetracker-nx.js','/assets/timetracker-nx.css']:
                    return self.reply((ROOT/'webapp/static'/self.path.split('/')[-1]).read_text(),kind='text/javascript' if self.path.endswith('.js') else 'text/css')
                return self.reply({},404)
            def do_POST(self):
                if self.headers.get('Origin')!=origin or self.headers.get('Host')!=f'127.0.0.1:{port}':return self.reply({'code':'invalid_data'},403)
                try:
                    size=int(self.headers.get('Content-Length','0'))
                    if size<1 or size>8192:raise NXError('invalid_data')
                    data=json.loads(self.rfile.read(size));path=self.path
                    if path=='/mock/task':
                        plugin.register_task(data['reference_id'],data['task_url'],data['work_item_id'],data.get('categories'));result={'ok':True}
                    elif path in ['/mock/preview','/mock/apply']:
                        ids=data['ids']
                        if not isinstance(ids,list) or len(set(ids))!=len(ids):raise NXError('invalid_data')
                        chosen=[next(row for row in records if row.reference_id==rid) for rid in ids]
                        result=plugin.preview_actuals(chosen) if path.endswith('preview') else plugin.apply_actuals(chosen,data['token'])
                    elif path=='/mock/reconcile':result=plugin.reconcile(data['id'])
                    elif path=='/mock/fixture':
                        if data['action']=='change':records[1]=make('qa-b','合成実績B','10:00','11:30')
                        elif data['action']=='timeout':mock.fail='after'
                        elif data['action']=='remote':
                            for row in mock.rows.values():row['updatedAt']='synthetic-remote-change'
                        else:raise NXError('invalid_data')
                        result={'ok':True}
                    else:return self.reply({},404)
                    self.reply(result)
                except NXError as error:self.reply({'code':error.code},400)
                except (KeyError, ValueError, TypeError, StopIteration):self.reply({'code':'invalid_data'},400)
                except ResultUnknown:self.reply({'code':'unknown'},400)
        server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
        print(origin,flush=True)
        try:server.serve_forever()
        finally:server.server_close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--port',type=int,default=25187)
    run(parser.parse_args().port)
