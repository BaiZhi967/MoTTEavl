"""Explicit local Trace retention; the storage contracts and signatures are unchanged.

Import this module explicitly. It never enables retention or schedules work.
"""
from motte_storage.trace_retention import (
    apply_trace_retention,
    collect_trace_protection,
    plan_trace_retention,
)
from motte_storage.trace_retention_models import (
    StoredTraceEvent,
    TraceArchiveInvalid,
    TraceArchiveReceipt,
    TraceEventWindow,
    TracePrefix,
    TraceProtection,
    TraceReferenceDocument,
    TraceRetentionConfig,
    TraceRetentionDisabled,
    TraceRetentionPlan,
    TraceRetentionPlanChanged,
    TraceRetentionResult,
)

__all__ = [
    'apply_trace_retention', 'collect_trace_protection', 'plan_trace_retention',
    'StoredTraceEvent', 'TraceArchiveInvalid', 'TraceArchiveReceipt', 'TraceEventWindow',
    'TracePrefix', 'TraceProtection', 'TraceReferenceDocument', 'TraceRetentionConfig',
    'TraceRetentionDisabled', 'TraceRetentionPlan', 'TraceRetentionPlanChanged',
    'TraceRetentionResult',
]
