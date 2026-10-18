import threading
import time
from pathlib import Path

import pytest
from test_client import request

from pipeline.client.service import CaptureService
from pipeline.client.upload import UploadAgent
from pipeline.shared.core import DomainError


def test_offline_capture_retained_and_retry_scheduled(settings_factory):
    s = settings_factory(server_url="http://127.0.0.1:1")
    c = CaptureService(s)
    r = c.create(request())
    c.start(r["id"])
    time.sleep(0.1)
    c.stop(r["id"])
    r = c.keep(r["id"])
    assert r["state"] == "SEALED"
    UploadAgent(c).process_once()
    updated = c.store.get("capture", r["id"])
    assert updated["upload_state"] == "RETRY"
    assert updated["retry_at"] > time.time()
    assert Path(updated["raw_root"]).exists()
    assert updated.get("receipt") is None


def test_restart_marks_uncertain_not_ready(settings_factory):
    s = settings_factory()
    c = CaptureService(s)
    r = c.create(request())
    r.update(state="STARTING")
    c.save(r)
    restarted = CaptureService(s)
    restarted.recover()
    assert restarted.store.get("capture", r["id"])["state"] == "RECOVERY_REQUIRED"
    with pytest.raises(DomainError):
        restarted.start(r["id"])


def test_concurrent_start_is_single_call(settings_factory):
    class Adapter:
        starts = 0

        def health(self):
            return {"ready": True}

        def start(self, *args):
            self.starts += 1
            time.sleep(0.1)

    a = Adapter()
    c = CaptureService(settings_factory(), a)
    r = c.create(request())
    threads = [threading.Thread(target=c.start, args=(r["id"],)) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert a.starts == 1
