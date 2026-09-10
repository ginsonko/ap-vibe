# 逻辑观察与档案回写

先选项目。GET /v1/ap-vibe/logic/status?project_id=ID 返回档案/归类来源中的代码目录候选、已配置目录和 Codex 分析历史。

- 原生查询目前提取 Python AST，只提供静态职责、影响与首断点线索。POST /v1/ap-vibe/logic/configure {"project_id":"ID","root_path":"已核对的项目目录"} 配置当前项目；不需要重启 daemon。
- 在工作台可用中文直接发起 Codex 逻辑分析，适合 JavaScript、TypeScript、Go 等代码或跨模块问题。POST /v1/ap-vibe/logic/analyze {"request_id":"唯一编号","project_id":"ID","question":"问题"} 创建任务，然后 organization/dispatch 启动本机 Codex CLI。
- Codex 分析任务只读源码，禁止生产变更与凭据读取。按服务注入的 JSON 协议返回 summary、findings、evidence_refs、unknown、next_action 和相关 sections。服务保存分析历史并更新相关项目章节。
- 缺源码时报告具体缺口，不能伪造成功或运行证据。输出引用文件和行号，静态分析标为 agent_reported，运行结论要有真实测试/执行证据。
- 当前任务自行分析后也遵照项目文档协议写回 architecture/evidence/work 等变化章节。只有实际调用了查询 API 才报告原生逻辑观察；不要把普通源码阅读冒充 API 运行记录。
