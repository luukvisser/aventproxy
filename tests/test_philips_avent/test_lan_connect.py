"""How the LAN client finds the camera: a known address first, the broadcast scan after.

Without the cloud the client only has what the integration kept from an earlier
session. The scan never reaches Home Assistant in a container on a bridge
network, so a cached address has to be tried before it.
"""

import asyncio

import pytest
from .ha_stubs import FakeHass, load


class Recorder:
    """Stands in for tinytuya: records every connection attempt and every scan."""

    def __init__(self, answers=None, scan=None):
        # (ip, version) -> what _get_socket returns; True means the handshake worked.
        self.answers = answers or {}
        self.scan_result = scan or {}
        self.events = []

    def device_class(self):
        recorder = self

        class Device:
            def __init__(self, dev_id, ip, local_key, version=3.3):
                self.ip, self.version = ip, version

            def set_socketPersistent(self, value):
                pass

            def set_socketTimeout(self, value):
                pass

            def _get_socket(self, renew):
                recorder.events.append(("connect", self.ip, self.version))
                return recorder.answers.get((self.ip, self.version), 901)

            def close(self):
                pass

        return Device

    def scan(self, maxretry=None):
        self.events.append(("scan",))
        return self.scan_result


@pytest.fixture
def lan(monkeypatch):
    module = load(monkeypatch, "lan")

    def make(recorder, **kwargs):
        monkeypatch.setattr(module.tinytuya, "Device", recorder.device_class())
        monkeypatch.setattr(module.tinytuya, "deviceScan", recorder.scan)
        return module.TuyaLANClient(FakeHass(), "cam1", "k" * 16, lambda dps: None, **kwargs)

    return make


def connect(client):
    return asyncio.run(client._connect())


class TestConnectOrder:
    def test_cached_address_is_tried_before_any_scan(self, lan):
        recorder = Recorder(answers={("192.168.1.50", 3.5): True})
        client = lan(recorder, ip="192.168.1.50", version=3.5)
        assert connect(client) is True
        assert recorder.events == [("connect", "192.168.1.50", 3.5)]
        assert client.session_ip == "192.168.1.50"
        assert client.version == 3.5

    def test_cached_33_stays_33(self, lan):
        # A cached 3.3 proves nothing (no key check), so the default order stays.
        recorder = Recorder(answers={("192.168.1.50", 3.3): True})
        client = lan(recorder, ip="192.168.1.50", version=3.3)
        assert connect(client) is True
        assert recorder.events == [("connect", "192.168.1.50", 3.3)]

    def test_a_cached_key_checking_protocol_never_drops_to_33(self, lan):
        # Cached from a 3.4+ session: 3.3 would mean no key check and no alarm DPS.
        recorder = Recorder(answers={("192.168.1.50", 3.3): True})
        client = lan(recorder, ip="192.168.1.50", version=3.5)
        connect(client)
        assert ("connect", "192.168.1.50", 3.3) not in recorder.events

    def test_scan_follows_when_the_cached_address_fails(self, lan):
        recorder = Recorder(
            answers={("192.168.1.77", 3.5): True},
            scan={"192.168.1.77": {"gwId": "cam1", "version": "3.5"}},
        )
        client = lan(recorder, ip="192.168.1.50", version=3.5)
        assert connect(client) is True
        assert recorder.events[0] == ("connect", "192.168.1.50", 3.5)
        assert ("scan",) in recorder.events
        assert recorder.events.index(("scan",)) > 0
        assert client.session_ip == "192.168.1.77"

    def test_without_a_cached_address_the_scan_is_the_first_step(self, lan):
        recorder = Recorder(scan={})
        client = lan(recorder)
        assert connect(client) is False
        assert recorder.events == [("scan",)]


class TestConnectionCallback:
    def test_reports_connect_and_disconnect_once_each(self, lan):
        seen = []
        recorder = Recorder(answers={("192.168.1.50", 3.5): True})
        client = lan(recorder, ip="192.168.1.50", version=3.5, on_connection_change=seen.append)
        connect(client)
        client._disconnect()
        client._disconnect()
        assert seen == [True, False]

    def test_a_failing_callback_does_not_break_the_client(self, lan):
        def boom(connected):
            raise RuntimeError("callback bug")

        recorder = Recorder(answers={("192.168.1.50", 3.5): True})
        client = lan(recorder, ip="192.168.1.50", version=3.5, on_connection_change=boom)
        assert connect(client) is True
        assert client.connected is True


