# dev-tools: house rules for Claude Code

The Projects4Me development tooling (see README.md for the folders). This file is the start of the rules
adt-4046 fills in; the workspace `CLAUDE.md` and the Engineering Guide still apply.

## Platform: every tool works on Windows and Linux

Everything here runs on Windows 10/11 (PowerShell, cmd and Git Bash) and on Linux (Ubuntu/Debian; macOS too where
possible). A change that works on only one is not done. In practice:

- **Python tools are started with Python, not a shell.** From Python, run another dev-tools tool as
  `[sys.executable, "<tool>.py", ...]` (see `ai/lib/common.py` `p4m()`, `talos/lib/ideas/kb.py`). Never
  `["sh", "<wrapper>"]`: there is no `sh` on PATH in PowerShell or cmd. No `shell=True`, no command strings.
- **bash only where a script needs it** (the `odysseus` wrapper), found with `find_bash()`: `bash` on Linux, Git
  for Windows' bash on Windows (never `System32\bash.exe`, which is WSL). Plain `sh` is dash on Ubuntu, so a
  bash script must not be run with it.
- **Every entry point has two launchers:** `<tool>` (POSIX `sh`, no bashisms, LF line endings, committed with the
  exec bit) and `<tool>.cmd` (Windows). The `sh` launcher picks `python` on Windows (Git Bash: `python3` there may
  be the Microsoft Store stub, which hangs) and `python3` elsewhere, falling back to whichever exists; the `.cmd`
  one prefers `py -3`. Copy an existing pair (`talos/talos`, `talos/talos.cmd`).
- **Paths** with `pathlib`; no hard-coded separators, drive letters or home paths (`Path.home()`,
  `os.path.expanduser`). Open text files with `encoding="utf-8"`; reconfigure stdout/stderr to UTF-8 in CLIs
  (the Windows console default isn't).
- **Child processes**: stop the whole tree. Windows: `taskkill /T /F /PID`; POSIX: start it with
  `start_new_session=True` and `os.killpg` (see `talos/lib/ideas/stream.py`). Give them `stdin` explicitly.
- **Servers**: bind to loopback; on Windows set `allow_reuse_address = False` (there `SO_REUSEADDR` lets a second
  process bind a port in use, so an old instance keeps answering).
- **Platform-specific code** goes behind `os.name == "nt"` with both branches written, next to each other.
- **Tests** don't depend on the OS: temp dirs from `tempfile`, no shell commands, no fixed ports (bind port 0).

## Runtime copy: the tools run `main`, the checkout is for issues

Talos, `ai`, `p4m`, gaia-mcp, Nestor and sim run from `<workspace>/.dev-tools-runtime`, a detached worktree of
`origin/main` (`p4m runtime setup|update|status`; see the Talos README). The launchers hand over to it, so a branch
checked out in `dev-tools/` doesn't change the tooling under running agents; `P4M_RUNTIME=off` runs the checkout's
code instead. When writing code:

- **Per-machine files** (state, `*.local.json`, `*.env`) live in `<workspace>/dev-tools` for both copies: build
  their paths from `LOCAL_DIR` (`WORKSPACE / "dev-tools"`), never from the module's own folder. Code, configs
  and prompts that ship with the repo come from the running copy.
- A merged change takes effect after `p4m runtime update` (and a restart of `talos run`). Test a branch with its
  unit tests (`ai test dev-tools`), not by running its launchers.

## Shared test containers
- The local `gaia-dev`/`gaia-test` containers can mount another checkout (e.g. `Projects4Me/gaia`, not `Pr4M/gaia`),
  so `p4m test` / `ai test` may not test the branch. Check the mount (`docker inspect`) before trusting a result;
  never touch the other checkout. To test a Pr4M branch, run a throwaway container from image `gaia-gaia-test` with
  `--network backend`, the env file from `docker inspect gaia-test`, and `-v <Pr4M>/gaia:/var/www/html`, then remove it.
