from motte_agent.protocol import decode_message, encode_message


def test_protocol_roundtrip():
    assert decode_message(encode_message({"type": "init"}))["type"] == "init"
