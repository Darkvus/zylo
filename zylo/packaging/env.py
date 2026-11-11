"""Virtual environments: an isolated directory holding the packages a project uses."""
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from zylo import __version__
from zylo.domain.errors import PackageError

ENV_DIR = ".zylo-env"
ENV_FILE = "env.toml"


@dataclass
class Env:
    """A virtual environment: an isolated directory holding the packages one project uses.

    Attributes:
        root: The environment's directory (usually `.zylo-env`).
    """
    root: Path

    @property
    def packages_dir(self) -> Path:
        """Directory holding one subdirectory per installed package."""
        return self.root / "packages"

    @property
    def tmp_dir(self) -> Path:
        """Scratch directory for downloads in progress (on the same filesystem, so moves are atomic)."""
        return self.root / ".tmp"

    def exists(self) -> bool:
        """Whether `root` is a valid environment (it has `env.toml` and `packages/`)."""
        return (self.root / ENV_FILE).is_file() and self.packages_dir.is_dir()

    def installed(self) -> list[str]:
        """Names of the installed packages, sorted."""
        if not self.packages_dir.is_dir():
            return []
        return sorted(p.name for p in self.packages_dir.iterdir() if p.is_dir())

    @staticmethod
    def create(root: Path, force: bool = False) -> "Env":
        """Create a new environment.

        Args:
            root: Where to create it.
            force: Recreate it if it already exists.

        Returns:
            The new environment.

        Raises:
            PackageError: If `root` is not empty and `force` is False, or if `force` would delete something that is not an
                environment.
        """
        env = Env(root)
        if root.exists() and any(root.iterdir()):
            if not force:
                raise PackageError(f"{root} already exists; use --force to recreate it")
            if not (root / ENV_FILE).is_file():
                raise PackageError(f"{root} does not look like a Zylo environment; refusing to delete it")
            shutil.rmtree(root)
        env.packages_dir.mkdir(parents=True)
        (root / ENV_FILE).write_text(f'zylo_version = "{__version__}"\n')
        return env


def discover(start: Path, explicit: str | Path | None = None, environ: Mapping[str, str] | None = None) -> Env | None:
    """The environment to use: `--env`, else `$ZYLO_ENV`, else the nearest `.zylo-env` above `start`."""
    environ = os.environ if environ is None else environ
    chosen = explicit or environ.get("ZYLO_ENV")
    if chosen:
        env = Env(Path(chosen))
        if not env.exists():
            raise PackageError(f"{chosen} is not a Zylo environment (create one with `zylo venv`)")
        return env
    cur = start.resolve()
    for d in [cur, *cur.parents]:
        env = Env(d / ENV_DIR)
        if env.exists():
            return env
    return None
