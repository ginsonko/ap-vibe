# 批量图片验收

当用户说“检查这几千张商品图/生图结果”“验收太慢或太贵”时，优先使用工作室专用批处理能力。复用已配置伙伴；普通编程伙伴仍默认Claude CLI，专用批处理直接向对应API发图片和标准。资料读取不会触发收费；start/resume才实际发图。必须在用户授权的素材、模型连接和费用范围内执行。

1. `ap_vibe_agents`看可用伙伴。按用户选择、实际多模态兼容性、同类质量和用量选初审及可选复核伙伴。模型名字不能证明渠道支持图片，也不能证明最便宜。没配置价格也照常运行，金额0必须同时称“未估算”，不要说免费。
2. 找到用户的产品规格/参考图，按SKU建立清单。每张用稳定id、product_id、绝对path、requirements，可选reference_path；不把文件名当标准。先选代表小样：合格、颜色/结构/文字错误、模糊及难例。真值另存，不发给待评模型。报告漏检、误拒、无法判断和实际耗时。
3. 写本地UTF-8 JSON清单，再调用`ap_vibe_image_qa_create({"manifest_path":"真实绝对路径"})`。顶层request_id、title、agent_id必填，project_id可填已有相关项目；batch_size、concurrency、sample_percent可按渠道调节，reviewer_agent_id可选。每项结果都保留图片hash与请求编号；重复id必须修正，不能跳过。
4. 用回执batch_id、revision和稳定request_id调用`ap_vibe_image_qa_action`，action=start。返回的是开始受理，随后`ap_vibe_image_qa`读取逐图结果。每次最多200条，用offset翻页。长任务交给本地后台处理，不让主模型空转轮询。用户可以在工作台“批量图片验收”页看进度、暂停、继续和导出CSV。
5. passed有可见依据才通过；uncertain、高风险及按比例抽检进入复核。复核意见与初审分别保存；分歧保留needs_review，不能因为两个模型同意就保证正确。浏览器人工复核可追加人工判断，不能由Agent冒充人工。
6. 暂停停止新请求，在途可能继续计费；恢复仅处理未提交图片。请求超时/503/服务断开等结果未知不会自动重放，先按provider_request_id/本地request_id核对。用户原授权允许有限补测时，可明确说明原因使用retry_unresolved发起新尝试（含note），保留旧账，不循环自动重试。缺失/改变的文件单独处理，其它图片继续。

最小清单：
```json
{"request_id":"商品主图-初审-1","title":"蓝色保温杯主图","agent_id":"已查询的真实ID","batch_size":4,"concurrency":2,"sample_percent":10,"items":[{"id":"SKU-A-001","product_id":"SKU-A","path":"D:\\商品图\\A001.png","reference_path":"D:\\规格\\A参考.png","requirements":"杯身蓝色，完整黑盖；NOVA与500mL清晰正确，无变形和多余贴纸"}]}
```

按本批usage记录初审/复核全部请求的已知token、未知用量数和按币种估价；不要拿整个伙伴历史账单当本批成本。可选token/金额上限由用户选择，未设则不限。在途并发可能略超本地上限；每个模型独立Key及渠道Key额度可作为额外控制。用户要求代查价格时，可以查其公开价格页，按每百万token换算后用预算工具保存价格/币种/来源，说明渠道分组价可能不同。
