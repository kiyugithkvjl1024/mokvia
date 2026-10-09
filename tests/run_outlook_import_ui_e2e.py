"""Optional synthetic browser QA. No Windows/account access or package installation.

GTD_E2E_PLAYWRIGHT_MODULE=/existing/playwright python3 tests/run_outlook_import_ui_e2e.py --output /tmp/outlook-ui-evidence
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY))


def run(output):
    from webapp.server import create_server
    from webapp.outlook_graph import GraphSource
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='fixture-', dir=output) as temporary:
        fixture=Path(temporary);config=fixture/'acquisition.json';config.write_text('{}')
        def collect(source,first,last):
            if json.loads(config.read_text()).get('fail'):raise OSError('Synthetic acquisition failure')
            if source=='graph':
                def get(url,token):
                    assert token=='synthetic-token'
                    return {'value':[{'id':'synthetic-id','iCalUId':'synthetic-uid','type':'singleInstance','subject':'合成会議A','start':{'dateTime':'2026-10-09T01:00:00','timeZone':'UTC'},'end':{'dateTime':'2026-10-09T02:00:00','timeZone':'UTC'},'isAllDay':False,'isCancelled':False}]}
                return GraphSource('synthetic-mailbox','synthetic-calendar',lambda:'synthetic-token',get=get).fetch(first,last)
            return {'version':1,'complete':True,'source':source,'scope':'synthetic-calendar','from':first,'to':last,'events':[{'uid':'synthetic-series','occurrence':'2026-10-09T01:00:00Z','title':'合成会議A','start':'2026-10-09T01:00:00Z','end':'2026-10-09T02:00:00Z','all_day':False,'cancelled':False},{'uid':'synthetic-series','occurrence':'2026-10-09T03:00:00Z','title':'合成会議B','start':'2026-10-09T03:00:00Z','end':'2026-10-09T04:00:00Z','all_day':False,'cancelled':False}]}
        servers=[];threads=[];origins={}
        try:
            for source in ('classic','graph'):
                root=fixture/source
                for name in ('inbox','tasks','projects','goals','purposes','visions','areas','reviews/daily','reviews/weekly','archive'):(root/name).mkdir(parents=True,exist_ok=True)
                with socket.socket() as reservation:
                    reservation.bind(('127.0.0.1',0));port=reservation.getsockname()[1]
                origin=f'http://127.0.0.1:{port}'
                server=create_server('127.0.0.1',port,root,bind_origin=origin,mutation_origins=(origin,),outlook_collector=collect)
                servers.append(server);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();threads.append(thread);origins[source]=origin
            args=fixture/'browser.json';args.write_text(json.dumps({**origins,'config':str(config),'output':str(output.resolve())}))
            subprocess.run(['node',str(REPOSITORY/'tests/outlook_import_ui_e2e.js'),str(args)],cwd=REPOSITORY,check=True)
        finally:
            for server in servers:server.shutdown();server.server_close()
            for thread in threads:thread.join()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    run(parser.parse_args().output)
