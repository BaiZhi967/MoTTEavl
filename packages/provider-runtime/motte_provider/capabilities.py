class UnsupportedParameterError(ValueError): pass
def validate_parameters(params, supported):
    for k,v in params.items():
        if k not in supported: raise UnsupportedParameterError(f"unsupported parameter: {k}")
        rule=supported[k]
        if isinstance(rule, tuple) and not rule[0] <= v <= rule[1]: raise UnsupportedParameterError(f"invalid parameter: {k}")
    return True
