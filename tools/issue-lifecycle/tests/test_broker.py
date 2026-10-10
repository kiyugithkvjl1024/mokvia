import json
import tempfile
from pathlib import Path
import unittest
from test_gate import Fake, POLICY, ROOT, REPO, BASE
from gate import Gate, GateError

class BrokerTests(unittest.TestCase):
    def test_start_cli_dispatch_uses_trusted_base_receipt_and_returns_after_success(self):
        api=Fake();gate=Gate(api,POLICY);original=api.call
        def call(path,method='GET',data=None):
            if path.endswith('/dispatches'):
                self.assertEqual(data['ref'],'main')
                inputs=data['inputs']
                manifest=gate.start(json.loads(inputs['issues']),inputs['base_sha'],True,inputs['branch'],REPO,
                           actor='start-bot',workflow_run_id=42,run=inputs['run'])
                api.publish_start_receipts(manifest)
                return None
            return original(path,method,data)
        api.call=call
        with tempfile.TemporaryDirectory() as d:
            journal=Path(d)/'start.json'
            m=gate.request_start([1],BASE,True,'work',REPO,journal,timeout=1)
            self.assertEqual(len(m['issues']),1)
            self.assertEqual(json.loads(journal.read_text())['status'],'started')

    def test_unknown_dispatch_has_intent_and_same_journal_never_retries(self):
        api=Fake();gate=Gate(api,POLICY);original=api.call;writes=[]
        def call(path,method='GET',data=None):
            if path.endswith('/dispatches'):
                writes.append(path);raise GateError('unknown dispatch')
            return original(path,method,data)
        api.call=call
        with tempfile.TemporaryDirectory() as d:
            journal=Path(d)/'start.json'
            with self.assertRaises(GateError):gate.request_start([1],BASE,True,'work',REPO,journal,timeout=1)
            self.assertEqual(json.loads(journal.read_text())['status'],'dispatch_intent')
            with self.assertRaises(FileExistsError):gate.request_start([1],BASE,True,'work',REPO,journal,timeout=1)
            self.assertEqual(len(writes),1)

if __name__=='__main__':unittest.main()
