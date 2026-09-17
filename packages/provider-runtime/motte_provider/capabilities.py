class UnsupportedParameterError(ValueError):
    pass


def validate_parameters(params, supported):
    for k, v in params.items():
        if k not in supported:
            raise UnsupportedParameterError(f"unsupported parameter: {k}")
        if k == "max_output_tokens" and (type(v) is not int or v <= 0):
            raise UnsupportedParameterError("max_output_tokens must be a positive integer")
        rule = supported[k]
        if isinstance(rule, tuple) and not rule[0] <= v <= rule[1]:
            raise UnsupportedParameterError(f"invalid parameter: {k}")
    return True
