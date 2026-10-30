# collimate

The Collimate server: a local-first workbench for working with language
models in parallel. Fan a problem out across generators, compare the results
side by side and keep what holds up. Every branch, draft and rejected
alternative persists as a versioned artifact.

This distribution is the server and its web UI. It bundles the first-party
generators, renderers and built-ins, and the frozen module graph those
renderers import, so it runs offline. Generators and renderers are written
against [`collimate-sdk`](https://pypi.org/project/collimate-sdk/), which it
depends on at the same version.

## Run it

```
uvx collimate
```

The server binds loopback only (`127.0.0.1`, port 5001) and prints the URL to
open in a browser. To keep it installed, use `uv tool install collimate`.

- `--port N` serves on another port.
- `--data-dir PATH` keeps state in `PATH/.collimate/`. Without it the data
  directory is `$COLLIMATE_DATA`, else the current directory. It is created
  if missing.
- `--token TOKEN` (or `$COLLIMATE_TOKEN`) requires a bearer token on every
  request except `GET /health`. Use it to serve the desktop app through a TLS
  reverse proxy. The server itself still binds loopback only.

Requires Python 3.13 or later on Linux or macOS. Docker is optional; it is
used for sandboxed generator runs.

## More

Source, the desktop app and documentation:
<https://github.com/mike-droptables/collimate>. MIT licensed.
