# ENMA Task System Reference (M3/M4)

How a request becomes an executed, audited, remembered task.

## Task lifecycle

```
REQUEST → CREATE TASK → PLAN → BUILD EXECUTION SPEC
       → RESOLVE SKILL → RESOLVE TOOLS → PERMISSION CHECK
       → EXECUTE → REFLECT → (bounded auto-retry when safe)
       → RESULT → AUDIT → MEMORY
```

States (`backend/core/task.py`): `PENDING → RUNNING → COMPLETED | FAILED | CANCELLED`,
with `FAILED → PENDING` for controlled retry. Every transition is validated by
`TaskService` against `VALID_TASK_TRANSITIONS`; nothing else may assign `Task.status`.

- Stable IDs: UUID4, assigned at creation, persisted.
- Observable state: `GET /api/tasks/{id}` returns status, timestamps, retry count,
  result, error, reflection and the full step list (order, tool, params, result,
  error, permission decision).
- Failures never disappear: step errors land on the step and the task, reflection
  classifies the run, and every stage appends to the audit JSONL.

## ExecutionSpec

`backend/core/execution_spec.py` — the immutable plan-of-record between planning
and execution. Built only by `build_execution_spec(planning, task_id, registry, ...)`.
Tool-less (advisory) planned steps are dropped; executable steps are renumbered
densely; unknown tools are counted DANGEROUS (fail-closed summary). Each step may
declare `depends_on` (orders of strictly earlier steps). `task_steps_from_spec`
(`agents/pipeline.py`) is the only place a plan becomes concrete `TaskStep`s.

## Dependencies

`TaskStep.dependencies` lists prerequisite step orders. `AutomationEngine`
enforces them fail-closed in both `run()` and `run_async()`: a step runs only if
every prerequisite is COMPLETED; unknown prerequisites at/after the first step of
the pass, or unmet prerequisites, fail the step and the workflow — a dependent
step is never executed out of order. The planner may emit `depends_on` (indices
into its steps list); forward/self references are dropped at parse time, and
references to dropped advisory steps are discarded when the spec is built.

## Skills

One registry (`skills/registry.py`), one router (deterministic lexical scoring),
one runner, one stage seam. Builtin skills: `memory-recall`, `note-summarizer`,
`document-creation`, `task-breakdown`, `skill-catalog`, `permission-explain`.
Skills needing async tools (model gateway) are coroutine functions
(`async def run`) and are awaited through `SkillRunner.execute_async`; sync skills
work unchanged. A skill whose required tool is missing is reported
`available: false` by `GET /api/skills` and is never executed — unavailable
capability is reported, never faked.

To add a skill: subclass `Skill` in `backend/skills/builtin/`, declare
`SkillMetadata` (name, description, `required_tools`, risk level, entrypoint),
implement `run` (or `async def run`), and add the class to `BUILTIN_SKILLS`.

## Tools

One registry (`backend/tools/registry.py`) + one dispatch table
(`TOOL_IMPLEMENTATIONS` in `core/agent_services.py`). A tool name must exist in
BOTH before anything runs. Every invocation goes
Task → AutomationEngine → Agent → PermissionPolicy → tool → structured result.

Current builtin tools:

| Tool | Risk | Purpose |
|---|---|---|
| `fs_read_file` | SAFE | Read a text file (≤1 MB, path-jailed) |
| `fs_list_directory` | SAFE | List directory entries |
| `fs_file_exists` | SAFE | Check file existence |
| `fs_write_file` | SENSITIVE | Write UTF-8 text inside the workspace root only (approval required) |
| `model_generate` | SAFE | One model answer through the single gateway |
| `memory_search` | SAFE | Query the persistent memory store (read-only) |
| `summarize` | SAFE | Summarize text through `model_generate` |
| `web_search` | SAFE | Public web search through the configured provider (read-only) |
| `web_fetch` | SAFE | Retrieve one public HTTP(S) page with SSRF/size/time/redirect limits |

To add a tool: implement a function `tool(params: dict) -> result` that validates
its own untrusted params and raises a typed error on bad input, register it in
`register_builtin_tools` (with category + risk level) and wire it into
`TOOL_IMPLEMENTATIONS`. Never call a provider or open a second registry.

## Permissions

`PermissionPolicy` semantics are unchanged: SAFE→ALLOW, SENSITIVE→REQUIRE_APPROVAL,
DANGEROUS→DENY, unknown→DENY (fail-closed). Approval grants are one-shot per
task+tool, created only when a run pauses, consumed only at execution time.
A denied approval terminalizes the workflow. Denials also get a dedicated
PERMISSION audit row.

## model_generate

The only model-facing tool (`tools/builtin/model.py`). It calls the single
`orchestrator` gateway; no provider is imported anywhere in the task system.
Length-bounded prompt/output, credential-redacted failures, async-only execution.

## Reflection, retry, recovery

`ReflectionEngine.reflect_on_task` classifies every finished run (SUCCEEDED /
PARTIAL / FAILED / DENIED / AWAITING_APPROVAL / UNKNOWN) from the real
AutomationResult — read-only, no execution. Automatic recovery is bounded and
safe by construction (`TaskRunner._can_auto_retry`): only FAILED runs whose steps
are ALL explicitly SAFE tools, with no permission denial, and under
`MAX_TASK_RETRIES` (3, tracked on the task). The number of automatic re-executions
is reported honestly in the run envelope (`auto_retries`) and on the task
metadata. Manual retry (POST /api/tasks/{id}/retry) remains available with the
same cap.

