# SPDX-License-Identifier: MIT
"""Pure handler metadata, type selection and accepted-derived identity."""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Mapping

# Every type the app's built-in handlers (builtins/definitions) declare.
RESERVED_TYPES = frozenset({".generator", ".renderer", ".runner", ".handler",
                            ".renderer_example", ".options_stack", ".git-checkout.json"})


def handler_path(value: str) -> str:
    if (not isinstance(value, str) or not value or "\\" in value or "\x00" in value
            or re.match(r"^[A-Za-z]:", value)
            or any(part in ("", ".", "..", "_runs") for part in value.split("/"))):
        raise ValueError("Invalid handler-relative path")
    return value


def derived_hash(files: Mapping[str, bytes]) -> str:
    """Hash paths relative to derived/, with exact bytes and unambiguous frames."""
    digest = hashlib.sha256()

    def add(value: bytes) -> None:
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)

    add(b"collimate-handler-derived-v1")
    for path in sorted(files, key=lambda item: item.encode("utf-8")):
        handler_path(path)
        if not isinstance(files[path], bytes):
            raise ValueError("Handler derived values must be bytes")
        add(path.encode("utf-8"))
        add(hashlib.sha256(files[path]).digest())
    return digest.hexdigest()


def _extension(value) -> str:
    if (not isinstance(value, str) or len(value) < 2
            or re.fullmatch(r"\.[^\s/\\\x00]+", value) is None):
        raise ValueError("Handler extensions must be dot-prefixed suffixes")
    return value.casefold()


def validate_handler(manifest: dict, *, allow_reserved: bool = False) -> dict:
    """Validate metadata without reading files or accepting a derived folder.

    Example membership is checked by the handler verifier against its package.
    Only a loader of the app's built-in handlers may allow reserved types.
    """
    fields = {"id", "name", "description", "types", "guidance", "examples", "derived"}
    if not isinstance(manifest, dict) or set(manifest) - fields:
        raise ValueError("Unknown handler field or non-mapping definition")
    for key in ("id", "name", "description", "guidance"):
        if not isinstance(manifest.get(key), str):
            raise ValueError(f"Handler {key} must be text")
    if re.fullmatch(r"[a-z][a-z0-9-]*", manifest["id"]) is None:
        raise ValueError("Handler id must be a lowercase slug")
    if not manifest["name"].strip():
        raise ValueError("Handler name must be nonempty")
    types = manifest.get("types")
    if not isinstance(types, list) or not types:
        raise ValueError("Handler types must be a nonempty list")
    normalized, seen = [], set()
    for value in types:
        if not isinstance(value, dict) or set(value) - {"ext", "render_as"}:
            raise ValueError("Unknown handler type field")
        ext = _extension(value.get("ext"))
        if ext in seen:
            raise ValueError("Handler has duplicate extensions")
        if ext in RESERVED_TYPES and not allow_reserved:
            raise ValueError(f"Handler type {ext} is reserved for the app's built-in handlers")
        render_as = value.get("render_as")
        normalized.append({"ext": ext,
                           "render_as": _extension(render_as) if render_as is not None else None})
        seen.add(ext)
    examples = manifest.get("examples")
    if not isinstance(examples, list):
        raise ValueError("Handler examples must be a list")
    folders = set()
    for example in examples:
        if not isinstance(example, dict) or set(example) != {"folder", "description", "expect", "feedback"}:
            raise ValueError("Handler examples need folder, description, expect and feedback")
        folder = handler_path(example["folder"])
        if folder in folders or example["expect"] not in ("valid", "invalid"):
            raise ValueError("Handler examples need unique folders and valid/invalid expectations")
        if any(not isinstance(example[key], str) for key in ("description", "feedback")):
            raise ValueError("Handler example description and feedback must be text")
        folders.add(folder)
    accepted = manifest.get("derived")
    if accepted is not None and (not isinstance(accepted, str) or re.fullmatch(r"[0-9a-f]{64}", accepted) is None):
        raise ValueError("Handler derived must be the accepted folder content hash")
    return {**manifest, "types": normalized, "examples": [dict(value) for value in examples]}


def handler_for(handlers, path: str):
    """Longest case-folded suffix; any clash disables that suffix for all owners."""
    counts = Counter(value["ext"].casefold() for handler in handlers for value in handler["types"])
    matches = [(len(value["ext"]), value["ext"].casefold(), handler)
               for handler in handlers for value in handler["types"]
               if path.casefold().endswith(value["ext"].casefold())]
    if not matches:
        return None
    _, ext, handler = max(matches, key=lambda item: item[0])
    return handler if counts[ext] == 1 else None


