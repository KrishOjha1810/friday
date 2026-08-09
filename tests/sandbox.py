"""Point Friday's config directory at a throwaway one for the duration of a
test run.

This exists because of real damage, not theory. tests/test_capabilities.py wrote
a deliberately-invalid token to ~/.friday/slack_token to prove that a fake token
is not treated as a connection, and then deleted the file in its cleanup. Both
operations hit the REAL config directory, so every run of the suite silently
destroyed a live Slack credential that had taken two rounds of setup to obtain.
It looked like a mysterious disappearance for hours.

A test may never touch anything a person would be upset to lose. Import this
first, before anything reads CONF_DIR.
"""

import atexit
import shutil
import tempfile
from pathlib import Path


def use_temp_config() -> Path:
    """Redirect every config path in the package to a temp dir. Idempotent."""
    tmp = Path(tempfile.mkdtemp(prefix="friday-test-"))
    atexit.register(shutil.rmtree, tmp, ignore_errors=True)

    from friday import connectors
    connectors.CONF_DIR = tmp

    from friday import mcp
    mcp.CONF = tmp
    mcp.SERVERS_FILE = tmp / "mcp_servers.json"

    return tmp
