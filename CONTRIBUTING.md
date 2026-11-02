# 参与贡献

请先通过 Issue 描述复现步骤、预期与实际行为，再提交范围明确的 Pull Request。
日志需脱敏，不上传凭据、会话正文或个人配置。贡献者须拥有提交代码的授权，并同意以 Apache-2.0 发布。

## 本地检查

```sh
python -I tests/test_agentlink.py
python -I tests/test_harness.py
node --check agentlink/web/app.js
node --check hosts/harness/agentlink-bridge.mjs
```

Python 测试使用临时用户目录与模拟 Harness 客户端。
Claude hook 的 TypeScript 测试依赖宿主测试 API，不包含在 Python 测试中。
不要在真实宿主配置上运行破坏性测试。
