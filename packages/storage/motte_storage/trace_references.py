"""Shared pure Trace reference extraction and exact Pass resolution.

Documents retain owning-edge context; no filesystem paths or current settings
are guessed. The same extraction handles live records and archived payloads.
"""
from __future__ import annotations

from motte_contracts.evaluation import EvidenceRef
from .trace_retention_models import TracePassReference, TraceReferenceDocument

_PASS_KEYS = frozenset({'scoring_pass_id', 'source_pass_id', 'previous_pass_id',
                        'current_scoring_pass_id'})


def identity(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'reference {name} must be a nonempty string')
    return value


def extract_reference_document(records) -> TraceReferenceDocument:
    runs, events, passes = set(), {}, []

    def walk(value, owner, *, top=False, ownership=False):
        if isinstance(value, (list, tuple)):
            for child in value:
                walk(child, owner)
            return
        if not isinstance(value, dict):
            return
        kind = value.get('kind')
        evidence = kind == 'event' or ('locator' in value and
            (kind in {'artifact', 'invocation'} or 'run_id' in value))
        if evidence:
            ref = EvidenceRef.model_validate(value)
            if ref.kind == 'event':
                events.setdefault(ref.run_id, set()).add(int(ref.locator))
            else:
                runs.add(ref.run_id)
            return
        expected = None
        if value.get('scoring_pass_id') is not None and value.get('run_id') is not None:
            expected = identity(value['run_id'], 'coupled run_id')
        target = value.get('target_type')
        if target == 'run':
            runs.add(identity(value.get('target_id'), 'target_id'))
        elif target == 'scoring_pass':
            passes.append(TracePassReference(pass_id=identity(value.get('target_id'), 'target_id')))
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError('reference record requires string keys')
            if (key == 'run_id' or key.endswith('_run_id')) and child is not None:
                target_run = identity(child, key)
                if not (key == 'run_id' and (top or ownership) and target_run == owner):
                    runs.add(target_run)
            elif (key == 'run_ids' or key.endswith('_run_ids')) and child is not None:
                if not isinstance(child, list):
                    raise ValueError('reference Run ids must be a list')
                runs.update(identity(item, key) for item in child)
            elif key in _PASS_KEYS and child is not None:
                passes.append(TracePassReference(
                    pass_id=identity(child, key),
                    owning_run_id=owner if top and key in {'scoring_pass_id', 'current_scoring_pass_id'} else None,
                    expected_run_id=expected if key == 'scoring_pass_id' else None))
            else:
                walk(child, owner, ownership=top and key == 'owner')

    for record, owner in records:
        walk(record, owner, top=True)
    return TraceReferenceDocument(schema_version=1, run_ids=tuple(runs), event_seqs={key: tuple(value) for key, value in events.items()},
                                   pass_references=tuple(passes))


def resolve_reference_document(document, get_pass):
    document = TraceReferenceDocument.model_validate(document)
    runs = set(document.run_ids)
    events = {key: set(value) for key, value in document.event_seqs.items()}
    owners = {}
    for ref in document.pass_references:
        if ref.pass_id not in owners:
            record = get_pass(ref.pass_id)
            if not isinstance(record, dict) or record.get('id') != ref.pass_id:
                raise ValueError('unresolved scoring Pass reference: ' + ref.pass_id)
            owners[ref.pass_id] = identity(record.get('run_id'), 'Pass run_id')
        target = owners[ref.pass_id]
        if ref.expected_run_id is not None and ref.expected_run_id != target:
            raise ValueError('coupled Run/Pass reference identity disagrees')
        if target != ref.owning_run_id:
            runs.add(target)
    return runs, events
