"""Lullaby track ids: reading DPS 248, the known blocks, and learning a block from a wrap."""

import json

from const import (
    LULLABY_CATALOGUE,
    learn_lullaby_block,
    lullaby_block,
    lullaby_catalogue_known,
    lullaby_track_id,
    lullaby_track_name,
    parse_lullaby_record,
)

RECORD = {"bizcode": "phi-no-bm", "id": 20030092, "errcode": 0}


class TestParse:
    def test_cloud_json_string(self):
        assert parse_lullaby_record(json.dumps(RECORD)) == 20030092

    def test_lan_push_escaped_once_more(self):
        # as an SCD951 sent it over the LAN, 01-11-2026
        assert parse_lullaby_record('{\\"bizcode\\":\\"phi-no-bm\\",\\"id\\":20030092,\\"errcode\\":0}') == 20030092

    def test_json_string_inside_a_json_string(self):
        assert parse_lullaby_record(json.dumps(json.dumps(RECORD))) == 20030092

    def test_dict_and_garbage(self):
        assert parse_lullaby_record(RECORD) == 20030092
        for bad in (None, "", "x", "{}", '{"id": "7"}', '{"id": true}', 42, []):
            assert parse_lullaby_record(bad) is None, bad


class TestKnownBlocks:
    def test_scd951_block_from_one_observation(self):
        assert lullaby_track_name(20030092) == "White Noise"
        assert lullaby_track_name(20030083) == "Baa Baa Black Sheep"
        assert lullaby_track_name(20030097) == "Rain Shower"

    def test_unknown_id_has_no_name(self):
        assert lullaby_track_name(2960760) is None
        assert lullaby_block(2960760) is None

    def test_track_id_follows_the_cameras_block(self):
        assert lullaby_track_id("Rain Shower", 20030092) == 20030097


class TestLearning:
    span = len(LULLABY_CATALOGUE) - 1

    def test_next_past_the_last_track_reveals_the_block(self):
        assert learn_lullaby_block(2960760 + self.span, 2960760) == 2960760

    def test_prev_past_the_first_track_reveals_the_block(self):
        assert learn_lullaby_block(2960760, 2960760 + self.span) == 2960760

    def test_ordinary_steps_teach_nothing(self):
        assert learn_lullaby_block(2960760, 2960761) is None
        assert learn_lullaby_block(2960761, 2960760) is None
        assert learn_lullaby_block(None, 2960760) is None
        assert learn_lullaby_block(2960760, None) is None

    def test_a_learned_block_names_tracks_and_picks_ids(self):
        learned = (2960760,)
        assert lullaby_track_name(2960769, learned) == "White Noise"
        assert lullaby_track_id("Heartbeat", 2960769, learned) == 2960767


class TestPickByName:
    def test_known_block_or_nothing_reported_can_pick(self):
        assert lullaby_catalogue_known(20030092)
        assert lullaby_catalogue_known(None)

    def test_unknown_block_cannot_until_learned(self):
        # The SCD973-family unit reported 2960760 (01-11-2026), in no known block.
        assert not lullaby_catalogue_known(2960760)
        block = learn_lullaby_block(2960760 + len(LULLABY_CATALOGUE) - 1, 2960760)
        assert block is not None
        assert lullaby_catalogue_known(2960760, (block,))
