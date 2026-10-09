"""Synthetic Task operation QA using fresh headless profiles and loopback only."""
import argparse,datetime,json,pathlib,socket,subprocess,sys,tempfile,threading
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
def run(output):
    from webapp.server import create_server
    output.mkdir(parents=True,exist_ok=True,mode=0o700)
    with tempfile.TemporaryDirectory(dir=output) as temporary:
        fixture=pathlib.Path(temporary);servers=[];threads=[];cases=[]
        try:
            for width in (1280,390,320):
                root=fixture/str(width)
                for name in ('inbox','tasks','projects','goals','purposes','visions','areas','reviews/daily','reviews/weekly','archive'):(root/name).mkdir(parents=True)
                stamp=(datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(minutes=10)).isoformat(timespec='seconds')
                for number,title,status,extra in [(1,'合成TaskA','next',''),(2,'合成TaskB','next',''),(3,'計測中Task','doing',f'work_started_at: {stamp}\nresume_status: next\n'),(4,'確定実績の元Task','done','work_started_at: 2026-10-09T01:00:00+00:00\nwork_ended_at: 2026-10-09T01:20:00+00:00\n')]:
                    (root/f'tasks/task-20261009-{number:03}.md').write_text(f'---\nid: task-20261009-{number:03}\ntype: task\ntitle: "{title}"\nstatus: {status}\ncreated_at: {stamp}\nupdated_at: {stamp}\n'+extra+'---\n\n合成データ\n')
                with socket.socket() as reservation:reservation.bind(('127.0.0.1',0));port=reservation.getsockname()[1]
                origin=f'http://127.0.0.1:{port}';server=create_server('127.0.0.1',port,root,bind_origin=origin,mutation_origins=(origin,));servers.append(server)
                thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();threads.append(thread);cases.append({'width':width,'origin':origin})
            config=fixture/'browser.json';config.write_text(json.dumps({'cases':cases,'output':str(output.resolve())}))
            subprocess.run(['node',str(ROOT/'tests/task_operations_ui_e2e.js'),str(config)],cwd=ROOT,check=True)
        finally:
            for server in servers:server.shutdown();server.server_close()
            for thread in threads:thread.join()
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=pathlib.Path,required=True);run(parser.parse_args().output)
