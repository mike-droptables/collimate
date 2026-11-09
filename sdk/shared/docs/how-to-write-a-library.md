# How to write a library

A library is code several generators share. It is a definition package like a
generator or a renderer: written in an ordinary work cell, added to the
channel's Capabilities stack with Add to Capabilities, and in use exactly
while a Capabilities cell holds it. A generator uses it by naming it in its
cell's `cell.yaml`; the host stages it beside the generator when a run starts.

## Layout

A library is the folder `<id>/` at the top of its cell, holding its
definition `<id>.library` beside its code:

```
_my_helpers/
  _my_helpers.library     # the definition
  __init__.py             # the code, imported as _my_helpers
  formatting.py
```

The id is an underscore name (`^_[a-z][a-z0-9_]*$`). It is the folder's
name, the name the host stages the folder under, and so the name the code
imports by. A library cannot sit deeper in its cell: the host would not find
it by its name.

## The definition

```yaml
id: _my_helpers
name: My helpers
version: 1.0.0            # optional
description: Formats report tables and parses the dates our generators read.
  Import it as _my_helpers (from _my_helpers import table).
```

`id`, `name` and `description` are required and nothing else is allowed.
Write the description for an agent that has never seen the code: what the
library offers, its main entry points and how to import them. The app's
built-in `library` handler checks the definition (Validate, admission) and
gives agents this guide.

## Using a library

A generator's cell names the libraries its generators import under `shared:`
in its `cell.yaml`:

```yaml
name: Reports
shared:
- _my_helpers
```

The generator imports it by name. At run time the host stages the generator's
package as `<app dir>/<generator folder>/` and each named library beside it as
`<app dir>/<id>/`, so put the app directory on `sys.path` and import:

```python
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _my_helpers import table  # noqa: E402
```

List every library the cell's generators import, including a library one of
them imports itself (a cell using `_agent_shared` also lists `_env_shared`).
The generator's package never holds a copy of a library; only the named ones
are staged.

## Resolution

Each name resolves, in this order, to:

1. the library of that id on the channel's Capabilities stack (in any of its
   cells), and
2. the library of that id the app ships (the Environment, Agent and Web
   libraries: `_env_shared`, `_agent_shared`, `_web_shared`).

A Standard channel starts with the shipped libraries on Capabilities, so its
generators and yours resolve them there. A Basic channel has none until you
add one from the catalog (Capabilities › Add from catalog), so its names
resolve to the shipped copies.

Admission refuses a cell whose `cell.yaml` names a library that resolves
nowhere, whether it arrives by Add to Capabilities, Add from catalog, an
import, a copy into a new channel, or a move of the cell onto Capabilities.
Add a library before the generator that names it. Add to Capabilities of a
generator carries its source cell's `shared:` list into the target cell's
`cell.yaml`. A run whose generator names a library that no longer resolves
is refused at start.

Two Capabilities cells cannot hold libraries with one id; rename one first.
Replacing a library (Add to Capabilities with Replace) changes it for every
generator that names it, from their next run.
