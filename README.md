# Collimate

Collimate is a local-first workbench for working with language models in
parallel: fan a problem out across generators, compare the results
side-by-side, keep what holds up. Every branch, draft, and rejected
alternative persists as a versioned artifact.

- **Why it exists:** [collimate.dev/why.html](https://collimate.dev/why.html) —
  the longer essay.

## The SDK

Generators and renderers, the two things users author, are written against a
small SDK that lives in this repository as `sdk/` and ships inside the one
PyPI distribution `collimate`: the Python `collimate` and `gen` packages for
generators, the JS `api` contract for renderers, and the shared fixtures that
pin cross-language behavior. The first-party generators, renderers and
suggestions live in `content/`, the app's built-ins in `builtins/` and the
renderers' frozen module graph in `module-snapshot/`; they ship in the same
distribution as the server (`api/collimate_server/`), which reads them in
place. One root `pyproject.toml`, MIT, one version, one tag.

## Concepts

- **Channel** — a workspace. Holds stacks, persisted server-side, streamed to
  clients over SSE.
- **Stack** — an ordered column of cells. The canvas lays stacks out
  horizontally; a stack shows its front cell, with the rest behind it.
- **Cell** — the unit of work: a named bundle of files. Cell
  contents are stored in a content-addressable blob store; edits checkpoint
  into versions, and version history forms a DAG (forks, merges, restores).
- **Generator** — user-authored Python, run as a subprocess (optionally
  Docker-sandboxed). Reads input cells, emits new cells and files; output
  streams into the UI live.
- **Renderer** — user-authored display component for a file type. Trusted
  renderers mount in-document inside shadow DOM; others run in sandboxed
  iframes served from a warm pool.
- **Views** — the same scene at two altitudes: the main view (each stack's
  front cell, side by side) and zoom-out (every cell of every stack as a
  grid). Any stack can also fan out in place: its cells line up side by side
  as columns inside a border, one stack at a time, until you collapse it
  (the border's Collapse button, Esc, or zooming out).

## Repository layout

| Path | What lives here |
| --- | --- |
| `api/` | The server package `collimate_server` (`api/collimate_server/`): FastAPI backend. Spawns generators as subprocesses, streams events over SSE, serves the built UI, and owns the per-channel SQLite + content-addressable blob store. |
| `frontend/` | React 19 + Vite 6 + Zustand SPA: the pannable canvas of stacks, the cell editor, and the renderer hosts (shadow DOM + pooled iframes). |
| `desktop/` | Electron shell. Ships no UI of its own: it starts (or connects to) the Python server and loads the server-served app over loopback. |
| `build/` | Scripts that assemble the self-contained server (`build_server.sh`) and package this checkout as a self-contained desktop app (`build_desktop.sh`). |
| `landing/` | Static landing and essay pages served at collimate.dev. |
| `playground/` | Self-contained browser demo build. Everything about it lives in that folder. |
| `docs/` | Internal working notes and checklists. |
| `llm/` | Prompt-development scratch space for the built-in generators. |

Tests live in `sdk/tests/`, `api/tests/` (both run by `uv run pytest` from the root) and `frontend/src/**` (vitest; the
Electron suite runs with `npx playwright test -c playwright.electron.config.ts`).

## Requirements

- Python ≥ 3.13
- Node ≥ 24.15 (24.x) and npm
- [uv](https://docs.astral.sh/uv/) for Python dependency management
- Linux/macOS for the server. The desktop client also runs on Windows, where
  it connects to a server elsewhere (see *Desktop app*). On Linux, the OS
  keyring backend needs `libsecret` (e.g. `apt install libsecret-1-0
  gnome-keyring`) if you want to persist API keys.
- Optional: Docker, used by the `launch` generator (VS Code / terminal
  runners) and by the Agent cell's containerized harnesses: its CLI surface
  always runs its harness in a container, and its Annotation and Chat
  surfaces do for CLI harnesses such as Codex.

`./setup.sh` starts with `build/install_toolchain.sh`, which checks git, uv,
Python and Node against these versions and installs or updates what falls
short (no sudo); `--check` only reports.

## Run from source

```bash
./setup.sh                  # toolchain, SDK prep + uv sync (build/prepare_sdk_checkout.sh), npm ci in frontend/
./build/dev_server.sh       # build (SDK renderer runtime + UI → api/collimate_server/static) and run the server
```

Open <http://localhost:5001>. The server binds loopback by default (`--listen`
serves the LAN; see `api/README.md`).
`dev_server.sh` forwards `--port` and `--data-dir` to the server CLI and
takes `--skip` to relaunch against the last build (both dev scripts do);
`./build/build_server.sh` is the build step on its own. Both dev scripts
also take `--wipe` (start fresh: delete the `.collimate/` state the run
would have used).

For frontend hot-reload during development, leave the server running and in
a second terminal run `cd frontend && npm run dev`; Vite proxies `/api` to
`127.0.0.1:5001` (see `frontend/vite.config.ts`). The dev UI on :5173 works
only against a server started with `COLLIMATE_DEV=1`, which
`build/dev_server.sh` exports; any other server treats those origins as a
foreign site.

State lives in a data directory — `.collimate/` is created inside it, and
the server `chdir`s there. The server's own CLI takes
it as a flag: `uv run python -m collimate_server --port 5001 --data-dir ~/collimate`
(falling back to `$COLLIMATE_DATA`, then the current directory).

## Desktop app

`desktop/` is a thin Electron shell around the same server and the same UI.
It starts the server, waits for `/health`, and loads it over loopback — plus
the few things a browser tab cannot do: native guest views of real Chromium,
persistent per-site partitions, and direct loopback access with no proxy hop.

```bash
./build/dev_desktop.sh                          # build + launch against this checkout from source
./build/build_desktop.sh                        # package this checkout → desktop/release/ (+ AppImage in ~/CollimateApp/)
```

`build_desktop.sh` packages everything in this checkout for this machine's
platform, unsigned: it builds the server and UI, builds the one
`collimate` wheel, and bundles it into the package with a stamp of the commit (and
whether it had uncommitted changes). The package IS that checkout
snapshot: it runs its bundled wheel, not PyPI, never offers itself an
update, and shows its commit under Settings → Servers. `dev_desktop.sh`
launches the separate Collimate Dev profile against this checkout. Use
`--help` for the flags. See the
[desktop development and installation guide](docs/ai_tools/notes/2026-10-11/project_desktop_dev_install.md)
for profile isolation, wiping/resetting channels, launcher icons, AppArmor,
server selection and release rehearsals.

From source the shell spawns `uv run --project <checkout>` on the port it used
last (settings.json `localPort`), else an ephemeral one. A
`build_desktop.sh` package, a developer's or a release's, runs the frozen
server environment it carries (`bin/python3 -I -B -m collimate_server`, a
pinned CPython with the `collimate` wheel and its locked dependencies, and
the pinned uv its generator runs start with), with no uv on the machine,
network or PyPI needed. In every case it passes `--data-dir <Electron
user-data dir>`. `COLLIMATE_SERVER_SPEC=local:<checkout>` in front of a
packaged binary runs a checkout for that one launch; any other value is
refused.

**Servers.** The app knows a local server and any number of saved remote
servers (Settings → Servers, or File → Servers…: add one by URL and access
token, rename, change its token, remove, start or stop the local one). The
channel menu lists every reachable server's channels under that server's
name; picking a channel on another server reopens the window on that server.
One window shows one server at a time, and switching never stops the local
server. A remote server's token stays in the shell and is sent only to that
server. To serve another machine, run `uvx collimate --listen 0.0.0.0` there:
it turns on TLS and a bearer token and prints the URL, token and certificate
fingerprint the Add server form asks for (`api/README.md` has the details).

**Keep-alive.** Quitting stops the local server, unless "Keep local server
running when the app quits" is on (Settings → Servers) or you choose File →
Quit, Keep Server Running. The next launch picks up a kept server when it is
the same version, data dir and spec, and restarts it otherwise. Remote
servers are never stopped by the app.

On **Windows** the app is remote-only: it has no local server, because without
POSIX process groups it cannot promise to clean one up. Run the server in WSL2
(or anywhere reachable) and point the app at its URL: a launch with no saved
server shows a connect form (the default is <http://127.0.0.1:5001>), and
Settings → Servers adds more. `COLLIMATE_CONNECT_URL` presets one for
automation.

## Status and license

Pre-1.0. Everything here, app and SDK, is [MIT](LICENSE). A `uvx collimate`
distribution on PyPI is planned; until then, run from source. Copyright ©
2026 Michael Morgan. See [`THIRD_PARTY_LICENSES`](THIRD_PARTY_LICENSES) for
bundled third-party components.

## Community

Opinions, questions, and feedback: [discord.gg/bKA3kXEGx](https://discord.gg/bKA3kXEGx).
Report security issues privately to <michael@collimate.dev> rather than
filing them publicly.
