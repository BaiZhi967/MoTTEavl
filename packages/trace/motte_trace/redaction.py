def redact(value):
    if isinstance(value, dict): return {k:("[REDACTED]" if any(x in k.lower() for x in ("key","secret","token","authorization")) else redact(v)) for k,v in value.items()}
    if isinstance(value, list): return [redact(v) for v in value]
    return value
