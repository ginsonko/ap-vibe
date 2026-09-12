"""Classify verified tool actions without retaining arguments or model reasoning.

These are display hints, never scheduling or permission decisions. Unknown
tools keep the last meaningful activity rather than inventing a new action.
"""
import json
import re

READ = {'read', 'readfile', 'readfiles', 'getfile', 'glob', 'grep', 'search',
        'webfetch', 'websearch', 'websearchpreview', 'viewimage', 'listdirectory',
        'listfiles', 'fetch', 'apvibecontext', 'apviberead', 'apvibeprojects',
        'apvibesessions', 'apvibesessionread', 'apvibeartifacts', '读取文件', '搜索', '查阅资料'}
WRITE = {'write', 'writefile', 'edit', 'editfile', 'multiedit', 'filechange',
         'applypatch', 'patch', 'apvibeupdate', 'apvibeupdatefile', 'apvibeclassify', '写入文件', '编辑文件'}
TEST = {'runtests', 'test', 'pytest', 'apvibereviewsubmit', 'apviberunreview', '测试'}
DISCUSSION = {'apvibecollaborationsend', 'apvibecollaborationbroadcast', 'sendmessagetothread'}
ACTIVITIES = {'read', 'write', 'test', 'discuss'}


def command_activity(command):
    if not isinstance(command, str):
        return None
    # Match actual command positions, not words in filenames or prose.
    text = command[:64000]
    start = r'(?:^|[;\n|&]\s*)\s*'
    if re.search(start + r'(?:python[\d.]*\s+-m\s+pytest|pytest|vitest|jest|npm\s+(?:run\s+)?test|pnpm\s+test|cargo\s+test|go\s+test|dotnet\s+test)\b', text, re.I):
        return 'test'
    if re.search(start + r'(?:apply_patch|Set-Content|Add-Content|Out-File|tee|touch|mkdir|Copy-Item|Move-Item)\b', text, re.I):
        return 'write'
    if re.search(start + r'(?:rg|grep|cat|head|tail|ls|find|Get-Content|Get-ChildItem|Select-String|git\s+(?:diff|show|status|log))\b', text, re.I):
        return 'read'
    return None


def tool_activity(name, arguments=None):
    name = str(name or '').rsplit('__', 1)[-1].rsplit('.', 1)[-1]
    token = re.sub(r'[_\-\s]', '', name).casefold()
    for group, activity in ((WRITE, 'write'), (TEST, 'test'), (READ, 'read'), (DISCUSSION, 'discuss')):
        if token in group:
            return activity
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (ValueError, TypeError):
            arguments = {'command': arguments}
    if not isinstance(arguments, dict):
        return None
    if token in {'bash', 'powershell', 'shell', 'shellcommand', 'execcommand', 'commandexecution', 'runcommand', 'execute'}:
        return command_activity(arguments.get('cmd') or arguments.get('command') or arguments.get('script'))
    if token == 'exec':
        code = arguments.get('code') or arguments.get('command') or ''
        # The Codex JS orchestration wrapper names its nested calls explicitly.
        if isinstance(code, str):
            if re.search(r'\btools\.apply_patch\s*\(', code):
                return 'write'
            match = re.search(r'\btools\.exec_command\s*\(\s*\{\s*cmd\s*:\s*("(?:[^"\\]|\\.)*")', code)
            if match:
                try:
                    return command_activity(json.loads(match[1]))
                except ValueError:
                    pass
            if re.search(r'\btools\.(?:view_image|read_mcp_resource)\s*\(', code):
                return 'read'
    return None


def event_activity(event):
    if not event:
        return None
    hint = event.get('activity')
    known = isinstance(hint, str) and hint in ACTIVITIES
    kind = event.get('kind', 'tool')
    if kind == 'tool_result':
        return hint if known else None
    if kind != 'tool':
        return None
    return hint if known else tool_activity(event.get('tool'))
