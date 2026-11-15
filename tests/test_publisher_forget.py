"""DUP-13 (§6 L3): a deleted device must not leak change-detection cache entries.
The publishers' caches are never pruned on delete; forget_device drops them.
Pure method tests (no broker/threads) — the caches are plain dicts under a lock.
"""
import threading

from multibus.mqtt_publisher import MQTTPublisher
from multibus.influxdb_publisher import InfluxDBPublisher


def test_mqtt_forget_device_prunes_all_four_topic_caches():
    pub = MQTTPublisher.__new__(MQTTPublisher)          # no broker, no reconnect thread
    pub.lock = threading.RLock()
    pub.last_values, pub.last_publish_at = {}, {}
    pub._availability_last, pub._runtime_last = {}, {}
    pub._throttle_pending = {}                              # MQTT-RATE-1 cache (set in __init__)
    for pre in ("mbg/devices/gone", "mbg/devices/keep"):
        pub.last_values[f"{pre}/power"] = 1
        pub.last_publish_at[f"{pre}/power"] = 1.0
        pub._availability_last[f"{pre}/status"] = "online"      # clear_topics misses these two
        pub._runtime_last[f"{pre}/runtime/status"] = "ok"

    n = pub.forget_device("mbg/devices/gone")

    assert n == 4
    for cache in (pub.last_values, pub.last_publish_at, pub._availability_last, pub._runtime_last):
        assert not any(k.startswith("mbg/devices/gone") for k in cache)   # gone pruned
        assert any(k.startswith("mbg/devices/keep") for k in cache)       # others kept
    assert pub.forget_device("") == 0                   # no prefix → no-op


def test_influx_forget_device_prunes_both_caches_by_device_id():
    pub = InfluxDBPublisher.__new__(InfluxDBPublisher)
    pub._cache_lock = threading.RLock()
    pub.last_values = {("gone", 100): 1, ("gone", 102): 9, ("keep", 100): 2}
    pub.last_write_time = {("gone", 100): 1.0, ("gone", 102): 9.0, ("keep", 100): 2.0}

    n = pub.forget_device("gone")

    assert n == 4                                        # 2 addresses x 2 caches
    assert ("gone", 100) not in pub.last_values and ("gone", 102) not in pub.last_values
    assert ("keep", 100) in pub.last_values             # other device kept
    assert ("gone", 100) not in pub.last_write_time and ("keep", 100) in pub.last_write_time
    assert pub.forget_device("") == 0
