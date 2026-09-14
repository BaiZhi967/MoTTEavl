import json
def encode_message(message): return json.dumps(message, separators=(",",":"))+"\n"
def decode_message(line):
 try: value=json.loads(line); assert isinstance(value,dict); return value
 except Exception as e: raise ValueError("malformed protocol message") from e
