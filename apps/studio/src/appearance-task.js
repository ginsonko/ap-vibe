export function appearanceTaskPrompt({name,appearance,requirements,origin}){
  const reference=appearance?{
    appearance_id:appearance.appearance_id,
    image_url:new URL(appearance.url,origin).href,
    frame_width:appearance.frame_width,frame_height:appearance.frame_height,
    atlas_width:appearance.atlas_width,atlas_height:appearance.atlas_height,
    anchor:appearance.anchor,animations:appearance.animations,
  }:null;
  return `为AP-Vibe伙伴制作原创像素风外观与动作素材。伙伴名称只是设计参考，不代表执行者或实际模型。
伙伴名称：${name||'新伙伴'}
用户的设计要求：${requirements.trim()||'保持现有角色的辨识度，制作精致、清晰的像素动作。'}
已有参考（若有，只读取角色素材，不读取任何连接配置或密钥）：${JSON.stringify(reference)}

先读取 ${origin}/agent-appearance-task.md 的素材合同。若已安装“银子万能媒体工作流”，用其Skill和本机保存的模型配置来生成；缺少可用生图能力时如实给出当前可交付素材和缺口，不得伪造图片成功。生成请求不确定时查询原任务，不重复收费提交。
输出透明PNG和配套JSON到当前任务成果目录，完整动作应包括idle、walk_front、walk_back、walk_left、walk_right、work、read、talk、wait、rest。只登记实际画好并逐帧检查通过的动作。旧静态图、翻转图或CSS位移不能冒充真实行走帧。
采用4方向真实步态、稳定角色比例与脚底锚点。不要将模型选择、URL或Key写入素材文件。检查PNG尺寸/透明背景/帧索引/基线，并实际播放验证方向、脚步和首尾循环。写acceptance.md，说明素材来源、制作方式、通过与未通过项。
成果完成后，使用ap_vibe_appearance_import({png_path:实际PNG路径,manifest_path:实际单角色JSON路径})登记，再用ap_vibe_appearances({appearance_id:返回ID})回读。无需猜HTTP路径，也不要把图片base64写进模型参数。说明在“编辑伙伴 -> 形象与职责”中选择刚导入的外观，但不要擅自覆盖其他伙伴或旧外观。项目资料按AP-Vibe Skill增量维护。`;
}
