from xml.etree import ElementTree as ET

from dialpass.telephony.twiml import join_conference, stream_and_conference


def test_stream_and_conference_is_valid_xml_with_stream_and_conference():
    xml = stream_and_conference("wss://example.test/media", "room-abc")
    root = ET.fromstring(xml)
    assert root.tag == "Response"
    assert root.find(".//Stream").attrib["url"] == "wss://example.test/media"
    assert root.find(".//Conference").text == "room-abc"


def test_join_conference_muted_flag():
    xml = join_conference("room-abc", muted=True)
    conf = ET.fromstring(xml).find(".//Conference")
    assert conf.attrib["muted"] == "true"
    assert conf.attrib["startConferenceOnEnter"] == "false"
