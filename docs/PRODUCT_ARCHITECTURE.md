# Product architecture / 产品架构

Samples + natural-language outcome become immutable task revisions. A leased search
worker gives training examples to a model planner, validates actual artifacts through
an open family registry, builds/executes them, evaluates common cases, performs
bounded edits and preserves earlier candidates. Final holdouts remain sealed until
a supported delivery adapter freezes a selected artifact for once-only checks.

The local control plane uses Python stdlib/SQLite and native browser modules. SQLite
transactions, leases and append-only evidence protect consistency; there is no
production tenancy/authentication. Money is reserved before provider requests;
root/global ceilings include all descendants and unknown charges remain reserved.
Invocation receipts capture every inference call, including paid invalid outputs;
selection uses all unique inference costs, with evaluator calls separately attributed.

Rules are bounded data interpreted by trusted code. Python code is data until it
enters a restricted Docker container; numeric ML fitting/deserialization stays there
as well. Workflow data bindings compose 2–4 Rules/Prompt/Python steps. Model steps
use the metered gateway outside containers. Candidate IDs remain open strings.

Delivery copies frozen artifacts and reviewed trusted runtime templates into an
independent HTTP source bundle. Supported code/ML services require the reviewed
local image/Engine, without the STS experiment DB/controller. Model-backed services
use a separate owner-only model profile and persistent usage DB. Bundles contain no
credentials or positive budget authorization. A delegated allowance reserves original
root/global headroom; independent usage is bounded locally and never silently refunded.
Request IDs atomically fence duplicate/concurrent/failed operations; successful repeats
return saved results across restart. This is an owner-local trust boundary, not a
cryptographically enforced distributed quota or production vault.

中文：任务契约、方案执行、评测证据和服务交付分离。规划器只看训练数据，候选通过
开放注册表构建和真实执行；公共验证、预算和保守迭代保留可追踪版本。最终数据在受
支持方案冻结后只使用一次。所有生成代码及模型加载都进入受限容器；模型步骤走独立
计费网关。服务消费冻结产物，无需 STS 控制进程或实验数据库；含模型步骤时使用独立
凭据和持久化用量数据库。划拨额度继续占用原预算，包内不含密钥或正数授权；重复成功
请求使用缓存，失败/中断请求禁止自动重放。当前为本机信任边界，不代表分布式防篡改
配额、生产安全或全局最优保证。


Experimental tool_agent.py adds agent.tools.v1 through the existing open registry.
Unlike a frozen workflow order, each input triggers a bounded model decision over
frozen tool descriptions and prior JSON observations. Tools reuse Rules/Python
adapters; Python stays container-only. Four decisions at most, no automatic recovery
calls, cancellation before tools/decisions and complete receipts. A correct tool
result does not bypass final-output evaluation. No new framework/dependency is
installed or vendored; general agents/harness and optimizer libraries remain open.

中文：实验性工具候选在每次输入后动态选择工具，不固定为某条工作流。工具复用已有
规则/Python 适配器，保留逐次调用、容器与终止证据；工具结果正确也不能绕过最终输出
评测。未安装或复制额外框架，通用 Agent/Harness 与成熟优化库接入仍待完成。
