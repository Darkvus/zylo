# Installing Zylo

Zylo compiles programs through Rust, so you need **Rust** (`rustc`) installed, and **git** if you want to
use packages. Install Rust with [rustup](https://rustup.rs):

```
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
```

## Linux and macOS

```
curl -fsSL https://raw.githubusercontent.com/Darkvus/zylo/main/install.sh | sh
```

This downloads the standalone `zylo` binary of the latest release, **verifies its SHA-256 checksum** and
installs it to `~/.local/bin` (it tells you if that folder is not on your `PATH`). Prebuilt binaries exist
for Linux x86-64 and macOS on Apple silicon; on other systems use the pip method below.

| Variable | Meaning |
|---|---|
| `ZYLO_VERSION` | install a specific release, for example `v0.5.0` (default: the latest) |
| `ZYLO_INSTALL_DIR` | where to put the binary (default: `~/.local/bin`) |

Uninstall with `sh install.sh --uninstall`. Your projects and `.zylo-env` folders are not touched.

## Windows

Download **`zylo-setup-x86_64.exe`** from the [latest release](https://github.com/Darkvus/zylo/releases/latest)
and run it. It installs per user (no administrator rights), adds Zylo to your `PATH`, and offers to open
the Rust installer if `rustc` is missing. Open a new terminal afterwards and run `zylo --version`.

For unattended installs use `zylo-setup-x86_64.exe /VERYSILENT`. Uninstall from *Settings → Apps*.

## With pip (any system with Python 3.11+)

```
pip install "zylo @ git+https://github.com/Darkvus/zylo.git@v0.4.1"
```

Replace the tag with the release you want, or use `@main` for the development version. Each release also
publishes a wheel (`zylo-<version>-py3-none-any.whl`) that you can install directly.

## From a clone (to hack on Zylo)

```
git clone https://github.com/Darkvus/zylo && cd zylo
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

## Check it works

```
zylo --version
echo 'print("hello from zylo")' > hello.zy && zylo run hello.zy
```

## While the repository is private

The commands above download from public URLs, so they only work once the repository is public. Until then, use
the GitHub CLI, which is authenticated:

```
gh release download --repo Darkvus/zylo --pattern 'zylo-linux-x86_64' --pattern SHA256SUMS
sha256sum -c --ignore-missing SHA256SUMS && chmod +x zylo-linux-x86_64 && mv zylo-linux-x86_64 ~/.local/bin/zylo
```

## How releases are made

Every pull request to `main` carries one version. When it is merged, the *Release* workflow builds the
binaries for Linux, macOS and Windows, the Windows installer, a wheel and `SHA256SUMS`, and publishes them
as a GitHub release. See [CONTRIBUTING.md](../CONTRIBUTING.md).
