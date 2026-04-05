"""Tests for blackboxrs.ros_monitor.frequency_tracker."""

from __future__ import annotations

import time

import pytest

from blackboxrs.ros_monitor.frequency_tracker import FrequencyTracker


class TestFrequencyTrackerRecord:
    """Test recording messages and computing frequency."""

    def test_single_record_returns_none(self):
        tracker = FrequencyTracker(window_sec=5.0)
        tracker.record("/cmd_vel")
        assert tracker.get_frequency("/cmd_vel") is None

    def test_two_records_returns_frequency(self):
        tracker = FrequencyTracker(window_sec=5.0)
        tracker.record("/cmd_vel")
        time.sleep(0.05)
        tracker.record("/cmd_vel")
        freq = tracker.get_frequency("/cmd_vel")
        assert freq is not None
        assert freq > 0

    def test_multiple_records_reasonable_frequency(self):
        tracker = FrequencyTracker(window_sec=5.0)
        # Simulate ~100 Hz by recording 10 messages over ~0.1s
        for _ in range(10):
            tracker.record("/scan")
            time.sleep(0.01)
        freq = tracker.get_frequency("/scan")
        assert freq is not None
        # Should be roughly 100 Hz, allow wide tolerance for CI timing
        assert 30 < freq < 300

    def test_get_all_frequencies(self):
        tracker = FrequencyTracker(window_sec=5.0)
        for _ in range(3):
            tracker.record("/cmd_vel")
            tracker.record("/scan")
            time.sleep(0.01)
        freqs = tracker.get_all_frequencies()
        assert "/cmd_vel" in freqs
        assert "/scan" in freqs


class TestFrequencyTrackerWindow:
    """Test sliding window expiration."""

    def test_old_records_expire(self):
        tracker = FrequencyTracker(window_sec=0.1)  # 100ms window
        tracker.record("/cmd_vel")
        time.sleep(0.05)
        tracker.record("/cmd_vel")
        # Should have data now
        assert tracker.get_frequency("/cmd_vel") is not None
        # Wait for window to expire
        time.sleep(0.15)
        # After expiration, insufficient data
        assert tracker.get_frequency("/cmd_vel") is None

    def test_invalid_window_raises(self):
        with pytest.raises(ValueError):
            FrequencyTracker(window_sec=0)
        with pytest.raises(ValueError):
            FrequencyTracker(window_sec=-1.0)


class TestFrequencyTrackerLatency:
    """Test latency estimate computation."""

    def test_latency_estimate_returns_ms(self):
        tracker = FrequencyTracker(window_sec=5.0)
        for _ in range(5):
            tracker.record("/cmd_vel")
            time.sleep(0.01)
        latency = tracker.get_latency_estimate("/cmd_vel")
        assert latency is not None
        assert latency > 0  # milliseconds

    def test_latency_is_reciprocal_of_frequency(self):
        tracker = FrequencyTracker(window_sec=5.0)
        for _ in range(5):
            tracker.record("/cmd_vel")
            time.sleep(0.01)
        freq = tracker.get_frequency("/cmd_vel")
        latency = tracker.get_latency_estimate("/cmd_vel")
        assert freq is not None and latency is not None
        expected_latency = (1.0 / freq) * 1000.0
        assert abs(latency - expected_latency) < 0.01


class TestFrequencyTrackerUnknownTopic:
    """Test behavior with unknown topics."""

    def test_unknown_topic_frequency_is_none(self):
        tracker = FrequencyTracker()
        assert tracker.get_frequency("/nonexistent") is None

    def test_unknown_topic_latency_is_none(self):
        tracker = FrequencyTracker()
        assert tracker.get_latency_estimate("/nonexistent") is None

    def test_get_all_frequencies_empty_tracker(self):
        tracker = FrequencyTracker()
        assert tracker.get_all_frequencies() == {}
