# bkbio-eval 真实 Adapter

本项目提供 `rnaseq-agent-bkbio-eval`，把 `bkbio-eval` 的 counts 用例接到
SysuccRNAseqAgent 的真实执行链。Adapter 不实现差异表达统计，也没有 CPM、t 检验
或其它回退算法。调用链固定为：

```text
counts.tsv + coldata.tsv
  -> 隔离的 counts 直入项目
  -> ProjectSession.plan()
  -> ProjectSession.confirm() / Analysis Contract
  -> ProjectSession.execute_stage("counts")
  -> attempts/<run_id>/downloads/extracted/diffexp/deseq2_results.tsv
  -> bkbio-eval/analyzer-result@1
```

Adapter 接受 evaluator 的四个标准参数：

```powershell
rnaseq-agent-bkbio-eval `
  --inputs C:\path\to\inputs `
  --params C:\path\to\params.json `
  --out C:\path\to\result.json `
  --case-id L0_smoke_synthetic
```

也可以直接运行模块：

```powershell
python -m rnaseq_agent.bkbio_eval_adapter --inputs ... --params ... --out ...
```

挂入 evaluator 时建议使用 Python 的绝对 Windows 路径，因为 evaluator 会在用例临时
目录中启动被测进程：

```powershell
bkbio-eval run --select L0_smoke_synthetic `
  --analyzer-cmd "C:/path/to/.venv/Scripts/python.exe -m rnaseq_agent.bkbio_eval_adapter"
```

## 执行环境

默认读取工作台已经保存的用户级服务器连接
`~/.rnaseq_agent/connection.json`（可用 `RNASEQ_AGENT_HOME` 改目录）。密码只解密到
进程内 SSH 凭据，不会写进评测项目、Analysis Contract 或结果 JSON。

容器、轮询和额外运行设置可以放在一个项目 JSON 中，并通过以下任一方式提供：

```powershell
$env:RNASEQ_AGENT_EVAL_PROJECT_TEMPLATE = "C:\path\to\eval-project-template.json"
```

或把参数放在 analyzer 命令中：

```powershell
python -m rnaseq_agent.bkbio_eval_adapter `
  --project-template C:\path\to\eval-project-template.json `
  --inputs ... --params ... --out ...
```

模板可包含 `server`、`container`、`reference` 和 `polling`。非默认 SSH 私钥可通过
`server.auth_mode: key` 与 `server.key_path` 指定；Adapter 只在本次运行期间把它装入
进程内 SSH 凭据，结束后恢复原凭据。用户级共享连接中的 host、user、scheduler、资源
和远程根目录会覆盖模板中的同名连接字段。若既没有完整共享连接，也没有完整模板，
命令以退出码 `3` 报告 unavailable。远程 R/DESeq2 或容器不可用时，提交作业前的只读
探针同样以退出码 `3` 报告 unavailable；若探针通过后真实执行仍失败，则以退出码 `5`
返回。两种情况都不会改用 Adapter 内置算法。

## 首版科学边界

首版只支持原始整数 counts 直入，并沿用主项目当前冻结的 DESeq2 门禁：

- `design` 必须等价于 `~ condition`；
- `paired` 必须为 `false`；
- 必须恰好有 reference/contrast 两组，且每组至少 3 个生物学重复；
- `counts.tsv` 样本列必须与 `coldata.tsv` 的 `sample` 精确一致；
- 当前真实 counts stage 未暴露 `min_count_prefilter`，因此该值必须为 `0` 或省略。

paired 或多因素设计会在创建项目和远程执行之前以 `NOT_EVALUABLE`、退出码 `4`
明确拒绝，绝不静默降级成非配对分析。因此当前 `L0_smoke_synthetic` 是首轮可执行
目标；现有 TCGA（`~ patient + condition`）和 airway（`~ cell + condition`）L1 用例
要等主项目真实 DESeq2 流程支持相应设计后才能接入。

`L0_smoke_synthetic` 的 8 个基因会让 DESeq2 的标准 dispersion curve 拟合报出
`all gene-wise dispersion estimates are within 2 orders of magnitude`。主流程只对这一条
明确错误采用 DESeq2 建议的 gene-wise dispersion 路径，其他 DESeq2 错误仍原样抛出。
在 DESeq2 1.46.0 的本地校准中，该路径得到预期的 3 个显著基因（G1/G2/G3）、2 个
上调和 1 个下调。

evaluator 的逐基因 log2FC 中心值来自 CPM + Welch t 检验参考实现，而不是 DESeq2。
真实 DESeq2 在该 L0 数据上有约 `+0.11176` 的 median-ratio 归一化偏移；L0 因此使用
经过变异测试校准的绝对容差接受两种合法方法，同时仍会拒绝未归一化的 G8
（log2FC 约 `2.04`）。Adapter 始终保留真实 DESeq2 数值，不用 CPM 值覆盖产物。
使用 R 4.5.2 / DESeq2 1.46.0 的 SSH 全链校准中，L0 的 18 项检查全部通过。

## 输出和追溯

`result.json` 包含 evaluator 所需的 metrics、基因集合、全基因
`tables.deseq2_results` 和真实 DESeq2 产物相对路径。显著集合只是在同一张真实
DESeq2 结果表上应用用例声明的 FDR/log2FC 阈值，不会重新计算统计量。

额外的 `provenance` 与 `params_history` 记录：

- 主项目版本和 Git commit；
- Analysis Contract ID 与 run ID；
- counts 输入和 DESeq2 产物 SHA-256；
- run/result manifest ID；
- 容器配置及显式提供的镜像 digest；
- 请求设计与实际执行设计。

若配置只有镜像 tag/path、没有 digest，Adapter 会把 digest 记录为 `null`，不会把
tag 或配置哈希冒充成镜像内容 digest。
