"""Small run rows for discovery; execution records remain available by ID."""
from urllib.parse import quote


def run_summary(run):
    fields = ('run_id', 'agent_id', 'name', 'model', 'executor_kind', 'state', 'project_id',
              'profile_revision', 'appearance_id', 'avatar', 'logical_task_id', 'depends_on', 'continue_run_id',
              'handoff_from_run_id', 'created_at', 'updated_at', 'execution_started_at',
              'execution_finished_at', 'document_maintenance')
    value = {key: run[key] for key in fields if key in run}
    prompt = str(run.get('prompt') or '')
    value.update(prompt=prompt[:240], prompt_truncated=len(prompt) > 240,
                 detail_url='/v1/ap-vibe/agents/runs?run_id=' + quote(run['run_id'], safe=''),
                 projection='summary')
    if run.get('task_snapshot'):
        value['task_snapshot'] = {'title': str(run['task_snapshot'].get('title') or '')[:160]}
    return value
