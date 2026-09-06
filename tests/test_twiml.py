from xml.etree import ElementTree as ET

from dialpass.telephony.twiml import (
    join_conference,
    play_digits_then_conference,
    stream_and_conference,
)


def test_stream_and_conference_is_valid_xml_with_stream_and_conference():
    xml = stream_and_conference("wss://example.test/media", "room-abc")
    root = ET.fromstring(xml)
    assert root.tag == "Response"
    assert root.find(".//Stream").attrib["url"] == "wss://example.test/media"
    assert root.find(".//Conference").text == "room-abc"
    # the media handler needs the conference name from the start event
    param = root.find(".//Stream/Parameter")
    assert param.attrib == {"name": "conference", "value": "room-abc"}


def test_play_digits_then_conference_plays_then_rejoins():
    xml = play_digits_then_conference("2", "room-xyz")
    root = ET.fromstring(xml)
    assert root.find(".//Play").attrib["digits"].endswith("2w")
    assert root.find(".//Conference").text == "room-xyz"


def test_play_digits_strips_non_dtmf():
    xml = play_digits_then_conference("2; DROP TABLE calls--1", "room")
    digits = ET.fromstring(xml).find(".//Play").attrib["digits"]
    assert set(digits) <= set("0123456789*#w")
    assert "21" in digits


def test_join_conference_muted_flag():
    xml = join_conference("room-abc", muted=True)
    conf = ET.fromstring(xml).find(".//Conference")
    assert conf.attrib["muted"] == "true"
    assert conf.attrib["startConferenceOnEnter"] == "false"
