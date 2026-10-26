"""Explicitly disable capture on a Client configured only for other capabilities."""

from pipeline.shared.core import DomainError


def unavailable(*_args, **_kwargs):
    raise DomainError("当前 Client 未启用采集后端", 503)


class DisabledCaptureAdapter:
    enable = staticmethod(unavailable)
    reset = staticmethod(unavailable)
    start = staticmethod(unavailable)
    stop = staticmethod(unavailable)
    joints = staticmethod(unavailable)

    def health(self):
        return {"ready": False, "detail": "当前 Client 未启用采集后端"}

    def status(self):
        return {"fail": False, "phase": "DISABLED"}

    def frame(self, _camera_id):
        return None

    def close(self):
        pass
