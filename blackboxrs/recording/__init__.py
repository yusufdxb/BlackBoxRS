"""Recording backends and capture-file adaptors."""

from blackboxrs.recording.rosbag2 import Rosbag2Recorder
from blackboxrs.recording.native_process import NativeCaptureProcess

from .native import (
    NativeCaptureDependencyError,
    NativeCaptureError,
    NativeCaptureEvent,
    NativeCaptureFormatError,
    NativeCaptureIssue,
    NativeCaptureReader,
    resolve_current_native_session,
)

__all__ = [
    "NativeCaptureDependencyError",
    "NativeCaptureError",
    "NativeCaptureEvent",
    "NativeCaptureFormatError",
    "NativeCaptureIssue",
    "NativeCaptureReader",
    "resolve_current_native_session",
    "Rosbag2Recorder",
    "NativeCaptureProcess",
]
