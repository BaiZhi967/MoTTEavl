# OpenCode Go real coding-feature verification

Scope: user-requested expanded real-model coverage, using synthetic coding data only.
Model/endpoint: `space-bunny-free`, `https://opencode.ai/zen/go/v1/chat/completions`.
Official source: https://opencode.ai/docs/go/ (checked 2026-09-30).
This is feature-path coverage, **not** a full benchmark or production qualification.

## Witness plan

| Feature | Smallest witness | Maximum new HTTP sends |
|---|---|---:|
| Nonstream provider, identity, usage | Correct one Python return statement | 1 |
| Provider SSE text | Same tiny correction with terminal/close checks | 1 |
| Provider SSE tools/history | Read-file tool delta → tool result → final text | 2 |
| Stream cancellation | Close after first delivered event; no retry | 1 |
| Native multi-turn session | Inspect code, then approve fix in same session | 8 |
| Legacy JSON action protocol | Read/write/final code repair | 6 |
| Public synchronous model-test API | Explicit coding prompt, capped at 16 output tokens | 1 |
| Queued direct coding run | Public creation → reopened SQLite → ordinary Worker → rescore/read | 1 |
| Native agent persisted run | Import one synthetic coding fixture → Worker → artifacts/score/report | 6 |
| Scenario multi-send workflow | Two coding turns, checkpoints, cleanup, report | 4 |
| Code-review judge | Single rubric and swapped-order pairwise on tiny Python code | separately bounded |
| Calibration lifecycle | Only if executable with honest synthetic lifecycle labels; never qualify | separately bounded |

