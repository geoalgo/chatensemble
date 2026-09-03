"""Progress reporting for cache fetches.

``Reporter`` is a silent no-op sink. ``TqdmReporter`` draws a per-month bar over
the channel scan. A plain ``callable(str)`` can also be passed anywhere a reporter
is accepted and it receives one pre-formatted line per event.
"""

from __future__ import annotations

from collections.abc import Callable


class Reporter:
    """No-op progress sink; override the hooks you care about."""

    def month_start(self, account: str, month: str, total: int, live: bool) -> None:
        ...

    def channel(self, account: str, month: str, index: int, total: int, name: str) -> None:
        ...

    def month_done(self, account: str, month: str, count: int) -> None:
        ...

    def close(self) -> None:
        ...


class CallbackReporter(Reporter):
    """Adapts a ``callable(str)`` into the Reporter hooks (one line per event)."""

    def __init__(self, sink: Callable[[str], None]) -> None:
        self._sink = sink

    def month_start(self, account: str, month: str, total: int, live: bool) -> None:
        self._sink(f"{account} {month}  {'fetching…' if live else '(cache)'}")

    def channel(self, account: str, month: str, index: int, total: int, name: str) -> None:
        self._sink(f"{account} {month}  [{index}/{total}]  {name}")


class TqdmReporter(Reporter):
    """A `tqdm` bar per live month: channels as units, current channel as postfix."""

    def __init__(self, **tqdm_kwargs) -> None:
        self._kwargs = {"unit": "ch", "leave": False, "dynamic_ncols": True}
        self._kwargs.update(tqdm_kwargs)
        self._bar = None

    def month_start(self, account: str, month: str, total: int, live: bool) -> None:
        if not live:
            return
        from tqdm import tqdm

        self._bar = tqdm(total=total, desc=f"{account} {month}", **self._kwargs)

    def channel(self, account: str, month: str, index: int, total: int, name: str) -> None:
        if self._bar is not None:
            self._bar.set_postfix_str(name[:28], refresh=False)
            self._bar.update(1)

    def month_done(self, account: str, month: str, count: int) -> None:
        if self._bar is not None:
            self._bar.set_postfix_str(f"{count} msgs")
            self._bar.close()
            self._bar = None

    def close(self) -> None:
        if self._bar is not None:
            self._bar.close()
            self._bar = None


def as_reporter(progress) -> Reporter:
    """Coerce ``True`` / a Reporter / a ``callable(str)`` / ``None`` into a Reporter."""
    if progress is None or progress is False:
        return Reporter()
    if progress is True:
        return TqdmReporter()
    if isinstance(progress, Reporter):
        return progress
    if callable(progress):
        return CallbackReporter(progress)
    raise TypeError(f"unsupported progress value: {progress!r}")
