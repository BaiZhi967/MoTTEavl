"""Smoke tests for the importable MoTTEavl workspace packages."""


def test_workspace_packages_import():
    import motte_contracts
    import motte_sdk
    import motte_provider
    import motte_agent
    import motte_harness
    import motte_skill
    import motte_sandbox
    import motte_eval
    import motte_trace
    import motte_storage

    assert motte_contracts.__version__
    assert motte_sdk.__version__
    assert motte_provider.__version__
    assert motte_agent.__version__
    assert motte_harness.__version__
    assert motte_skill.__version__
    assert motte_sandbox.__version__
    assert motte_eval.__version__
    assert motte_trace.__version__
    assert motte_storage.__version__
