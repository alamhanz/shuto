"""The cached hardware profile of this machine."""

from __future__ import annotations

import threading

from shuto.ai import hardware

_hw_lock = threading.Lock()


_hw_cache: list[hardware.HardwareProfile] = []


def hardware_profile(refresh: bool = False) -> hardware.HardwareProfile:
    with _hw_lock:
        if refresh or not _hw_cache:
            _hw_cache[:] = [hardware.detect()]
        return _hw_cache[0]