## Audit

Append-only JSONL (`GHOST_AUDIT_PATH`, default `backend/data/audit.jsonl`,
rotation, secret redaction). Stages include request, planned, spec, skill,
permission, tool, approval, model, result, reflection, memory, lifecycle, error.

## Memory bridge

`MemoryBridge.record_outcome` writes a ≤700-char lesson to the existing
MemoryService when reflection recommends it (failures/partial outcomes), and
never breaks the run. Memory content feeds back into planning as context refs —
never duplicated into the spec or audit.

## Persistence

Tasks persist to JSONL (`GHOST_TASKS_PATH`, default `backend/data/tasks.jsonl`):
one record per mutation, newest line per id wins on load, corrupt lines skipped.
Bookkeeping only (state, result, reflection summary, timestamps) — workflow steps
remain in-memory; the audit log is the durable step record. A restarted task can
be inspected and audited but not resumed mid-workflow.

## Task APIs

| Endpoint | Purpose |
|---|---|
| `POST /api/tasks` | Plan + create + execute from a natural-language request |
| `GET /api/tasks` | List tasks (metadata) |
| `GET /api/tasks/{id}` | Full detail incl. step progress |
| `POST /api/tasks/{id}/cancel` | Cooperative cancellation at step boundary |
| `POST /api/tasks/{id}/retry` | Re-queue a FAILED task (cap 3) |
| `GET /api/approvals` | Pending approvals |
| `POST /api/approvals/{id}/decision` | Record human decision (never executes) |
| `POST /api/tasks/{id}/resume` | Continue an approved workflow |
| `GET /api/skills` | Skill catalog incl. availability |
| `GET /api/audit` | Read-only audit rows (task_id/stage filters) |

## Testing a task

Unit: see `tests/test_m4_task_system.py` (tools, dependencies, retry,
persistence) and `tests/test_pipeline.py` (full stack with fakes — no network).
API: `tests/test_tasks_api.py`, `tests/test_task_lifecycle.py` use the
`auth_client` fixture (TestClient + real session auth; the planner gateway is
monkeypatched, never the permission path).

## External intelligence layer (M5)

### Provider architecture

`backend/tools/builtin/web_search.py` — the `web_search` tool goes through
`WebSearchService`, which resolves one swappable `WebSearchProvider` adapter:

- `tavily` (default when `TAVILY_API_KEY` is set) — key from the environment only
- `duckduckgo` (keyless) — set `ENMA_SEARCH_PROVIDER=duckduckgo` to force it

With no provider configured, the tool fails with a diagnostic explaining how to
configure one. Search results are never invented as a fallback. A search result
is a LEAD (provenance stub), not a source.

### URL retrieval

`backend/tools/builtin/web_fetch.py` retrieves ONE public page per call:

- http/https only; embedded credentials refused
- every resolved IP must be public — loopback, RFC1918/CGNAT, link-local
  (169.254.169.254 metadata) and reserved ranges are refused, for literal-IP
  hosts and DNS-resolved names alike; redirects are followed manually with
  re-validation per hop (max 5)
- 15 s timeout, 512 KB body cap (Content-Length and streamed read), HTML
  reduced to title + visible text (script/style dropped)

### Source provenance

`backend/research/sources.py` — `ResearchSource` (url, title, domain,
retrieved_at, content excerpt, status, error, via_query). Statuses are honest:
`retrieved | failed | timeout | blocked | skipped`. URLs are normalized and
deduplicated. A failed retrieval is recorded as failed — never presented as
evidence.

### research skill

`skills/builtin/research.py` — objective → search → retrieve top pages →
synthesize via `model_generate` → structured result:

```json
{
  "topic": "...",
  "summary": "...synthesis with [n] source attributions...",
  "findings": [],
  "sources": [{"url": "...", "status": "retrieved", ...}],
  "limitations": ["dead links, timeouts, conflicts..."],
  "source_summary": {"total": n, "retrieved": n, "domains": [...]}
}
```

Conflicts between sources are preserved as disagreements, not resolved
silently. If retrieval all fails: zero findings, explicit limitation — nothing
fabricated. Limits (env-overridable): `ENMA_RESEARCH_MAX_PAGES` (default 3,
cap 8), `ENMA_RESEARCH_MAX_QUERIES` (default 1, cap 3).

### Untrusted web content

Retrieved page text is DATA, never instructions: the synthesis prompt wraps
every excerpt in `<untrusted_content>` framing with an explicit rule that
instructions inside webpage content are ignored. Page text can never override
system, task, tool or permission policy.

### Step output references

A step's params may consume an earlier step's output through
`{"from_step": N, "field": "a.b"}` reference objects (dot-paths support dict
keys and list indices). Strict validation: the referenced step must be in the
consuming step's `dependencies`, must be COMPLETED, and the field must exist.
Resolved values are stringified and capped at 8000 chars. Without a declared
dependency there is no data flow.

### Research → memory and audit

A completed research run appends one bounded memory record through the existing
MemoryBridge (topic + synthesis excerpt + source domains; full source URLs in
the record metadata) and one `research`-stage audit row with the source
summary (counts + domains, never page text). Uses the single AuditLog — no
parallel audit system.
