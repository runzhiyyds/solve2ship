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
local image/Engine, without the STS DB/controller/key. Model-backed independent
serving is unavailable until its own credential/budget lifecycle is implemented.

中文：任务契约、方案执行、评测证据和服务交付分离。规划器只看训练数据，候选通过
开放注册表构建和真实执行；公共验证、预算和保守迭代保留可追踪版本。最终数据在受
支持方案冻结后只使用一次。所有生成代码及模型加载都进入受限容器；模型步骤走独立
计费网关。服务消费冻结产物，受支持的导出无需 STS 控制进程；含模型步骤的独立凭据与
预算生命周期尚待实现。当前为本机开发架构，不代表生产安全或全局最优保证。