class TestConfiguredAddresses:
    """With lan_hosts, a client also tries addresses that may be another camera's."""

    def test_configured_hosts_only_on_key_checking_protocols(self, lan):
        recorder = Recorder(answers={("192.168.1.51", 3.4): True})
        client = lan(recorder, hosts="192.168.1.50, 192.168.1.51")
        assert connect(client) is True
        assert all(version in (3.5, 3.4) for _, _, version in recorder.events if _ == "connect")
        assert client.session_ip == "192.168.1.51"

    def test_another_cameras_refusal_is_not_a_changed_key(self, lan):
        rejected = []
        recorder = Recorder(answers={("192.168.1.50", 3.5): 914, ("192.168.1.50", 3.4): 914,
                                     ("192.168.1.51", 3.4): True})
        client = lan(recorder, hosts="192.168.1.50, 192.168.1.51", on_key_rejected=lambda: rejected.append(1))
        assert connect(client) is True
        assert rejected == []

    def test_own_last_address_refusing_the_key_is_reported(self, lan):
        rejected = []
        # A re-paired camera refuses the old key on both key-checked versions.
        recorder = Recorder(answers={("192.168.1.50", 3.4): 914, ("192.168.1.50", 3.5): 914})
        client = lan(recorder, ip="192.168.1.50", version=3.4, hosts="192.168.1.50",
                     on_key_rejected=lambda: rejected.append(1))
        connect(client)
        assert rejected

    def test_own_address_stays_own_after_a_failed_round(self, lan):
        # Round one: unreachable, which clears the session address. Round two:
        # the old key refused. Still this camera's address, so reported and
        # not skipped as another camera's.
        rejected = []
        recorder = Recorder(answers={("192.168.1.50", 3.4): 901, ("192.168.1.50", 3.5): 901})
        client = lan(recorder, ip="192.168.1.50", version=3.4, hosts="192.168.1.50",
                     on_key_rejected=lambda: rejected.append(1))
        connect(client)
        recorder.answers.update({("192.168.1.50", 3.4): 914, ("192.168.1.50", 3.5): 914})
        connect(client)
        assert rejected
        assert not client._foreign.skipped("192.168.1.50", 0)

    def test_two_cameras_that_swapped_addresses_both_reconnect(self, monkeypatch):
        # cam1 was at .50 and cam2 at .51, both claimed (as the cache seeds
        # them after a restart); then their leases swapped. Each own address
        # now refuses the key, which must release the claim on it, or each
        # camera skips the other's address for good.
        module = load(monkeypatch, "lan")
        where = {"192.168.1.50": "cam2", "192.168.1.51": "cam1"}
        events = []

        class Device:
            def __init__(self, dev_id, ip, local_key, version=3.3):
                self.dev_id, self.ip, self.version = dev_id, ip, version

            def set_socketPersistent(self, value):
                pass

            def set_socketTimeout(self, value):
                pass

            def set_socketRetryLimit(self, value):
                pass

            def set_socketRetryDelay(self, value):
                pass

            def _get_socket(self, renew):
                events.append((self.dev_id, self.ip, self.version))
                return True if where.get(self.ip) == self.dev_id else 914

            def close(self):
                pass

        monkeypatch.setattr(module.tinytuya, "Device", Device)
        monkeypatch.setattr(module.tinytuya, "deviceScan", lambda maxretry=None: {})
        claims = module.AddressClaims()
        claims.seed({"cam1": "192.168.1.50", "cam2": "192.168.1.51"})
        hosts = "192.168.1.50, 192.168.1.51"
        cam1 = module.TuyaLANClient(FakeHass(), "cam1", "k" * 16, lambda dps: None,
                                    ip="192.168.1.50", version=3.4, hosts=hosts, claims=claims)
        cam2 = module.TuyaLANClient(FakeHass(), "cam2", "k" * 16, lambda dps: None,
                                    ip="192.168.1.51", version=3.4, hosts=hosts, claims=claims)
        results = []
        for _ in range(2):
            results += [connect(cam1), connect(cam2)]
        assert cam1.connected and cam2.connected, results
        assert (cam1.ip, cam2.ip) == ("192.168.1.51", "192.168.1.50")
        assert claims.owned() == {"192.168.1.51": "cam1", "192.168.1.50": "cam2"}

    def test_cached_33_on_a_configured_address_heals_to_34(self, lan):
        recorder = Recorder(answers={("192.168.1.50", 3.4): True, ("192.168.1.50", 3.3): True})
        client = lan(recorder, ip="192.168.1.50", version=3.3, hosts="192.168.1.50")
        assert connect(client) is True
        assert client.version == 3.4
        assert ("connect", "192.168.1.50", 3.3) not in recorder.events


class TestLastWorkingAddressAfterAFailedRound:
    def test_it_is_tried_again_on_key_checked_versions(self, lan):
        recorder = Recorder(answers={("192.168.1.50", 3.4): True})
        client = lan(recorder, ip="192.168.1.50", version=3.4)
        assert connect(client) is True
        client._disconnect()
        # The camera reboots: a whole round fails, which clears the current address.
        recorder.answers = {}
        assert connect(client) is False
        assert client.session_ip is None
        # Back on its address: found again without a scan or configured hosts.
        recorder.answers = {("192.168.1.50", 3.4): True}
        recorder.events.clear()
        assert connect(client) is True
        assert client.session_ip == "192.168.1.50"
        tried = [e for e in recorder.events if e[0] == "connect"]
        assert tried and all(version in (3.4, 3.5) for _, _, version in tried)
        assert ("scan",) not in recorder.events
