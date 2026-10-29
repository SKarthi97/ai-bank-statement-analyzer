"""File-system protections.

Phase 2 provides only what the database layer needs: a private data directory and private
files. File validation, safe filenames and temp-file handling are added in Phase 3 and 7.

On POSIX the directory is chmod 0700 and files 0600. On Windows, ``chmod`` does not set ACLs;
the default per-user location (%LOCALAPPDATA%) is inherited as user-only, and a stricter ACL
check is part of Phase 7. This limitation is intentional and documented, not hidden.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` (and parents) and restrict it to the current user where supported."""
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        path.chmod(stat.S_IRWXU)
    return path


def restrict_file(path: Path) -> None:
    """Restrict an existing file to the current user where supported."""
    if os.name == "posix" and path.exists():
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
