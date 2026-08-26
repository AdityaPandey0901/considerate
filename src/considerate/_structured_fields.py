"""A small RFC 8941 (Structured Field Values) Dictionary parser.

Scope, deliberately: this parses a top-level **Dictionary of bare Items**
(String, Token, Integer, Decimal, Boolean, Byte Sequence) — exactly what
`Considerate-Agent` uses. It does **not** implement Inner Lists or
Parameters (RFC 8941 §3.1.2, §3.1.5); nothing in this protocol's header
needs them, and implementing them fully is a lot of surface area for a
header three fields wide. If a future SPEC.md revision needs them, extend
this rather than hand-rolling a second parser.

This replaces a hand-rolled comma/equals splitter that couldn't correctly
handle an escaped quote inside a value. Serialization (`identity.py`'s
`to_header`) already emits valid SF Dictionary syntax for string members,
so only parsing needed real work.
"""

from __future__ import annotations

SFValue = str | int | float | bool | bytes


def parse_sf_dictionary(value: str) -> dict[str, SFValue]:
    """Parse an RFC 8941 §4.2.2-shaped Dictionary. Raises ValueError on
    anything malformed rather than guessing — a caller should fall back to
    treating the header as absent, not act on a misparse.
    """
    parser = _Parser(value)
    result: dict[str, SFValue] = {}
    parser.skip_ows()
    if parser.eof():
        return result
    while True:
        key = parser.parse_key()
        parser.skip_ows_to_eq_or_comma()
        if parser.peek() == "=":
            parser.advance()
            item = parser.parse_item()
            result[key] = item
        else:
            result[key] = True  # bare key is shorthand for `key=?1`
        parser._skip_parameters()  # accepted but discarded — see module docstring
        parser.skip_ows()
        if parser.eof():
            break
        parser.expect(",")
        parser.skip_ows()
        if parser.eof():
            raise ValueError("trailing comma with no following member")
    return result


class _Parser:
    def __init__(self, s: str) -> None:
        self.s = s
        self.i = 0

    def eof(self) -> bool:
        return self.i >= len(self.s)

    def peek(self) -> str:
        return self.s[self.i] if not self.eof() else ""

    def advance(self) -> str:
        ch = self.peek()
        self.i += 1
        return ch

    def expect(self, ch: str) -> None:
        if self.peek() != ch:
            raise ValueError(f"expected {ch!r} at position {self.i} in {self.s!r}")
        self.advance()

    def skip_ows(self) -> None:
        while self.peek() in (" ", "\t"):
            self.advance()

    def skip_ows_to_eq_or_comma(self) -> None:
        self.skip_ows()

    def parse_key(self) -> str:
        start = self.i
        if not (self.peek().isalpha() or self.peek() == "*"):
            raise ValueError(f"invalid key start at position {self.i} in {self.s!r}")
        while self.peek() and (self.peek().isalnum() or self.peek() in "_-.*"):
            self.advance()
        return self.s[start : self.i]

    def _skip_parameters(self) -> None:
        # Parameters (`;name=value`) are valid SF syntax we choose not to
        # surface — skip them so a header that includes one doesn't fail to
        # parse outright, without pretending we interpreted it.
        while self.peek() == ";":
            self.advance()
            self.skip_ows()
            self.parse_key()
            self.skip_ows()
            if self.peek() == "=":
                self.advance()
                self.parse_item()
            self.skip_ows()

    def parse_item(self) -> SFValue:
        ch = self.peek()
        if ch == '"':
            return self._parse_string()
        if ch == "?":
            return self._parse_boolean()
        if ch == ":":
            return self._parse_byte_sequence()
        if ch == "-" or ch.isdigit():
            return self._parse_number()
        if ch.isalpha() or ch == "*":
            return self._parse_token()
        raise ValueError(f"unexpected character {ch!r} at position {self.i} in {self.s!r}")

    def _parse_string(self) -> str:
        self.expect('"')
        out: list[str] = []
        while True:
            if self.eof():
                raise ValueError("unterminated string")
            ch = self.advance()
            if ch == '"':
                return "".join(out)
            if ch == "\\":
                if self.eof():
                    raise ValueError("dangling escape in string")
                nxt = self.advance()
                if nxt not in ('"', "\\"):
                    raise ValueError(f"invalid escape \\{nxt!r} in string")
                out.append(nxt)
            else:
                out.append(ch)

    def _parse_token(self) -> str:
        start = self.i
        while self.peek() and (self.peek().isalnum() or self.peek() in "_-.:/*!#$%&'^`|~"):
            self.advance()
        return self.s[start : self.i]

    def _parse_boolean(self) -> bool:
        self.expect("?")
        ch = self.advance()
        if ch == "1":
            return True
        if ch == "0":
            return False
        raise ValueError(f"invalid boolean literal ?{ch}")

    def _parse_byte_sequence(self) -> bytes:
        import base64

        self.expect(":")
        start = self.i
        while self.peek() != ":":
            if self.eof():
                raise ValueError("unterminated byte sequence")
            self.advance()
        encoded = self.s[start : self.i]
        self.expect(":")
        return base64.b64decode(encoded)

    def _parse_number(self) -> int | float:
        start = self.i
        if self.peek() == "-":
            self.advance()
        if not self.peek().isdigit():
            raise ValueError(f"invalid number at position {start} in {self.s!r}")
        while self.peek().isdigit():
            self.advance()
        is_decimal = False
        if self.peek() == ".":
            is_decimal = True
            self.advance()
            if not self.peek().isdigit():
                raise ValueError("invalid decimal: no digits after '.'")
            while self.peek().isdigit():
                self.advance()
        text = self.s[start : self.i]
        return float(text) if is_decimal else int(text)
