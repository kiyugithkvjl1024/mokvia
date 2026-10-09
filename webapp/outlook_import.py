"""Compatibility facade for existing Outlook state, backup and test imports."""
from webapp.integrations.external_events import ImportError, instant, write_json, read_json, normalize_snapshot, window, stamp, os, fcntl
from webapp.integrations.external_events import ExternalEvents

class OutlookImport(ExternalEvents):
    def __init__(self, root, handoff=None, *, collector=None, timeout=25):
        super().__init__(root,handoff,collector=collector,timeout=timeout,sources=('graph','classic'),state_namespace='outlook-import')
