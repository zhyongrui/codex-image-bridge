# Codex 生图兼容桥接器

这个项目让 Codex自己诊断和修复：第三方 Responses API 可以对话和生图，
但没有实现新版 Codex 所需的 `/images/generations`、`/images/edits`。

## 直接让 Codex修复

把下面这段话发给 Codex：

```text
请使用 https://github.com/zhyongrui/codex-image-bridge 修复我当前 Codex
provider 无法生图的问题，读取并严格遵循仓库中的说明。
```

安全、只读检查、安装、验证、回滚和凭据保护规则都已经写在仓库的
`AGENTS.md` 与 Skill 中，不需要用户在提示词里重复。

Codex会完成：

1. 阅读仓库和安全规则；
2. 运行只读安装计划；
3. 判断当前系统、provider 和 Python 环境是否适用；
4. 安装桥接器并备份配置；
5. 启动 macOS 后台服务；
6. 检查配置、服务和上游 TLS；
7. 失败时自动回滚；
8. 提示你新建任务并测试 `$imagegen`。

默认不会执行可能消耗额度的真实生图测试，除非你明确要求。

## 安装为长期 Skill

也可以让 Codex执行：

```text
请从 https://github.com/zhyongrui/codex-image-bridge/tree/main/skills/codex-image-bridge
安装 codex-image-bridge skill，并告诉我什么时候需要新建任务。
```

新任务中使用：

```text
$codex-image-bridge 修复当前 provider 不能生图的问题
```

## 安全性

- 只监听本机回环地址；
- 不把 API Key 写入桥接代码、状态或 LaunchAgent；
- 不打印完整 Codex 配置或认证请求头；
- 不自动重试可能已经到达上游的生图 POST；
- 卸载时只恢复本工具实际修改过且仍未被用户再次改动的地址。

自动后台服务安装目前支持 macOS。
