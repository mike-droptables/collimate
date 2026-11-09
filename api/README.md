# collimate

The Collimate server: a local-first workbench for working with language
models in parallel. Fan a problem out across generators, compare the results
side by side and keep what holds up. Every branch, draft and rejected
alternative persists as a versioned artifact.

This one distribution is the whole app: the server (`collimate_server`) and
its web UI, the SDK its generators and renderers are written against (the
`collimate` and `gen` packages), the first-party generators, renderers and
built-ins, and the frozen module graph those renderers import, so it runs
offline. The wheels are per platform, for glibc Linux and macOS 11+ on x64
and ARM64, because the SDK carries a native verifier binding.

## Run it

```
uvx collimate
```

`python -m collimate_server` is the same command. By default the server binds loopback only (`127.0.0.1`, port 5001) and prints
the URL to open in a browser. To keep it installed, use `uv tool install
collimate`.

- `--port N` serves on another port.
- `--data-dir PATH` keeps state in `PATH/.collimate/`. Without it the data
  directory is `$COLLIMATE_DATA`, else the current directory. It is created
  if missing.
- `--listen ADDR` serves the LAN for the desktop app on another machine:
  `0.0.0.0` for every interface, or one interface's IPv4 address. TLS and a
  bearer token are then both on. The token is `$COLLIMATE_TOKEN` if set,
  else one minted into `<data dir>/.collimate/access-token` on first use and
  kept (owner-only; the data dir defaults to the directory you run from, so
  the server also writes a `.gitignore` beside it). The certificate is
  self-signed, minted once per user into `~/.config/collimate/tls/`
  (`~/Library/Application Support/collimate/tls/` on macOS) and kept. At
  startup the server prints the URL, the token (only when stdout is a
  terminal; otherwise where it is kept) and the certificate fingerprint,
  which is what Settings → Servers → Add server asks for in the desktop
  app; the app shows the same fingerprint on first connect and remembers
  it. Deleting the `tls/` folder mints a new certificate, and every desktop
  that paired before must then remove the server and add it again.
- `$COLLIMATE_TOKEN` without `--listen` requires the token on a loopback
  server, for serving the desktop app through your own TLS reverse proxy
  (Caddy, nginx, Tailscale Serve) with a certificate browsers trust.

Requires Python 3.13 or later on Linux or macOS. Docker is optional; it is
used for sandboxed generator runs.

## More

Source, the desktop app and documentation:
<https://github.com/mike-droptables/collimate>. MIT licensed.
