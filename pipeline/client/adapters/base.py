from __future__ import annotations

from pathlib import Path
from typing import Protocol


class CaptureAdapter(Protocol):
    def health(self) -> dict:
        """Report readiness for a new recording.

        Adapters that observe externally started recordings may also expose
        ``controller_ready`` and ``controller_reasons``. Those describe whether
        a dataset may be selected for controller recording, including an active
        session that the adapter has identified as eligible for adoption.
        """
        ...

    def start(self, session_id: str, raw_root: Path, instruction: str) -> dict: ...
    def stop(self, session_id: str) -> dict: ...
    def status(self) -> dict: ...
    def frame(self, camera_id: str) -> bytes | None: ...
    def close(self): ...
