"""Materialize a per-session Claude plugin from the shared document contract."""
from pathlib import Path
import json
import shutil


def observe_document_result(tool, block, state, project_id):
    """Summarize actual MCP receipts, never the assistant's completion prose."""
    if block.get('is_error') or tool not in {'mcp__ap-vibe__ap_vibe_update','mcp__ap-vibe__ap_vibe_update_file','mcp__ap-vibe__ap_vibe_read'}:
        return None
    content=block.get('content')
    texts=[content] if isinstance(content,str) else [item.get('text','') for item in content or [] if isinstance(item,dict) and item.get('type')=='text']
    for text in texts:
        try:result=json.loads(text)
        except (ValueError,TypeError):continue
        if not isinstance(result,dict) or not result.get('ok') or result.get('project_id')!=project_id:
            continue
        revision=result.get('revision')
        if type(revision) is not int:continue
        if tool.endswith(('ap_vibe_update', 'ap_vibe_update_file')):
            state.update(revision=revision,updated_sections=result.get('updated_sections') or [],
                         read_sections=[],readback_complete=False)
            return f'项目档案已写入第 {revision} 版，正在等待实际章节回读。'
        if revision==state.get('revision') and not state.get('readback_complete'):
            state['read_sections']=sorted(set(state.get('read_sections',[])) | set(result.get('sections') or {}))
            if state.get('updated_sections') and set(state['updated_sections']).issubset(state['read_sections']):
                state['readback_complete']=True
                return f'项目档案第 {revision} 版已回读全部更新章节；内容质量仍需成果验收。'
    return None


def prepare_plugin(directory: Path, product_root: Path) -> Path:
    plugin = directory / 'ap-vibe-plugin'
    manifest = plugin / '.claude-plugin'
    skill = plugin / 'skills' / 'project-context'
    manifest.mkdir(parents=True, exist_ok=True)
    (skill / 'references').mkdir(parents=True, exist_ok=True)
    (manifest / 'plugin.json').write_text(json.dumps({
        'name': 'ap-vibe', 'version': '0.1.0',
        'description': 'AP-Vibe project context and incremental dossier maintenance',
    }), encoding='utf-8')
    shutil.copyfile(product_root / 'skills/ap-vibe-claude/SKILL.md', skill / 'SKILL.md')
    for name in ('project-documents.md', 'agent-collaboration.md', 'session-continuation.md'):
        shutil.copyfile(product_root / 'skills/ap-vibe-task-context/references' / name,
                        skill / 'references' / name)
    return plugin
