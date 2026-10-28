"""Headers: an ordered, case-insensitive multimap with lower-case names."""

from __future__ import annotations

from collections.abc import Iterable, Iterator


class Headers:
    """Field names are stored lower-case; values are str (decoded as Latin-1)."""

    def __init__(self, items: Iterable[tuple[str, str]] = ()) -> None:
        self._items: list[tuple[str, str]] = [(k.lower(), v) for k, v in items]

    @classmethod
    def from_bytes(cls, items: Iterable[tuple[bytes, bytes]]) -> Headers:
        return cls((k.decode("latin-1"), v.decode("latin-1")) for k, v in items)

    def to_bytes(self) -> list[tuple[bytes, bytes]]:
        return [(k.encode("latin-1"), v.encode("latin-1")) for k, v in self._items]

    def get(self, name: str, default: str | None = None) -> str | None:
        """The first value for name, or default."""
        name = name.lower()
        for k, v in self._items:
            if k == name:
                return v
        return default

    def get_all(self, name: str) -> list[str]:
        name = name.lower()
        return [v for k, v in self._items if k == name]

    def set(self, name: str, value: str) -> None:
        """Replace every value of name with one value (appended if it was absent)."""
        name = name.lower()
        for i, (k, _) in enumerate(self._items):
            if k == name:
                self._items[i] = (name, value)
                self._items = [
                    item for j, item in enumerate(self._items) if j <= i or item[0] != name
                ]
                return
        self._items.append((name, value))

    def add(self, name: str, value: str) -> None:
        self._items.append((name.lower(), value))

    def remove(self, name: str) -> None:
        name = name.lower()
        self._items = [item for item in self._items if item[0] != name]

    def items(self) -> list[tuple[str, str]]:
        return list(self._items)

    def copy(self) -> Headers:
        return Headers(self._items)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and any(k == name.lower() for k, _ in self._items)

    def __iter__(self) -> Iterator[tuple[str, str]]:
        return iter(list(self._items))

    def __len__(self) -> int:
        return len(self._items)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Headers) and self._items == other._items

    __hash__ = None  # type: ignore[assignment]  # mutable, like list

    def __repr__(self) -> str:
        return f"Headers({self._items!r})"
