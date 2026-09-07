from xml.etree import ElementTree as ET

from dialpass.telephony.twiml import connect_stream


def test_connect_stream_is_a_bidirectional_pipe_with_group_and_role():
    xml = connect_stream("wss://example.test/media", "dialpass-abc123", "agent")
    root = ET.fromstring(xml)
    assert root.tag == "Response"
    # <Connect> (bidirectional, terminal) — not <Start> (one-way fork)
    assert root.find(".//Connect/Stream").attrib["url"] == "wss://example.test/media"
    assert root.find(".//Start") is None
    assert root.find(".//Conference") is None
    params = {p.attrib["name"]: p.attrib["value"] for p in root.findall(".//Stream/Parameter")}
    assert params == {"group": "dialpass-abc123", "role": "agent"}


def test_connect_stream_escapes_parameters():
    xml = connect_stream("wss://x/media", 'g&"<', "user")
    ET.fromstring(xml)  # would raise if the & / quotes weren't escaped


def test_connect_stream_intro_is_spoken_before_the_stream():
    xml = connect_stream("wss://x/media", "dialpass-abc", "user", intro="Stay on the line.")
    root = ET.fromstring(xml)
    kids = list(root)
    assert kids[0].tag == "Say" and kids[0].text == "Stay on the line."
    assert kids[1].tag == "Connect"  # Say comes first, then the stream opens
    params = {p.attrib["name"]: p.attrib["value"] for p in root.findall(".//Stream/Parameter")}
    assert params == {"group": "dialpass-abc", "role": "user"}
