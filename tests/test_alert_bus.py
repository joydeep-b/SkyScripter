import threading

import pytest

from sky_scripter.alert_bus import Alert, AlertBus, AlertLevel


def _make_alert(level=AlertLevel.INFO, source="test", code="T001", message="msg"):
    return Alert(level=level, source=source, code=code, message=message)


def test_raise_and_get_pending():
    bus = AlertBus()
    alert = _make_alert()
    bus.raise_alert(alert)
    pending = bus.get_pending()
    assert pending == [alert]
    assert bus.get_pending() == []


def test_emergency_event():
    bus = AlertBus()
    assert not bus.emergency_event.is_set()
    bus.raise_alert(_make_alert(level=AlertLevel.EMERGENCY))
    assert bus.emergency_event.is_set()
    bus.clear_emergency()
    assert not bus.emergency_event.is_set()


def test_history():
    bus = AlertBus()
    alerts = [_make_alert(message=f"msg{i}") for i in range(5)]
    for a in alerts:
        bus.raise_alert(a)
    history = bus.get_history(3)
    assert len(history) == 3
    assert history == alerts[2:]


def test_thread_safety():
    bus = AlertBus()
    barrier = threading.Barrier(10)

    def raise_one(idx):
        barrier.wait()
        bus.raise_alert(_make_alert(message=f"thread-{idx}"))

    threads = [threading.Thread(target=raise_one, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    history = bus.get_history(50)
    assert len(history) == 10
    messages = {a.message for a in history}
    assert messages == {f"thread-{i}" for i in range(10)}


def test_callbacks():
    bus = AlertBus()
    received = []
    bus.on_alert(lambda a: received.append(a))
    alert = _make_alert(code="CB01", message="callback test")
    bus.raise_alert(alert)
    assert len(received) == 1
    assert received[0] is alert
    assert received[0].code == "CB01"


def test_alert_levels():
    bus = AlertBus()
    for level in (AlertLevel.INFO, AlertLevel.WARNING, AlertLevel.CRITICAL):
        bus.raise_alert(_make_alert(level=level))
        assert not bus.emergency_event.is_set()
    bus.raise_alert(_make_alert(level=AlertLevel.EMERGENCY))
    assert bus.emergency_event.is_set()
