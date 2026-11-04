"""A camera's own LAN address from the options ("camera=address"), and protocol 3.3 on it.

A camera on old firmware speaks only 3.3, whose handshake proves no key, so a
plain configured address is never tried on it (one such attempt once attached
two cameras' clients to the same camera). Bound to one camera by the user, an
address may be tried on 3.3, and the session then has to prove the camera with
its first readable message. A 3.3 camera's alerts do not arrive over the LAN,
so its cloud poll stays.
"""

import asyncio
from datetime import timedelta

import pytest

from lan_policy import (
    BOUND_MISMATCH_HOLD,
    IDENTITY_OK,
    IDENTITY_PENDING,
    IDENTITY_UNREADABLE_LIMIT,
    IDENTITY_WRONG,
    MAX_PAYLOAD_ERRORS,
    ForeignAddresses,
    SessionIdentity,
    bound_versions,
    lan_hosts,
    normalized_lan_hosts,
    parse_lan_hosts,
    resolve_lan_bindings,
    shared_hosts_for,
)

from .test_lan_connect import Recorder, lan  # noqa: F401 - fixture
from .test_offline_start import CACHED, FakeAPI, FakeCache, FakeLANClient, env  # noqa: F401 - fixture

CAMERAS = {
    "bf1111111111aaaaaaaaaa": ["Philips Avent Baby Monitor", "Nursery"],
    "bf2222222222bbbbbbbbbb": ["Philips Avent Baby Monitor bf2222"],
}


class TestOption:
    def test_shared_and_bound_entries(self):
        shared, bound = parse_lan_hosts("192.168.1.50, bf2222=192.168.1.60")
        assert shared == ["192.168.1.50"]
        assert bound == {"bf2222": "192.168.1.60"}

    def test_old_values_still_read(self):
        assert parse_lan_hosts("192.168.1.50 avent-2,192.168.1.50") == (["192.168.1.50", "avent-2"], {})
        assert lan_hosts("192.168.1.50, 192.168.1.51") == ["192.168.1.50", "192.168.1.51"]

    def test_names_with_spaces(self):
        _, bound = parse_lan_hosts("Philips Avent Baby Monitor bf2222 = 192.168.1.60; Nursery=192.168.1.61")
        assert bound == {"Philips Avent Baby Monitor bf2222": "192.168.1.60", "Nursery": "192.168.1.61"}

    @pytest.mark.parametrize("value", ["=192.168.1.60", "bf2222=", "bf2222=192.168.1.60:6668", "192.168.1.50:1"])
    def test_invalid(self, value):
        assert parse_lan_hosts(value) is None
        assert normalized_lan_hosts(value) is None

    def test_normalized_keeps_the_bindings(self):
        assert normalized_lan_hosts(" bf2222 = 192.168.1.60 , 192.168.1.50") == "192.168.1.50, bf2222=192.168.1.60"

    def test_shared_list_leaves_out_bound_addresses(self):
        assert lan_hosts("192.168.1.50, bf2222=192.168.1.60") == ["192.168.1.50"]


class TestResolve:
    def test_by_id_prefix_name_and_user_name(self):
        resolved, unresolved = resolve_lan_bindings(
            {"BF2222": "192.168.1.60", "nursery": "192.168.1.61"}, CAMERAS
        )
        assert resolved == {"bf2222222222bbbbbbbbbb": "192.168.1.60", "bf1111111111aaaaaaaaaa": "192.168.1.61"}
        assert unresolved == []

    def test_ambiguous_short_or_unknown_keys_bind_nothing(self):
        # "bf" is shorter than six characters and starts both ids; the name is
        # shared by both cameras' cloud names in a family of same-named ones.
        cameras = {**CAMERAS, "bf3333333333cccccccccc": ["Philips Avent Baby Monitor"]}
        resolved, unresolved = resolve_lan_bindings(
            {"bf": "192.168.1.60", "Philips Avent Baby Monitor": "192.168.1.61", "kitchen": "192.168.1.62"}, cameras
        )
        assert resolved == {}
        assert unresolved == ["bf", "Philips Avent Baby Monitor", "kitchen"]

    def test_an_address_bound_to_another_camera_is_never_shared(self):
        resolved = {"bf2222222222bbbbbbbbbb": "192.168.1.60"}
        assert shared_hosts_for(["192.168.1.50", "192.168.1.60"], resolved, "bf1111111111aaaaaaaaaa") == ["192.168.1.50"]
        assert shared_hosts_for(["192.168.1.50", "192.168.1.60"], resolved, "bf2222222222bbbbbbbbbb") == [
            "192.168.1.50", "192.168.1.60",
        ]


