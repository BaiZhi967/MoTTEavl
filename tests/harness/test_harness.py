import pytest
from motte_harness.protocol import parse_jsonl
from motte_harness.channel import TerminalChannel
from motte_harness.probe import probe_version
def test_jsonl_parser_keeps_malformed_as_error():
 assert parse_jsonl('{"x":1}\nnope\n')[1]["error"]
def test_terminal_channel_rejects():
 with pytest.raises(PermissionError): TerminalChannel().send("x")
def test_probe(): assert probe_version("tool", "1.2")["version"]=="1.2"
