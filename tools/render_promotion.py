"""Render the public AP-Vibe guide and narrated walkthrough from reviewed assets.

Optional authoring dependencies: Pillow, numpy, edge-tts; FFmpeg and FFprobe.
The product runtime does not depend on this script. No model/API credentials used.
"""
from __future__ import annotations
import argparse, asyncio, hashlib, json, math, re, subprocess, wave
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageOps
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
MEDIA = ROOT / 'docs/media'
NAVY, PANEL, MINT, WHITE, MUTED, PEACH = '#09192b', '#142a3d', '#8ce4c4', '#f4f7f5', '#b0c6d4', '#f6caa5'
STORY = json.loads((MEDIA / 'storyboard.json').read_text(encoding='utf-8'))
FONTS = Path('C:/Windows/Fonts')


def font(size, bold=False):
    return ImageFont.truetype(str(FONTS / ('msyhbd.ttc' if bold else 'msyh.ttc')), size)


def text(im, xy, value, size=42, fill=WHITE, bold=False):
    ImageDraw.Draw(im).text(xy, value, font=font(size, bold), fill=fill, spacing=int(size*.3))


def paragraph(im, xy, value, width, size=42, fill=MUTED, line_height=None):
    d=ImageDraw.Draw(im); f=font(size); lines=[]; line=''
    for ch in re.findall(r'[A-Za-z0-9_./:+-]+|.',value,re.DOTALL):
        if ch=='\n' or d.textlength(line+ch,font=f)>width:
            lines.append(line);line='' if ch=='\n' else ch
        else:line+=ch
    if line:lines.append(line)
    step=line_height or int(size*1.55)
    for i,line in enumerate(lines):text(im,(xy[0],xy[1]+step*i),line,size,fill)
    return xy[1]+len(lines)*step


def panel(im, box, fill=PANEL, radius=26, outline=None):
    ImageDraw.Draw(im).rounded_rectangle(box,radius=radius,fill=fill,outline=outline,width=2)


def paste_fit(im, source, box, contain=True):
    src=source if isinstance(source,Image.Image) else Image.open(source)
    size=(int(box[2]-box[0]),int(box[3]-box[1]))
    src=(ImageOps.contain if contain else ImageOps.fit)(src,size,method=Image.Resampling.LANCZOS)
    im.paste(src,(int(box[0]+(size[0]-src.width)/2),int(box[1]+(size[1]-src.height)/2)),src if src.mode=='RGBA' else None)


