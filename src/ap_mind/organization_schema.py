"""Expose the existing curation contract to structured-output capable clients."""
from .project_documents import DIMENSIONS, SECTION_INFO


def output_schema(index):
    if index.get("scope") == "logic_analysis":
        return None
    text = {"type":"string"}
    refs = {"type":"array","items":text}
    keys = [item["source_key"] for item in index.get("sources", [])]
    source = {"type":"string", **({"enum":keys} if keys else {})}
    assessment = {"type":"object", "properties":{
        "key":{"type":"string","enum":[key for key,_ in DIMENSIONS]},
        "score":{"type":["number","null"],"minimum":0,"maximum":100},
        "reason":text,"risk":text,"improvement":text,"evidence_refs":refs},
        "required":["key","score","reason","risk","improvement","evidence_refs"]}
    chapters = {key:{"type":"object"} for key in SECTION_INFO}
    chapters["risks"] = {"type":"object","properties":{"assessment":{"type":"array","minItems":10,"maxItems":10,"items":assessment}},
                        "required":["assessment"]}
    sections = {"type":"object","properties":chapters,"required":list(chapters)}
    project_ids = [p["project_id"] for p in index.get("projects",[])]
    project = {"type":"string", **({"enum":project_ids} if project_ids else {})}
    if index.get("scope") == "project_refresh":
        value = {"type":"object","properties":{"project_id":project,"expected_revision":{"type":"integer","minimum":0},
                 "evidence_refs":refs,"sections":sections},"required":["project_id","expected_revision","evidence_refs","sections"]}
        return {"type":"object","properties":{"project":value},"required":["project"]}
    group = {"type":"object","properties":{"project_id":project,"name":text,
             "expected_revision":{"type":"integer","minimum":0}, "source_keys":{"type":"array","items":source,"minItems":1},
             "rationale":text,"evidence_refs":refs,"sections":sections},
             "required":["source_keys","rationale","evidence_refs","sections"]}
    skip = {"type":"object","properties":{"source_key":source,"reason":text,"category":text},"required":["source_key","reason"]}
    return {"type":"object","properties":{"groups":{"type":"array","items":group},
            "skipped":{"type":"array","items":skip}},"required":["groups","skipped"]}
