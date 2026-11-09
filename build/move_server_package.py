#!/usr/bin/env python3
"""Move the server's top-level modules under one package, ``collimate_server``.

    python3 build/move_server_package.py            # move and rewrite, then report
    python3 build/move_server_package.py --dry-run  # report what would change; touch nothing

The server used to install as top-level modules (``main``, ``routes``,
``genesis``, ...) found through ``api/`` on ``sys.path``. This script makes
them the package ``api/collimate_server/`` and rewrites every reference, so
the move is one mechanical, replayable step. It is idempotent: run on a tree
that is already moved, it moves only top-level modules that appeared in
``api/`` since (another branch's), rewrites only what still names the old
modules, and otherwise changes nothing. Run it from a clean checkout and
commit its result on its own; review ``git diff`` and the report it prints.

1. **Move.** Every tracked top-level module of ``api/`` (``*.py`` but
   ``hatch_build.py``), every tracked top-level package (a folder holding an
   ``__init__.py``, but ``tests_live``) and ``builtins.json`` go to
   ``api/collimate_server/`` with ``git mv``. ``api/tests/``,
   ``api/tests_live/``, the build hook and the project files stay. The
   package gets an ``__init__.py`` and a ``__main__.py``
   (``python -m collimate_server`` runs the server CLI).
2. **Imports**, in every tracked Python file but the SDK's and the
   content's (``sdk/``, ``content/``, ``builtins/``, which never import the
   server): ``import X`` becomes ``import collimate_server.X as X``
   (the same binding, and a module dropped from ``sys.modules`` is imported
   afresh, as before), ``import X as Y`` and ``import X.Y as Z`` keep their
   names, ``import X.Y`` also binds ``X`` from the package, and
   ``from X[.Y] import ...`` becomes ``from collimate_server.X[.Y] import ...``.
   Relative imports inside the package become absolute, so one spelling
   holds everywhere.
3. **Strings that name a module**, in those files, where the context says
   it is a module name and not data: ``importlib.import_module``,
   ``__import__`` (rewritten to return the leaf module), ``sys.modules``
   lookups, ``setitem``/``delitem`` on ``sys.modules``, ``ModuleType``,
   string patch targets (``patch``, ``monkeypatch.setattr``/``delattr``)
   and ASGI app strings (``"collimate_server.main:app"``).
4. **Paths** in every tracked text file outside the dated plan records:
   ``api/<module>.py``, ``api/collimate_server/routes`` (and ``process_manager``,
   ``sessions``), ``api/collimate_server/builtins.json`` and ``api/collimate_server/static`` gain the
   ``collimate_server/`` segment.
5. **Literal edits** the new depth needs (the checkout root is two levels
   above the package now; ``main.py`` no longer puts its own folder on
   ``sys.path``; the tests' module reset evicts package attributes too).
   Each applies when its old text is present and is reported otherwise.

It prints, last, every remaining line that still looks like an old module
reference so a reviewer can settle it by hand.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "api"
PACKAGE = "collimate_server"
TARGET = API / PACKAGE
STAYS = {"hatch_build.py"}
STAY_DIRS = {"tests", "tests_live", PACKAGE}
EXTRA_FILES = ("builtins.json",)
# Python that never imports the server (the import-boundary tests hold
# that): its local modules (state, models, events, ...) are left alone.
NO_SERVER_IMPORTS = ("sdk/", "content/", "builtins/")
# Dated records describe the tree as it was; the move leaves their paths:
# docs/plan<date>/, docs/plans/, docs/deferred/, and a folder's own nested
# docs/ (every docs/ below the top level but the SDK's guides).
HISTORICAL = ("docs/plan", "docs/deferred/")
TEXT_SUFFIXES = {".py", ".md", ".ts", ".tsx", ".mts", ".mjs", ".js", ".json", ".yml", ".yaml", ".toml", ".sh",
                 ".txt", ".cfg", ".ini", ".html", ".css", ".csv", ""}
SKIP_NAMES = {"uv.lock", "package-lock.json"}

INIT = '''"""The Collimate server: the FastAPI app (``collimate_server.main``), its
routes, the run manager and the stores they share. ``python -m
collimate_server`` and the ``collimate`` console script start it."""
'''
MAIN = '''"""``python -m collimate_server``: the server CLI (``collimate``)."""
from collimate_server.main import cli_main

cli_main()
'''

# (file under the root, old text, new text): what the depth change needs.
LITERAL_EDITS: tuple[tuple[str, str, str], ...] = (
    (f"api/{PACKAGE}/main.py",
     "# Ensure the api/ directory is on the Python path for relative imports\n"
     "current_dir = os.path.dirname(os.path.abspath(__file__))\n"
     "if current_dir not in sys.path:\n"
     "    sys.path.insert(0, current_dir)\n",
     "current_dir = os.path.dirname(os.path.abspath(__file__))\n"),
    (f"api/{PACKAGE}/genesis.py",
     "    return os.path.join(os.path.dirname(_API_DIR), tree)\n",
     "    return os.path.join(os.path.dirname(os.path.dirname(_API_DIR)), tree)\n"),
    (f"api/{PACKAGE}/genesis.py",
     "    data when it is importable, else this checkout's workspace root beside\n    api/.\n",
     "    data when it is importable, else this checkout's root, two levels above\n"
     "    this package (api/collimate_server/).\n"),
    (f"api/{PACKAGE}/bundled_integrity.py",
     "    workspace = Path(genesis.__file__).resolve().parent.parent\n",
     "    workspace = Path(genesis.__file__).resolve().parents[2]\n"),
    (f"api/{PACKAGE}/bundled_integrity.py",
     '        installed = distribution("collimate").locate_file("main.py")\n',
     '        installed = distribution("collimate").locate_file("collimate_server/main.py")\n'),
    ("api/tests/conftest.py",
     'API_DIR = Path(__file__).resolve().parent.parent\n'
     'if str(API_DIR) not in sys.path:\n'
     '    sys.path.insert(0, str(API_DIR))\n',
     'API_DIR = Path(__file__).resolve().parent.parent\n'),
    ("api/tests/conftest.py",
     '    for name in tuple(sys.modules):\n'
     '        if any(name == root or name.startswith(root + ".") for root in _API_MODULE_ROOTS):\n'
     '            sys.modules.pop(name, None)\n',
     '    roots = tuple(f"collimate_server.{root}" for root in _API_MODULE_ROOTS)\n'
     '    for name in tuple(sys.modules):\n'
     '        if any(name == root or name.startswith(root + ".") for root in roots):\n'
     '            module = sys.modules.pop(name, None)\n'
     '            parent, _, child = name.rpartition(".")\n'
     '            # ``from collimate_server import x`` reads the package attribute\n'
     '            # before sys.modules, so the attribute goes with the entry.\n'
     '            if getattr(sys.modules.get(parent), child, None) is module:\n'
     '                delattr(sys.modules[parent], child)\n'),
    ("build/sync_builtins.py",
     'LIST = ROOT / "api" / "builtins.json"\n',
     'LIST = ROOT / "api" / "collimate_server" / "builtins.json"\n'),
)


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout


def tracked() -> list[str]:
    return [line for line in git("ls-files", "-z").split("\0") if line]


def to_move(files: list[str]) -> list[str]:
    """api/'s tracked top-level modules, packages and data files that move."""
    names: dict[str, None] = {}
    for name in files:
        parts = name.split("/")
        if parts[0] != "api" or len(parts) < 2:
            continue
        if len(parts) == 2:
            if (parts[1].endswith(".py") and parts[1] not in STAYS) or parts[1] in EXTRA_FILES:
                names[parts[1]] = None
        elif parts[1] not in STAY_DIRS and f"api/{parts[1]}/__init__.py" in files:
            names[parts[1]] = None
    return list(names)


