"""Fresh headless Playwright QA with isolated roots and loopback-only providers.

Set GTD_E2E_PLAYWRIGHT_MODULE to the existing Playwright package. No install,
credentials, company endpoint, existing browser profile or external connection.
"""
import argparse
import contextlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from webapp.server import create_server
from webapp.store import Store
from tests.test_timetracker_nx import MockNX


class ControlledNX(MockNX):
    def __init__(self,control,stats):
        super().__init__();self.control,self.stats=control,stats;self.generation=0
    def request(self,*args,**kwargs):
        config=json.loads(self.control.read_text());self.fail=config.get('fail')
        if config.get('remote_change') and self.generation==0:
            self.generation=1
            for row in self.rows.values():row['updatedAt']='synthetic-remote-change'
        try:return super().request(*args,**kwargs)
        finally:self.stats.write_text(json.dumps({'mutations':len(self.mutations()),'rows':len(self.rows)}))


def run(output):
    output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='integration-ui-',dir=output) as temp:
        fixture=Path(temp);servers=[];threads=[];cases=[]
        try:
            for width in [1440,390,320]:
                case={'width':width}
                for mode in ['default','mock']:
                    root=fixture/(str(width)+'-'+mode)
                    for name in ('inbox','tasks','projects','goals','purposes','visions','areas','reviews/daily','reviews/weekly','archive'):(root/name).mkdir(parents=True,exist_ok=True)
                    with contextlib.closing(Store(root)) as store:
                        for title,start,end,status in [('合成実績A','01:00','02:00','done'),('合成実績B','02:00','03:00','done'),('未確定実績','04:00',None,'doing')]:
                            fields={'title':title,'status':status,'work_started_at':'2026-10-09T'+start+':00Z'}
                            if status=='doing':fields['resume_status']='next'
                            if end:fields['work_ended_at']='2026-10-09T'+end+':00Z'
                            store.create_entity('task',fields,'Synthetic only')
                    with socket.socket() as reservation:reservation.bind(('127.0.0.1',0));port=reservation.getsockname()[1]
                    origin=f'http://127.0.0.1:{port}';options={}
                    if mode=='mock':
                        control=fixture/(str(width)+'-control.json');control.write_text('{}');stats=fixture/(str(width)+'-stats.json');stats.write_text('{}');mock=ControlledNX(control,stats)
                        options={'integration_options':{'timetracker_nx':{'api_base':'https://example.invalid/api','timezone':'Asia/Tokyo','granularity':5}},'integration_services':{'timetracker_nx_transport':mock}}
                        case.update(control=str(control),stats=str(stats))
                    server=create_server('127.0.0.1',port,root,bind_origin=origin,mutation_origins=(origin,),**options)
                    servers.append(server);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();threads.append(thread);case[mode]=origin
                cases.append(case)
            configuration=fixture/'config.json';configuration.write_text(json.dumps({'cases':cases,'output':str(output.resolve())}))
            subprocess.run(['node',str(ROOT/'tests/integration_ui_e2e.js'),str(configuration)],cwd=ROOT,check=True)
        finally:
            for server in servers:server.shutdown();server.server_close()
            for thread in threads:thread.join()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);run(parser.parse_args().output)
