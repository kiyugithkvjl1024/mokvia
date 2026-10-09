"""Synthetic DailyReview QA using fresh headless profiles and loopback only."""
import argparse,json,pathlib,socket,subprocess,sys,tempfile,threading
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
def run(output):
    from webapp.server import create_server
    output.mkdir(parents=True,exist_ok=True,mode=0o700)
    with tempfile.TemporaryDirectory(dir=output) as temporary:
        fixture=pathlib.Path(temporary);servers=[];threads=[];cases=[]
        try:
            for width in (1280,390,320):
                root=fixture/str(width)
                for name in ('inbox','tasks','projects','goals','purposes','visions','areas','reviews/daily','reviews/weekly','progress','archive'):(root/name).mkdir(parents=True)
                for number,title,occurred,created in [(1,'昨日の合成変化','2026-10-09','2026-10-10T03:00:00+09:00'),(2,'今日の合成変化','2026-10-10','2026-10-08T03:00:00+09:00'),(3,'一昨日の合成変化','2026-10-08','2026-10-10T03:00:00+09:00')]:
                    (root/f'progress/progress-20261010-{number:03}.md').write_text(f'---\nid: progress-20261010-{number:03}\ntype: progress\ntitle: "{title}"\noccurred_on: {occurred}\nvisibility: private\ncreated_at: {created}\nupdated_at: {created}\n---\n\n## メリット・学び\n\n合成の学び\n')
                with socket.socket() as reservation:reservation.bind(('127.0.0.1',0));port=reservation.getsockname()[1]
                origin=f'http://127.0.0.1:{port}';server=create_server('127.0.0.1',port,root,bind_origin=origin,mutation_origins=(origin,));servers.append(server)
                thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();threads.append(thread);cases.append({'width':width,'origin':origin})
            config=fixture/'browser.json';config.write_text(json.dumps({'cases':cases,'output':str(output.resolve())}))
            subprocess.run(['node',str(ROOT/'tests/daily_review_ui_e2e.js'),str(config)],cwd=ROOT,check=True)
        finally:
            for server in servers:server.shutdown();server.server_close()
            for thread in threads:thread.join()
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=pathlib.Path,required=True);run(parser.parse_args().output)
