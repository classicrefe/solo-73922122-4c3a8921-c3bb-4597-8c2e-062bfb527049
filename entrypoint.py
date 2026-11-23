"""Container entrypoint: prepare the data directory, then drop privileges.

A named Docker volume mounted at ``/data`` is created by the daemon with the
ownership of the image's ``/data`` directory, but bind mounts and pre-existing
volumes may not be writable by the ``app`` user.  Fixing the ownership here
while still root (when the container is started as root) makes both cases
work; when the container is already started as a non-root user the script
simply execs the command.
"""

from __future__ import annotations

import os
import pwd
import sys
from pathlib import Path

APP_USER = "app"
DB_PATH = os.environ.get("CITATION_DB_PATH", "/data/citations.db")


def main() -> None:
    if os.geteuid() == 0:
        data_dir = Path(DB_PATH).parent
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            try:
                user = pwd.getpwnam(APP_USER)
            except KeyError:
                sys.exit(f"user {APP_USER!r} does not exist")
            os.chown(data_dir, user.pw_uid, user.pw_gid)
            for entry in data_dir.iterdir():
                try:
                    os.chown(entry, user.pw_uid, user.pw_gid, follow_symlinks=False)
                except OSError:
                    pass
            os.setgroups([])
            os.setgid(user.pw_gid)
            os.setuid(user.pw_uid)
        except OSError as exc:  # pragma: no cover - depends on container fs
            sys.exit(f"could not prepare {data_dir}: {exc}")

    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
