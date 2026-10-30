"""Manifest linter for Collimate generators and renderers.

Entry points
------------

- :func:`lint_generator_manifest`       — lint a ``*.generator`` file.
- :func:`lint_renderer_manifest`        — lint a ``*.renderer`` file.
- :func:`lint_runner_manifest`           — lint a ``*.runner`` file
                                          (optionally with a target-generator
                                          resolver for cross-checks).
- :func:`lint_generator_manifest_data`  — lint a parsed dict (in-memory),
                                          checking files against a stage
                                          ``files_present`` set. Used by the
                                          generator-builders to validate the
                                          agent's scaffold before commit.
- :func:`lint_renderer_manifest_data`   — same, for renderers.
- :func:`lint_runner_manifest_data`      — same, for runners.
- :func:`lint_path`                     — auto-detect by filename or walk a
                                          directory (generator manifests are
                                          indexed first so runner manifests
                                          resolve their target generator).

Each returns a list of :class:`Issue` values. :class:`GeneratorRef` is the
resolver entry a runner's cross-checks consult; :func:`declared_enforces`
reads a generator manifest's ``enforces`` for it.
:func:`collimate.lint.cli.main` is the ``collimate-lint`` entry point (also
``python -m collimate.lint``).
"""

from .core import (
    GENERATOR_FIELDS,
    RENDERER_FIELDS,
    RUNNER_FIELDS,
    GENERATOR_RUNS_ON,
    CONFIG_FOLDER_FIELDS,
    ACCOUNT_KINDS,
    ACCOUNT_REQUIREMENT_FIELDS,
    ACCOUNT_REQUIREMENT_LIST_FIELDS,
    ACCOUNT_REQUIREMENT_SCALAR_FIELDS,
    ACCOUNT_PROPERTY_ROOTS,
    ACCOUNT_PROPERTY_FORBIDDEN,

    GeneratorRef,
    declared_enforces,
    Issue,
    Level,
    lint_generator_manifest,
    lint_generator_manifest_data,
    lint_path,
    lint_runner_manifest,
    lint_runner_manifest_data,
    lint_renderer_manifest,
    lint_renderer_manifest_data,
)

__all__ = [
    "GENERATOR_FIELDS",
    "RENDERER_FIELDS",
    "RUNNER_FIELDS",
    "GENERATOR_RUNS_ON",
    "CONFIG_FOLDER_FIELDS",
    "ACCOUNT_KINDS",
    "ACCOUNT_REQUIREMENT_FIELDS",
    "ACCOUNT_REQUIREMENT_LIST_FIELDS",
    "ACCOUNT_REQUIREMENT_SCALAR_FIELDS",
    "ACCOUNT_PROPERTY_ROOTS",
    "ACCOUNT_PROPERTY_FORBIDDEN",

    "GeneratorRef",
    "declared_enforces",
    "Issue",
    "Level",
    "lint_generator_manifest",
    "lint_generator_manifest_data",
    "lint_runner_manifest",
    "lint_runner_manifest_data",
    "lint_renderer_manifest",
    "lint_renderer_manifest_data",
    "lint_path",
]