def module_names(moving: list[str]) -> set[str]:
    """Every server top-level module name, moved now or by an earlier run."""
    names = {name.removesuffix(".py") for name in moving if not name.endswith(".json")}
    if TARGET.is_dir():
        for path in TARGET.iterdir():
            if path.suffix == ".py" and path.stem not in ("__init__", "__main__"):
                names.add(path.stem)
            elif path.is_dir() and (path / "__init__.py").is_file():
                names.add(path.name)
    return names


# -- 2. imports ----------------------------------------------------------------------------
IMPORT = re.compile(r"^(?P<indent>[ \t]*)import[ \t]+(?P<names>[A-Za-z_][\w. \t,]*?)[ \t]*(?P<tail>#.*)?$")
FROM = re.compile(r"^(?P<indent>[ \t]*)from[ \t]+(?P<module>\.*[A-Za-z_][\w.]*|\.+)[ \t]+import\b")


def package_of(path: Path) -> list[str] | None:
    """The dotted package a moved file belongs to, as parts, or None."""
    try:
        rel = path.relative_to(API)
    except ValueError:
        return None
    if rel.parts[0] != PACKAGE:
        return None
    parts = list(rel.parts[:-1])
    return parts


def rewrite_imports(path: Path, text: str, mods: set[str]) -> str:
    lines = text.split("\n")
    package = package_of(path)
    out: list[str] = []
    # Code inside a string (a script a test writes or hands to ``python -c``)
    # imports the server the same way, so strings are not skipped.
    for line in lines:
        m = FROM.match(line)
        if m:
            module = m.group("module")
            if module.startswith("."):
                if package is not None:
                    level = len(module) - len(module.lstrip("."))
                    base = package[: len(package) - (level - 1)] if level > 1 else package
                    rest = module.lstrip(".")
                    absolute = ".".join(base + ([rest] if rest else []))
                    line = line[: m.start("module")] + absolute + line[m.end("module"):]
            elif module.split(".")[0] in mods:
                line = line[: m.start("module")] + f"{PACKAGE}.{module}" + line[m.end("module"):]
            out.append(line)
            continue
        m = IMPORT.match(line)
        if m and not line.rstrip().endswith(("\\", "(")):
            out.extend(_rewrite_import(m, mods))
        else:
            out.append(line)
    return "\n".join(out)


