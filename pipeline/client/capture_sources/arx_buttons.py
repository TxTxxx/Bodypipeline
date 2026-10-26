"""Observe ARX gravity start, then home -> return-and-save for each episode.

This class never publishes commands. Its caller serializes access with the
observation source lock. Times are host monotonic receipt times.
"""

from __future__ import annotations


class ArxButtonSequence:
    def __init__(self):
        self.last = None
        self.seen_at = None
        self.armed_at = None
        self.home_at = None
        self.triggered_at = None
        self.recording_at = None
        self.save_requested_at = None

    def arm(self, at):
        self.armed_at = at
        self.home_at = None
        self.triggered_at = None
        self.recording_at = None
        self.save_requested_at = None

    def disarm(self):
        self.armed_at = None
        self.home_at = None
        self.triggered_at = None
        self.recording_at = None
        self.save_requested_at = None

    def recording_started(self, at):
        self.recording_at = at
        self.save_requested_at = None

    def observe(self, values, at, *, ignore=False):
        if len(values) < 2:
            return
        current = tuple(values[:2])
        previous, self.last = self.last, current
        self.seen_at = at
        # Establish a baseline: a key held before arming is not a new press.
        if ignore or previous is None or self.armed_at is None or at <= self.armed_at:
            return
        if self.recording_at is not None:
            if at > self.recording_at and current[1] == 1 and previous[1] == 0:
                # The controller performs GO_HOME; the recorder waits for arrival.
                if self.save_requested_at is None:
                    self.save_requested_at = at
            return
        if current[1] == 1 and previous[1] == 0:
            self.home_at = at
            self.triggered_at = None
        elif (
            current == (1, 0)
            and previous[0] == 0
        ):
            # If both keys are held, GO_HOME wins in the installed controller.
            self.triggered_at = at

    def status(self):
        return {
            "triggered_at": self.triggered_at,
            "save_requested_at": self.save_requested_at,
            "message": "等待 data[0] 开始下一条录制，准备阶段不录制",
        }
