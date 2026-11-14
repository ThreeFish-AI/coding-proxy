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

运行时配置存放在 `~/.coding-proxy/config.db`（SQLite，0600 权限；密钥字段 Fernet 加密），无需创建或编辑任何 YAML：包内默认值开箱即用，最小配置只需提供一个 API Key。两种方式任选：

```bash
# 方式一：环境变量（默认配置即引用 ${ZHIPU_API_KEY}）
export ZHIPU_API_KEY="your-api-key-here"

# 方式二：密钥加密写入 config.db（值不回显）
coding-proxy config set-secret vendors.zhipu.api_key --stdin
```

日常的全部业务配置（vendors、tiers、failover、模型映射、定价、native_api、会话策略）在 Dashboard → Config 页在线编辑，保存即热切换生效；无需重启。

> **从旧版本升级（已有 `~/.coding-proxy/config.yaml`）**：首次 `coding-proxy start` 会自动做一次性导入（源文件不改写），此后不再读取 YAML。
>
> - 项目目录下的 `./config.yaml` **不会**被自动导入（防止第三方仓库内的配置被误当全局配置）；如需导入请显式 `coding-proxy start -c ./config.yaml` 或 `coding-proxy config import ./config.yaml`。
> - 导入源本体（或 `start -c` 指定文件）被修改会导致启动被拒（内容与已导入版本不一致），请改用 Dashboard / `coding-proxy config` 修改；其余来源仅提示。

> **安全最佳实践**：
> - API Key 优先使用环境变量引用或 `config set-secret` 加密写入，避免明文散落
> - 密钥在 config.db 中以 Fernet 加密存储（主密钥 `~/.coding-proxy/master.key`，0600），展示仅尾 4 位
> - OAuth Token 存储于 `~/.coding-proxy/tokens.json`，建议 `chmod 600` 限制访问
> - Dashboard 配置管理需 admin token（`coding-proxy config token` 查看），管理 API 只认 `X-Coding-Proxy-Admin` 请求头
> - 若设置 `server.host: "0.0.0.0"` 接受外部连接，确保在可信网络环境中运行

## 4. 启动服务

```bash
# 使用存储的配置启动（首次启动自动导入旧 YAML，如存在）
coding-proxy start

# 指定端口 / 监听地址（仅本次运行生效，不改存储）
coding-proxy start --port 8080

# 指定旧 YAML 作为一次性导入源（导入后配置存入 config.db）
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
