"""Fail setup if native admission did not install the declared dependencies."""
from importlib.metadata import version

import aiohttp  # noqa: F401
import audioop  # noqa: F401
import inkbox  # noqa: F401
import segno  # noqa: F401
from gateway.status import read_runtime_status  # noqa: F401

# Pin the *independent* protocol/test runner to the SDK actually admitted by PM.
# This is a package version only, never application configuration or credentials.
print(f"inkbox=={version('inkbox')}")