def _rewrite_import(m: re.Match, mods: set[str]) -> list[str]:
    indent, tail = m.group("indent"), m.group("tail")
    kept: list[str] = []
    moved: list[str] = []
    for part in [p.strip() for p in m.group("names").split(",")]:
        name, _, alias = (s.strip() for s in part.partition(" as "))
        top = name.split(".")[0]
        if top not in mods:
            kept.append(part)
        elif alias:
            moved.append(f"import {PACKAGE}.{name} as {alias}")
        elif "." in name:
            moved.append(f"import {PACKAGE}.{name}  # noqa: F401 (loads {name})")
            moved.append(f"from {PACKAGE} import {top}")
        else:
            moved.append(f"import {PACKAGE}.{name} as {name}")
    if not moved:
        return [m.group(0)]
    result = ([f"{indent}import {', '.join(kept)}"] if kept else []) + [indent + line for line in moved]
    if tail:
        result[0] += "  " + tail
    return result


# -- 3. strings that name modules ------------------------------------------------------------
def string_patterns(mods: set[str]) -> list[tuple[re.Pattern, str]]:
    names = "|".join(sorted(map(re.escape, mods), key=len, reverse=True))
    module = rf"(?P<q>['\"])(?P<name>(?:{names})(?:\.[A-Za-z_]\w*)*)(?P=q)"
    dotted = rf"(?P<q>['\"])(?P<name>(?:{names})\.[A-Za-z_][\w.]*)(?P=q)"
    leading = [
        r"import_module\(\s*",
        r"sys\.modules\.(?:pop|get)\(\s*",
        r"sys\.modules\[\s*",
        r"(?:setitem|delitem)\(\s*sys\.modules\s*,\s*",
        r"ModuleType\(\s*",
        r"in\s+sys\.modules",  # handled below: the name precedes
    ]
    patterns = [(re.compile(f"(?P<pre>{pre})" + module), "module") for pre in leading[:-1]]
    patterns.append((re.compile(module + r"(?P<post>\s+(?:not\s+)?in\s+sys\.modules)"), "module"))
    for pre in (r"(?<![\w.])patch\(\s*", r"mock\.patch\(\s*", r"monkeypatch\.(?:setattr|delattr)\(\s*"):
        patterns.append((re.compile(f"(?P<pre>{pre})" + dotted), "module"))
    patterns.append((re.compile(rf"(?P<q>['\"])(?P<name>(?:{names})):(?P<attr>app|cli_main)(?P=q)"), "app"))
    patterns.append((re.compile(rf"(?P<pre>__import__\(\s*)(?P<q>['\"])(?P<name>(?:{names}))(?P=q)\s*\)"),
                     "dunder"))
    # Code in a one-line string (``python -c "import sys; from collimate_server.cas import x"``):
    # an import statement right after a quote, a ``;`` or an escaped newline.
    patterns.append((re.compile(rf"(?P<pre>(?:['\"]|;[ \t]*|\\n[ \t]*))(?P<kw>from|import)[ \t]+"
                                rf"(?P<name>(?:{names})(?:\.\w+)*)(?P<post>[ \t]+import\b|[ \t]+as[ \t]+\w+"
                                rf"|(?=[ \t]*(?:;|['\"]|\\n|$)))"), "inline"))
    return patterns


