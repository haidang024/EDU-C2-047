# Template Design Specification — EDU-C2-047

## Position in AgentCore Architecture

- **Agent Class**: `Graph` (`src.graph.graph.Graph`)
- **L1 Base**: `AgentBaseGraph` (direct L1 inheritance — no retired L2 base)
- **Three-Layer Separation**:
  - State: flat TypedDict (`State` extends `AgentState`) — no Pydantic, msgpack-safe
  - Node: L1 `FunctionNode` inheritance — `execute(self, state: dict) -> dict` override only
  - Graph: composition via `register_nodes()` — outer `AgentBaseGraph` + inner `BaseGraph`

---

## Architecture Overview

### Node Configuration

| Node | Responsibility | Input State Keys | Output State Keys | Class / Trust |
|------|---------------|-----------------|-------------------|---------------|
| `initialize` | Framework init | — | `correlation_id`, `trace_id`, `session_id` | `InitializeNode` (default) |
| `pre_process` | Config validation & guidance index load (Step 1) | `user_input`, `input_context.config` | `guidance_index_ref`, `guidance_version`, `guidance_last_updated`, `guidance_staleness_warning`, `review_period`, `institution_id`, `output_format`, `field_mapping`, `record_query_selector`, `freshness_threshold_days` | `PreProcessNode` / `VERIFIED_EXTERNAL` |
| `main` | Delegates to inner `DomainWorkflowGraph` | `validated_input`, all Step-1 fields | `result` (briefing), `consistency_findings`, `retrieval_provenance`, `retrieval_error`, `check_error`, `output_gate_blocked` | `AccommodationPlanWorkflowNode` (GraphNode) |
| `post_process` | Deliver briefing or block on failure (outer gate) | `result`, `retrieval_error`, `check_error` | `formatted_output` | `PostProcessNode` / `VERIFIED_EXTERNAL` |
| `finalize` | Framework metadata | — | `execution_time`, final status | `FinalizeNode` (default) |

**Inner `DomainWorkflowGraph` nodes (all `ANONYMOUS` — trust-trap anti-pattern avoided):**

| Inner Node | Responsibility | Trust |
|-----------|---------------|-------|
| `record_retrieval` | Fetch & PII-minimise records (Step 2) | `ANONYMOUS` |
| `consistency_check` | Grounded LLM analysis & citation validation (Step 3) | `ANONYMOUS` |
| `briefing_generation` | Render Markdown & apply output gate (Step 4) | `ANONYMOUS` |

### Data Flow

```
START → initialize → pre_process → main (AccommodationPlanWorkflowNode)
                                      │
                                      ▼ inner DomainWorkflowGraph
                                      record_retrieval
                                           │ failure → END (error)
                                      consistency_check
                                           │ failure → END (error)
                                      briefing_generation
                                           │
                                      ◄──── sub_result returned
                                      │
                              → post_process → finalize → END
                     (retry ↑ on RETRY status, max 3)
```

Failure branches halt before downstream side effects:
- Config/validation failure in `pre_process` → no record access
- Retrieval failure → no LLM call, no output
- Check failure → no briefing
- Output gate → cleans identifiers/credentials before delivery

### State Definition

| Field | Type | Purpose | Introduced by |
|-------|------|---------|--------------|
| `guidance_index_ref` | `str` | Opaque SHA-256 reference to the session guidance index | pre_process |
| `guidance_version` | `str` | e.g. `"2024-Q4-v1"` | pre_process |
| `guidance_last_updated` | `str` | ISO-8601 date | pre_process |
| `guidance_staleness_warning` | `str` | Non-empty when guidance age exceeds threshold | pre_process |
| `review_period` | `str` | e.g. `"2025-01"` | pre_process |
| `institution_id` | `str` | Opaque institution identifier — no PII | pre_process |
| `output_format` | `str` | `"markdown"` only | pre_process |
| `field_mapping` | `dict` | Canonical → source-system field name map | pre_process |
| `record_query_selector` | `str` | Record filter expression | pre_process |
| `freshness_threshold_days` | `int` | Max guidance age before staleness warning | pre_process |
| `sanitized_plan_records` | `list` | Sanitized plan dicts — no direct student PII | record_retrieval |
| `retrieval_source_type` | `str` | `"api"` \| `"csv"` \| `"json"` | record_retrieval |
| `retrieval_provenance` | `dict` | `{source_type, source_ref, record_count}` | record_retrieval |
| `retrieval_error` | `str` | Non-empty on retrieval failure | record_retrieval |
| `consistency_findings` | `list` | Structured finding dicts with citation | consistency_check |
| `findings_suppressed_count` | `int` | Findings dropped for missing citation | consistency_check |
| `check_error` | `str` | Non-empty on LLM/check failure | consistency_check |
| `briefing_markdown` | `str` | Final Markdown briefing | briefing_generation |
| `output_gate_blocked` | `bool` | True when gate cleaned/blocked content | briefing_generation |
| `output_gate_reason` | `str` | Description of what the gate triggered on | briefing_generation |
| `audit_ids` | `list` | Trace/correlation IDs collected during run | — |

