"""Out-of-process JSON Schema validator: a terminable pattern-execution guard.

``jsonschema`` executes user-supplied ``pattern``/``patternProperties`` regexes
synchronously with CPython ``re`` (no match timeout), so a catastrophic pattern
inside a schema can wedge the evaluating worker forever. Validation runs in a
spawned child process that the caller can terminate when the deadline expires.

The child only imports jsonschema/referencing (no application stack) and uses
the same remote-``$ref``-blocked registry as the in-process validator, so the
scoring process never issues network requests here either.

Result kinds: ``ok`` (list of violation messages), ``config_error`` (the schema
itself is invalid), ``unresolvable`` (e.g. blocked remote ``$ref``), ``error``
(any other crash), ``timeout`` is produced by the parent side.
"""
from __future__ import annotations

from multiprocessing.connection import Connection


def run_validation(schema: dict, instance: object) -> tuple[str, object]:
    import jsonschema
    from jsonschema.exceptions import SchemaError
    from referencing import Registry
    from referencing.exceptions import Unresolvable

    def blocked(uri: str):
        raise Unresolvable(ref=uri)

    try:
        validator = jsonschema.Draft202012Validator(
            schema, registry=Registry(retrieve=blocked),
        )
        errors = sorted(
            validator.iter_errors(instance), key=lambda item: list(item.absolute_path)
        )
    except Unresolvable as error:
        return ("unresolvable", f"{type(error).__name__}: {error}")
    except SchemaError as error:
        return ("config_error", f"{type(error).__name__}: {error}")
    return ("ok", [error.message for error in errors])


def worker_main(conn: Connection, schema: dict, instance: object) -> None:
    try:
        conn.send(run_validation(schema, instance))
    except BaseException as error:  # noqa: BLE001 - report every failure to the parent
        conn.send(("error", f"{type(error).__name__}: {error}"))
    finally:
        conn.close()
