# Solve2Ship（STS）

**定义目标。让方案竞争。交付最佳方案。**

[English](README.md) · [简体中文](README.zh-CN.md)

提供输入样本和自然语言期望，系统自主提出不同的可执行技术路线，构建、真实评测，
在预算内迭代，并将受支持的胜出方案交付为可调用的本机服务。标签和初始工作流可选。
规则、代码、机器学习和大模型方案在统一证据下竞争，简单程序也可以胜过复杂工作流。

**v0.12.0 工程预览版。** 仓库只交付产品源码、运行配方、自动化测试和使用文档。
密钥、真实用户数据、本机数据库、Agent 上下文和开发过程记录不进入公开仓库。

## 当前能力

- JSON/JSONL 样本、自然语言目标及可选约束，保存为不可变任务版本。
- 真实生成和执行规则、Prompt、混合、隔离 Python、数值分类器、有界模型工作流与实验性受限工具调用方案，
  不以虚构图表或输出冒充优化结果。
- 实际输出/耗时、模型费用上界、逐步调用归因、数据划分、有界修改、配对重测、
  保守剪枝和历史回退。
- 默认 English，在 Settings 切换简体中文；控件、状态、费用与错误均有对应中文展示。
- 规则/Python/机器学习及含模型调用的 Prompt/混合/工作流方案可导出源码 ZIP，
  具有独立 HTTP 验证能力；模型服务使用独立凭据和持久化预算。

生成代码和模型加载只在经过验收、限制资源的 Docker 容器内进行。模型请求经过容器
外的计费网关；候选容器不会得到密钥、Docker 套接字或宿主挂载。费用不明时保留预留
金额，不会自动重放请求。

## 启动

需要 Python 3.13+ 和 SQLite；测试还需要 Node 24+。控制层无需安装第三方应用包。

```sh
git clone https://github.com/runzhiyyds/solve2ship.git
cd solve2ship
make check
make run
```

打开 http://127.0.0.1:8000。这是仅绑定本机的开发服务，尚未具备生产鉴权、TLS 和多租户。

```sh
PYTHONPATH=src python3 -m solve2ship configure-model
PYTHONPATH=src python3 -m solve2ship authorize-budget --cny YOUR_APPROVED_LIMIT
PYTHONPATH=src python3 -m solve2ship model-status
```

把 YOUR_APPROVED_LIMIT 替换为实际批准的累计预算。密钥只在终端隐藏输入，不要放入
聊天、命令参数、样本或浏览器存储。本机配置仅文件所有者可读，但仍是明文，不是生产
凭据保险库。预算不会清零已发生用量。当前付费网关只接受已核验的 DeepSeek Flash
地址和模型，价格按日期过期；其他模型/地址必须先审查才能付费调用。

规则/Prompt/混合方案不需要 Docker；Python/机器学习/工作流需要本机运行验收。
可选运行环境的准备步骤见[使用说明](docs/USAGE.md)，输入与目标示例见 [examples](examples/)。

## 证据与边界

产品测试集包含 138 项 Python 测试和 5 项前端测试。协议夹具与真实模型/运行环境测试
分开；本机合成任务已验证模型与代码组合以及部分独立服务导出，但小型样例不能代表
真实业务准确率、普遍优越性或已核验优化收益。用户标签未核验，模型裁判未校准。一次中文混合工具任务保留了 Agent 输出失败，
最终选择更简单的 Python 方案；工具调用只是参与竞争的路线，不预设更好。

通用 Agent/Harness、成熟优化器、可信评价、更多数据/
模型类型和生产托管仍待实现。最终测试只使用一次；缺少交付能力时在暴露数据前停止。
模型 token 估算不包含本机计算与托管成本。

## 文档与发布

- [使用说明](docs/USAGE.md)
- [产品架构](docs/PRODUCT_ARCHITECTURE.md)
- [依赖与许可证](docs/DEPENDENCIES.md)
- [安全与能力边界](docs/SECURITY.md)
- [版本变动](CHANGELOG.md)、[贡献说明](CONTRIBUTING.md)

已配置推送/PR 和版本标签检查；首发时托管检查无法启动，已通过本机测试。托管执行恢复
后，标签将先测试再发布双语预览说明。只同步已验收的
产品变更，不自动上传普通文件保存或私有开发记录。

## 许可证

[Apache-2.0](LICENSE)，Copyright 2026 郭辰远（Chenyuan Guo）。请保留 [NOTICE](NOTICE)。
第三方组件保留各自许可与声明，运行环境/容器镜像不随源码打包，其分发义务须单独履行。
尚未作完整法律、漏洞或正式 SPDX 认证。
