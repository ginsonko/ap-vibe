# Structured project document protocol

This API is local document management with task attribution. It does not
replace AP action selection or turn agent prose into reviewed knowledge.

| Chapter | Keep here |
| --- | --- |
| identity | name, summary, audience, purpose |
| requirements | goals, acceptance, redlines |
| architecture | summary, flow, modules, intentional_design and reasons |
| sources | documents: title, path, purpose; code_roots: absolute paths; connections: server/location, method and credential_location (location only, never credential values) |
| decisions | items: id, status, old_logic, new_logic, reason, evidence_refs |
| work | completed, remaining, not_implemented, blocked, next_action |
| risks | incidents, assessment; unresolved conditions and improvement actions |
| evidence | checks, discoveries, incidents; actual results and evidence paths |
| dependencies | items: name, purpose, status, entry |
| status | summary, phase, unknown |
| recovery | summary, next_action, entry_points, do_not_repeat |

First obtain `knowledge` without `--sections` for a compact catalog; read at
most 3 chapters per request. URLs are in the catalog. It returns the current
agent document revision, a reviewed baseline revision and per-chapter source.
The document revision is independent of the reviewed revision.

Each included chapter replaces that chapter. Preserve existing fields and
items that remain valid, explicitly mark superseded/revoked decisions and
resolve old pending work. Omitted chapters are not changed. Empty object
replacements are rejected to avoid accidental erasure. Unknown custom fields
are preserved within chapters. Prefer bounded facts and pointers over logs.

Example (substitute the actual revision and facts):

```json
{
  "request_id": "project-checkpoint-unique-id",
  "expected_revision": 0,
  "sections": {
    "identity": {
      "name": "家庭记账工具",
      "summary": "在本机登记家庭支出，按月查看分类汇总。",
      "audience": "家庭成员"
    },
    "requirements": {
      "goals": ["离线也可记账，用户能看懂月度收支"],
      "redlines": ["账单不上传外部服务"]
    },
    "work": {
      "completed": ["分类表单已完成，验证依据：docs/form-check.md"],
      "remaining": ["核验离线保存和重开恢复"],
      "next_action": "运行离线恢复验证"
    }
  }
}
```

Assessment has 10 stable keys: `intent`, `logic`, `completeness`, `reliability`,
`security`, `performance`, `maintainability`, `compatibility`, `usability`,
`validation`. A list entry example:

```json
{"key":"reliability","score":null,"reason":"尚未运行断电恢复测试","evidence_refs":[],"risk":"写入期间关闭程序的结果未知","improvement":"核验事务提交与冷启动恢复"}
```

A numerical score must be finite and between 0 and 100, with a nonempty reason
and evidence references. State what the evidence covers and misses. A practical
rubric: 0–24 has a demonstrated critical gap; 25–49 works only in narrow cases;
50–69 has a usable path with material gaps; 70–84 has representative validation
with known limits; 85–100 needs broad real use and resilience evidence. Unknown
is null, never zero. The interface shows reasons, risks and next improvements.

History stays immutable. To inspect a past agent snapshot use
`knowledge --revision N --sections decisions`; a historical query shows only
that agent snapshot, never an overlay from a newer reviewed baseline.

`document_revision_conflict`: reread, merge, save a new patch id.
`document_request_conflict`: the same id was used for different input; do not
retry changed content under it. `document_hash_mismatch`: stop using that
document and investigate; do not overwrite to hide the inconsistency.

On first curation of a maintainable project, all 11 chapters must be present.
That is only the minimum shape: a dossier is not ready when its fields are
copied from auto discovery. Any `provenance=auto_detected` or
`confidence=unverified` value is provisional and the UI must label it as such.
Identity must be checked against real project goals, code, design or deployment
evidence. Unknown facts are explicit, with a specific verification entry.
Later tasks read and update affected chapters while filling missing chapters;
do not mechanically rewrite all chapters. The `project_refresh` operation is
for registered projects only and cannot create projects or move sessions. It
requires all 11 chapters and exactly ten assessment entries, including a
reason, risk, improvement and evidence boundary for every unknown score. A
failed refresh is a retained draft and never replaces the previous revision.
`credential_location`, `api_key_path` and similar location fields preserve the
pointer, never the value. Never open secret files just to catalogue their
location.

## Incremental maintenance and task closure

Read each affected chapter before composing its replacement. An update is a
new project snapshot, not a transcript of this one task:

- Additive records: decisions, incidents, discoveries, verification receipts,
  dependencies and source pointers retain still-useful existing entries. Use
  stable IDs or source references to deduplicate. Keep revoked decisions with
  old_logic, new_logic, reason and evidence; do not silently erase them.
- Whole-project truth: identity, requirements, architecture and status describe
  the entire maintained project. Merge this task's changes into that context.
  A single feature completion does not mean the whole product is complete.
- Work and recovery: move resolved work to completed with evidence; preserve
  unrelated remaining work, redlines and recovery entry points. Old next steps
  that no longer apply are marked superseded with a reason.
- Assessment: keep exactly ten stable dimensions. Reassess dimensions affected
  by the task and fill previously absent dimensions from existing evidence.
  Preserve unaffected assessments and their evidence dates. Known gaps warrant
  proportionate scores; unknown facts stay null with a concrete reading or
  verification entry, never a copied generic sentence for every dimension.
  For every scored dimension, connect a concrete project behavior or source
  finding to the judgement, a specific failure scenario, and an actionable
  improvement. Reusing one source is fine; copying the same explanation and
  changing only the score is not an assessment. Review existing explanations
  as well as new ones. Chapter replacement must preserve still-valid details;
  brevity must not erase connections, incidents, decisions or unfinished work.
- User-edited fields remain authoritative. Agent batch refresh retains those
  chapters. If a correction is needed, explain the conflicting evidence in a
  separate note rather than overwriting the user's content.

Example: adding retry to an upload service updates architecture.modules.upload
and its recovery flow, adds a decision explaining the former timeout behavior,
adds the real retry test to evidence.checks, moves that one work item to
completed, and updates reliability with its evidence boundary. Other features,
past billing incidents, deployment locations and unimplemented work remain.

Before finishing a long-term task, read the current catalog and risks, complete
missing chapters or dimensions, submit the merged patch, then read back the
changed chapters. Report the returned revision and actual coverage (11 chapters
and 10 assessed dimensions are different counts). A saved patch or a timeout
is pending work, not proof of successful writeback. Keep the same request_id
for an uncertain transport result; reread and merge on a revision conflict.
