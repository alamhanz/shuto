"""The cached hardware profile of this machine."""

from __future__ import annotations

import threading

from shuto.ai import hardware


class _HardwareCache:
    """One detected profile, kept for the life of the process (detection runs subprocesses)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._profile: hardware.HardwareProfile | None = None

    def get(self, refresh: bool = False) -> hardware.HardwareProfile:
        with self._lock:
            if refresh or self._profile is None:
                self._profile = hardware.detect()
            return self._profile

    def clear(self) -> None:
        with self._lock:
            self._profile = None


_cache = _HardwareCache()


def hardware_profile(refresh: bool = False) -> hardware.HardwareProfile:
    return _cache.get(refresh)


def clear_cache() -> None:
    """Forget the detected profile (the next call detects again)."""
    _cache.clear()
