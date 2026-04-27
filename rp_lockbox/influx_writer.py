"""Async InfluxDB 3 writer for lock telemetry.

Provides a daemon thread that accepts metric dicts from the BLACS tab and
writes them to InfluxDB at a decimated rate (default: 1 point per 10 s per
channel).  If ``INFLUXDB_URL`` is not set, the writer is a no-op stub.

Configuration via environment variables:
    INFLUXDB_URL        InfluxDB host (e.g. ``http://localhost:8181``).
    INFLUXDB_TOKEN      Auth token with write access.
    INFLUXDB_DATABASE   Target database name.
    LOCKBOX_INFLUX_INTERVAL  Seconds between writes per channel (default 10).
    LOCKBOX_ERROR_THRESHOLD  error_rms threshold for ``locked`` field (default 0.05).
"""

import logging
import os
import queue
import threading
import time

LOG = logging.getLogger(__name__)

_INFLUXDB_URL = os.environ.get('INFLUXDB_URL', '')
_INFLUXDB_TOKEN = os.environ.get('INFLUXDB_TOKEN', '')
_INFLUXDB_DATABASE = os.environ.get('INFLUXDB_DATABASE', 'rp_lockbox')
_WRITE_INTERVAL = float(os.environ.get('LOCKBOX_INFLUX_INTERVAL', '10'))
_ERROR_THRESHOLD = float(os.environ.get('LOCKBOX_ERROR_THRESHOLD', '0.05'))

MEASUREMENT = 'rp_lockbox'


def get_error_threshold():
    return _ERROR_THRESHOLD


class InfluxWriterThread(threading.Thread):
    """Daemon thread that drains a queue of metric dicts into InfluxDB 3.

    Call :meth:`put` from any thread (e.g. a BLACS tab tick handler).
    Points are time-decimated: only one point per *interval* seconds per
    channel is actually written; intermediate calls are silently dropped.
    """

    def __init__(self, host_tag='', device_tag=''):
        super().__init__(daemon=True, name='influx-writer')
        self._queue = queue.Queue(maxsize=100)
        self._stop_event = threading.Event()
        self._host_tag = host_tag
        self._device_tag = device_tag
        self._last_write = {}  # channel -> monotonic timestamp
        self._client = None

    @property
    def enabled(self):
        return bool(_INFLUXDB_URL)

    def put(self, point_dict):
        """Submit a metric dict for writing.

        Expected keys: ``channel`` (str), ``error_rms`` (float),
        ``output_mean`` (float).  Extra keys are ignored.

        Time-decimation happens here: if less than *interval* seconds have
        elapsed since the last write for this channel, the point is dropped.
        """
        if not self.enabled:
            return
        ch = point_dict.get('channel', '?')
        now = time.monotonic()
        last = self._last_write.get(ch, 0.0)
        if (now - last) < _WRITE_INTERVAL:
            return
        self._last_write[ch] = now
        try:
            self._queue.put_nowait(point_dict)
        except queue.Full:
            LOG.warning('InfluxDB write queue full -- dropping point')

    def stop(self):
        self._stop_event.set()
        self.join(timeout=5.0)

    def run(self):
        if not self.enabled:
            return
        try:
            from influxdb_client_3 import InfluxDBClient3
        except ImportError:
            LOG.warning(
                'influxdb3-python not installed; telemetry disabled '
                '(pip install influxdb3-python)',
            )
            return

        try:
            self._client = InfluxDBClient3(
                host=_INFLUXDB_URL,
                token=_INFLUXDB_TOKEN,
                database=_INFLUXDB_DATABASE,
            )
            LOG.info('InfluxDB writer connected to %s (db=%s)', _INFLUXDB_URL, _INFLUXDB_DATABASE)
        except Exception:
            LOG.exception('Failed to connect to InfluxDB at %s', _INFLUXDB_URL)
            return

        while not self._stop_event.is_set():
            try:
                point_dict = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            try:
                self._write_point(point_dict)
            except Exception:
                LOG.exception('InfluxDB write failed')

        self._flush_remaining()
        try:
            self._client.close()
        except Exception:
            pass

    def _write_point(self, point_dict):
        error_rms = point_dict.get('error_rms')
        output_mean = point_dict.get('output_mean')
        if error_rms is None or output_mean is None:
            return
        locked = error_rms < _ERROR_THRESHOLD
        record = {
            'measurement': MEASUREMENT,
            'tags': {
                'channel': str(point_dict.get('channel', '?')),
                'host': self._host_tag,
                'device': self._device_tag,
            },
            'fields': {
                'error_rms': float(error_rms),
                'output_mean': float(output_mean),
                'locked': locked,
            },
        }
        self._client.write(record=record)

    def _flush_remaining(self):
        while not self._queue.empty():
            try:
                point_dict = self._queue.get_nowait()
                self._write_point(point_dict)
            except (queue.Empty, Exception):
                break


class _NoOpWriter:
    """Drop-in stub when InfluxDB is not configured."""

    enabled = False

    def put(self, point_dict):
        pass

    def start(self):
        pass

    def stop(self):
        pass


def create_writer(host_tag='', device_tag=''):
    """Factory: returns an InfluxWriterThread if configured, else a no-op stub."""
    if _INFLUXDB_URL:
        return InfluxWriterThread(host_tag=host_tag, device_tag=device_tag)
    return _NoOpWriter()
