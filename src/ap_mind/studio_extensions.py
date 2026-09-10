"""Optional installed tools, scoped to a run and independent of model choice."""
import json
import os
from pathlib import Path
import shutil
import sys
from urllib.parse import urlsplit

from .contracts import ContractError


def extension_ids(raw):
    values = raw.get('extensions', [])
    if not isinstance(values, list) or any(not isinstance(x, str) or x != 'yinzi-media' for x in values):
        raise ContractError('agent_extensions_invalid')
    return sorted(set(values))


def prepare_extensions(selected, directory, environ=None):
    env = os.environ if environ is None else environ
    result = {'mcp': {}, 'plugins': [], 'allowed_tools': [], 'env': {}, 'instructions': '', 'status': []}
    if 'yinzi-media' not in selected:
        return result
    base = Path(env.get('LOCALAPPDATA') or (Path.home() / '.local/share')) / 'Yinzi/CodexVideoWorkflow'
    source = Path(env.get('AP_VIBE_MEDIA_PLUGIN_ROOT') or base / 'plugin-runtime/plugins/codex-yinzi-universal-video-workflow')
    registry = Path(env.get('AP_VIBE_MEDIA_RUNTIME_PATH') or base / 'runtime.json')
    node = env.get('AP_VIBE_NODE') or shutil.which('node')
    skill = source / 'skills/codex-yinzi-universal-video/SKILL.md'
    server = source / 'mcp/server.mjs'
    if not node or not skill.is_file() or not server.is_file():
        result['instructions'] = ('\n本次选择了媒体工作流，但本机尚未找到完整插件或Node。'
            '可继续处理已有本地素材；不得声称调用了生图。请报告缺失组件，不读取密钥来绕行。')
        result['status'] = [{'id': 'yinzi-media', 'available': False, 'reason': 'plugin_or_node_missing'}]
        return result
    binding = {}
    if registry.is_file():
        try:
            runtime = json.loads(registry.read_text(encoding='utf-8-sig'))
            url = runtime.get('api_base', '')
            parts = urlsplit(url)
            if parts.scheme == 'http' and parts.hostname in {'localhost', '127.0.0.1', '::1'} and not parts.username and not parts.password:
                binding['YINZI_WORKFLOW_URL'] = url.rstrip('/')
        except (OSError, ValueError, TypeError):
            pass
    # Keep relative imports inside the installed Skill/CLI valid, without
    # copying .mcp.json or any user configuration into the managed workspace.
    plugin = directory / 'extensions/yinzi-media'
    for name in ('skills', 'scripts', 'mcp'):
        if (source / name).is_dir():
            shutil.copytree(source / name, plugin / name, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns('__pycache__', '.env', '*.log'))
    manifest = plugin / '.claude-plugin'
    manifest.mkdir(parents=True, exist_ok=True)
    (manifest / 'plugin.json').write_text(json.dumps({'name': 'yinzi-media', 'version': '0.1.0',
        'description': 'Installed Yinzi media workflow for this AP-Vibe run'}), encoding='utf-8')
    proxy = Path(__file__).resolve().parents[2] / 'tools/media_mcp_proxy.py'
    result['mcp']['yinzi-media'] = {'command': sys.executable,
        'args': [str(proxy), '--node', str(node), '--server', str(plugin / 'mcp/server.mjs'),
                 '--results-dir', str(directory / 'media-tool-results')], 'env': binding}
    result['plugins'].append(str(plugin))
    result['allowed_tools'].append('mcp__yinzi-media__*')
    result['env'].update(binding)
    result['status'].append({'id': 'yinzi-media', 'available': True,
                            'binding': binding.get('YINZI_WORKFLOW_URL'), 'source': str(source)})
    result['instructions'] = (
        '\n本任务已接入银子万能媒体工作流。Claude用Skill加载 yinzi-media:codex-yinzi-universal-video；'
        f'Codex读取 {plugin / "skills/codex-yinzi-universal-video/SKILL.md"}。'
        '媒体MCP名称前缀为yinzi-media。当前扩展代码是已安装插件的任务副本，勿升级该副本。'
        'get_session默认给紧凑目录；确认session只看身份/状态，需要时用node_keys读取具体节点，不要通读历史文件。'
        '先读取真实任务/配置目录，仅使用保存配置ID，不读取或输出Key。'
        '恢复已有媒体工作必须复用原session和generation；不确定结果只对账。'
        '创建新媒体任务的稳定幂等键使用当前run_id加任务目的，原任务在续办时继续复用。'
        '本地命令工具可用于当前成果目录的图像提取、透明处理、哈希和播放验收。'
        f'本次可用Python解释器为 {sys.executable}；优先使用Pillow等现成图像库，避免手写PNG解码器。'
        '实际模型生成遵守用户授权；工具已载入不表示图片已生成。'
    )
    return result
