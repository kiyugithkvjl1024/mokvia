import json
import pathlib
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from webapp.server import create_server

class LocalHttpTest(unittest.TestCase):
    def test_host_origin_and_local_extension(self):
        import local_runtime as r
        with tempfile.TemporaryDirectory() as t:
            root=pathlib.Path(t); r.initialize(root)
            server=create_server('127.0.0.1',0,root,bind_origin=r.ORIGINS[0],mutation_origins=r.ORIGINS,strict_hosts=('localhost:24873',),extra_handler=lambda *a:(200,{'local':True}) if a[2]=='/api/v1/local-notifications' else None)
            thread=threading.Thread(target=server.serve_forever); thread.start()
            try:
                url='http://127.0.0.1:'+str(server.server_port)
                with urlopen(Request(url+'/api/v1/local-notifications',headers={'Host':'localhost:24873'})) as res:
                    self.assertEqual({'local':True},json.load(res))
                with self.assertRaises(HTTPError) as err: urlopen(Request(url+'/api/v1/snapshot',headers={'Host':'attacker.example'}))
                self.assertEqual(403,err.exception.code); err.exception.close()
                with self.assertRaises(HTTPError) as err:
                    urlopen(Request(url+'/api/v1/mutations/preview',data=b'{}',headers={'Host':'localhost:24873','Origin':'http://attacker.example','X-GTD-Web':'1','Content-Type':'application/json'}))
                self.assertEqual(403,err.exception.code); err.exception.close()
            finally: server.shutdown();thread.join();server.server_close()