class TestBoundVersions:
    def test_nothing_known_tries_key_checking_first_then_33(self):
        assert bound_versions(None, None) == [3.5, 3.4, 3.3]

    def test_a_known_33_camera_goes_straight_to_33(self):
        assert bound_versions(3.3, 3.3) == [3.3]

    def test_a_camera_that_proved_34_never_drops_to_33(self):
        assert 3.3 not in bound_versions(3.4, 3.4)
        assert 3.3 not in bound_versions(None, 3.5)


class TestSessionIdentity:
    def test_key_checking_sessions_need_nothing(self):
        assert SessionIdentity("cam1", required=False).check({"Err": "904"}) == IDENTITY_OK

    def test_a_readable_push_proves_the_camera(self):
        identity = SessionIdentity("cam1", required=True)
        assert identity.check({"dps": {"101": True}}) == IDENTITY_OK
        assert identity.verified
        # Once proven, odd frames are the normal error handling's business.
        assert identity.check({"Err": "904"}) == IDENTITY_OK

    def test_its_own_id_proves_it(self):
        assert SessionIdentity("cam1", required=True).check({"devId": "cam1", "t": 1}) == IDENTITY_OK

    def test_another_sender_is_another_device(self):
        assert SessionIdentity("cam1", required=True).check({"devId": "cam2", "dps": {"1": 1}}) == IDENTITY_WRONG

    def test_messages_in_another_key_are_another_device(self):
        identity = SessionIdentity("cam1", required=True)
        results = [identity.check({"Err": "904", "Error": "Unexpected Payload"}) for _ in range(IDENTITY_UNREADABLE_LIMIT)]
        assert results[-1] == IDENTITY_WRONG
        assert set(results[:-1]) <= {IDENTITY_PENDING}

    def test_json_errors_count_too(self):
        identity = SessionIdentity("cam1", required=True)
        assert [identity.check({"Err": "900"}) for _ in range(IDENTITY_UNREADABLE_LIMIT)][-1] == IDENTITY_WRONG

    def test_one_odd_frame_then_a_readable_one_is_this_camera(self):
        identity = SessionIdentity("cam1", required=True)
        assert identity.check({"Err": "904"}) == IDENTITY_PENDING
        assert identity.check({"dps": {"1": True}}) == IDENTITY_OK

    def test_other_errors_and_empty_answers_settle_nothing(self):
        identity = SessionIdentity("cam1", required=True)
        assert identity.check({"Err": "901"}) == IDENTITY_PENDING
        assert identity.check({}) == IDENTITY_PENDING
        assert identity.check(None) == IDENTITY_PENDING
        assert not identity.verified

    def test_decided_before_the_unreadable_frames_force_a_reconnect(self):
        # Otherwise every reconnect would start the count again, and the wrong
        # device would keep one of its sessions taken for good.
        assert IDENTITY_UNREADABLE_LIMIT < MAX_PAYLOAD_ERRORS

    def test_a_mismatch_holds_the_address_longer(self):
        foreign = ForeignAddresses()
        foreign.note("192.168.1.60", 0.0, BOUND_MISMATCH_HOLD)
        assert foreign.skipped("192.168.1.60", BOUND_MISMATCH_HOLD - 1)
        assert not foreign.skipped("192.168.1.60", BOUND_MISMATCH_HOLD + 1)


def connect(client):
    return asyncio.run(client._connect())


class TestClientOnABoundAddress:
    def test_33_is_tried_on_the_bound_address(self, lan):  # noqa: F811 - fixture
        recorder = Recorder(answers={("192.168.1.60", 3.3): True})
        client = lan(recorder, hosts="192.168.1.50", bound="192.168.1.60")
        assert connect(client) is True
        assert recorder.events[:3] == [
            ("connect", "192.168.1.60", 3.5), ("connect", "192.168.1.60", 3.4), ("connect", "192.168.1.60", 3.3),
        ]
        assert (client.session_ip, client.version) == ("192.168.1.60", 3.3)
        # A 3.3 session has to prove the camera before anything counts.
        assert not client._identity.verified

    def test_never_33_on_a_shared_address(self, lan):  # noqa: F811 - fixture
        recorder = Recorder(answers={("192.168.1.50", 3.3): True})
        client = lan(recorder, hosts="192.168.1.50", bound="192.168.1.60")
        assert connect(client) is False
        assert ("connect", "192.168.1.50", 3.3) not in recorder.events
        assert ("connect", "192.168.1.60", 3.3) in recorder.events

    def test_a_key_checked_session_is_proven_at_once(self, lan):  # noqa: F811 - fixture
        recorder = Recorder(answers={("192.168.1.60", 3.4): True})
        client = lan(recorder, bound="192.168.1.60")
        assert connect(client) is True
        assert client.version == 3.4
        assert client._identity.verified

    def test_another_device_is_dropped_and_its_address_held(self, lan):  # noqa: F811 - fixture
        recorder = Recorder(answers={("192.168.1.60", 3.3): True})
        client = lan(recorder, bound="192.168.1.60")
        assert connect(client) is True
        assert client._identity.check({"devId": "someone-else", "dps": {}}) == IDENTITY_WRONG
        client._not_this_camera()
        assert client._device is None
        assert client.session_ip is None
        # The 3.3 "session" proved nothing, so it is not remembered as the camera's protocol.
        assert client.version is None
        recorder.events.clear()
        assert connect(client) is False
        assert not any(e[0] == "connect" and e[1] == "192.168.1.60" for e in recorder.events)

    def test_bound_address_is_not_also_a_shared_one(self, lan):  # noqa: F811 - fixture
        client = lan(Recorder(), hosts="192.168.1.50, 192.168.1.60", bound="192.168.1.60")
        assert client._hosts == ["192.168.1.50"]


