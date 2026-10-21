# Installing CompassCairn

CompassCairn is one Python file with **no third-party dependencies**. There is nothing to
download, no database to run and no service to configure.

## Prerequisites

| Requirement | Detail |
|---|---|
| Python | 3.9 or newer (`python3`). The tool is tested on CPython 3.11. |
| Operating system | Any POSIX system (Linux, macOS) or Windows with CPython. |
| Network | None. CompassCairn makes no outbound calls at install or run time. |
| Package manager | `pip` (bundled with CPython) — used **only** for the optional console-script install, with `--no-index` so it never touches the network. |

Nothing else is required. In particular: no compiler, no `libsqlite3`, no Node, no Docker.

## Install steps

1. **Verify the interpreter.**

   ```bash
   python3 -V        # must print Python 3.9.x or newer
   ```

2. **Run the install script** (checks the interpreter, checks that the module imports, and
   installs the local console script offline when pip is available). It exits when done — it
   never starts anything in the foreground.

   ```bash
   bash install.sh
   ```

   Expected output:

   ```
   install: console script 'compasscairn' installed
   install: ready - try 'python3 compasscairn.py --help' or 'sh demo.sh'
   ```

   If pip is unavailable you will see the note line instead; the tool is still fully usable as
   `python3 compasscairn.py ...`.

3. **Confirm it runs.**

   ```bash
   python3 compasscairn.py --version     # compasscairn 1.0.0
   python3 compasscairn.py --help
   ```

4. **Optional: run the demo** (no prompts, no config, uses a temporary store):

   ```bash
   sh demo.sh
   ```

5. **Optional: install the tests' runner** (only needed to run the suite; `pytest` may already
   be present):

   ```bash
   python3 -m pip install "pytest==8.2.2"
   python3 -m pytest tests -q
   ```

## Start using it

`init` is the only setup step, and it creates the store in the current directory:

```bash
export COMPASSCAIRN_PASSPHRASE='choose-a-long-passphrase'   # optional; otherwise prompted
python3 compasscairn.py init
```

Then see [USAGE.md](USAGE.md) or the quickstart in [README.md](README.md).

## Where state lives

| Thing | Location |
|---|---|
| Store (waypoints, declinations, threshold, scrypt hash + salt) | `./compasscairn.json`, or `--store PATH` |
| Passphrase | Never stored — only a salted scrypt hash (`n=2**14, r=8, p=1`) plus its 16-byte salt |
| Configuration | Only `COMPASSCAIRN_PASSPHRASE` (optional) and the `--store`, `--json`, `--no-color` flags |

Back up or diff the store like any other text file. Keep it out of version control if the
waypoint names are sensitive — the file is readable JSON by design.

## Uninstalling

Delete `compasscairn.py` (and, if you installed it, run `python3 -m pip uninstall compasscairn`).
Your data lives in `compasscairn.json`; remove it yourself — nothing else is written.
