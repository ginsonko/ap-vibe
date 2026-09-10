# AP-Vibe 像素外观制作合同

本文件是公开的素材格式说明。任务目标与授权以用户本次委托为准。

## 成果

- 一个透明PNG，每帧推荐96x96或128x128；最大边2048，文件2MB以内。固定相同宽高网格，无水印、文字和背景。不能把整张图集当作一个角色显示。
- 一个UTF-8 JSON：frame_width、frame_height、anchor（脚底坐标）、animations。每个动作包含frames（从0开始按行编号）、fps（0到30，多帧必须大于0）、loop、direction（可选）。idle必需，其他动作只登记真正完成的帧。
- 一个acceptance.md：来源与作者、身份参考、动作完成度、像素/透明/锚点检测、逐帧与循环观察结果。未知项与缺失动作明确列出。

## 动作示例

```json
{
  "frame_width": 96,
  "frame_height": 96,
  "anchor": {"x": 48, "y": 94},
  "animations": {
    "idle": {"frames": [0], "fps": 0, "loop": false},
    "walk_front": {"frames": [0, 1, 2, 3], "fps": 6, "loop": true, "direction": "front"}
  }
}
```

方向以角色在地图上移动的方向为准：front向下、back向上、left向左、right向右。可提供walk作为无方向旧客户端兼容动作。工作、读书、交流、等候和休息使用work、read、talk、wait、rest。不存在的动作将回退idle，不把回退视为制作完成。

## 验收

先完成一位角色小样并查看实际播放，再扩展整套。需要看到脚步交替和不同方向轮廓，保持服装、发色、装饰、身高与脚底锚点。对话动作有真实手势或表情变化。近邻缩放，避免照片式平滑插值。导入前拒绝越界帧、裁切肢体、重叠角色及不透明底色。

## 导入

Agent优先使用标准工具：`ap_vibe_appearance_import({"png_path":"本机PNG绝对路径","manifest_path":"本机单角色JSON绝对路径"})`，再用`ap_vibe_appearances({"appearance_id":"返回ID"})`回读。普通查询`ap_vibe_appearances({})`即可。图片字节由本地工具读取，不把base64塞进模型上下文。单角色JSON可以是角色对象，或characters恰好只有一项的manifest；多角色图集请明确选择角色后再导入。

在伙伴配置中导入PNG与JSON，预览通过后保存伙伴。每个素材生成不可变ID，历史版本继续可读。若自动登记，POST /v1/ap-vibe/agents/appearances/save，正文为display_name、png_base64（PNG的base64）、manifest（JSON对象）、attribution（可选来源说明）；回读 /v1/ap-vibe/agents/appearances?id=返回的appearance_id。新建素材不会自动修改任何伙伴。不要删除旧素材、重复提交不确定的生图请求或将密钥写入结果。
