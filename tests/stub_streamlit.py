"""A stand-in ``streamlit`` (and ``pandas``) so the page scripts can run without a browser.

What this checks: that each page's own code runs end to end (imports, names, calls into
``bank_analyzer``, the shapes of data handed to widgets). What it cannot check: whether
Streamlit's real API is used correctly or how a page looks. ``tests/ui/test_pages_apptest.py``
covers that with Streamlit's own test runner when Streamlit is installed.

Widgets return neutral defaults (first option, unchecked, empty text). A test can name buttons and
checkboxes to "click" and provide an uploaded file.
"""

from __future__ import annotations

import datetime
import types
from collections.abc import Iterable
from typing import Any


class _Selection(list):  # type: ignore[type-arg]
    def tolist(self) -> list[Any]:
        return list(self)


class _Loc:
    def __init__(self, frame: FakeFrame) -> None:
        self._frame = frame

    def __getitem__(self, key: tuple[Iterable[bool], str]) -> _Selection:
        mask, column = key
        return _Selection(v for v, m in zip(self._frame.cols[column], mask, strict=False) if m)


class FakeFrame:
    """The small part of a pandas DataFrame the pages use."""

    def __init__(self, data: Any = None, index: Any = None) -> None:
        self.index = list(index) if index is not None else None
        if isinstance(data, list):
            names = list(data[0]) if data else []
            self.cols = {n: [row[n] for row in data] for n in names}
        else:
            self.cols = {k: list(v) for k, v in (data or {}).items()}
        self.columns = list(self.cols)
        self.loc = _Loc(self)

    def insert(self, position: int, name: str, value: Any) -> None:
        length = len(next(iter(self.cols.values()), []))
        self.cols[name] = [value] * length
        self.columns = list(self.cols)

    def __getitem__(self, name: str) -> list[Any]:
        return self.cols[name]


class _Context:
    """Usable as ``with``; any attribute is a harmless callable (st.sidebar, columns, tabs)."""

    def __init__(self, st: FakeStreamlit) -> None:
        self._st = st

    def __enter__(self) -> _Context:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def __getattr__(self, name: str) -> Any:
        return self._st.widget(name)


class FakeStreamlit(types.ModuleType):
    def __init__(
        self,
        *,
        clicks: Iterable[str] = (),
        upload: tuple[str, bytes] | None = None,
    ) -> None:
        super().__init__("streamlit")
        self.session_state: dict[str, Any] = {}
        self.sidebar = _Context(self)
        self.clicks = set(clicks)
        self.upload = upload
        self.calls: list[str] = []
        self.clicked: list[str] = []
        self.shown: list[Any] = []  # data handed to dataframe/table widgets

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        return self.widget(name)

    def widget(self, name: str) -> Any:
        def call(*args: Any, **kw: Any) -> Any:
            self.calls.append(name)
            label = args[0] if args and isinstance(args[0], str) else ""
            if name == "columns":
                n = args[0] if args else 1
                return [_Context(self) for _ in range(len(n) if isinstance(n, list | tuple) else n)]
            if name == "tabs":
                return [_Context(self) for _ in args[0]]
            if name in ("expander", "form", "spinner"):
                return _Context(self)
            if name in ("selectbox", "radio"):
                options = list(args[1] if len(args) > 1 else kw["options"])
                return options[kw.get("index", 0)]
            if name == "select_slider":
                return kw.get("value")
            if name in ("checkbox", "button", "form_submit_button", "toggle"):
                if label in self.clicks:
                    self.clicked.append(label)
                    return True
                return False
            if name == "text_input":
                return kw.get("value", "")
            if name == "number_input":
                return kw.get("value", 1)
            if name == "date_input":
                return kw.get("value", datetime.date(2024, 3, 20))
            if name == "file_uploader":
                if self.upload is None:
                    return None
                file_name, data = self.upload
                return types.SimpleNamespace(name=file_name, getvalue=lambda: data)
            if name == "data_editor":
                return args[0]
            if name in ("dataframe", "table", "bar_chart"):
                data = args[0]
                if isinstance(data, list):
                    assert all(isinstance(row, dict) for row in data), "table rows must be dicts"
                self.shown.append(data)
            return None

        return call


def fake_pandas() -> types.ModuleType:
    module = types.ModuleType("pandas")
    module.DataFrame = FakeFrame  # type: ignore[attr-defined]
    return module
