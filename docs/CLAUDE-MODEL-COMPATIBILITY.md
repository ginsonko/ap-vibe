# Claude 执行器的模型兼容

通过 Claude 执行器连接第三方模型时，伙伴的“模型名称”始终是上游真实模型名。它控制实际路由和用量归属，不要为了消除本地 `unrecognized_model` 报错把它改成 `sonnet`。

工作室将两个标识分开：

- `model`：交给本地网关绑定的真实上游模型，沿用伙伴现有配置。
- `cli_model`：仅供本机 Claude CLI 识别的别名。留空默认 `sonnet`，通常不用修改。特殊 CLI 版本可在“执行器兼容设置”中自定义该版本支持的值。

旧配置没有 `cli_model` 时自动使用默认值，无需重新填写 Key 或迁移项目。保存、复制、导出导入都保留自定义别名。普通工作室任务和项目整理共用这一规则。使用本机登录的独立项目整理路径仍沿用原登录配置。

`configuration_state=ready` 只说明配置字段齐全。`auth_unavailable`、账户耗尽、429 和无可用渠道仍是上游的实际错误；本地别名兼容不能恢复供应商服务。执行器退出或出现 `completed` 请求事件也不代表已交付合格文件，应检查任务成果。

诊断时先读取原运行的错误、上游请求标识与成果，不通过重放结果未知的原任务来测试。需要验证连接时使用独立的新测试，并保留其真实请求与费用边界。

## 验证范围

模型分离测试覆盖 OpenAI Chat Completions、Anthropic Messages、普通工作室、项目整理、自定义别名和旧配置默认值。另在 Windows Claude Code 2.1.215 上使用真实 CLI 与本地模拟上游，验证 Grok、Gemini 和未知供应商模型的原名仍到达网关上游。该测试不调用真实供应商，不证明所有 CLI 版本或供应商健康。

运行定向检查：`python -m pytest -q tests/test_claude_model.py tests/test_agent_studio.py tests/test_studio_agent_sharing.py`。
