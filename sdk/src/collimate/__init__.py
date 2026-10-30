# SPDX-License-Identifier: MIT
"""Collimate SDK — build generators and renderers for Collimate.

A generator's runtime API is the top-level :mod:`gen` package (a plain
re-export of :mod:`collimate.native`): ``import gen``, no class, no ``ctx``.

The public modules under :mod:`collimate`:

* :mod:`collimate.permissions` — the grant axes, their ladders, the typed
  ``Grant`` ``gen.grant()`` returns, and ``effective_commands``.
* :mod:`collimate.fs` — symlink-safe file access under a directory a
  container can write.
* :mod:`collimate.git_hosts` — the pure Git account checks: the published
  host keys of github.com and gitlab.com, known_hosts and OpenSSH
  private-key parsing, and the account address rules.
* :mod:`collimate.pointers` — the pointer-file shapes (``.terminal``,
  ``.website``, ``.chat``) generators write and renderers read.
* :mod:`collimate.transcript` — the open chat-transcript format.
* :mod:`collimate.config_values` — ``.set_yaml`` field semantics (empty
  values, conditions, validation), shared by generators, the linter and the
  host.
* :mod:`collimate.providers` — the model providers: one row each (API
  family, standard endpoint, key environment name, default model, plan).
* :mod:`collimate.account_taxonomy`, :mod:`collimate.harness_placement` and
  :mod:`collimate.host_harness` — harness metadata shared by the host's
  account editor and the agent generator: which harnesses a model account
  serves, where a harness runs under a grant (and the least grant it needs),
  and whether a harness CLI is on this machine. Their per-harness facts are
  :mod:`collimate.harness_catalog`, generated from the Agent's harness
  adapters.
* :mod:`collimate.handlers` — handler manifests, lookup and accepted skill validation.
* :mod:`collimate.surfaces` — served-file paths, the run's inbox
  declaration, and ``resolve_opens``: the files a run opens, as the host and
  the run both read a manifest's ``opens``.
* :mod:`collimate.options_stack` — options parsing and pure Split path mapping.
* :mod:`collimate.testing` — the host-free run engine for generator tests.
* :mod:`collimate.shared` — this SDK's own files at runtime: the
  ``shared/`` docs tree, the toolchain contract, and ``sdk_distribution``
  (this SDK source's canonical runtime metadata, for generator build and
  test tooling).
* :mod:`collimate.lint` — the manifest linter (``collimate-lint``).

Every other module is internal: ``collimate.native`` (the implementation
behind :mod:`gen`; import ``gen``), the modules with a leading underscore
(``_results``, the types :mod:`gen` re-exports), and the host plumbing
under :mod:`collimate._host` (the generator's IPC bridge, the IPC and
channel-event contracts, renderer compilation and verification, the module
snapshot, the asset-tree reader), which the app and the SDK's own tooling
import and no generator may.
"""
from importlib import import_module

__all__ = [
    "account_taxonomy",
    "config_values",
    "fs",
    "git_hosts",
    "handlers",
    "harness_catalog",
    "harness_placement",
    "host_harness",
    "lint",
    "options_stack",
    "permissions",
    "pointers",
    "providers",
    "shared",
    "surfaces",
    "testing",
    "transcript",
]


def __getattr__(name: str):
    # Public submodules load on first use, so ``import collimate`` (which
    # every generator process does through ``gen``) stays light.
    if name in __all__:
        return import_module(f"collimate.{name}")
    raise AttributeError(f"module 'collimate' has no attribute {name!r}")
