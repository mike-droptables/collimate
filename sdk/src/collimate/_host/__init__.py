# SPDX-License-Identifier: MIT
"""Host plumbing: private to the SDK and the application host.

Not the authoring contract and not documented for authors. A generator
imports :mod:`gen` and the public :mod:`collimate` modules; the app and the
SDK's own scripts, build hook and tests import these as
``collimate._host.<module>``:

* :mod:`.bridge` — the generator process's IPC client (``HostBridge``) behind
  :mod:`gen`;
* :mod:`.ipc` and :mod:`.channel_events` — the IPC ops and the channel's SSE
  event records both sides share;
* :mod:`.renderer_bundle`, :mod:`.verification`, :mod:`.verifier_engine`,
  :mod:`.package_hash` and :mod:`.esm_lexer` — renderer compilation, handler
  verification and package identity;
* :mod:`.module_mirror` and :mod:`.module_bump` — the module snapshot and its
  pin mover (``python -m collimate._host.module_mirror``);
* :mod:`.packaging` — the asset-tree layout and its reader.

The package itself imports nothing, so ``import collimate._host.ipc`` stays
as light as the module it names.
"""