def rewrite_strings(text: str, patterns: list[tuple[re.Pattern, str]]) -> str:
    for pattern, kind in patterns:
        def replace(m: re.Match) -> str:
            name = m.group("name")
            if kind == "inline":
                pre, kw, post = m.group("pre"), m.group("kw"), m.group("post")
                if kw == "import" and not post and "." not in name:
                    return f"{pre}import {PACKAGE}.{name} as {name}"
                return f"{pre}{kw} {PACKAGE}.{name}{post}"
            q = m.group("q")
            if kind == "dunder":
                return f"{m.group('pre')}{q}{PACKAGE}.{name}{q}, fromlist=[{q}{name}{q}])"
            if kind == "app":
                return f"{q}{PACKAGE}.{name}:{m.group('attr')}{q}"
            whole = m.group(0)
            start = m.start("name") - m.start(0)
            return whole[:start] + f"{PACKAGE}." + whole[start:]
        text = pattern.sub(replace, text)
    return text


# -- 4. paths --------------------------------------------------------------------------------
def path_patterns(mods: set[str]) -> list[tuple[re.Pattern, str]]:
    files = "|".join(sorted((re.escape(m) for m in mods if (TARGET / m).is_dir() is False), key=len, reverse=True))
    packages = "|".join(sorted(re.escape(m) for m in mods if (API / m).is_dir() or (TARGET / m).is_dir()))
    edge = r"(?<![\w.-])"
    patterns = [
        (re.compile(rf"{edge}api/(?P<rest>(?:{files})\.py)\b"), r"api/collimate_server/\g<rest>"),
        (re.compile(rf"{edge}api/(?P<rest>builtins\.json)"), r"api/collimate_server/\g<rest>"),
        (re.compile(rf"{edge}api/(?P<rest>static)\b"), r"api/collimate_server/\g<rest>"),
    ]
    if packages:
        patterns.append((re.compile(rf"{edge}api/(?P<rest>(?:{packages}))(?=/|\b)"), r"api/collimate_server/\g<rest>"))
    return patterns


def rewrite_paths(text: str, patterns) -> str:
    for pattern, replacement in patterns:
        text = pattern.sub(replacement, text)
    return text


def is_text(name: str) -> bool:
    path = Path(name)
    nested_docs = "docs" in name.split("/")[1:-1] and not name.startswith("sdk/shared/docs/")
    if path.name in SKIP_NAMES or name.startswith(HISTORICAL) or nested_docs or name.startswith("module-snapshot/"):
        return False
    return path.suffix in TEXT_SUFFIXES or path.name in (".gitignore",)


