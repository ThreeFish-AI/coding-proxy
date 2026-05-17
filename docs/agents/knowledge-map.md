# Knowledge Map（知识索引）

> 项目所有文档的统一入口与权威索引。由 [AGENTS.md §Knowledge Map](../../AGENTS.md) 锚定，文档目录变更时**必须**即时同步更新本文件。
>
> **使用方式**：按"受众 × 目的"二维定位所需文档；不确定起点时，从「入口导航」开始。

[TOC]

---

## 1. 入口导航

| 文档                                          | 角色                                            | 受众            |
| --------------------------------------------- | ----------------------------------------------- | --------------- |
| [README.md](../../README.md)                  | 项目首页（英文版门面）                          | 公开访客        |
| [docs/zh-CN/README.md](../zh-CN/README.md)    | 项目首页中文镜像（与英文版功能对等）            | 中文公开访客    |
| [docs/user-guide.md](../user-guide.md)        | 用户操作上位导航 + 配置概览速查                 | 终端用户        |
| [docs/framework.md](../framework.md)          | 架构枢纽（项目动机、设计目标、模块清单）        | 架构师/贡献者   |

---

## 2. 用户向（[docs/guide/](../guide/)）

> 面向最终用户的操作手册，按"安装 → 配置 → 运行 → 观测 → 排障"线性铺陈。

| 文档                                              | 主旨                                                |
| ------------------------------------------------- | --------------------------------------------------- |
| [guide/quickstart.md](../guide/quickstart.md)     | 环境要求、安装、最小配置、启动、Claude Code 集成    |
| [guide/vendors.md](../guide/vendors.md)           | 全部 9 种供应商配置详情、模型映射、定价表           |
| [guide/cli-reference.md](../guide/cli-reference.md) | start / status / usage / reset / auth 全部命令      |
| [guide/api-reference.md](../guide/api-reference.md) | /v1/messages、health、status、reset、dashboard 等   |
| [guide/dashboard.md](../guide/dashboard.md)       | Web 可视化看板功能与交互                            |
| [guide/monitoring.md](../guide/monitoring.md)     | 日志、用量统计、性能调优、常见场景、故障排查        |

---

## 3. 架构向（[docs/arch/](../arch/)）

> 面向贡献者与维护者的架构与实现细节，从 [framework.md](../framework.md) 正交分解而来。

| 文档                                                  | 主旨                                                  |
| ----------------------------------------------------- | ----------------------------------------------------- |
| [arch/config-reference.md](../arch/config-reference.md) | 配置参数权威定义（Single Source of Truth）            |
| [arch/design-patterns.md](../arch/design-patterns.md) | 13 种设计模式详解（熔断器、状态机、Composite 等）     |
| [arch/routing.md](../arch/routing.md)                 | 路由引擎 12 个子模块职责                              |
| [arch/vendors.md](../arch/vendors.md)                 | Vendor 类层次结构与 9 种实现                          |
| [arch/convert.md](../arch/convert.md)                 | Anthropic ↔ Gemini ↔ OpenAI 三向格式转换              |
| [arch/testing.md](../arch/testing.md)                 | 测试覆盖矩阵与工具链                                  |

---

## 4. 运维向（[docs/ops/](../ops/)）

> 面向运维与发布工程的流程文档。

| 文档                                | 主旨                                              |
| ----------------------------------- | ------------------------------------------------- |
| [ops/ci-cd.md](../ops/ci-cd.md)     | 发布流程、热修复、回滚、CI/CD 故障排查            |

---

## 5. Agent 协作（[docs/agents/](./)）

> AGENTS.md 工程行为准则的卫星文件，定义 AI Agent 协作过程中的规范与协议。

| 文档                                                            | 主旨                                          |
| --------------------------------------------------------------- | --------------------------------------------- |
| [agents/knowledge-map.md](./knowledge-map.md)                   | 本文件——项目文档统一索引                      |
| [agents/reference-specifications.md](./reference-specifications.md) | IEEE 文献引用格式模板与实践指南               |
| [agents/browser-validation.md](./browser-validation.md)         | 浏览器验证协议（连通性自检、凭证管理、E2E）   |

---

## 6. 问题档案

| 文档                              | 主旨                                                  |
| --------------------------------- | ----------------------------------------------------- |
| [docs/issue.md](../issue.md)      | 已处理 Issue 摘要档案（表因、根因、防范）              |

---

## 7. 工程规范（顶层）

| 文档                              | 主旨                                                  |
| --------------------------------- | ----------------------------------------------------- |
| [AGENTS.md](../../AGENTS.md)      | 工程行为准则与 AI Agent 协作协议（与 CLAUDE.md 同源） |
| [CHANGELOG.md](../../CHANGELOG.md)| 版本历史与变更日志                                    |

---

## 维护约束

1. **同步原则**：新增/删除/重命名 `docs/` 下任意 .md 文件时，**必须**同步本索引。
2. **路径基准**：本文件位于 `docs/agents/`，所有相对路径以此为基准（向上一级 `../` 访问 `docs/`，向上两级 `../../` 访问仓库根）。
3. **链接验证**：维护者修改本文件后应通过 grep 自检：所有 `[...](path)` 中的 `path` 文件存在。
