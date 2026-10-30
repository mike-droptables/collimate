# SPDX-License-Identifier: MIT
"""Public options declarations and pure cell-relative Split path planning."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
import re
from typing import Literal, NotRequired, TypedDict



class OptionDefinition(TypedDict):
    name: str
    root: NotRequired[str]
    files: list[str]


class OptionsDocument(TypedDict):
    path: str
    options: list[OptionDefinition]
    rest: Literal['keep', 'drop', 'each']


class MappedOption(TypedDict):
    name: str
    files: dict[str, str]


@dataclass
class SplitPlan:
    options: list[MappedOption]
    kept: set[str]
    retain_source: bool


def _path(path: str) -> str:
    if (not isinstance(path, str) or not path or path.startswith('/') or '\\' in path
            or '\x00' in path or re.match(r'^[A-Za-z]:', path)
            or any(p in ('', '.', '..') for p in path.split('/'))):
        raise ValueError(f'Invalid cell-relative path: {path!r}')
    return path


def select_options_path(paths: Iterable[str]) -> str | None:
    candidates = [_path(p) for p in paths if isinstance(p, str) and p.endswith('.options_stack')]
    return min(candidates, key=lambda p: (p.count('/'), p), default=None)


def parse_options_stack(path: str, content: str | bytes) -> OptionsDocument:
    import yaml

    _path(path)
    try:
        raw = yaml.safe_load(content.decode('utf-8') if isinstance(content, bytes) else content)
    except (UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f'{path}: invalid options YAML: {exc}') from exc
    if not isinstance(raw, dict) or set(raw) - {'options', 'rest'}:
        raise ValueError(f'{path}: expected options and optional rest')
    options = raw.get('options')
    if not isinstance(options, list) or not options:
        raise ValueError(f'{path}: options must be a nonempty list')
    parsed: list[OptionDefinition] = []
    for number, option in enumerate(options, 1):
        label = option.get('name', f'#{number}') if isinstance(option, dict) else f'#{number}'
        error = f'{path}: option {label!r}'
        if not isinstance(option, dict) or set(option) - {'name', 'root', 'files'}:
            raise ValueError(f'{error}: expected name, optional root, and files')
        name, files = option.get('name'), option.get('files')
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f'{error}: name must be nonempty text')
        if not isinstance(files, list) or not files or any(not isinstance(g, str) or not g for g in files):
            raise ValueError(f'{error}: files must be a nonempty list of globs')
        entry: OptionDefinition = {'name': name, 'files': list(files)}
        if 'root' in option:
            if not isinstance(option['root'], str) or not option['root']:
                raise ValueError(f'{error}: root must be a literal folder')
            entry['root'] = option['root']
        parsed.append(entry)
    rest = raw.get('rest', 'keep')
    if rest not in ('keep', 'drop', 'each'):
        raise ValueError(f'{path}: rest must be keep, drop or each')
    return {'path': path, 'options': parsed, 'rest': rest}


def read_options_stack(paths: Iterable[str], read_file: Callable[[str], str | bytes], *,
                       protected_paths: Iterable[str] = ()) -> OptionsDocument | None:
    path = select_options_path(paths)
    if path is None:
        return None
    if path in set(protected_paths):
        raise ValueError(f'{path}: selected options definition is protected')
    return parse_options_stack(path, read_file(path))


def _resolve(base: str, relative: str) -> str:
    if not relative or relative.startswith('/') or '\\' in relative or '\x00' in relative or re.match(r'^[A-Za-z]:', relative):
        raise ValueError(f'invalid relative path {relative!r}')
    parts = base.split('/') if base else []
    for part in relative.split('/'):
        if part in ('', '.'):
            continue
        if part == '..':
            if not parts:
                raise ValueError(f'path escapes cell or traverses a glob: {relative!r}')
            parts.pop()
        else:
            parts.append(part)
    return '/'.join(parts)


def _glob(root: str, pattern: str) -> re.Pattern:
    if (not pattern or pattern.startswith('/') or '\\' in pattern or '\x00' in pattern
            or re.match(r'^[A-Za-z]:', pattern)):
        raise ValueError(f'invalid relative path {pattern!r}')
    # Root and definition-directory components are always literal. Only
    # components introduced by files patterns participate in the glob dialect.
    pieces = [(part, False) for part in root.split('/')] if root else []
    for part in pattern.split('/'):
        if part in ('', '.'):
            continue
        if part == '..':
            if not pieces or (pieces[-1][1] and any(c in pieces[-1][0] for c in '*?')):
                raise ValueError(f'path escapes cell or traverses a glob: {pattern!r}')
            pieces.pop()
        else:
            pieces.append((part, True))
    out = ''
    for n, (piece, glob_component) in enumerate(pieces):
        if not glob_component:
            out += re.escape(piece)
        elif piece == '**':
            out += '.*' if n == len(pieces) - 1 else '(?:[^/]+/)*'
            continue
        else:
            i = 0
            while i < len(piece):
                if piece[i:i + 2] == '**':
                    out += '.*'
                    i += 2
                elif piece[i] == '*':
                    out += '[^/]*'
                    i += 1
                elif piece[i] == '?':
                    out += '[^/]'
                    i += 1
                else:
                    out += re.escape(piece[i])
                    i += 1
        if n != len(pieces) - 1:
            out += '/'
    return re.compile('^' + out + '$')


def map_options_stack(document: OptionsDocument, paths: Iterable[str], *,
                      protected_paths: Iterable[str] = ()) -> SplitPlan:
    import yaml

    # Validate generated documents through the same schema as read declarations.
    doc = parse_options_stack(document['path'], yaml.safe_dump({k: document[k] for k in ('options', 'rest')}))
    available = {_path(path) for path in paths}
    protected = set(protected_paths) & available
    consumed = doc['path']
    if consumed in protected:
        raise ValueError(f'{consumed}: selected options definition is protected')
    base = consumed.rsplit('/', 1)[0] if '/' in consumed else ''
    exportable = available - protected - {consumed}
    selected: set[str] = set()
    mapped: list[MappedOption] = []
    for option in doc['options']:
        try:
            root = _resolve(base, option['root']) if 'root' in option else base
            if 'root' in option and not any(path.startswith(root + '/') if root else True for path in available):
                raise ValueError(f'root {option["root"]!r} does not exist')
            patterns = [_glob(root, pattern) for pattern in option['files']]
            matches = sorted(path for path in exportable if any(p.fullmatch(path) for p in patterns))
            if not matches:
                raise ValueError('globs match no exportable files')
            files: dict[str, str] = {}
            for source in matches:
                prefix = root + '/' if root else ''
                if source.startswith(prefix):
                    destination = source[len(prefix):]
                else:
                    prefix = base + '/' if base else ''
                    if not source.startswith(prefix):
                        raise ValueError(f'output path for {source!r} escapes the options folder')
                    destination = source[len(prefix):]
                _path(destination)
                if destination in files and files[destination] != source:
                    raise ValueError(f'output path collision at {destination!r}')
                files[destination] = source
            mapped.append({'name': option['name'], 'files': files})
            selected.update(matches)
        except ValueError as exc:
            raise ValueError(f'{consumed}: option {option["name"]!r}: {exc}') from exc
    remainder = exportable - selected
    if doc['rest'] == 'each':
        for option in mapped:
            for source in sorted(remainder):
                if source in option['files'] and option['files'][source] != source:
                    raise ValueError(f'{consumed}: option {option["name"]!r}: remainder collision at {source!r}')
                option['files'][source] = source
    kept = protected | (remainder if doc['rest'] == 'keep' else set())
    return SplitPlan(mapped, kept, doc['rest'] == 'keep' or bool(protected))


def sanitize_folder(name: str, fallback: str) -> str:
    """Preserve case/spaces, replace separators, strip leading dots and trim."""
    safe = (name or '').replace('/', '-').replace('\\', '-').lstrip('.').strip()
    return fallback if safe in ('', '.', '..') else safe


def dedupe_folder(base: str, used: set[str]) -> str:
    folder, number = base, 2
    while folder in used:
        folder = f'{base}-{number}'
        number += 1
    used.add(folder)
    return folder


__all__ = ['OptionDefinition', 'OptionsDocument', 'MappedOption', 'SplitPlan',
           'select_options_path', 'parse_options_stack', 'read_options_stack',
           'map_options_stack', 'sanitize_folder', 'dedupe_folder']
