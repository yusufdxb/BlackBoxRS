"""Optional Prometheus metrics exporter for BlackBoxRS.

Subscribes to the EventBus and maintains Prometheus counters/gauges
that can be scraped by any Prometheus-compatible monitoring stack.
The ``prometheus_client`` library is imported lazily so that it is
only required when the feature is actually enabled.
"""

from __future__ import annotations

import logging
import threading
import time
from queue import Empty, Queue

from blackboxrs.core.config import PrometheusConfig
from blackboxrs.core.event_bus import EventBus
from blackboxrs.core.schemas import BlackBoxEvent

logger = logging.getLogger(__name__)

_DRAIN_TIMEOUT_SEC = 0.25


class PrometheusExporter:
    """Expose BlackBoxRS metrics via a Prometheus HTTP endpoint.

    Follows the same component lifecycle protocol as other BlackBoxRS
    components (``start()`` / ``stop()``).  When started, it subscribes
    globally to the EventBus, spawns a background thread to drain
    events and update metrics, and starts the Prometheus HTTP server.

    Args:
        event_bus: The shared EventBus instance.
        config: A PrometheusConfig with ``enabled``, ``port``, ``host``.
    """

    def __init__(self, event_bus: EventBus, config: PrometheusConfig) -> None:
        self._event_bus = event_bus
        self._config = config
        self._running = False
        self._queue: Queue[BlackBoxEvent] | None = None
        self._thread: threading.Thread | None = None
        self._server: object | None = None
        self._start_time: float = 0.0
        self._metrics_initialized = False

    def _init_metrics(self) -> bool:
        """Lazily import prometheus_client and create metric objects.

        Returns True on success, False if the library is unavailable.
        """
        if self._metrics_initialized:
            return True

        try:
            from prometheus_client import Counter, Gauge, CollectorRegistry

            self._registry = CollectorRegistry()

            self._events_total = Counter(
                "blackboxrs_events_total",
                "Total events processed by BlackBoxRS",
                ["event_type"],
                registry=self._registry,
            )
            self._anomalies_total = Counter(
                "blackboxrs_anomalies_total",
                "Total anomalies detected by BlackBoxRS",
                ["detector_name"],
                registry=self._registry,
            )
            self._uptime_seconds = Gauge(
                "blackboxrs_uptime_seconds",
                "Seconds since BlackBoxRS exporter started",
                registry=self._registry,
            )
            self._topics_monitored = Gauge(
                "blackboxrs_topics_monitored",
                "Number of ROS topics currently monitored",
                registry=self._registry,
            )
            self._cpu_percent = Gauge(
                "blackboxrs_cpu_percent",
                "System CPU utilization percentage",
                registry=self._registry,
            )
            self._memory_percent = Gauge(
                "blackboxrs_memory_percent",
                "System memory utilization percentage",
                registry=self._registry,
            )
            self._gpu_temp_c = Gauge(
                "blackboxrs_gpu_temp_celsius",
                "GPU temperature in Celsius",
                registry=self._registry,
            )

            self._metrics_initialized = True
            return True
        except ImportError:
            logger.warning(
                "prometheus_client is not installed; "
                "install blackboxrs[prometheus] to enable metrics export"
            )
            return False

    def start(self) -> None:
        """Start the exporter: init metrics, start HTTP server, subscribe to bus."""
        if not self._config.enabled:
            logger.info("Prometheus exporter is disabled by configuration")
            return

        if self._running:
            logger.warning("PrometheusExporter.start() called but already running")
            return

        if not self._init_metrics():
            return

        self._start_time = time.monotonic()

        try:
            from prometheus_client import start_http_server

            self._server = start_http_server(
                self._config.port,
                addr=self._config.host,
                registry=self._registry,
            )
            logger.info(
                "Prometheus HTTP server started on %s:%d",
                self._config.host,
                self._config.port,
            )
        except Exception:
            logger.warning(
                "Failed to start Prometheus HTTP server on %s:%d",
                self._config.host,
                self._config.port,
                exc_info=True,
            )
            return

        self._queue = self._event_bus.subscribe()
        self._running = True
        self._thread = threading.Thread(
            target=self._run,
            name="prometheus-exporter",
            daemon=True,
        )
        self._thread.start()
        logger.info("Prometheus exporter started")

    def stop(self) -> None:
        """Stop the exporter and shut down the HTTP server."""
        if not self._running:
            return

        self._running = False

        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

        if self._queue is not None:
            self._event_bus.unsubscribe(self._queue)
            self._queue = None

        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:
                logger.debug("Error shutting down Prometheus HTTP server", exc_info=True)
            self._server = None

        logger.info("Prometheus exporter stopped")

    def _run(self) -> None:
        """Drain events from the bus and update Prometheus metrics."""
        if self._queue is None:
            return

        seen_topics: set[str] = set()

        while self._running:
            self._uptime_seconds.set(time.monotonic() - self._start_time)

            try:
                event = self._queue.get(timeout=_DRAIN_TIMEOUT_SEC)
            except Empty:
                continue

            try:
                self._process_event(event, seen_topics)
            except Exception:
                logger.debug("Error processing event for metrics", exc_info=True)

    def _process_event(self, event: BlackBoxEvent, seen_topics: set[str]) -> None:
        """Update metrics based on a single event."""
        self._events_total.labels(event_type=event.event_type).inc()

        if event.source == "anomaly_engine":
            detector = event.data.get("detector", "unknown")
            self._anomalies_total.labels(detector_name=detector).inc()

        if event.event_type == "system.cpu":
            cpu = event.data.get("cpu_percent")
            if cpu is not None:
                self._cpu_percent.set(float(cpu))

        if event.event_type == "system.memory":
            mem = event.data.get("memory_percent")
            if mem is not None:
                self._memory_percent.set(float(mem))

        if event.event_type == "system.gpu":
            temp = event.data.get("gpu_temp_c")
            if temp is not None:
                self._gpu_temp_c.set(float(temp))

        if event.event_type == "ros.frequency":
            topic = event.data.get("topic")
            if topic and topic not in seen_topics:
                seen_topics.add(topic)
                self._topics_monitored.set(len(seen_topics))