**State Constraints (mandatory):**
- Flat TypedDict only (primitives + JSON-serializable types)
- No JWT, API keys, credentials in State (checkpoint DB leakage)
- `InvocationContext` via `InvocationContext.from_state(state)` only — not stored in State
- No Pydantic models, dataclass, or arbitrary Python objects (msgpack incompatible)
- No raw student identifiers (name, email, student number, DOB) — replaced by `review_ref`

---

## Framework Utilization

### Shared Components Used
- [x] `InvocationContext` — `from_state(state)` in `RecordRetrievalNode` to access `ctx.secrets`
- [x] `SecurityViolationError` — raised by `_extra_security_gate_input` on PII/credential detection
- [x] S-2: `_extra_security_gate_input()` — implemented on `PreProcessNode` (student ID in query), `RecordRetrievalNode` (credential in config)
- [x] S-3: `_extra_security_gate_output()` — implemented on `ConsistencyCheckNode` (decision language), `BriefingGenerationNode` (PII/credential in Markdown)
- [x] S-4: `emit_trace_event()` — at least one domain event in every `execute()` path

> S-2/S-3 gates on `FunctionNode` subclasses run automatically via `@final` — extended via hooks only.
> `AccommodationPlanWorkflowNode` (GraphNode) intentionally has no extra gates (ADR-017 no-op rule).

### Composition Pattern

- **Pattern**: GraphNode (subgraph) — `AccommodationPlanWorkflowNode` wraps `DomainWorkflowGraph`
- **Composition target**: `src/graph/domain_workflow_graph.py` — `DomainWorkflowGraph(BaseGraph)`
- **Error propagation strategy**: `propagate` — inner errors re-raised as `SubgraphError`

## Import Isolation Confirmation
- [x] Template does not import `agenticstar` platform SDK (Level 0) — PB-4 AST scan enforces this
- [x] Import targets: `framework.*`, `shared.*`, `src.*`, `langgraph.*` only

---

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | `AgentBaseGraph` | `AutonomousBaseGraph` | `AgentBaseGraph` | The pipeline is deterministic and bounded; no LLM-driven think-act loop required |
| Inner graph parent | `BaseGraph` (custom topology) | `AgentBaseGraph` (fixed backbone) | `BaseGraph` | Inner nodes have custom names and conditional failure routing; `AgentBaseGraph` would force pre_process/main/post_process slots unnecessarily |
| PII gate strategy | Hard stop on any PII field detected | Strip and continue | Hard stop | Partial data with stripped PII risks silent data quality issues; hard stop forces correct field mapping |
| Guidance freshness | Hard error on stale guidance | Non-blocking warning in briefing | Non-blocking warning | Stale guidance is a reviewer concern, not a system failure; the briefing prominently flags it |
| Output gate on decision language | Strip | Flag and log | Flag and log | Decision language in rationale text is a reviewer signal; stripping it would obscure the finding |

## Dependency Injection Boundary

The standalone adapter loads `config/config.yaml` and provisions a chained
environment/domain secret provider. Nodes create Azure clients per invocation
without storing clients in State. `Graph.register_nodes()` uses the scaffold
contract:

```python
self._nodes["main"] = AccommodationPlanWorkflowNode(
    config=self.config,
    llm=self.config.get("llm"),
)
```

The wrapper preserves the client while creating `DomainWorkflowGraph`, which
injects it into `ConsistencyCheckNode`. Missing credentials therefore degrade
to `None` without preventing application boot. The comparison currently uses
the deterministic, citation-gated implementation; the injected interface is
available for a provider-backed implementation without introducing a global
client or reading environment variables in a domain node.

## EU AI Act Article 13 Transparency

- The output identifies itself as a restricted review aid and states that it
  does not determine accommodation eligibility or approval.
- Each finding carries the reviewed reference, guidance source, section,
  version, severity, and a mandatory human-confirmation flag.
- The briefing exposes source provenance, record count, guidance freshness,
  suppressed findings, and blocking errors.
- Operators must validate institutional guidance and source mappings before
  relying on a run; stale guidance is prominently disclosed.
- Known limitations include synthetic comparison logic, bounded approved source
  adapters, and possible false positives or omissions requiring human review.
