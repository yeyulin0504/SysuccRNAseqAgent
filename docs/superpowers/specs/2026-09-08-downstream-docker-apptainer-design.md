# 下游分析 Docker 与 Apptainer 设计

## 目标与范围

为 bulk RNA-seq 下游分析提供固定版本、可复现的容器运行环境。镜像覆盖 R、DESeq2 和 CMScaller；FASTQ 质控、比对、定量与融合检测继续使用现有执行环境，后续再拆分上游镜像。

## 镜像

- 仓库提供一个 Dockerfile，安装固定版本的 R、Bioconductor、DESeq2、CMScaller 及运行依赖。
- 镜像暴露 `Rscript`、DESeq2 和 CMScaller，不运行常驻服务。
- 构建过程执行包加载验证；版本信息写入镜像标签与运行产物。
- 镜像支持两种输入：原始整数 counts 用于 DESeq2 和 `CMScaller(RNAseq=TRUE)`；已标准化表达矩阵只用于 `CMScaller(RNAseq=FALSE)`。

## HPC 执行

- Docker/OCI 镜像在服务器通过 `apptainer pull docker://<image>` 转换为 `.sif`，也支持直接填写已有 `.sif` 路径。
- Slurm 作业使用 `apptainer exec --cleanenv --bind ... <image.sif> Rscript ...`。
- 默认只绑定项目 attempt、输入与输出目录；额外 bind path 必须来自结构化配置。
- 执行前验证 Apptainer 可用、SIF 可读，以及镜像内 R、DESeq2、CMScaller 可加载。

## 配置与网页

- 设置页增加容器区：启用开关、引擎、Docker/OCI URI、服务器 SIF 路径、额外挂载目录。
- 提供“保存容器配置”“拉取/构建 SIF”“测试容器”按钮。
- 拉取动作在远程服务器执行受限的结构化命令，只允许 `docker://` 或 `oras://` URI，并将目标限制到配置的容器目录。
- 测试结果显示引擎版本、镜像可读状态和三个 R 组件的加载结果。

## 对话式样本发现

- 用户可以不先填写样本表、参考文件或分析参数，直接在项目对话中要求浏览服务器目录并寻找 RNA-seq 样本。
- 对话路由把“浏览目录/找样本”转换为结构化的只读样本发现动作；大模型不能生成或执行任意 shell。
- 未提供路径时使用已保存的 `server.remote_workdir`，提供路径时只允许绝对 POSIX 路径。
- 扫描结果只展示 FASTQ 配对候选和未配对文件，不自动生成计划或提交作业；用户确认后才写入项目样本配置。
- SSH 密码继续由本机凭据存储读取，不进入对话内容、响应或审计记录。

## 可追溯性

- Analysis Contract 冻结容器引擎、镜像 URI、SIF 路径、bind paths 与 SHA256。
- 每次 attempt 保存 Apptainer 版本、镜像 SHA256、R sessionInfo 和包版本。
- 镜像或挂载配置变化使下游分析产物进入 stale 状态，必须产生新 attempt。

## 错误处理

- Apptainer 缺失、镜像不可读、拉取失败或包加载失败均在执行前阻断。
- 网页只显示清理后的错误摘要，不返回服务器原始环境变量或凭据。
- 标准化表达矩阵禁止进入 DESeq2；探针矩阵必须先完成平台注释与 gene symbol 聚合。

## 验收

- Docker 镜像可以构建，且容器内成功加载 DESeq2 与 CMScaller。
- 模拟远程测试覆盖拉取命令、路径限制、容器预检和 Apptainer 包装命令。
- 网页可以保存、回读和测试容器配置。
- 对话“浏览我的服务器目录找样本”可调用现有 SSH 连接并返回候选样本；无需先填写样本表。
- counts 模式和标准化表达模式生成不同且正确的 CMScaller 参数。
- 现有非容器执行方式继续可用。
