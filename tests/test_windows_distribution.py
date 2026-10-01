"""Distribution safety checks; Windows functional acceptance is documented separately."""
import pathlib
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

class WindowsDistributionTest(unittest.TestCase):
    def test_scripts_exist_and_do_not_bypass_policy_or_delete_data(self):
        for name in ('Common', 'Start', 'Stop', 'Notify', 'Backup', 'Restore'):
            path = ROOT / 'windows' / (name + '.ps1')
            self.assertTrue(path.exists(), name)
            source = path.read_text()
            self.assertNotRegex(source.lower(), r'executionpolicy\s+bypass|down\s+-v|volume\s+rm|git\s+push')

    def test_notification_has_mutex_and_heartbeat_and_origin(self):
        source = (ROOT / 'windows/Notify.ps1').read_text()
        for required in ('System.Threading.Mutex', 'local-notifications/heartbeat', 'X-GTD-Web', 'Origin', 'ShowBalloonTip', 'last-notification-id'):
            self.assertIn(required, source)

    def test_backup_avoids_powershell_binary_redirect_and_restore_stops_first(self):
        backup = (ROOT / 'windows/Backup.ps1').read_text()
        restore = (ROOT / 'windows/Restore.ps1').read_text()
        self.assertIn('docker cp', backup)
        self.assertNotIn('> $', backup)
        self.assertIn('Stop.ps1', restore)
        self.assertIn('Backup.ps1', restore)
        self.assertIn("@('cp', $inputFile", restore)

    def test_backup_stages_volume_archive_and_cleans_after_copy(self):
        source = (ROOT / 'windows/Backup.ps1').read_text()
        detached = "@('run', '--no-deps', '-d', '--name', $name"
        create = "@('exec', $name, 'python3', '-m', 'local_runtime', 'backup'"
        copy = "@('cp', ($name + ':' + $staged)"
        cleanup = "@('rm', '-f', $name)"
        for operation in (detached, create, copy, cleanup):
            self.assertIn(operation, source)
        self.assertLess(source.index(detached), source.index(create))
        self.assertLess(source.index(create), source.index(copy))
        self.assertLess(source.index(copy), source.index(cleanup))
        self.assertNotIn("'--rm'", source)
        self.assertIn('/data/.local-state/', source)
        self.assertNotIn('/tmp/gtd-backup.tar', source)
        self.assertIn('unlink(missing_ok=True)', source)

    def test_start_checks_ownership_before_up_and_never_kills_port_process(self):
        source = (ROOT / 'windows/Start.ps1').read_text()
        self.assertLess(source.index('Assert-PortOwnership'), source.index("'up'"))
        self.assertNotIn('Stop-Process', source)
        self.assertIn('/api/v1/health', source)
        self.assertIn("$health.distribution -eq 'gtd-local'", source)
        self.assertIn("$health.status -eq 'ok'", source)

    @unittest.skipUnless(shutil.which('pwsh'), 'PowerShell unavailable on this Linux host')
    def test_powershell_parser(self):
        for script in (ROOT / 'windows').glob('*.ps1'):
            quoted = str(script).replace("'", "''")
            subprocess.run(['pwsh', '-NoProfile', '-Command', "$e=$null; [System.Management.Automation.Language.Parser]::ParseFile('" + quoted + "',[ref]$null,[ref]$e) > $null; if($e.Count){$e; exit 1}"], check=True)
