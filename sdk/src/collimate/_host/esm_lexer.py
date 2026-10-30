# SPDX-License-Identifier: MIT
#
# Import scan ported from es-module-lexer 3.0.4's minimal build (src/lexer.c,
# src/lexer.h compiled with -DLEXER_MIN, and src/lexer.ts's decoding),
# https://github.com/guybedford/es-module-lexer. Its license:
#
#   MIT License
#   -----------
#
#   Copyright (C) 2018-2022 Guy Bedford
#
#   Permission is hereby granted, free of charge, to any person obtaining a
#   copy of this software and associated documentation files (the
#   "Software"), to deal in the Software without restriction, including
#   without limitation the rights to use, copy, modify, merge, publish,
#   distribute, sublicense, and/or sell copies of the Software, and to permit
#   persons to whom the Software is furnished to do so, subject to the
#   following conditions:
#
#   The above copyright notice and this permission notice shall be included
#   in all copies or substantial portions of the Software.
#
#   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
#   OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
#   MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN
#   NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
#   DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
#   OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE
#   USE OR OTHER DEALINGS IN THE SOFTWARE.
"""ES module import scan: a pure-Python port of es-module-lexer 3.0.4's
minimal build (``es-module-lexer/minimal``).

The module mirror rewrites the specifiers of upstream module bodies, in the
server on a miss and in the SDK's freeze, and the server runs no Node. This
port follows ``lexer.c`` state for state: strings, templates and their
``${}`` nesting, comments, the regular-expression-versus-division heuristics,
static, re-export and literal-dynamic imports, ``import.meta`` and the export
statement walk whose cursor movement the import scan depends on. It records
only what the mirror needs: each import's specifier span and value. Like the
lexer it fails on nesting deeper than its fixed token stacks (1024 open
tokens, 512 open dynamic imports) and on a quoted specifier or export name
whose escapes do not decode.

Character runs the C lexer walks one by one are skipped with regular
expressions; the observable state (cursor, last-token position, the open
token stack and its stale slots) is the C lexer's.
``tests/test_esm_lexer_oracle.py`` compares it with the real es-module-lexer
over the whole shipped module snapshot and the lexer's own fixtures.

Positions are indexes into the Python string (code points); the JavaScript
lexer counts UTF-16 units, which differ only after astral characters.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["ImportRecord", "LexError", "scan_imports"]


class LexError(ValueError):
    """The source is not lexable as an ES module; ``position`` is where."""

    def __init__(self, message: str, position: int):
        super().__init__(message)
        self.position = position


@dataclass(frozen=True, slots=True)
class ImportRecord:
    """One import. For a static or literal-dynamic import, ``start``/``end``
    delimit the specifier text inside its quotes and ``name`` is its value.
    A computed ``import()`` has ``name`` None and spans its expression."""

    start: int
    end: int
    name: str | None
    dynamic: bool


# lexer.h's open-token states.
_ANY_PAREN, _ANY_BRACE, _TEMPLATE, _TEMPLATE_BRACE, _IMPORT_PAREN, _CLASS_BRACE = 1, 2, 3, 4, 5, 6
_ANY_BRACKET = 8
# Import.dynamic markers (any other value is the import()'s paren position).
_STANDARD = -1
_META = -2

_OPEN_TOKEN_STACK_SIZE = 1024
_DYNAMIC_IMPORT_STACK_SIZE = 512

# lexer.c's KEYWORDS: the keyword tails, then the ';' STATEMENT_END points at.
# The parse starts with lastTokenPos there, a statement boundary below the
# source; it sits in front of the sentinels, as the C's static data does.
_KEYWORDS = ("xport" "port" "lass" "rom" "eta" "vo" "yie" "dele" "instan" "ty" "retur" "debugge"
             "awai" "thr" "while" "for" "if" "catc" "finall" "els" "brea" "contin" "sync"
             "unction" "ource" "efer" ";")
_STATEMENT_END = len(_KEYWORDS) - 1
_PAD = 64  # '\0' sentinels around the source: the C reads past both ends.
_SOURCE = len(_KEYWORDS) + _PAD  # the source's first index
_EMPTY = _SOURCE - 1  # a '\0' below the source: a token stack slot never written

_PUNCT = frozenset("!%&()*+,-./:;<=>?[]^{|}~")
_EXPR_PUNCT = frozenset("!%&(*+,-.:;<=>?[^{|~")
_BR_OR_WS = frozenset("\t\n\x0b\x0c\r \xa0")
_WS_NOT_BR = frozenset("\t\x0b\x0c \xa0")
_MAIN_WS = frozenset("\t\n\x0b\x0c\r ")  # the main loop's skip set (no NBSP)
_BR_WS_PUNCT_NOT_DOT = (_BR_OR_WS | _PUNCT) - {"."}

_RUN_CLASS = "0-9A-Za-z$_\\\\\x80-\x9f\xa1-\U0010ffff"
_RUN = re.compile(f"[{_RUN_CLASS}]*")
# Keywords can only start a token where the previous character ends a run.
_KEYWORD = f"(?<![{_RUN_CLASS}])(?:import|export|class)"
_NEXT = re.compile(f"[()\\[\\]{{}}'\"/`]|{_KEYWORD}")
_NEXT_COMMA = re.compile(f"[(),\\[\\]{{}}'\"/`]|{_KEYWORD}")
_STRING_STOP = {"'": re.compile(r"['\\\n\r]"), '"': re.compile(r'["\\\n\r]')}
_TEMPLATE_STOP = re.compile(r"[$`\\]")
_REGEX_STOP = re.compile(r"[/\[\\\n\r]")
_CLASS_STOP = re.compile(r"[\]\\\n\r]")
_LINE_END = re.compile(r"[\n\r]")
_BLOCK_END_BR = re.compile(r"[\n\r]|\*/")
_WS_PUNCT_OR_NUL = re.compile("[\t-\r \xa0!%&()*+,\\-./:;<=>?\\[\\]^{|}~\x00]")


def _is_run_char(ch: str) -> bool:
    return ("a" <= ch <= "z" or "A" <= ch <= "Z" or "0" <= ch <= "9" or ch in "$_\\"
            or (ch > "\x7f" and ch != "\xa0"))


def _is_value_char(ch: str) -> bool:
    return "0" <= ch <= "9" or "A" <= ch <= "Z" or "a" <= ch <= "z" or ch in "_$" or ch >= "\x80"


class _Import:
    __slots__ = ("start", "end", "statement_end", "dynamic", "safe")  # safe: the SafeString flag

    def __init__(self, start, end, statement_end, dynamic, safe):
        self.start = start
        self.end = end
        self.statement_end = statement_end
        self.dynamic = dynamic
        self.safe = safe


class _Lexer:
    def __init__(self, source: str):
        self.src = _KEYWORDS + "\0" * _PAD + source + "\0" * _PAD
        self.S = _SOURCE
        self.end = _SOURCE + len(source) - 1
        self.pos = _SOURCE - 1
        self.last = _STATEMENT_END  # lastTokenPos
        self.tok = [0] * _OPEN_TOKEN_STACK_SIZE  # openTokenStack[].token; popped slots stay readable
        self.tpos = [_EMPTY] * _OPEN_TOKEN_STACK_SIZE  # openTokenStack[].pos
        self.depth = 0
        self.dyn: list[_Import] = []  # dynamicImportStack
        self.imports: list[_Import] = []
        self.head_last: int | None = None  # import_write_head_last, as an index
        # Every export's (start, end, local start, local end); the last one is
        # export_write_head. The decode reads their quoted names.
        self.exports: list[tuple[int, int, int | None, int | None]] = []
        self.division = False  # lastSlashWasDivision
        self.next_class = False  # nextBraceIsClass
        self.error = False
        self.error_pos = 0

    # -- state helpers ------------------------------------------------------
    def push(self, token: int, at: int) -> bool:
        """pushOpenToken: False (a syntax error) when the stack is full."""
        d = self.depth
        if d == _OPEN_TOKEN_STACK_SIZE:
            self.syntax_error()
            return False
        self.tok[d] = token
        self.tpos[d] = at
        self.depth = d + 1
        return True

    def syntax_error(self) -> None:
        self.error = True
        self.error_pos = self.pos - self.S
        self.pos = self.end + 1

    def add_import(self, start: int, end, dynamic: int) -> _Import:
        if dynamic == _META:
            record = _Import(start, end, end, dynamic, False)
        elif dynamic == _STANDARD:
            record = _Import(start, end, end + 1, dynamic, True)
        else:
            record = _Import(start, None, None, dynamic, False)
        self.head_last = len(self.imports) - 1 if self.imports else None
        self.imports.append(record)
        return record

    def add_export(self, start: int, end: int, local_start: int | None, local_end: int | None) -> None:
        self.exports.append((start, end, local_start, local_end))

    # -- character predicates over positions --------------------------------
    def is_spread(self, p: int) -> bool:
        src = self.src
        return src[p] == "." and src[p - 1] == "." and src[p - 2] == "."

    def br_ws_punct_spread_not_dot(self, p: int) -> bool:
        c = self.src[p]
        return c in _BR_OR_WS or (c in _PUNCT and (c != "." or self.is_spread(p)))

    def keyword_start(self, p: int) -> bool:
        return p == self.S or self.br_ws_punct_spread_not_dot(p - 1)

    def preceding1(self, p: int, c1: str) -> bool:
        if p < self.S:
            return False
        return self.src[p] == c1 and (p == self.S or self.src[p - 1] in _BR_WS_PUNCT_NOT_DOT)

    def preceding(self, p: int, word: str) -> bool:
        s = p - len(word) + 1
        if s < self.S:
            return False
        return self.src.startswith(word, s) and (s == self.S or self.br_ws_punct_spread_not_dot(s - 1))

    def is_expression_keyword(self, p: int) -> bool:
        src = self.src
        c = src[p]
        if c == "d":
            b = src[p - 1]
            if b == "i":
                return self.preceding(p - 2, "vo")
            if b == "l":
                return self.preceding(p - 2, "yie")
            return False
        if c == "e":
            b = src[p - 1]
            if b == "s":
                b2 = src[p - 2]
                if b2 == "l":
                    return self.preceding1(p - 3, "e")
                if b2 == "a":
                    return self.preceding1(p - 3, "c")
                return False
            if b == "t":
                return self.preceding(p - 2, "dele")
            if b == "u":
                return self.preceding(p - 2, "contin")
            return False
        if c == "f":
            if src[p - 1] != "o" or src[p - 2] != "e":
                return False
            b3 = src[p - 3]
            if b3 == "c":
                return self.preceding(p - 4, "instan")
            if b3 == "p":
                return self.preceding(p - 4, "ty")
            return False
        if c == "k":
            return self.preceding(p - 1, "brea")
        if c == "n":
            return self.preceding1(p - 1, "i") or self.preceding(p - 1, "retur")
        if c == "o":
            return self.preceding1(p - 1, "d")
        if c == "r":
            return self.preceding(p - 1, "debugge")
        if c == "t":
            return self.preceding(p - 1, "awai")
        if c == "w":
            b = src[p - 1]
            if b == "e":
                return self.preceding1(p - 2, "n")
            if b == "o":
                return self.preceding(p - 2, "thr")
            return False
        return False

    def is_paren_keyword(self, p: int) -> bool:
        return self.preceding(p, "while") or self.preceding(p, "for") or self.preceding(p, "if")

    def is_break_or_continue(self, p: int) -> bool:
        c = self.src[p]
        if c == "k":
            return self.preceding(p - 1, "brea")
        if c == "e" and self.src[p - 1] == "u":
            return self.preceding(p - 2, "contin")
        return False

    def is_expression_terminator(self, p: int) -> bool:
        c = self.src[p]
        if c == ">":
            return self.src[p - 1] == "="
        if c == ";" or c == ")":
            return True
        if c == "h":
            return self.preceding(p - 1, "catc")
        if c == "y":
            return self.preceding(p - 1, "finall")
        if c == "e":
            return self.preceding(p - 1, "els")
        return False

    def is_for_of_binding(self, p: int) -> bool:
        src = self.src
        c = src[p]
        if c not in _BR_OR_WS and c not in "]})":
            return False
        while p > self.S and src[p] in _BR_OR_WS:
            p -= 1
        c = src[p]
        return c in "]})" or c not in _PUNCT

    # -- scanners -----------------------------------------------------------
    def skip_run(self) -> None:
        self.pos = _RUN.match(self.src, self.pos + 1).end() - 1

    def line_comment(self) -> None:
        p, end = self.pos, self.end
        if p >= end:
            self.pos = p + 1
            return
        m = _LINE_END.search(self.src, p + 1, end + 1)
        self.pos = m.start() if m else end + 1

    def block_comment(self, br: bool) -> None:
        p, end = self.pos + 1, self.end
        if p >= end:
            self.pos = p + 1
            return
        if br:
            k = self.src.find("*/", p + 1, end + 1)
            self.pos = k + 1 if k >= 0 else end + 1
            return
        m = _BLOCK_END_BR.search(self.src, p + 1, end + 1)
        if m is None:
            self.pos = end + 1
        elif m.group() == "*/":
            self.pos = m.start() + 1
        else:
            self.pos = m.start()

    def comment_whitespace(self, br: bool) -> str:
        src, end = self.src, self.end
        skip = _BR_OR_WS if br else _WS_NOT_BR
        while True:
            ch = src[self.pos]
            if ch == "/":
                nx = src[self.pos + 1]
                if nx == "/":
                    self.line_comment()
                elif nx == "*":
                    self.block_comment(br)
                else:
                    return ch
            elif ch not in skip:
                return ch
            if self.pos >= end:
                self.pos += 1
                return ch
            self.pos += 1

    def string_literal(self, quote: str) -> None:
        src, end, stop = self.src, self.end, _STRING_STOP[quote]
        p = self.pos
        while True:
            if p >= end:
                self.pos = p + 1
                self.syntax_error()
                return
            m = stop.search(src, p + 1, end + 1)
            if m is None:
                self.pos = end + 1
                self.syntax_error()
                return
            k = m.start()
            ch = src[k]
            if ch == quote:
                self.pos = k
                return
            if ch == "\\":
                k += 1
                if src[k] == "\r" and src[k + 1] == "\n":
                    k += 1
                p = k
                continue
            self.pos = k
            self.syntax_error()
            return

    def template_string(self) -> None:
        src, end = self.src, self.end
        p = self.pos
        while True:
            if p >= end:
                self.pos = p + 1
                self.syntax_error()
                return
            m = _TEMPLATE_STOP.search(src, p + 1, end + 1)
            if m is None:
                self.pos = end + 1
                self.syntax_error()
                return
            k = m.start()
            ch = src[k]
            if ch == "$":
                if src[k + 1] == "{":
                    self.pos = k + 1
                    self.push(_TEMPLATE_BRACE, k + 1)
                    return
                p = k
                continue
            if ch == "`":
                self.pos = k
                self.depth -= 1
                if self.tok[self.depth] != _TEMPLATE:
                    self.syntax_error()
                return
            p = k + 1

    def no_substitution_template(self) -> bool:
        src, end = self.src, self.end
        start = p = self.pos
        while p < end:
            p += 1
            ch = src[p]
            if ch == "`":
                self.pos = p
                return True
            if ch == "\\":
                p += 1
                continue
            if ch == "$" and src[p + 1] == "{":
                break
        self.pos = start
        return False

    def regex_class(self) -> None:
        src, end = self.src, self.end
        p = self.pos
        while True:
            if p >= end:
                self.pos = p + 1
                self.syntax_error()
                return
            m = _CLASS_STOP.search(src, p + 1, end + 1)
            if m is None:
                self.pos = end + 1
                self.syntax_error()
                return
            k = m.start()
            ch = src[k]
            if ch == "]":
                self.pos = k
                return
            if ch == "\\":
                p = k + 1
                continue
            self.pos = k
            self.syntax_error()
            return

    def regular_expression(self) -> None:
        src, end = self.src, self.end
        while True:
            p = self.pos
            if p >= end:
                self.pos = p + 1
                self.syntax_error()
                return
            m = _REGEX_STOP.search(src, p + 1, end + 1)
            if m is None:
                self.pos = end + 1
                self.syntax_error()
                return
            k = m.start()
            ch = src[k]
            if ch == "/":
                self.pos = k
                return
            if ch == "[":
                self.pos = k
                self.regex_class()
            elif ch == "\\":
                self.pos = k + 1
            else:
                self.pos = k
                self.syntax_error()
                return

    def read_to_ws_or_punct(self) -> str:
        """readToWsOrPunctuator with ch == *pos: stops at whitespace or a
        punctuator, or at a NUL after the first character."""
        src, p = self.src, self.pos
        c = src[p]
        if c in _BR_OR_WS or c in _PUNCT:
            return c
        self.pos = _WS_PUNCT_OR_NUL.search(src, p + 1).start()
        return src[self.pos]

    # -- the tokenizer ------------------------------------------------------
    def handle_slash(self) -> bool:
        """Division, regular expression or comment; True for a comment."""
        src = self.src
        pos = self.pos
        nx = src[pos + 1]
        if nx == "/":
            self.line_comment()
            return True
        if nx == "*":
            self.block_comment(True)
            return True
        last = self.last
        lt = src[last]
        d = self.depth
        if ((lt in _EXPR_PUNCT
             and not (lt == "." and "0" <= src[last - 1] <= "9")
             and not (lt == "+" and src[last - 1] == "+")
             and not (lt == "-" and src[last - 1] == "-"))
                or (lt == ")" and self.is_paren_keyword(self.tpos[d]))
                or (d > 0 and self.tok[d - 1] == _ANY_PAREN and lt == "f" and src[last - 1] == "o"
                    and self.is_for_of_binding(last - 2) and self.preceding(self.tpos[d - 1], "for"))
                or (lt == "}" and (self.is_expression_terminator(self.tpos[d])
                                   or self.tok[d] == _CLASS_BRACE))
                or self.is_expression_keyword(last)
                or (lt == "/" and self.division)
                or lt == "\0"
                or (self.exports and self.exports[-1][0] <= last <= self.exports[-1][1])):
            self.regular_expression()
            self.division = False
        else:
            while last > self.S:
                last -= 1
                if src[last] in _BR_WS_PUNCT_NOT_DOT:
                    break
            if src[last] in _WS_NOT_BR:
                while last > self.S:
                    last -= 1
                    if src[last] not in _WS_NOT_BR:
                        break
                if self.is_break_or_continue(last):
                    self.regular_expression()
                    self.division = False
                    return False
            self.division = True
        return False

    def consume(self, ch: str) -> bool:
        """consumeToken: one token at ``self.pos``; False on a syntax error."""
        src = self.src
        pos = self.pos
        comment = False
        if ch == "e":
            if self.depth == 0 and self.keyword_start(pos) and src.startswith("xport", pos + 1):
                self.export_statement()
            else:
                self.skip_run()
        elif ch == "i":
            if src[pos + 1] == "m" and self.keyword_start(pos) and src.startswith("port", pos + 2):
                self.import_statement()
            else:
                self.skip_run()
        elif ch == "c":
            if (src[pos + 1] == "l" and self.keyword_start(pos) and src.startswith("ass", pos + 2)
                    and src[pos + 5] in _BR_OR_WS):
                self.next_class = True
            self.skip_run()
        elif ch == "(":
            if not self.push(_ANY_PAREN, self.last):
                return False
        elif ch == "[":
            if not self.push(_ANY_BRACKET, self.last):
                return False
        elif ch == "]":
            if self.depth == 0:
                self.syntax_error()
                return False
            self.depth -= 1
        elif ch == ",":
            if self.dyn and self.depth > 0 and self.tok[self.depth - 1] == _IMPORT_PAREN:
                current = self.dyn[-1]
                if current.end is None:
                    current.end = self.last + 1
                    self.pos += 1
                    self.comment_whitespace(True)
                    self.pos -= 1
        elif ch == ")":
            if self.depth == 0:
                self.syntax_error()
                return False
            self.depth -= 1
            if self.dyn and self.tok[self.depth] == _IMPORT_PAREN:
                current = self.dyn[-1]
                if current.end is None:
                    current.end = self.last + 1
                current.statement_end = self.pos + 1
                self.dyn.pop()
        elif ch == "{":
            # A dynamic import followed by { is a method named import: drop it.
            last = self.last
            if src[last] == ")" and self.imports and self.imports[-1].statement_end == last + 1:
                if self.head_last is None:
                    del self.imports[:]
                else:
                    del self.imports[self.head_last + 1:]
            if not self.push(_CLASS_BRACE if self.next_class else _ANY_BRACE, self.last):
                return False
            self.next_class = False
        elif ch == "}":
            if self.depth == 0:
                self.syntax_error()
                return False
            self.depth -= 1
            if self.tok[self.depth] == _TEMPLATE_BRACE:
                self.template_string()
        elif ch == "'" or ch == '"':
            self.string_literal(ch)
        elif ch == "/":
            comment = self.handle_slash()
        elif ch == "`":
            if not self.push(_TEMPLATE, self.last):
                return False
            self.template_string()
        elif _is_run_char(ch):
            self.skip_run()
        if not comment:
            self.last = self.pos
        return True

    def parse(self) -> bool:
        src, end = self.src, self.end
        # The tokenizer (the minimal build has no module-only facade pass). A
        # stretch without a special character changes no state but the
        # last-token position, so it is skipped whole.
        while self.pos < end:
            p = self.pos + 1
            m = (_NEXT_COMMA if self.dyn else _NEXT).search(src, p, end + 1)
            q = m.start() if m is not None else end + 1
            if q > p:
                k = q - 1
                while k >= p and src[k] in _MAIN_WS:
                    k -= 1
                if k >= p:
                    self.last = k
            if m is None:
                self.pos = end + 1
                break
            self.pos = q
            if not self.consume(src[q]):
                return False
        return not (self.depth or self.error or self.dyn)

    # -- statements ---------------------------------------------------------
    def import_statement(self) -> None:
        src, end = self.src, self.end
        start = self.pos
        self.pos += 6
        ch = self.comment_whitespace(True)
        maybe_phase = self.pos
        phase = 0
        if ch == ".":
            self.pos += 1
            ch = self.comment_whitespace(True)
            member_ok = self.is_spread(self.last) or src[self.last] != "."
            if ch == "m" and src.startswith("eta", self.pos + 1) and member_ok:
                self.add_import(start, self.pos + 4, _META)
                return
            if ch == "s" and src.startswith("ource", self.pos + 1) and member_ok:
                phase = 1
                self.pos += 6
                ch = self.comment_whitespace(True)
            elif ch == "d" and src.startswith("efer", self.pos + 1) and member_ok:
                phase = 2
                self.pos += 5
                ch = self.comment_whitespace(True)
            else:
                return
        elif (self.pos > start + 6 and ch == "s" and src.startswith("ource", self.pos + 1)
              and src[self.pos + 6] in _BR_OR_WS):
            phase = 1
            self.pos += 6
            ch = self.comment_whitespace(True)
            if self.pos == maybe_phase + 6 or ch == "," or (ch == "f" and src.startswith("rom", self.pos + 1)
                                                            and src[self.pos + 4] in _BR_WS_PUNCT_NOT_DOT):
                self.pos = maybe_phase
                ch = src[self.pos]
                phase = 0
        elif (self.pos > start + 5 and ch == "d" and src.startswith("efer", self.pos + 1)
              and src[self.pos + 5] in _BR_OR_WS):
            phase = 2
            self.pos += 5
            ch = self.comment_whitespace(True)
            if ch != "*":
                self.pos = maybe_phase
                ch = src[self.pos]
                phase = 0

        if ch == "(":
            if not self.push(_IMPORT_PAREN, self.pos):
                return
            if src[self.last] == ".":
                return
            if len(self.dyn) == _DYNAMIC_IMPORT_STACK_SIZE:
                self.syntax_error()
                return
            dynamic = self.pos
            self.pos += 1
            ch = self.comment_whitespace(True)
            record = self.add_import(self.pos, None, dynamic)
            self.dyn.append(record)
            if ch == "'" or ch == '"':
                self.string_literal(ch)
            elif ch == "`" and self.no_substitution_template():
                pass
            else:
                self.pos -= 1
                return
            self.pos += 1
            end_pos = self.pos
            ch = self.comment_whitespace(True)
            if ch == ",":
                self.pos += 1
                self.comment_whitespace(True)
                record.end = end_pos
                record.safe = True
                self.pos -= 1
            elif ch == ")":
                self.depth -= 1
                record.end = end_pos
                record.statement_end = self.pos + 1
                record.safe = True
                self.dyn.pop()
            else:
                self.pos -= 1
            return

        if ch == "{" and phase == 0:
            if self.depth != 0:
                self.pos -= 1
                return
            while self.pos < end:
                ch = self.comment_whitespace(True)
                if ch == "'" or ch == '"':
                    self.string_literal(ch)
                elif ch == "}":
                    self.pos += 1
                    break
                self.pos += 1
            ch = self.comment_whitespace(True)
            if ch == "f" and not src.startswith("rom", self.pos + 1):
                self.syntax_error()
                return
            self.pos += 4
            ch = self.comment_whitespace(True)
            if ch != "'" and ch != '"':
                self.syntax_error()
                return
            self.read_import_string(start, ch)
            return

        if not (ch == '"' or ch == "'" or ch == "*"):
            if self.pos == start + 6:
                self.pos -= 1
                return
        if (phase == 2 and ch != "*") or self.depth != 0:
            self.pos -= 1
            return
        while self.pos < end:
            ch = src[self.pos]
            if ch == "'" or ch == '"':
                self.read_import_string(start, ch)
                return
            self.pos += 1
        self.syntax_error()

    def read_import_string(self, statement_start: int, ch: str) -> None:
        src = self.src
        start = self.pos + 1
        if ch == "'" or ch == '"':
            self.string_literal(ch)
        else:
            self.syntax_error()
            return
        record = self.add_import(start, self.pos, _STANDARD)
        self.pos += 1
        ch = self.comment_whitespace(True)
        p = self.pos
        if not (ch == "w" and src[p + 1] == "i" and src[p + 2] == "t" and src[p + 3] == "h"):
            self.pos -= 1
            return
        attr_index = p
        self.pos += 4
        ch = self.comment_whitespace(True)
        if ch != "{":
            self.pos = attr_index
            return
        self.pos += 1
        while True:
            ch = self.comment_whitespace(True)
            if ch == "}":
                break
            if ch == "'" or ch == '"':
                self.string_literal(ch)
                self.pos += 1
                ch = self.comment_whitespace(True)
            else:
                ch = self.read_to_ws_or_punct()
                if ch != ":":
                    ch = self.comment_whitespace(True)
            if ch != ":":
                self.pos = attr_index
                return
            self.pos += 1
            ch = self.comment_whitespace(True)
            if ch == "'" or ch == '"':
                self.string_literal(ch)
            else:
                self.pos = attr_index
                return
            self.pos += 1
            ch = self.comment_whitespace(True)
            if ch == ",":
                self.pos += 1
                continue
            if ch == "}":
                break
            self.pos = attr_index
            return
        record.statement_end = self.pos + 1

    def export_statement(self) -> None:
        src, end = self.src, self.end
        statement_start = self.pos
        first_export = len(self.exports)  # prev_export_write_head's next
        self.pos += 6
        cur = self.pos
        ch = self.comment_whitespace(True)
        if self.pos == cur and ch not in _PUNCT:
            return
        if ch == "{":
            self.pos += 1
            ch = self.comment_whitespace(True)
            while True:
                start = self.pos
                if ch != "'" and ch != '"':
                    ch = self.read_to_ws_or_punct()
                else:
                    self.string_literal(ch)
                    self.pos += 1
                end_pos = self.pos
                self.comment_whitespace(True)
                ch = self.read_export_as(start, end_pos)
                if ch == ",":
                    self.pos += 1
                    ch = self.comment_whitespace(True)
                if ch == "}":
                    break
                if self.pos == start or self.pos > end:
                    self.syntax_error()
                    return
            self.pos += 1
            ch = self.comment_whitespace(True)
        elif ch == "*":
            self.pos += 1
            self.comment_whitespace(True)
            ch = self.read_export_as(self.pos, self.pos)
            ch = self.comment_whitespace(True)
        else:
            if ch == "d":
                # export default async? function*? name? / class name?
                start = self.pos
                self.pos += 7
                ch = self.comment_whitespace(True)
                local_name = False
                function = False
                if ch == "a":
                    if src.startswith("sync", self.pos + 1) and src[self.pos + 5] in _WS_NOT_BR:
                        self.pos += 5
                        ch = self.comment_whitespace(False)
                        function = True
                elif ch == "f":
                    function = True
                elif ch == "c":
                    after = src[self.pos + 5]
                    if src.startswith("lass", self.pos + 1) and (after in _BR_OR_WS or after == "{"):
                        self.pos += 5
                        ch = self.comment_whitespace(True)
                        if ch != "{":
                            local_name = True
                if function:
                    after = src[self.pos + 8]
                    if src.startswith("unction", self.pos + 1) and (after in _BR_OR_WS or after in "*("):
                        self.pos += 8
                        ch = self.comment_whitespace(True)
                        if ch == "*":
                            self.pos += 1
                            ch = self.comment_whitespace(True)
                        if ch != "(":
                            local_name = True
                if local_name:
                    local_start = self.pos
                    self.read_to_ws_or_punct()
                    if self.pos > local_start:
                        self.add_export(start, start + 7, local_start, self.pos)
                        self.pos -= 1
                        return
                self.add_export(start, start + 7, None, None)
                self.pos = start + 6
                return
            if ch == "a" or ch == "f":
                # export async? function*? name
                if ch == "a":
                    self.pos += 5
                    self.comment_whitespace(False)
                self.pos += 8
                ch = self.comment_whitespace(True)
                if ch == "*":
                    self.pos += 1
                    self.comment_whitespace(True)
                start = self.pos
                self.read_to_ws_or_punct()
                self.add_export(start, self.pos, start, self.pos)
                self.pos -= 1
                return
            if ch == "c":
                if src.startswith("lass", self.pos + 1) and src[self.pos + 5] in _BR_WS_PUNCT_NOT_DOT:
                    self.pos += 5
                    self.comment_whitespace(True)
                    start = self.pos
                    self.read_to_ws_or_punct()
                    self.add_export(start, self.pos, start, self.pos)
                    self.pos -= 1
                    return
                self.pos += 2  # const: falls through to the binding list
                ch = "v"
            if ch == "v" or ch == "l":
                # export var/let/const binding (, binding)*
                self.pos += 3
                ch = self.comment_whitespace(True)
                while self.pos <= end:
                    binding_start = self.pos
                    ch = self.read_binding_target(ch)
                    if self.pos == binding_start:
                        break
                    if ch == "=":
                        ch = self.skip_expression(True)
                    if ch != ",":
                        break
                    self.pos += 1
                    ch = self.comment_whitespace(True)
                self.pos -= 1
                return
            return

        if ch == "f" and src.startswith("rom", self.pos + 1):
            self.pos += 4
            self.read_import_string(statement_start, self.comment_whitespace(True))
            if self.error:
                return
            # The clause names are imported names, not local bindings.
            for index in range(first_export, len(self.exports)):
                self.exports[index] = self.exports[index][:2] + (None, None)
        else:
            self.pos -= 1

    def read_export_as(self, start: int, end_pos: int) -> str:
        ch = self.src[self.pos]
        local_start, local_end = (None, None) if start == end_pos else (start, end_pos)
        if ch == "a":
            self.pos += 2
            ch = self.comment_whitespace(True)
            start = self.pos
            if ch != "'" and ch != '"':
                ch = self.read_to_ws_or_punct()
            else:
                self.string_literal(ch)
                self.pos += 1
            end_pos = self.pos
            ch = self.comment_whitespace(True)
        if self.pos != start:
            self.add_export(start, end_pos, local_start, local_end)
        return ch

    def read_binding_target(self, ch: str) -> str:
        if ch == "{" or ch == "[":
            self.read_binding_pattern()
            self.pos += 1
        else:
            name_start = self.pos
            self.read_to_ws_or_punct()
            if self.pos > name_start:
                self.add_export(name_start, self.pos, name_start, self.pos)
        return self.comment_whitespace(True)

    def read_binding_pattern(self) -> None:
        src, end = self.src, self.end
        is_object = src[self.pos] == "{"
        close = "}" if is_object else "]"
        self.pos += 1
        ch = self.comment_whitespace(True)
        while ch != close and self.pos <= end:
            if ch == "." and src[self.pos + 1] == "." and src[self.pos + 2] == ".":
                self.pos += 3
                ch = self.comment_whitespace(True)
                ch = self.read_binding_target(ch)
                continue
            if is_object:
                key_start = key_end = self.pos
                if ch == "[":
                    self.skip_expression(False)
                    self.pos += 1
                    ch = self.comment_whitespace(True)
                elif ch == "'" or ch == '"':
                    self.string_literal(ch)
                    self.pos += 1
                    ch = self.comment_whitespace(True)
                elif "0" <= ch <= "9":
                    self.pos += 1
                    ch = src[self.pos]
                    while ("0" <= ch <= "9" or ch in "._eEnxXbBoO" or "a" <= ch <= "f" or "A" <= ch <= "F"
                           or (ch in "+-" and src[self.pos - 1] in "eE")):
                        self.pos += 1
                        ch = src[self.pos]
                    ch = self.comment_whitespace(True)
                else:
                    self.read_to_ws_or_punct()
                    key_end = self.pos
                    ch = self.comment_whitespace(True)
                if ch == ":":
                    self.pos += 1
                    ch = self.comment_whitespace(True)
                    ch = self.read_binding_target(ch)
                elif key_end > key_start:
                    self.add_export(key_start, key_end, key_start, key_end)
            elif ch == ",":
                self.pos += 1
                ch = self.comment_whitespace(True)
                continue
            else:
                ch = self.read_binding_target(ch)
            if ch == "=":
                ch = self.skip_expression(False)
            if ch == ",":
                self.pos += 1
                ch = self.comment_whitespace(True)
            else:
                break

    def skip_expression(self, asi: bool) -> str:
        """Skip an initializer to its depth-0 terminator, cursor AT it."""
        src, end = self.src, self.end
        base = self.depth
        last_was_value = False
        self.last = self.pos
        while self.pos < end:
            self.pos += 1
            ch = src[self.pos]
            if ch in _WS_NOT_BR:
                continue
            if self.depth == base:
                if ch in ",;)]}":
                    return ch
                if asi and last_was_value and (ch == "\n" or ch == "\r"):
                    return ch
            if ch == "\n" or ch == "\r":
                continue
            before = self.last
            self.consume(ch)
            if self.error:
                return "\0"
            if self.last == before:
                here = src[self.pos]
                if asi and self.depth == base and last_was_value and (here == "\n" or here == "\r"):
                    return here
            elif ch == "/":
                last_was_value = not self.division
            else:
                last_was_value = _is_value_char(ch) or ch in ")]}'\"`"
        self.pos += 1
        return "\0"


# -- the specifier value, as es-module-lexer's decodeStringLiteral yields it --
_SIMPLE_ESCAPES = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}
_HEX = frozenset("0123456789abcdefABCDEF")
_LINE_TERMINATORS = "\n\r\u2028\u2029"


def _decode_literal(body: str, template: bool) -> str | None:
    """The value of a validated string or no-substitution template literal's
    contents; None where es-module-lexer's decode throws (an invalid escape)."""
    if template:
        # Raw line endings normalise; an escaped one is a line continuation.
        body = body.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    i, n = 0, len(body)
    while i < n:
        c = body[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        i += 1
        if i >= n:
            return None
        c = body[i]
        i += 1
        if c in _LINE_TERMINATORS:
            if c == "\r" and i < n and body[i] == "\n":
                i += 1
        elif c in _SIMPLE_ESCAPES:
            out.append(_SIMPLE_ESCAPES[c])
        elif c == "x":
            digits = body[i:i + 2]
            if len(digits) != 2 or not set(digits) <= _HEX:
                return None
            out.append(chr(int(digits, 16)))
            i += 2
        elif c == "u":
            if body[i:i + 1] == "{":
                close = body.find("}", i + 1)
                digits = body[i + 1:close] if close > 0 else ""
                if not digits or not set(digits) <= _HEX or int(digits, 16) > 0x10FFFF:
                    return None
                out.append(chr(int(digits, 16)))
                i = close + 1
            else:
                digits = body[i:i + 4]
                if len(digits) != 4 or not set(digits) <= _HEX:
                    return None
                out.append(chr(int(digits, 16)))
                i += 4
        elif c == "0":
            if i < n and "0" <= body[i] <= "9":
                return None  # a legacy octal escape
            out.append("\0")
        elif "1" <= c <= "9":
            return None  # a legacy octal or \8 \9 escape
        else:
            out.append(c)
    value = "".join(out)
    # JavaScript strings are UTF-16: an escaped surrogate pair is one character.
    return value.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")


def _parse_error(source: str, position: int) -> LexError:
    line = source.count("\n", 0, position) + 1
    column = position - source.rfind("\n", 0, position)
    return LexError(f"Parse error @:{line}:{column}", position)


def scan_imports(source: str) -> tuple[ImportRecord, ...]:
    """Every import of ``source`` except ``import.meta``, in source order.

    Raises :class:`LexError` where es-module-lexer reports a parse error:
    the lexer's own, then the first quoted specifier and then the first
    quoted export name whose escapes do not decode.
    """
    lexer = _Lexer(source)
    try:
        ok = lexer.parse()
    except IndexError:  # past the sentinels: only truncated input reads there
        ok, lexer.error_pos = False, len(source)
    if not ok:
        raise _parse_error(source, lexer.error_pos)
    S = lexer.S
    records: list[ImportRecord] = []
    for item in lexer.imports:
        if item.dynamic == _META:
            continue
        if item.dynamic == _STANDARD:
            start, end = item.start - S, item.end - S
            name = _decode_literal(source[start:end], False)
            if name is None:
                raise _parse_error(source, start)
            records.append(ImportRecord(start, end, name, False))
        elif item.safe:
            start, end = item.start - S, item.end - S
            name = _decode_literal(source[start + 1:end - 1], source[start] == "`")
            if name is None:
                raise _parse_error(source, start)
            records.append(ImportRecord(start + 1, end - 1, name, True))
        else:
            start = item.start - S
            end = (item.end if item.end is not None else item.start) - S
            records.append(ImportRecord(start, end, None, True))
    for start, end, local_start, local_end in lexer.exports:
        for first, last in ((local_start, local_end), (start, end)):
            if first is None:
                continue
            first, last = first - S, last - S
            if (first < len(source) and source[first] in "'\""
                    and _decode_literal(source[first + 1:last - 1], False) is None):
                raise _parse_error(source, first)
    return tuple(records)
