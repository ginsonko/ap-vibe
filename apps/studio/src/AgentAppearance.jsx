import { createContext, useContext, useEffect, useRef, useState } from 'react';
import { Robot, CheckCircle, UploadSimple, Trash, MagicWand } from '@phosphor-icons/react';
import './agent-appearance.css';

const manifests = import.meta.glob('../../../assets/characters/*/manifest.json', { eager: true, import: 'default' });
const files = import.meta.glob(['../../../assets/characters/*/*-idle.png','../../../assets/characters/*/*-walk.png','../../../assets/characters/*/*-atlas.png'], { eager: true, query: '?url', import: 'default' });
const builtins = Object.entries(manifests).flatMap(([path,manifest])=>manifest.characters.map(c => ({ ...c, atlas_width:c.atlas_width||c.frame_width, atlas_height:c.atlas_height||c.frame_height, url:files[path.replace('manifest.json',c.atlas)] })));
const AppearanceContext=createContext({items:builtins,refresh:async()=>{},ensure:async()=>{}});
const actionNames={idle:'静立',walk:'行走',walk_front:'向下走',walk_back:'向上走',walk_left:'向左走',walk_right:'向右走',work:'工作',read:'阅读',talk:'交流',wait:'等候',rest:'休息'};
async function request(path,body) {
  const r=await fetch('/v1/ap-vibe/agents/appearances'+path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});
  const result=await r.json();
  if(!r.ok||result.ok===false)throw new Error([result.error?.message,result.error?.solution].filter(Boolean).join(' ')||'外观暂时无法读取，请重试。');
  return result;
}
export function AppearanceProvider({children}) {
  const [items,setItems]=useState(builtins);
  const pending=useRef(new Set()),mounted=useRef(true);
  async function refresh(){
    const result=await request('');
    if(mounted.current)setItems(old=>[...builtins,...result.appearances,...old.filter(x=>x.custom&&!result.appearances.some(a=>a.appearance_id===x.appearance_id)).map(x=>({...x,archived:true}))]);
  }
  async function ensure(id){
    if(!id?.startsWith('custom-')||pending.current.has(id))return;
    pending.current.add(id);
    try{const result=await request('?id='+encodeURIComponent(id));if(mounted.current)setItems(old=>[...old.filter(x=>!result.appearances.some(a=>a.appearance_id===x.appearance_id)),...result.appearances]);}
    finally{pending.current.delete(id);}
  }
  useEffect(()=>{mounted.current=true;refresh().catch(()=>{});const timer=setInterval(()=>{if(!document.hidden)refresh().catch(()=>{});},15000);return()=>{mounted.current=false;clearInterval(timer);};},[]);
  return <AppearanceContext.Provider value={{items,refresh,ensure}}>{children}</AppearanceContext.Provider>;
}
export function useReducedMotion(){
  const [reduced,setReduced]=useState(()=>window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  useEffect(()=>{const query=window.matchMedia('(prefers-reduced-motion: reduce)');const changed=()=>setReduced(query.matches);query.addEventListener('change',changed);return()=>query.removeEventListener('change',changed);},[]);
  return reduced;
}
export function useDocumentHidden(){
  const [hidden,setHidden]=useState(()=>document.hidden);
  useEffect(()=>{const changed=()=>setHidden(document.hidden);document.addEventListener('visibilitychange',changed);return()=>document.removeEventListener('visibilitychange',changed);},[]);
  return hidden;
}
export function PixelSprite({character,action='idle',direction='front',animate=false,size=96}){
  const reduced=useReducedMotion();
  const [frame,setFrame]=useState(0),[failed,setFailed]=useState(false);
  const spec=character?.animations?.[action+'_'+direction]||character?.animations?.[action]||character?.animations?.idle,frames=spec?.frames||[0];
  const sequence=frames.join(',');
  useEffect(()=>{
    setFrame(0);if(!animate||reduced||frames.length<2||!spec?.fps)return;
    let cursor=0;const timer=setInterval(()=>{if(document.hidden)return;cursor=spec.loop?(cursor+1)%frames.length:Math.min(cursor+1,frames.length-1);setFrame(cursor);},1000/spec.fps);
    return()=>clearInterval(timer);
  },[character?.appearance_id,sequence,spec?.fps,spec?.loop,animate,reduced]);
  useEffect(()=>setFailed(false),[character?.url]);
  if(!character?.url||failed)return <Robot size={size*.65} aria-label="通用伙伴形象"/>;
  const columns=character.atlas_width/character.frame_width,index=frames[Math.min(frame,frames.length-1)]||0;
  const scale=size/Math.max(character.frame_width,character.frame_height),w=character.frame_width*scale,h=character.frame_height*scale;
  return <span className="pixel-sprite" role="img" aria-label={character.display_name+'像素形象'} style={{width:w,height:h,left:w/2-(character.anchor?.x??character.frame_width/2)*scale,top:(size-h)/2+h-(character.anchor?.y??character.frame_height)*scale,backgroundImage:`url("${character.url}")`,backgroundSize:`${character.atlas_width*scale}px ${character.atlas_height*scale}px`,backgroundPosition:`${-(index%columns)*w}px ${-Math.floor(index/columns)*h}px`}}><img src={character.url} alt="" onError={()=>setFailed(true)} className="sprite-load-check"/></span>;
}
export function AgentPortrait({profile,compact=false,action='idle',direction='front',animate=false,size}){
  const {items,ensure}=useContext(AppearanceContext),character=items.find(x=>x.appearance_id===profile?.appearance_id);
  useEffect(()=>{if(!character)ensure(profile?.appearance_id).catch(()=>{});},[profile?.appearance_id,character?.appearance_id]);
  const animated=(character?.animations?.[action+'_'+direction]||character?.animations?.[action])?.frames.length>1;
  return <span className={'agent-portrait'+(compact?' compact':'')} style={size?{width:size,height:size,flexBasis:size}:undefined} title={character?character.display_name+(animated?' · 动作图集':' · 静态外观'):profile?.appearance_id?'外观未安装，暂用通用形象':'通用伙伴外观'}><PixelSprite character={character} size={size??(compact?40:96)} action={action} direction={direction} animate={animate}/></span>;
}
async function readPNG(file){
  if(!file||file.size>2*1024*1024||file.type!=='image/png')throw new Error('请选择2MB以内的PNG图片。');
  const bitmap=await createImageBitmap(file);
  try{if(bitmap.width>2048||bitmap.height>2048)throw new Error('图片最长边应不超过2048像素。');const canvas=document.createElement('canvas');canvas.width=bitmap.width;canvas.height=bitmap.height;canvas.getContext('2d').drawImage(bitmap,0,0);return {url:canvas.toDataURL('image/png'),width:bitmap.width,height:bitmap.height};}
  finally{bitmap.close();}
}
export function AppearancePicker({value='',onChange,onCreate}){
  const {items,refresh}=useContext(AppearanceContext),character=items.find(x=>x.appearance_id===value);
  const [image,setImage]=useState(null),[spec,setSpec]=useState(null),[specError,setSpecError]=useState(false),[name,setName]=useState(''),[credit,setCredit]=useState(''),[notice,setNotice]=useState(''),[busy,setBusy]=useState(false);
  const [design,setDesign]=useState(false),[requirements,setRequirements]=useState('');
  const [previewAction,setPreviewAction]=useState('idle');
  useEffect(()=>{setPreviewAction('idle');},[value]);
  const visible=items.filter(x=>!x.archived||x.appearance_id===value);
  async function choose(file){setNotice('');setImage(null);setSpec(null);setSpecError(false);try{const result=await readPNG(file);setImage(result);setName(file.name.replace(/\.png$/i,''));}catch(e){setNotice(e.message);}}
  async function importAsset(){setBusy(true);setNotice('');try{const result=await request('/save',{display_name:name,png_base64:image.url.split(',')[1],manifest:spec,attribution:credit});await refresh();onChange(result.appearances[0].appearance_id);setImage(null);setSpec(null);setNotice('外观已存入本机。保存伙伴配置后生效；历史运行的外观保持原版本。');}catch(e){setNotice(e.message);}finally{setBusy(false);}}
  return <fieldset className="appearance-picker"><legend>选择外观</legend>
    <div className="appearance-current"><AgentPortrait profile={{appearance_id:value}} size={80} action={previewAction} animate/><span>{character?.display_name||(value?'未安装的外观':'通用伙伴')}<small>{character?.animations?.walk?.frames.length>1?'含动作图集':'静态形象'}</small>{character&&<label>预览动作<select aria-label="预览动作" value={previewAction} onChange={e=>setPreviewAction(e.target.value)}>{Object.keys(character.animations).filter(a=>a!=='walk'||!character.animations.walk_front).map(a=><option value={a} key={a}>{actionNames[a]||a}</option>)}</select></label>}{!!character?.missing_animations?.length&&<small>尚缺：{character.missing_animations.map(a=>actionNames[a]||a).join('、')}</small>}</span></div>
    <div className="appearance-options">{[{appearance_id:'',display_name:'通用伙伴'},...visible,...(value&&!character?[{appearance_id:value,display_name:'保留未安装外观'}]:[])].map(item=><label key={item.appearance_id} className={'appearance-option'+(value===item.appearance_id?' selected':'')}><input type="radio" name="appearance" value={item.appearance_id} checked={value===item.appearance_id} onChange={()=>onChange(item.appearance_id)}/><PixelSprite character={item} size={64}/><span>{item.display_name}</span>{value===item.appearance_id&&<CheckCircle className="appearance-check" size={17} weight="fill" aria-hidden="true"/>}</label>)}</div>
    {onCreate&&<div className="appearance-commission"><button type="button" className="text-action" onClick={()=>setDesign(v=>!v)} aria-expanded={design}><MagicWand size={18}/>请伙伴制作外观</button>{design&&<><label>想要什么形象<textarea rows={3} value={requirements} onChange={e=>setRequirements(e.target.value)} placeholder="例如：保留蓝发与白色围裙，画四个方向走路、读书和打招呼的像素动作。"/></label><button type="button" className="text-action" onClick={()=>onCreate({appearance:character,requirements})}>准备制作任务</button><p>下一步选择制作伙伴并开始，成果会出现在任务输出中。生成图片可能使用已配置的付费API。</p></>}</div>}
    <details className="appearance-import"><summary>导入自己的像素形象</summary><p>一张透明PNG即可。已有动作图集时，再选择配套JSON；素材仅保存到本机。</p>
      <label>PNG图片<input type="file" accept="image/png" onChange={e=>choose(e.target.files[0])}/></label>
      {image&&<><img className="appearance-import-preview" src={image.url} alt="待导入的外观预览"/><label>外观名称<input value={name} maxLength={80} onChange={e=>setName(e.target.value)}/></label>
        <label>动作图集JSON（可选）<input type="file" accept=".json,application/json" onChange={async e=>{setSpec(null);setNotice('');setSpecError(false);try{const file=e.target.files[0];if(!file)return;if(file.size>64000)throw new Error('素材JSON过大。');setSpec(JSON.parse(await file.text()));}catch(err){setSpecError(true);setNotice('JSON无法读取：'+err.message);}}}/></label>
        <label>素材作者与来源（可选）<input value={credit} maxLength={1000} onChange={e=>setCredit(e.target.value)}/></label><button type="button" className="text-action" disabled={busy||specError||!name.trim()} onClick={importAsset}><UploadSimple size={18}/>{busy?'导入中…':'导入并选择'}</button></>}
      {character?.custom&&<button type="button" className="text-action" disabled={busy} onClick={async()=>{setBusy(true);try{await request('/archive',{appearance_id:value});onChange('');await refresh();setNotice('已从可选外观中移除，历史任务仍保留该图片。');}catch(e){setNotice(e.message);}finally{setBusy(false);}}}><Trash size={18}/>从可选外观中移除</button>}
      <a href="/appearance-example.json" download>下载动作图集示例</a>
    </details>{notice&&<p role="status" className="agent-notice">{notice}</p>}
  </fieldset>;
}
