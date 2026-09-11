import {useEffect,useRef,useState} from 'react';
import {LAYOUT_V2} from './OfficeLayout';

export function OfficeViewport({children,className='',fitHeight=false}){
  const host=useRef(null),map=useRef(null),[width,setWidth]=useState(LAYOUT_V2.scene.width),[height,setHeight]=useState(LAYOUT_V2.scene.height),[zoom,setZoom]=useState('fit');
  useEffect(()=>{
    const measure=()=>{setWidth(map.current.clientWidth);setHeight(Math.max(300,window.innerHeight-host.current.getBoundingClientRect().top-70));};
    const observer=new ResizeObserver(measure);
    observer.observe(host.current);window.addEventListener('resize',measure);measure();
    return()=>{observer.disconnect();window.removeEventListener('resize',measure);};
  },[fitHeight]);
  const scale=zoom==='fit'?Math.min(1,width/LAYOUT_V2.scene.width,fitHeight?height/LAYOUT_V2.scene.height:1):Number(zoom);
  return <div className="office-viewport" ref={host}>
    <div className="office-zoom" data-export-hide><span>点击伙伴查看工作 · 动画不影响后台执行</span><label>视野 <select aria-label="地图缩放" value={zoom} onChange={e=>setZoom(e.target.value)}><option value="fit">完整地图</option><option value="1">原始大小</option><option value="1.25">放大 125%</option></select></label></div>
    <div ref={map} className="office-map-scroll" tabIndex={0} role="region" aria-label="可缩放的像素办公室">
      <div style={{width:LAYOUT_V2.scene.width*scale,height:LAYOUT_V2.scene.height*scale,position:'relative',overflow:'hidden',left:Math.max(0,(width-LAYOUT_V2.scene.width*scale)/2)}}>
        <div className={'office-world '+className} style={{width:LAYOUT_V2.scene.width,height:LAYOUT_V2.scene.height,position:'absolute',margin:0,transform:`scale(${scale})`,transformOrigin:'top left'}}>{children}</div>
      </div>
    </div>
  </div>;
}
