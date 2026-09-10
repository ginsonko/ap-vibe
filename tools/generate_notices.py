"""Preserve installed frontend dependency notices alongside the pinned lockfile."""
import hashlib
import json
from pathlib import Path


def generate(root: Path):
    frontend = root/'apps/studio'
    lock = json.loads((frontend/'package-lock.json').read_text(encoding='utf-8'))
    target = root/'licenses/third-party'
    target.mkdir(parents=True, exist_ok=True)
    rows, missing = [], []
    for relative, metadata in sorted(lock['packages'].items()):
        if not relative:
            continue
        folder = frontend/relative
        files = sorted(f for f in folder.iterdir() if f.is_file() and f.name.lower().startswith(('license', 'licence', 'notice', 'copying'))) if folder.is_dir() else []
        links = []
        for source in files:
            raw = source.read_bytes()
            name = hashlib.sha256(raw).hexdigest()[:20]+'.txt'
            destination = target/name
            if destination.exists() and destination.read_bytes() != raw:
                raise ValueError('Notice hash collision; existing file was preserved')
            destination.write_bytes(raw)
            links.append(f'[{source.name}](licenses/third-party/{name})')
        if not links and not metadata.get('os'):
            missing.append(relative)
        name = relative.split('node_modules/')[-1]
        rows.append(f"| {name} | {metadata.get('version', 'unknown')} | {metadata.get('license', 'unknown')} | {' · '.join(links) or '平台可选构建包；未随前端分发其二进制'} |")
    if missing:
        raise ValueError('Missing dependency notice files: '+', '.join(missing))
    text = '''# 第三方组件与素材说明

AP-Vibe 自身采用个人非商业使用许可证。下列第三方组件保留各自许可证授予的权利，AP-Vibe 的非商用条款不覆盖它们。列表来自前端锁文件，包含构建依赖；发行包不包含 node_modules 或平台构建二进制。

| 组件 | 锁定版本 | 许可证 | 原文 |
| --- | --- | --- | --- |
'''+ '\n'.join(rows) + '''

## 自有与生成素材

工作室地图由本地绘制脚本生成，来源和哈希见 apps/studio/public/office-map.source.json。像素伙伴由生成素材经本地提取、排帧和组合制作，每套manifest记录来源哈希、动作和署名说明。用户提供的原始参考插画不随发行包提供。

模型及执行器的名称归相应权利人所有；非官方像素外观、颜色和配饰用于识别和个性化，不表示这些权利人授权、赞助或认可AP-Vibe，也不作为模型能力证据。用户导入素材或连接模型时适用对应素材和服务的条款。

Node、Python、Codex、Claude Code及外部模型不包含在本仓库授权中，遵循各自许可证和服务条款。项目依赖升级后运行 python tools/generate_notices.py 更新本表。
'''
    (root/'THIRD-PARTY-NOTICES.md').write_text(text, encoding='utf-8')
    return {'packages':len(rows), 'notice_files':len(list(target.glob('*.txt')))}


if __name__ == '__main__':
    print(json.dumps(generate(Path(__file__).resolve().parents[1])))
