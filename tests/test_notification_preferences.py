"""Synthetic notification policy and durable settings contracts."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from webapp.notification_preferences import Preferences, PreferenceError

class PreferencesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.p=Preferences(self.root)
    def now(self,value):return dt.datetime.fromisoformat(value)
    def quiet(self):
        value=self.p.read();value.update(quiet_enabled=True,quiet_start='22:00',quiet_end='08:00',timezone='Asia/Tokyo');return self.p.update(value)
    def test_japan_timezone_without_os_zoneinfo_database(self):
        from unittest.mock import patch
        from zoneinfo import ZoneInfoNotFoundError
        with patch('webapp.notification_preferences.ZoneInfo',side_effect=ZoneInfoNotFoundError):
            self.quiet()
            self.assertFalse(self.p.allows('break_finished',self.now('2026-10-09T13:00:00Z')))
            self.assertTrue(self.p.allows('break_finished',self.now('2026-10-09T23:00:00Z')))
            value=self.p.read();value['timezone']='America/New_York'
            with self.assertRaises(PreferenceError):self.p.update(value)

    def test_default_missing_and_restart(self):
        value=self.p.read();self.assertEqual(0,value['revision']);self.assertFalse(value['quiet_enabled'])
        self.assertTrue(all(value['types'].values()));self.quiet()
        self.assertEqual(self.p.read(),Preferences(self.root).read())
    def test_cross_midnight_boundaries_and_no_mutation_on_read(self):
        self.quiet();raw=self.p.path.read_bytes()
        for hour,allowed in [('21:59',True),('22:00',False),('00:00',False),('07:59',False),('08:00',True)]:
            self.assertEqual(allowed,self.p.allows('break_finished',self.now('2026-10-09T'+hour+':00+09:00')))
        self.assertEqual(raw,self.p.path.read_bytes())
    def test_types_and_unknown_fail_closed(self):
        value=self.p.read();value['types']['focus_reminder']=False;self.p.update(value)
        self.assertFalse(self.p.allows('focus_reminder',self.now('2026-10-09T12:00:00Z')))
        self.assertTrue(self.p.allows('focus_anomaly',self.now('2026-10-09T12:00:00Z')))
        self.assertFalse(self.p.allows('new_unclassified',self.now('2026-10-09T12:00:00Z')))
    def test_stale_revision_and_strict_input(self):
        stale=self.p.read();self.quiet()
        with self.assertRaises(PreferenceError):self.p.update(stale)
        for key,value in [('timezone','/etc/passwd'),('timezone','../UTC'),('timezone','Missing/Zone'),('quiet_start','24:00'),('quiet_enabled',1),('revision',True)]:
            body=self.p.read();body[key]=value
            with self.assertRaises(PreferenceError):self.p.update(body)
        body=self.p.read();body['quiet_end']=body['quiet_start']
        with self.assertRaises(PreferenceError):self.p.update(body)
    def test_corrupt_and_symlink_fail_closed(self):
        self.quiet();self.p.path.write_text('{"unexpected":1}')
        self.assertFalse(self.p.allows('break_finished',self.now('2026-10-09T12:00:00Z')))
        self.p.path.unlink();self.p.path.symlink_to(self.root/'outside')
        with self.assertRaises(PreferenceError):self.p.read()
    def test_daytime_quiet_and_dst_instant(self):
        value=self.p.read();value.update(quiet_enabled=True,quiet_start='09:00',quiet_end='17:00',timezone='America/New_York');self.p.update(value)
        self.assertFalse(self.p.allows('focus_anomaly',self.now('2026-11-01T15:00:00Z')))
        self.assertTrue(self.p.allows('focus_anomaly',self.now('2026-11-01T23:00:00Z')))
