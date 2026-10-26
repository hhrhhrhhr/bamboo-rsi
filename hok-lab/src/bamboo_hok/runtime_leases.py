"""User-wide port ownership survives host crashes until that run is recovered."""
from contextlib import ExitStack
from pathlib import Path

from bamboo_rsi.storage import Lease, read, write


def registry_root():
    return Path.home() / ".bamboo-rsi" / "hok-port-leases"


class RuntimeOwnership:
    def __init__(self, config, directory):
        self.directories = [registry_root() / str(port) for port in sorted(config["ai_ports"])]
        self.owner = {"run_directory": str(Path(directory).resolve()), "server": config["server"]}
        self.stack = ExitStack()

    def __enter__(self):
        try:
            for directory in self.directories:
                self.stack.enter_context(Lease(directory))
            for directory in self.directories:
                path = directory / "owner.json"
                previous = read(path) if path.exists() else None
                if previous is not None and previous != self.owner:
                    raise RuntimeError("AI port belongs to another run; recover its original experiment first")
            for directory in self.directories:
                write(directory / "owner.json", self.owner)
            return self
        except BaseException:
            self.stack.close()
            raise

    def complete(self):
        # Only called after both game input and recording have stopped.
        for directory in self.directories:
            write(directory / "owner.json", None)

    def __exit__(self, *args):
        self.stack.close()


def release_unstarted_claim(config, directory):
    """No request exists, so no worker could start. Never clear another run."""
    claim = RuntimeOwnership(config, directory)
    for directory in claim.directories:
        try:
            with Lease(directory):
                path = directory / "owner.json"
                if path.exists() and read(path) == claim.owner:
                    write(path, None)
        except RuntimeError:
            # A different live runner owns the port; this unstarted run has no
            # right to stop it or reset its ownership record.
            continue