# -- driver ----------------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true", help="run on a tree with uncommitted edits")
    args = parser.parse_args()
    if git("status", "--porcelain", "--untracked-files=no").strip() and not (args.dry_run or args.allow_dirty):
        print("error: the working tree has uncommitted changes; commit or set them aside first", file=sys.stderr)
        return 1
    files = tracked()
    moving = to_move(files)
    mods = module_names(moving)
    if not mods:
        print("error: no server modules found under api/", file=sys.stderr)
        return 1
    print(f"move: {len(moving)} entries to api/{PACKAGE}/: {' '.join(sorted(moving))}" if moving
          else f"move: nothing left at api/'s top level; api/{PACKAGE}/ holds {len(mods)} modules")
    # `git mv a b` moves a INTO b when b is a directory: an ignored leftover
    # there (a __pycache__ from an earlier run) would nest the package.
    occupied = sorted(f"api/{PACKAGE}/{name}" for name in moving if (TARGET / name).exists())
    if occupied:
        print("error: these destinations already exist (ignored leftovers of an earlier move?); "
              "remove them first: " + " ".join(occupied), file=sys.stderr)
        return 1
    if not args.dry_run:
        TARGET.mkdir(exist_ok=True)
        for name in moving:
            git("mv", f"api/{name}", f"api/{PACKAGE}/{name}")
        # The built UI (ignored by version control) follows the package, so a
        # dev tree keeps serving it.
        if (API / "static").is_dir() and not (TARGET / "static").exists():
            (API / "static").rename(TARGET / "static")
        for name, body in (("__init__.py", INIT), ("__main__.py", MAIN)):
            if not (TARGET / name).exists():
                (TARGET / name).write_text(body)
                git("add", f"api/{PACKAGE}/{name}")
        files = tracked()

    def mapped(name: str) -> str:
        parts = name.split("/")
        if not args.dry_run or parts[0] != "api" or len(parts) < 2:
            return name
        return f"api/{PACKAGE}/" + "/".join(parts[1:]) if parts[1] in moving else name

    strings = string_patterns(mods)
    paths = path_patterns(mods)
    changed: list[str] = []
    for name in files:
        if not is_text(name):
            continue
        path = ROOT / mapped(name)
        source = ROOT / name
        try:
            text = source.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        new = text
        if name.endswith(".py") and not name.startswith(NO_SERVER_IMPORTS):
            new = rewrite_imports(path, new, mods)
            new = rewrite_strings(new, strings)
        new = rewrite_paths(new, paths)
        if new != text:
            changed.append(name)
            if not args.dry_run:
                source.write_text(new, encoding="utf-8")
    print(f"rewrite: {len(changed)} files")

    missing = []
    for name, old, new in LITERAL_EDITS:
        path = ROOT / name
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        if old in text:
            if not args.dry_run:
                path.write_text(text.replace(old, new, 1), encoding="utf-8")
        elif new not in text:
            missing.append(f"{name}: {old.splitlines()[0].strip()!r}")
    if missing:
        print("literal edits not applied (text not found; settle by hand):")
        for line in missing:
            print("  " + line)

    # Whatever still reads like an old module name, for a reviewer.
    names = "|".join(sorted(map(re.escape, mods), key=len, reverse=True))
    suspect = re.compile(rf"^\s*(?:import\s+(?:{names})\b|from\s+(?:{names})(?:\.\w+)*\s+import\b)")
    leftovers = []
    if not args.dry_run:
        for name in tracked():
            if name.endswith(".py") and not name.startswith(NO_SERVER_IMPORTS) and is_text(name):
                for number, line in enumerate((ROOT / name).read_text(encoding="utf-8").splitlines(), 1):
                    if suspect.match(line):
                        leftovers.append(f"  {name}:{number}: {line.strip()}")
    if leftovers:
        print(f"{len(leftovers)} lines still import an old top-level name (inside strings, or not rewritten):")
        print("\n".join(leftovers))
    print("next: review git diff, run python3 scripts/generate.py (hashes over moved paths), commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