The executable harness derives its hard total from selected per-feature caps; it
never silently expands a feature or uses paid fallbacks. Each request also has a
512-token output cap for ordinary coding features (4096 only for bounded structured
code-review Judge witnesses), a 20-second ordinary / 120-second Judge socket timeout,
redirects disabled, and zero retries. Each feature also has an absolute deadline of
min(120 seconds for ordinary features / 240 seconds for Judge, its call cap × that feature's socket timeout);
the whole batch is capped at 900 seconds, and an in-flight timeout stops later dispatch.
A batch stops on authentication errors, 429, network uncertainty, or reported-model
mismatch. Zero automatic retries means Retry-After is recorded, never ignored by a
rapid retry loop. These limits do not claim server-side generation cancellation.

## Separate boundaries, not invented coverage

- Event SSE (`MotteClient.stream_events`) is platform event delivery, distinct from
  provider token SSE. Read/report/rescore/replay should reuse the same live witness
  without spending new model calls.
- Generic C-Eval/CMMLU/GSM8K/MMLU/TruthfulQA etc. are noncoding benchmarks; do not send
  those datasets through a coding-agent-only service. Their parsing/aggregation is
  offline coverage, not a missing excuse for bulk model traffic.
- Responses and Anthropic wire adapters cannot be validated with this model's
  documented Chat Completions endpoint. Other models/providers are out of scope.
- Pi uses an independent SDK path. Exact Go endpoint, client/session headers and
  output/send caps must be proven before a real Pi request; scripted Pi tests are
  not real-provider evidence.
- Codex/Claude external runtimes and Harbor need verified endpoint routing,
  installation/preflight and bounded execution. No fabricated availability.
- Structured message pass-through does not establish vision/audio/video/PDF
  product support. Unsupported modalities remain unsupported/unverified.
- Calibration qualification requires genuine policy-compliant reviewed samples;
  do not invent human reviews or lower policy to turn a tiny smoke green.
- Strict production stream identity enforcement is now implemented for Chat, Responses
  and Anthropic stream shapes: mismatched/changing identity fails before that chunk
  yields, missing identity fails before final finish, and connections close on failure.
  Earlier deltas remain provisional and cannot be recalled. The live harness also
  independently records exact model-match booleans from actual Chat chunks.

## Evidence and credentials

Reports record per-feature pass/fail/blocked, actual HTTP counts, numeric usage when
reported, status codes, opaque session hashes and client identity checks. Raw keys,
Authorization headers and raw upstream errors are never included. The only key path
is the normal provider credential resolver using the user-entered saved profile.

Prior evidence is preserved: 36 real requests, 10 successful coding cases and one
historical three-step-budget termination. Expansions add a separate cumulative ledger;
they do not overwrite that failure or relabel a local run as a published/full CI gate.

## Historical observed result (2026-09-30, before 21:28 acceptance)

Machine-readable receipt: [live summary](opencode-go-live-2026-09-30/summary.json).
All raw provider messages and credentials are excluded from these receipts.

| Feature family | Result at 20:14 checkpoint | Evidence |
|---|---|---|
| Nonstream completion, identity and usage | PASS | core |
| Provider text SSE | PASS, repeated after production identity fix | core + final |
| Streamed tool call and tool-result history | PASS, repeated after identity fix | core + final |
| Client stream cancellation | PASS for closing after first text delta; server cancellation unknown | core |
| Native two-turn tool conversation | PASS; same session across turns | core |
| Legacy JSON action protocol | PASS | core |
| Synchronous public model-test API | PASS | integration |
| Queued direct run, reopened SQLite, ordinary Worker | PASS | integration |
| Native persisted agent tools, artifact AST/hash, invocation ledger | PASS | integration |
| Two-turn workflow/scenario | PASS; persisted steps and session continuity | integration |
| Single code-review Judge | PASS | integration |
| Pairwise code-review Judge | FAIL / indeterminate, bounded output and one historical timeout | integration + final + diagnostic |
| Candidate-only calibration lifecycle | FAIL, structured-output parse failure; never qualified | integration + diagnostic |
| Direct LLM v2 publication/profile/run | PASS | final |
| One-cell Experiment lifecycle and idempotent allocation | PASS | final |
| Three-arm Skill injection/ablation wiring | PASS at n=1, no quality comparison claim | final |

The persisted run witnesses also pass SDK/API/CLI report equality, immutable historical
scoring passes, deterministic rescore and replay, SDK terminal waiting, and platform
SSE terminal reconciliation, without extra model calls.

### Request ledger and failures retained

- Prior coding smoke: 36 authorized HTTP attempts; 10 cases passed and one old
  three-step budget termination remains recorded
- Expanded feature checks: 39 authorized attempts; results at that checkpoint were 14 passed
  feature families and 2 failed families, with all earlier trials retained
- Combined authorized attempts: **75**, including one outcome-unknown network attempt
- Separate test-isolation incident: **3 synthetic-key attempts** (one confirmed 401,
  two outcome-unverified failures), conservatively included in **78 total attempts**
- No saved user credential was read by the offline test worker. That test fixture
  missed the bounded opener; both openers plus socket/urllib escape paths are now
  guarded. See [incident receipt](opencode-go-live-2026-09-30/isolation-incident.json)
- Expanded receipts contain numeric usage for 37 of 39 attempts: 19,491 reported
  prompt tokens + 6,956 completion tokens = 26,447 total. This is a reported subset,
  not a complete billing total; the canceled stream and network-unknown call lack
  final usage, and the prior 36-call summary did not capture usage

Structured judging was first capped at 512 output tokens, then at 1024. A later
fresh diagnostic batch used 60-second Judge socket timeouts, a 120-second absolute
feature deadline, at most four new requests, and no retries. It sent three requests:
Pairwise stopped after a length-limited response; calibration returned one normal
response and one length-limited response, leaving one parse failure and three
unscored metrics. The earlier timed-out job was never replayed. Those historical failures remain preserved. A later acceptance batch is recorded below.

Ordinary coding witnesses retain a 512-token/20-second socket cap. Judge witnesses
are now separately bounded at 4096 tokens/120 seconds; selected feature call caps and
the 900-second absolute batch limit still apply. All execution was serial with no
paid-model fallback or redirect following.

### Known boundaries

The blocked/not-applicable list above and in summary.json remains part of the result.
In particular, no live coverage is claimed for Pi/Codex/Claude on an unverified Go
route, Docker/Harbor execution, other wire protocols, unsupported modalities,
generic noncoding benchmark datasets, remote Celery infrastructure, or genuine
human-reviewed calibration qualification. Passing these tiny coding witnesses does
not establish a production gate, statistical model quality, or service guarantees.


### Adequate-budget acceptance (21:28 UTC)

The user requested completion of the recoverable Judge acceptance gaps. Source
`bdcedbc7839e463b526c5a9520ef0bcd10102f90` used 4096 output tokens, 120-second
request timeouts, 240-second feature deadlines, and at most four new serial sends.
All four returned HTTP 200, the exact reported model, and `finish_reason=stop`.
Pairwise passed both swapped presentations with immutable reports and idempotent
submission. Candidate calibration completed its durable job and parsed every
criterion, but the two real grades disagreed: the stronger model-consistency
assertion correctly failed. This is genuine observed model-quality instability,
not truncation or a software lifecycle failure. The prior receipt is retained as
[acceptance.json](opencode-go-live-2026-09-30/acceptance.json).

Functional calibration acceptance checks that measured repeat stability and the
qualification gate match the actual grades. It must not require model-quality
success or label synthetic candidate data as genuine human-reviewed calibration.

## Final functional acceptance (21:37 UTC)

A single additional two-call diagnostic, source
`f2a15ef8dc011c271258aa60a7243d2d118242b3`, retained the actual synthetic
criterion booleans. The first response marked task completion, constraint adherence,
and evidence grounding false; the repeat marked all three true. Both completed
normally with exact model identity. The platform correctly reported stability 0/1,
recorded an instability rejection reason, kept `qualified=false` and
`gate_eligible=false`, and preserved a complete immutable ledger. There are zero
human-reviewed samples. Thus the **calibration lifecycle is accepted; this Judge
model's quality/qualification is not accepted**. No more requests were made.

Latest functional coverage is 16 passed witnesses. Historical failures remain in
all original receipts. Final accounting is 81 authorized attempts plus the separate
3 synthetic-key isolation incident attempts, conservatively 84 total. Expanded
coverage used 45 authorized attempts; reported usage is available for 43 of them:
24,747 prompt + 11,220 completion = 35,967 tokens. The earlier usage limitations
remain. See [final receipt](opencode-go-live-2026-09-30/calibration-lifecycle.json)
and [current summary](opencode-go-live-2026-09-30/summary.json).

Final focused regression: 368 passed, two existing deprecation warnings; separate
qualification suite 39 passed / one environment skip. Independent harness/helper
review reran 65 tests and Ruff successfully. Deterministic software-only fixtures
verify that measured stable/correct judgments require proper reviewed provenance,
while stable-but-wrong, unstable, incomplete, or fabricated provenance remains
ineligible. These fixture tests are not genuine human calibration data.
