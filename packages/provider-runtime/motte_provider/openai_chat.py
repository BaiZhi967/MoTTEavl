from motte_contracts.messages import ModelResponse


def normalize_response(data):
    c = data.get("choices", [{}])[0]
    m = c.get("message", {})
    return ModelResponse(
        model=data.get("model", ""),
        content=m.get("content", "") or "",
        finish_reason=c.get("finish_reason"),
        usage=data.get("usage", {}),
    )


def normalize_stream(events):
    for e in events:
        d = e.get("choices", [{}])[0].get("delta", {}).get("content", "")
        if d:
            yield d
