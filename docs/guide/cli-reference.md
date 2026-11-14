# CLI 命令参考

<details>
<summary><strong>📑 目录（点击展开）</strong></summary>

- [CLI 命令参考](#cli-命令参考)
  - [1. coding-proxy start](#1-coding-proxy-start)
  - [2. coding-proxy status](#2-coding-proxy-status)
  - [3. coding-proxy usage](#3-coding-proxy-usage)
  - [4. coding-proxy reset](#4-coding-proxy-reset)
  - [5. coding-proxy auth login](#5-coding-proxy-auth-login)
  - [6. coding-proxy auth status](#6-coding-proxy-auth-status)
  - [7. coding-proxy auth reauth](#7-coding-proxy-auth-reauth)
  - [8. coding-proxy auth logout](#8-coding-proxy-auth-logout)
  - [9. coding-proxy config](#9-coding-proxy-config)
    - [9.1 config path](#91-config-path)
    - [9.2 config export](#92-config-export)
    - [9.3 config import](#93-config-import)
    - [9.4 config set](#94-config-set)
    - [9.5 config reset](#95-config-reset)
    - [9.6 config set-secret](#96-config-set-secret)
    - [9.7 config token](#97-config-token)

</details>

## 1. coding-proxy start

启动代理服务。

```bash
coding-proxy start [OPTIONS]
```

| 参数       | 缩写 | 说明                                                                                                                              |
| ---------- | ---- | --------------------------------------------------------------------------------------------------------------------------------- |
| `--config` | `-c` | 旧 YAML 的一次性导入源（存储尚未初始化时导入；与已导入版本不一致时拒绝启动）                                                      |
| `--port`   | `-p` | 监听端口（仅本次运行覆盖存储配置）                                                                                                |
| `--host`   | `-h` | 监听地址（仅本次运行覆盖存储配置）                                                                                                |

运行时配置来自 `~/.coding-proxy/config.db`（可用 env `CODING_PROXY_HOME` 重定向）；首次启动若存在 `~/.coding-proxy/config.yaml` 会自动一次性导入（等价校验通过才落库，源文件不改写）。

**示例**：

```bash
# 以存储的配置启动
coding-proxy start

# 首次启动时从指定 YAML 导入
coding-proxy start -c ~/my-config.yaml

# 自定义监听地址和端口
coding-proxy start --host 0.0.0.0 --port 3392
```

> 若启用了 Copilot 或 Antigravity 供应商但未配置凭证，启动时会自动触发 OAuth 浏览器登录流程。

## 2. coding-proxy status

查看当前代理状态和各供应商层级信息。

```bash
coding-proxy status [OPTIONS]
```

| 参数     | 缩写 | 说明                      |
| -------- | ---- | ------------------------- |
| `--port` | `-p` | 代理服务端口（默认 3392） |

**输出示例**：

```
zhipu
  熔断器: closed  失败=0
  配额: within_quota  45.2% (452000000/1000000000)

anthropic
  熔断器: closed  失败=0
```

每个 tier 独立展示名称、熔断器状态和配额守卫状态。

**熔断器状态说明**：

| 状态        | 含义                         |
| ----------- | ---------------------------- |
| `closed`    | 正常运行                     |
| `open`      | 熔断中，跳过该层降级到下一层 |
| `half_open` | 恢复测试中                   |

## 3. coding-proxy usage

查看 Token 使用统计与费用估算。

```bash
coding-proxy usage [OPTIONS]
```

| 参数       | 缩写 | 说明                                                            |
| ---------- | ---- | --------------------------------------------------------------- |
| `--days`   | `-d` | 统计天数（默认 7）                                              |
| `--week`   | `-w` | 最近第 N 周统计（按周聚合，默认 1）                             |
| `--month`  | `-m` | 最近第 N 月统计（按月聚合，默认 1）                             |
| `--total`  | `-t` | 统计全部历史记录（按供应商+模型聚合）                           |
| `--vendor` | `-v` | 过滤供应商（支持逗号分隔多选，如 `anthropic,zhipu`）            |
| `--model`  | —    | 过滤实际服务模型（支持逗号分隔多选，如 `glm-5v-turbo,glm-5.1`） |
| `--db`     | —    | 覆盖用量数据库路径（默认取存储配置的 `database.path`）          |

> 用量统计按存储的配置解析数据库路径（不解密密钥）；存储尚未初始化（未做过首次导入）时按旧 YAML 只读解析并提示先执行 `start`。

> **时间维度互斥**，优先级：`-t > -m > -w > -d`。

**示例**：

```bash
# 查看最近 7 天统计（默认）
coding-proxy usage

# 本周统计（第 1 周）
coding-proxy usage -w 1

# 本月统计（第 1 月）
coding-proxy usage -m 1

# 全部历史，按供应商+模型聚合
coding-proxy usage -t

# 最近 30 天，仅 Anthropic 和智谱供应商
coding-proxy usage -d 30 -v anthropic,zhipu

# 按实际服务模型过滤
coding-proxy usage --model glm-5v-turbo,claude-sonnet-4-6
```

**输出字段说明**：

| 字段                              | 说明                                         |
| --------------------------------- | -------------------------------------------- |
| 日期                              | 统计日期                                     |
| 供应商                            | 处理请求的供应商名称                         |
| 请求模型                          | 客户端请求的原始模型名称                     |
| 实际模型                          | 供应商实际使用的模型名称（经映射后）         |
| 请求数                            | 总请求数                                     |
| 输入/输出/缓存创建/缓存读取 Token | 各维度 Token 消耗                            |
| 总 Token                          | 所有维度之和                                 |
| Cost                              | 基于定价配置计算的费用；未配置定价时显示 `-` |
| 平均耗时(ms)                      | 平均响应时间                                 |

## 4. coding-proxy reset

手动重置所有层级的熔断器和配额守卫，恢复使用最高优先级供应商。支持运行时重排序 N-tier 链路。

```bash
coding-proxy reset [OPTIONS]
```

| 参数       | 缩写 | 说明                                            |
| ---------- | ---- | ----------------------------------------------- |
| `--port`   | `-p` | 代理服务端口（默认 3392）                       |
| `--vendor` | `-v` | 提升/重排序 vendor 优先级（单个或逗号分隔多个） |

**重排序语义**：
- 单个 vendor：提升该 vendor 到最高优先级，其余保持相对顺序
  ```bash
  # 提升 anthropic 到最高优先级
  coding-proxy reset -v anthropic
  ```
- 多个 vendor：替换整个 N-tier 链路顺序
  ```bash
  # 替换整条链路
  coding-proxy reset -v anthropic,zhipu,copilot
  ```

**重置范围**：所有层级的熔断器状态（→ CLOSED）、配额守卫状态（→ WITHIN_QUOTA）、周级配额守卫状态（→ WITHIN_QUOTA）、Rate Limit 截止时间（→ 清除）。

> **仅复位状态，不清用量**：配额守卫的滑动窗口用量计数（`window_usage_tokens` / `usage_percent`）**原样保留**。该基线仅在进程启动时从数据库回填，一旦清零，Dashboard 的配额百分比会永久停在 0% 直到重启。若某 vendor 窗口用量确已超过 `token_budget × threshold_percent`，守卫**保持** `QUOTA_EXCEEDED`（不伪造用量），但仍会清除上游 cap 错误卡死标志并把被 `Retry-After` 拉长的探测间隔还原为默认值；真正被解开的是熔断、Rate Limit 与 cap 错误卡死标志。

## 5. coding-proxy auth login

执行 OAuth 浏览器登录，获取供应商访问凭证。

```bash
coding-proxy auth login [OPTIONS]
```

| 参数         | 缩写 | 说明                                                     |
| ------------ | ---- | -------------------------------------------------------- |
| `--provider` | `-p` | 指定 provider（`github` / `google`）；省略则依次登录两者 |

**示例**：

```bash
# 仅登录 GitHub（Copilot）
coding-proxy auth login -p github

# 仅登录 Google（Antigravity）
coding-proxy auth login -p google

# 依次登录两者
coding-proxy auth login
```

## 6. coding-proxy auth status

查看已登录的 OAuth 凭证状态。

```bash
coding-proxy auth status
```

**输出示例**：

```
github: 有效  有 refresh_token
google: 已过期  无 refresh_token
```

## 7. coding-proxy auth reauth

触发运行中代理的 OAuth 重认证。

```bash
coding-proxy auth reauth PROVIDER [OPTIONS]
```

| 参数       | 缩写 | 说明                                       |
| ---------- | ---- | ------------------------------------------ |
| `PROVIDER` | —    | provider 名称（必填）：`github` / `google` |
| `--port`   | `-p` | 代理服务端口（默认 3392）                  |

**示例**：

```bash
# 触发 GitHub 重认证
coding-proxy auth reauth github

# 指定端口
coding-proxy auth reauth google -p 9090
```

> 此命令通过 [`POST /api/reauth/{provider}`](./api-reference.md#10-post-apireauthprovider) 向运行中的代理发送重认证请求，无需重启服务。

## 8. coding-proxy auth logout

清除已存储的 OAuth 凭证。

```bash
coding-proxy auth logout [OPTIONS]
```

| 参数         | 缩写 | 说明                                   |
| ------------ | ---- | -------------------------------------- |
| `--provider` | `-p` | 指定 provider；省略则登出所有 provider |

> `auth login` / `auth status` / `auth logout` 的 Token Store 路径与 Google 凭据同样按存储的配置解析；存储尚未初始化时按旧 YAML 只读解析并提示先执行 `start`。

## 9. coding-proxy config

配置管理子命令（读写 `~/.coding-proxy/config.db`；业务配置保存后对运行中的代理热切换生效，bootstrap 字段需重启）。

### 9.1 config path

显示配置相关文件路径（home、config.db、master.key、admin_token）。

```bash
coding-proxy config path
```

### 9.2 config export

把当前生效配置导出为 YAML（密钥只输出 `${VAR}` 或 `<redacted>`，不泄露明文）。

```bash
coding-proxy config export [--out FILE]
```

### 9.3 config import

从 YAML 导入配置（整体替换当前覆盖，先做与旧 loader 的等价校验；导出文件中的 `<redacted>` 表示保留本地已存密钥）。

```bash
coding-proxy config import FILE [--dry-run]
```

`--dry-run` 只输出计划不落盘；导入后源文件若再被修改，`start` 会拒绝启动并提示重导。

### 9.4 config set

修改单个字段（JSON 值；含 Dashboard 不可写的 bootstrap 字段，如 `server.port`）。

```bash
coding-proxy config set vendors.zhipu.timeout_ms 60000
coding-proxy config set server.port 8080          # bootstrap 字段，需重启生效
```

### 9.5 config reset

删除某 section 的覆盖，恢复包内默认值（密钥不受影响）。

```bash
coding-proxy config reset vendors
```

### 9.6 config set-secret

写入密钥（值从不回显；仅允许可打印 Latin-1 字符）。

```bash
echo -n "sk-..." | coding-proxy config set-secret vendors.zhipu.api_key --stdin
coding-proxy config set-secret vendors.zhipu.api_key --env ZHIPU_API_KEY    # 绑定任意环境变量（仅 CLI）
coding-proxy config set-secret vendors.zhipu.api_key --default-env          # 恢复包内默认变量
coding-proxy config set-secret vendors.zhipu.api_key --clear                # 显式置空
```

> vendor 的 `base_url` / `token_url` 变化时，未在同一请求显式写入的密钥会被清空（防密钥发往新目标），命令输出会列出被清空的路径。

### 9.7 config token

输出 Dashboard 管理 token（不存在时创建）；用于 Config 页解锁或直接调 `GET` / `PUT /api/config`（请求头 `X-Coding-Proxy-Admin`）。

```bash
coding-proxy config token
```
