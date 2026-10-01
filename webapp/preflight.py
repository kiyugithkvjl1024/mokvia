"""Compatibility constants for the local distribution HTTP transport."""
from pathlib import Path
PRODUCT_HOST = "127.0.0.1"
PRODUCT_PORT = 24873
PRODUCT_ORIGIN = "http://localhost:24873"
REPOSITORY_ROOT = Path("/data")
def activate_validator(root):
    return None

def run_preflight(root):
    raise RuntimeError("Use python -m local_runtime serve")