def sprite(family, frame=28, scale=3):
    folder=ROOT/'assets/characters'/f'{family}-v3'
    m=json.loads((folder/'manifest.json').read_text(encoding='utf8'))['characters'][0]
    atlas=Image.open(folder/m['atlas']).convert('RGBA');w=m['frame_width'];h=m['frame_height'];columns=atlas.width//w
    crop=atlas.crop(((frame%columns)*w,(frame//columns)*h,(frame%columns+1)*w,(frame//columns+1)*h))
    a=np.array(crop);rgb=a[:,:,:3].astype(int)
    if 'chroma_key' in m:
        green=(rgb[:,:,1]>rgb[:,:,0]+m.get('chroma_dominance',35))&(rgb[:,:,1]>rgb[:,:,2]+m.get('chroma_dominance',35))
        a[green,3]=0
    return Image.fromarray(a).resize((w*scale,h*scale),Image.Resampling.NEAREST)


def poster():
    im=Image.new('RGB',(1440,5920),NAVY);d=ImageDraw.Draw(im)
    text(im,(90,66),'AP-Vibe',68,MINT,True);text(im,(958,90),'个人非商用内测',30,MUTED)
    text(im,(88,238),'让每一次接力，',105,WHITE,True);text(im,(88,374),'都接得住。',105,MINT,True)
    paragraph(im,(94,550),'Codex × Claude Code\n一个工作台，看进度、留决定、接上下文。',1220,43)
    paste_fit(im,MEDIA/'studio-hero.webp',(18,714,1422,1650))
    text(im,(1010,1635),'品牌场景插画',26,MUTED)
    for i,(big,small) in enumerate([('看得见','任务与进展'),('留得住','决定与教训'),('接得上','下一次工作')]):
        x=90+i*430;panel(im,(x,1730,x+400,1925));text(im,(x+34,1761),big,58,MINT,True);text(im,(x+34,1848),small,34)
    text(im,(90,2050),'少一点重复交代，多一点连续工作。',59,WHITE,True)
    for i,(title,body) in enumerate([('又要从头解释？','先读项目目录，再按需要看目标、事故和下一步。'),('任务多了看不懂？','按真实标题看消息，活动曲线帮助你找到现场。'),('旧决定总被覆盖？','长期项目增量维护，保留撤销原因与人工修改。')]):
        y=2180+i*190;text(im,(95,y),f'0{i+1}',39,MINT,True);text(im,(196,y),title,49,WHITE,True);paragraph(im,(196,y+75),body,1115,39)
    d.line((90,2785,1350,2785),fill='#30485a',width=2)
    text(im,(90,2880),'一份项目档案，多个会话共同维护。',59,WHITE,True)
    paragraph(im,(90,2980),'11 章结构化资料 · 十维风险与理由 · 可查看、修改、导出',1270,39)
    chapters=['项目简介','目标与红线','逻辑与架构','资料入口','决策变更','完成与待办','风险与十维','验证与发现','依赖与协作','当前进度','恢复与下一步']
    for i,label in enumerate(chapters):
        x=90+(i%3)*430;y=3086+(i//3)*98;panel(im,(x,y,x+405,y+76));text(im,(x+23,y+14),label,35)
    paragraph(im,(92,3510),'先给目录 → 按需读取 → 执行任务 → 更新变化并回读\n不知道的评分保留为空；资料齐全不等于产品满分。',1250,36)
    text(im,(90,3735),'认识你的 Agent 工作室。',66,WHITE,True)
    paragraph(im,(90,3840),'安排实现、互相交接、独立验收。角色与模型自由组合。',1250,40)
    families=['gpt','claude','deepseek','gemini','grok']
    for i,f in enumerate(families):
        s=sprite(f,28,2);im.paste(s,(120+i*252,3950),s);text(im,(130+i*252,4160),{'gpt':'Codex','claude':'Claude','deepseek':'DeepSeek','gemini':'Gemini','grok':'Grok'}[f],29,MUTED)
    paragraph(im,(92,4270),'URL + Key + 模型 → 添加第三方伙伴\n动画表现任务状态；提速、省钱与质量要用真实任务检验。',1250,38)
    panel(im,(90,4490,1350,4760),fill='#21363e');text(im,(125,4520),'AP 认知 · 可选实验项',47,MINT,True)
    paragraph(im,(125,4600),'基础记忆无需额外认知 Key。外部教师默认关闭；开启后需要网络与 API 费用，可能变慢或出错，长期净收益待观察。',1170,35)
    text(im,(90,4880),'把这张图发给 Codex，说：',59,WHITE,True)
    panel(im,(90,4985,1350,5560),fill='#e7f5ee')
    text(im,(129,5025),'“请帮我安装 AP-Vibe。”',54,'#163a32',True)
    text(im,(130,5123),'github.com/ginsonko/ap-vibe',49,'#126a59',True)
    paragraph(im,(132,5220),'先读 README.md 与 docs/INSTALL-CODEX.md，安装 Skill、MCP 和本地服务，打开实际工作台地址。推荐授权整理最近 7 天活跃任务；AP 外部教师先不开。',1150,39,'#294c47')
    text(im,(94,5645),'需要 Windows、可用的 Codex 与 Python 3.11+。',32,MUTED)
    text(im,(94,5720),'个人非商用 · 允许个人二开与免费分享 · 修改须保留来源',30,MUTED)
    text(im,(94,5790),'完整手册与真实验收记录，见仓库。',32,MINT)
    im.save(MEDIA/'ap-vibe-guide.png',optimize=True)
    im.resize((720,2960),Image.Resampling.LANCZOS).save(MEDIA/'ap-vibe-guide-preview.webp',quality=87)
    return im


def slide(scene):
    im=Image.new('RGB',(1920,1080),NAVY)
    text(im,(84,56),'AP-Vibe',42,MINT,True);text(im,(1400,71),scene['eyebrow'],24,MUTED)
    text(im,(88,170),scene['title'],83,WHITE,True)
    paragraph(im,(92,417),scene['subtitle'],800,31,MUTED)
    for i,b in enumerate(scene['bullets']):
        y=560+i*88;ImageDraw.Draw(im).ellipse((94,y+12,105,y+23),fill=MINT);paragraph(im,(128,y),b,775,31,WHITE)
    key=scene['key'];box=(945,180,1840,880)
    if key in ('intro','close'):
        paste_fit(im,MEDIA/'studio-hero.webp',box)
    elif key=='dossier':
        paste_fit(im,MEDIA/'workbench-dossier.png',box);text(im,(960,902),'实际界面 · 独立迁移演练项目',25,MUTED)
    elif key=='ap':
        panel(im,(975,238,1810,813));text(im,(1018,281),'基础模式',49,MINT,True)
        paragraph(im,(1020,364),'本地记录 / 结构化资料\n检索 / 恢复 / 协作',735,38)
        ImageDraw.Draw(im).line((1020,502,1760,502),fill='#365261',width=2)
        text(im,(1020,546),'外部教师默认关闭',43,PEACH,True);paragraph(im,(1020,626),'约 ¥1/小时：仅情景参考\n实际取决于模型与活动量',730,34)
    elif key=='install':
        panel(im,(970,236,1810,816),fill='#e7f5ee');text(im,(1010,283),'请帮我安装 AP-Vibe',49,'#163a32',True)
        paragraph(im,(1010,386),'https://github.com/\nginsonko/ap-vibe',730,48,'#126a59')
        paragraph(im,(1010,553),'先阅读安装说明，再执行。\n安装完成后打开实际工作台。',730,34,'#294c47')
        text(im,(1010,737),'推荐整理最近 7 天活跃任务',30,'#126a59')
    elif key in ('context','agents'):
        labels=['项目资料目录','按章读取','执行真实任务','更新与回读'] if key=='context' else ['明确小任务','伙伴实现','独立读取成果','通过 / 返工']
        for i,label in enumerate(labels):
            y=205+i*164;panel(im,(1010,y,1775,y+116));text(im,(1050,y+23),f'0{i+1}',38,MINT,True);text(im,(1160,y+26),label,36)
            if i<3:text(im,(1380,y+112),'↓',38,MINT)
        text(im,(1020,907),'流程示意 · 结果以实际成果与回读为准',24,MUTED)
    elif key=='monitor':
        panel(im,(967,232,1820,806));text(im,(1008,266),'现场一览',42,MINT,True)
        for i,(a,b,c) in enumerate([('设备登记 · 修复导入','近期有新消息',MINT),('阅读工具 · 编写测试','等待上游成果',PEACH),('个人站点 · 设计记录','历史可继续',MUTED)]):
            y=356+i*135;panel(im,(1000,y,1788,y+110),fill='#20394a');text(im,(1024,y+13),a,33);text(im,(1024,y+64),b,26,c)
        text(im,(985,876),'内容示意 · 展示阅读顺序，不是实测指标',24,MUTED)
    elif key=='pixels':
        paste_fit(im,ROOT/'apps/studio/public/office-map.png',box)
        text(im,(978,905),'产品地图与角色素材 · 动作展示',25,MUTED)
    ImageDraw.Draw(im).line((86,966,1834,966),fill='#30485a',width=2)
    return im


def probe(path, ffprobe):
    return json.loads(subprocess.check_output([ffprobe,'-v','error','-show_format','-show_streams','-of','json',str(path)],text=True))


async def voice(work):
    import edge_tts
    for scene in STORY['scenes']:
        key=scene['key'];mp3=work/f'{key}.mp3';bounds=work/f'{key}-speech.json'
        if mp3.exists() and bounds.exists():continue
        chunks=[];partial=mp3.with_suffix('.partial')
        with partial.open('wb') as f:
            async for event in edge_tts.Communicate(scene['narration'],STORY['voice'],rate='+3%',boundary='SentenceBoundary').stream():
                if event['type']=='audio':f.write(event['data'])
                elif event['type']=='SentenceBoundary':chunks.append(event)
        if not partial.stat().st_size or not chunks:raise RuntimeError('Speech output incomplete')
        partial.replace(mp3);bounds.write_text(json.dumps(chunks,ensure_ascii=False,indent=2),encoding='utf8')
        print('voice',key,flush=True)


def srt_time(value):
    ms=round(value*1000);h,ms=divmod(ms,3600000);m,ms=divmod(ms,60000);s,ms=divmod(ms,1000)
    return f'{h:02}:{m:02}:{s:02},{ms:03}'


def video(work, ffmpeg, ffprobe):
    rate=24;clips=[];timeline=[];subs=[];offset=0
    sprite_cache={f:[sprite(f,n,2) for n in (12,13,14,15)] for f in ['gpt','claude','deepseek','gemini','grok']}
    for scene in STORY['scenes']:
        key=scene['key'];duration=float(probe(work/f'{key}.mp3',ffprobe)['format']['duration'])+1.4
        frames=math.ceil(duration*rate);duration=frames/rate;clip=work/f'{key}.mp4';clips.append(clip)
        timeline.append({'key':key,'start':offset,'end':offset+duration,'title':scene['title'].replace('\n','')})
        events=json.loads((work/f'{key}-speech.json').read_text(encoding='utf8'))
        for event in events:
            start=event['offset']/1e7+.4;end=start+event['duration']/1e7
            subs.append((offset+start,offset+end,event['text']))
        offset+=duration
        base=slide(scene);base.save(work/f'{key}-slide.png')
        tours=[]
        if key=='dossier':
            for chapter in ('dossier','decisions','work','risks'):
                tour=Image.new('RGB',(1920,1080),NAVY)
                text(tour,(84,32),'AP-Vibe · 点击目录，查看不同章节',34,MINT,True)
                text(tour,(1320,43),'实际界面 · 独立演练项目',24,MUTED)
                paste_fit(tour,MEDIA/f'workbench-{chapter}.png',(200,97,1720,940))
                tours.append(tour)
        if clip.exists():continue
        args=[ffmpeg,'-hide_banner','-loglevel','error','-y','-f','rawvideo','-vcodec','rawvideo','-pix_fmt','rgb24','-s','1920x1080','-r',str(rate),'-i','pipe:0','-i',str(work/f'{key}.mp3'),'-filter_complex','[1:a]adelay=400|400,apad[a]','-map','0:v','-map','[a]','-c:v','libx264','-preset','fast','-crf','20','-pix_fmt','yuv420p','-c:a','aac','-b:a','160k','-t',str(duration),'-movflags','+faststart',str(clip)]
        p=subprocess.Popen(args,stdin=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            for frame in range(frames):
                t=frame/rate;im=(tours[min(3,int((t-6)/max(1,(duration-6)/4)))] if tours and t>=6 else base).copy();d=ImageDraw.Draw(im)
                # A restrained progress line and a short arrival fade keep the focus on reading.
                d.rectangle((86,966,86+int(1748*frame/max(1,frames-1)),969),fill=MINT)
                if key=='pixels':
                    for j,f in enumerate(sprite_cache):
                        s=sprite_cache[f][int(t*5)%4];x=995+(j*141+int(t*20))%650;y=450+(j%2)*120;im.paste(s,(x,y),s)
                current=next((e for e in events if e['offset']/1e7+.4<=t<=(e['offset']+e['duration'])/1e7+.6),None)
                if current:
                    sentence=current['text'];size=33 if len(sentence)>45 else 38
                    if len(sentence)>48:
                        half=len(sentence)//2;sentence=sentence[:half]+'\n'+sentence[half:]
                    f=font(size);bbox=d.multiline_textbbox((0,0),sentence,font=f,spacing=4);tw=bbox[2];text(im,((1920-tw)/2,978),sentence,size,WHITE)
                if t<.5:im=Image.blend(Image.new('RGB',im.size,NAVY),im,t/.5)
                p.stdin.write(im.tobytes())
            p.stdin.close();err=p.stderr.read();p.wait()
            if p.returncode:raise RuntimeError(err.decode(errors='replace'))
        except BaseException:
            p.kill();p.wait();raise
        print('rendered',key,round(duration,1),flush=True)
    concat=work/'concat.txt';concat.write_text('\n'.join("file '"+p.name+"'" for p in clips),encoding='utf8')
    silent=work/'joined.mp4'
    subprocess.run([ffmpeg,'-v','error','-y','-f','concat','-safe','0','-i',str(concat),'-c','copy',str(silent)],check=True)
    # Original, low-volume ambient chords. Voice remains the foreground.
    sr=22050;t=np.arange(int(offset*sr))/sr;music=np.zeros(t.size,dtype=np.float32)
    chords=[(130.81,164.81,196),(110,130.81,164.81),(87.31,110,130.81),(98,123.47,146.83)]
    for start in np.arange(0,offset,8):
        a=int(start*sr);b=min(a+8*sr,len(t));u=np.arange(b-a)/sr;env=np.sin(np.pi*u/8)**2
        for f in chords[int(start/8)%4]:music[a:b]+=(np.sin(2*np.pi*f*u)+.2*np.sin(4*np.pi*f*u))*env*.008
    musicpath=work/'ambient-original.wav'
    with wave.open(str(musicpath),'wb') as wf:wf.setnchannels(1);wf.setsampwidth(2);wf.setframerate(sr);wf.writeframes((np.clip(music,-1,1)*32767).astype('<i2').tobytes())
    output=MEDIA/'ap-vibe-introduction.mp4'
    subprocess.run([ffmpeg,'-v','error','-y','-i',str(silent),'-i',str(musicpath),'-filter_complex','[0:a][1:a]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[a]','-map','0:v','-map','[a]','-c:v','copy','-c:a','aac','-b:a','192k','-movflags','+faststart',str(output)],check=True)
    (MEDIA/'ap-vibe-introduction.srt').write_text('\n\n'.join(f'{i+1}\n{srt_time(a)} --> {srt_time(b)}\n{s}' for i,(a,b,s) in enumerate(subs)),encoding='utf8')
    (MEDIA/'video-chapters.json').write_text(json.dumps(timeline,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({'seconds':offset,'bytes':output.stat().st_size,'sha256':hashlib.sha256(output.read_bytes()).hexdigest()}),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['poster','voice','slides','video']);p.add_argument('--work',type=Path,required=True);p.add_argument('--ffmpeg',default='ffmpeg');p.add_argument('--ffprobe',default='ffprobe');p.add_argument('--font-dir',type=Path,default=FONTS);a=p.parse_args();FONTS=a.font_dir;a.work.mkdir(parents=True,exist_ok=True)
    if a.action=='poster':poster()
    elif a.action=='voice':asyncio.run(voice(a.work))
    elif a.action=='slides':
        for scene in STORY['scenes']:slide(scene).save(a.work/(scene['key']+'-slide.png'))
    else:video(a.work,a.ffmpeg,a.ffprobe)