class QueryDevice:
    """A 3.3 session's device for one status query: answers with `answer`, or as tinytuya does after
    decrypting "json obj data unvalid" (it switches to device22 and returns its ERR_DEVTYPE)."""

    def __init__(self, answer):
        self.answer = answer
        self.dev_type, self.dps_to_request = "default", {}
        self.sent = []

    def generate_payload(self, command):
        return ("payload", command)

    def _send_receive(self, payload, minresponse):
        self.sent.append(payload)
        if isinstance(self.answer, Exception):
            raise self.answer
        if self.answer == "unvalid":
            self.dev_type, self.dps_to_request = "device22", {"1": None}
            return {"Error": "Device22 Detected: Retry Command", "Err": "903"}
        return self.answer

    def close(self):
        pass


def on_bound_33(lan, answer):  # noqa: F811 - fixture
    """A client with a fresh 3.3 session on its bound address, its device answering the query with `answer`."""
    client = lan(Recorder(answers={("192.168.1.60", 3.3): True}), bound="192.168.1.60")
    assert connect(client) is True
    client._device = QueryDevice(answer)
    published = []
    client._on_dps_update = lambda dps, baseline: published.append((dps, baseline))
    return client, published


class TestA33SessionProvesItselfByAsking:
    """A quiet 3.3 camera pushes nothing, so its session is asked once (07-11-2026: an SCD923 started
    without the cloud stayed unavailable for minutes, its session never proven)."""

    def test_data_unvalid_proves_it_and_leaves_the_framing(self, lan):  # noqa: F811 - fixture
        client, published = on_bound_33(lan, "unvalid")
        assert asyncio.run(client._prove_33()) is True
        assert client._identity.verified and client.proven
        assert client.ip == "192.168.1.60"  # the address is published once proven
        assert (client._device.dev_type, client._device.dps_to_request) == ("default", {})
        assert client._device.sent == [("payload", 10)]  # one read, no device22 retry
        assert published == []

    def test_its_own_state_proves_it_and_is_the_baseline(self, lan):  # noqa: F811 - fixture
        client, published = on_bound_33(lan, {"devId": "cam1", "dps": {"101": True}})
        assert asyncio.run(client._prove_33()) is True
        assert client.proven
        assert published == [({"101": True}, True)]

    def test_another_device_answering_is_refused(self, lan):  # noqa: F811 - fixture
        client, _ = on_bound_33(lan, {"devId": "someone-else", "dps": {"1": 1}})
        assert asyncio.run(client._prove_33()) is False
        assert not client._identity.verified

    @pytest.mark.parametrize("answer", [None, {"Err": "904", "Error": "Unexpected Payload"}, OSError("timed out")])
    def test_no_answer_settles_nothing(self, lan, answer):  # noqa: F811 - fixture
        client, published = on_bound_33(lan, answer)
        assert asyncio.run(client._prove_33()) is True
        assert not client._identity.verified and not client.proven
        assert client.ip is None
        assert published == []


class TestCloudPollFollowsWhereAlertsCome:
    def _connected(self, env, version):  # noqa: F811 - fixture
        coordinator, hass = env(FakeAPI(), FakeCache(CACHED), lan_preferred=True)

        async def scenario():
            await coordinator.async_start()
            lan_client = FakeLANClient.instances[-1]
            lan_client.version = version
            lan_client.come_up()
            await hass.settle()

        asyncio.run(scenario())
        return coordinator

    def test_a_33_session_keeps_the_cloud_poll_for_alerts(self, env):  # noqa: F811 - fixture
        coordinator = self._connected(env, 3.3)
        assert coordinator.lan_alerts_local is False
        assert coordinator._cloud_poll_off is False
        assert coordinator.update_interval == timedelta(seconds=30)

    def test_a_34_session_lets_the_poll_go(self, env):  # noqa: F811 - fixture
        coordinator = self._connected(env, 3.4)
        assert coordinator.lan_alerts_local is True
        assert coordinator._cloud_poll_off is True
        assert coordinator.update_interval is None
