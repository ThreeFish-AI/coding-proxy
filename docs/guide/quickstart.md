# 快速开始

## 1. 环境要求

- **Python** >= 3.12
- **UV** 包管理器（推荐）或 pip
- **API Key**：至少一个供应商的 API Key（如 [智谱](https://open.bigmodel.cn) 的 `ZHIPU_API_KEY`）
- **Claude Code** 已安装并可用

## 2. 安装

```bash
# 方式一：使用 UV（推荐）
uv sync

# 方式二：使用 pip
pip install -e .
```

安装完成后，`coding-proxy` 命令即可使用。

## 3. 最小配置

```bash
# 复制配置模板到项目根目录（模板已内置完整默认值，仅需覆盖密钥）
cp config.default.yaml config.yaml
```

设置智谱 API Key（默认 Tier 0 供应商）：

```bash
export ZHIPU_API_KEY="your-api-key-here"
```

配置文件中使用 `${ZHIPU_API_KEY}` 引用，代理启动时自动替换。

> **安全最佳实践**：
> - API Key 优先使用 `${ENV_VAR}` 环境变量引用，避免明文写入配置文件
> - `config.yaml` 已在 `.gitignore` 中，不会被提交到版本库
> - OAuth Token 存储于 `~/.coding-proxy/tokens.json`，建议 `chmod 600` 限制访问
> - 若设置 `server.host: "0.0.0.0"` 接受外部连接，确保在可信网络环境中运行

## 4. 启动服务

```bash
# 使用默认配置启动
coding-proxy start

# 指定端口
coding-proxy start --port 8080

# 指定配置文件
coding-proxy start --config /path/to/config.yaml
```

启动成功后输出：

```
INFO:     Started server process [75773]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:3392 (Press CTRL+C to quit)
```

> 若启用了 Copilot 或 Antigravity 供应商但未配置凭证，启动时会自动触发 OAuth 浏览器登录。

## 5. 验证服务

```bash
# 健康检查
curl http://127.0.0.1:3392/health
# 期望返回: {"status":"ok"}

# 查看代理状态
coding-proxy status
```

## 6. 配置 Claude Code

将 Claude Code 的 API 端点指向 coding-proxy：

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:3392
```

Claude Code 使用的 OAuth token 会被代理透传到 Anthropic API，无需额外配置认证信息。

### 验证集成

1. 确保 coding-proxy 正在运行：`coding-proxy status`
2. 使用 Claude Code 发送一条消息
3. 查看 coding-proxy 的终端日志，确认请求经过代理
4. 使用 `coding-proxy usage` 查看是否有新的用量记录

### 日常使用流程

1. **启动代理**：`coding-proxy start`（可使用 `tmux` 后台运行）
2. **OAuth 认证**（如启用 Copilot/Antigravity）：启动时自动检查凭证，缺失则触发浏览器登录
3. **正常使用 Claude Code**：代理在后台透明工作
4. **定期查看用量**：[`coding-proxy usage`](./cli-reference.md#3-coding-proxy-usage)
5. **按需手动干预**：[`coding-proxy reset`](./cli-reference.md#4-coding-proxy-reset) 强制切回最高优先级供应商
6. **运行时重认证**：[`coding-proxy auth reauth`](./cli-reference.md#7-coding-proxy-auth-reauth) 无需重启即可刷新凭证
7. **查看可视化看板**：浏览器访问 `http://127.0.0.1:3392/dashboard`