def type_for(handler, path: str):
    """Select the longest matching type from an already selected handler."""
    if handler is None:
        return None
    return max((value for value in handler["types"]
                if path.casefold().endswith(value["ext"].casefold())),
               key=lambda value: len(value["ext"]), default=None)


MAX_ADDED_SKILL_BYTES = 1024 * 1024
MAX_ADDED_SKILL_FILES = 128
SCRIPT_EXTENSIONS = frozenset({'.py', '.pyw', '.js', '.mjs', '.cjs', '.jsx', '.ts', '.tsx',
                              '.sh', '.bash', '.zsh', '.fish', '.ps1', '.bat', '.cmd',
                              '.pl', '.rb', '.php', '.lua', '.exe', '.dll', '.so', '.wasm'})


def _skill_names(files: Mapping[str, bytes], *, strict: bool = False) -> set[str]:
    import yaml
    names = set()
    owners = {}
    for path, body in files.items():
        parts = path.split('/')
        if len(parts) != 3 or parts[0] != 'skills' or parts[2] != 'SKILL.md':
            continue
        local_names = {parts[1].lower()}
        names.update(local_names)
        try:
            text = body.decode('utf-8', 'strict')
        except UnicodeError:
            if strict:
                raise ValueError('Added skill files must be UTF-8 text') from None
            continue
        if text.startswith('---'):
            front, separator, _rest = text[3:].partition('\n---')
            if not separator:
                if strict:
                    raise ValueError('Skill frontmatter is incomplete')
                continue
            try:
                value = yaml.safe_load(front)
            except yaml.YAMLError:
                if strict:
                    raise ValueError('Skill frontmatter is malformed') from None
                continue
            if value is not None and not isinstance(value, dict):
                if strict:
                    raise ValueError('Skill frontmatter must be a mapping')
                continue
            if isinstance(value, dict) and 'name' in value:
                if not isinstance(value['name'], str):
                    if strict:
                        raise ValueError('Skill name must be text')
                    continue
                local_names.add(value['name'].strip().lower() or parts[1].lower())
        for name in local_names:
            if strict and name in owners and owners[name] != parts[1]:
                raise ValueError('Added skill names must be unique within their handler')
            owners[name] = parts[1]
        names.update(local_names)
    return names


def validate_added_skills(package_files: Mapping[str, bytes], accepted_files: Mapping[str, bytes]) -> dict[str, str]:
    """Validate additive text guidance beside derived/, without changing its hash.

    Names cannot shadow this handler's current derived or previously accepted
    skills. Other handlers have separate namespaces. Scripts and executable
    verifier assets are never additive.
    """
    current = {path[len('derived/'):]: body for path, body in package_files.items()
               if path.startswith('derived/')}
    reserved = _skill_names(current) | _skill_names(accepted_files)
    added = {path: body for path, body in package_files.items() if path.startswith('skills/')}
    if len(added) > MAX_ADDED_SKILL_FILES:
        raise ValueError('Added skills exceed the 128-file bound')
    if any(not isinstance(body, bytes) for body in added.values()):
        raise ValueError('Added skill files must be UTF-8 text bytes')
    if sum(map(len, added.values())) > MAX_ADDED_SKILL_BYTES:
        raise ValueError('Added skills exceed the 1 MiB text bound')
    names = _skill_names(added, strict=True)
    if names & reserved:
        raise ValueError('Added skill name collides with this handler\'s accepted or derived skills')
    total = 0
    result = {}
    for path, body in added.items():
        handler_path(path)
        parts = path.split('/')
        if (len(parts) < 3 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', parts[1])
                or f'skills/{parts[1]}/SKILL.md' not in added):
            raise ValueError('Added skills require skills/<name>/SKILL.md')
        lower = [part.casefold() for part in parts[1:]]
        if ('derived' in lower or 'scripts' in lower or lower[-1] == 'verify.ts'
                or any(lower[-1].endswith(ext) for ext in SCRIPT_EXTENSIONS)):
            raise ValueError('Added skills cannot contain derived content, verifiers or scripts')
        if not isinstance(body, bytes):
            raise ValueError('Added skill files must be UTF-8 text bytes')
        total += len(body)
        if total > MAX_ADDED_SKILL_BYTES:
            raise ValueError('Added skills exceed the 1 MiB text bound')
        try:
            text = body.decode('utf-8', 'strict')
        except UnicodeError as exc:
            raise ValueError('Added skill files must be UTF-8 text') from exc
        if '\x00' in text or text.lstrip().startswith('#!'):
            raise ValueError('Added skills cannot contain binary data or executable shebangs')
        result[path] = text
    return result


__all__ = ["RESERVED_TYPES", "handler_path", "derived_hash", "validate_handler", "handler_for", "type_for", "validate_added_skills"]
