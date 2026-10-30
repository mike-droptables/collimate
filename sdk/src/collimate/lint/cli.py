"""``collimate-lint`` CLI entry point.

Usage
-----

    collimate-lint path/to/custom.generator
    collimate-lint path/to/custom.runner
    collimate-lint path/to/generator_directory
    collimate-lint ./generators ./renderers

A directory containing direct definition files selects that package. Other
folders discover shallow packages using .generator, .renderer, .runner and
.handler boundaries; nested definitions remain package content. Runners are
cross-checked against generators found in the same traversal.

Exit code is 1 if any issue at ERROR level was found, 0 otherwise.
Warnings do not affect the exit code. Pass ``--strict`` to also fail on
warnings.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .core import Issue, Level, lint_path


def _print_issues(issues: list[Issue], *, use_color: bool) -> None:
    red = "\033[31m" if use_color else ""
    yellow = "\033[33m" if use_color else ""
    reset = "\033[0m" if use_color else ""
    for i in issues:
        prefix = (red + "error" + reset) if i.level == Level.ERROR else (yellow + "warning" + reset)
        print(i.format(prefix))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="collimate-lint",
        description="Validate Collimate generator, renderer, and runner manifests.",
    )
    parser.add_argument(
        "paths",
        nargs="+",
        help="Definition files, package directories, or parent trees to scan.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero on warnings as well as errors.",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI color output.",
    )
    args = parser.parse_args(argv)

    use_color = not args.no_color and sys.stdout.isatty()
    all_issues: list[Issue] = []
    for p in args.paths:
        all_issues.extend(lint_path(Path(p)))

    _print_issues(all_issues, use_color=use_color)

    errors = sum(1 for i in all_issues if i.level == Level.ERROR)
    warnings = sum(1 for i in all_issues if i.level == Level.WARNING)

    if not all_issues:
        print("collimate-lint: all manifests ok")
    else:
        print(f"collimate-lint: {errors} error(s), {warnings} warning(s)")

    if errors:
        return 1
    if args.strict and warnings:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
