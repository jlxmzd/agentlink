# 参与贡献

请先通过 Issue 描述复现步骤、预期与实际行为，再提交范围明确的 Pull Request。
日志需脱敏，不上传凭据、会话正文或个人配置。贡献者须拥有提交代码的授权，并同意以 Apache-2.0 发布。

## 本地检查

```sh
python -I tests/test_agentlink.py
python -I tests/test_harness.py
python -I tests/test_isolation.py
node --check agentlink/web/app.js
node --check agentlink/web/flow.js
node --check hosts/harness/agentlink-bridge.mjs
node tests/test_panel.js
node tests/test_flow.js
```

Python 测试使用临时用户目录与模拟 Harness 客户端。
新增测试与复现脚本须在导入 AgentLink 前，把 `AGENTLINK_USER_HOME` 和 `AGENTLINK_HOME` 显式设置为同一临时沙箱中的用户目录与 `.agentlink` 目录，项目也必须位于沙箱；禁止真实通知、投递和唤醒，传输测试使用假 RPC。修改入口后运行隔离回归，确认外层哨兵注册表不变。
Claude hook 的 TypeScript 测试依赖宿主测试 API，不包含在 Python 测试中。
不要在真实宿主配置上运行破坏性测试。
