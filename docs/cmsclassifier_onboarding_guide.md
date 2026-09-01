# CMSclassifier 跑通指南（生信小白版）

> 写给师弟：这份文档的目的，是让你从零开始把 CMSclassifier 这个结直肠癌分子分型工具跑起来，并且产出我们项目后续接入所需要的全部材料。你不需要懂 R 语言的高级用法，只需要照着步骤做、把每一步的结果保存好。

## TL;DR

用 R 包 CMSclassifier，拿作者的示例表达矩阵跑一遍，得到和作者一致的分型结果（CMS1-4），然后把环境、代码、输入、输出整理成可以复现的交付物。跑通不难，难的是让「跑通」这件事本身可以被别人重复。

## 为什么选 CMSclassifier

我们项目的设计文档里，结直肠癌（CRC）分型模型的首选就是 CMSclassifier。原因有四个：

- **输入干净**：只需要一个 bulk RNA 表达矩阵，正好接我们现有的 fastp→STAR→RSEM 流程输出
- **R 包形态**：正好验证我们设计的 `Rscript wrapper` 执行契约
- **有作者示例数据**：可以做 golden test，验证你跑的结果和作者一致
- **true single-sample**：随机森林版本（CMS-RF）可以单样本跑，不依赖队列中心化，最适合接入 Agent

## 交付物清单（容器版）

下周同步时，你需要带来以下东西：

| 序号 | 交付物 | 说明 |
|------|--------|------|
| 1 | `cmsclassifier.tar` | Docker 镜像文件（`docker save` 导出） |
| 2 | `Dockerfile` | 构建镜像的文本文件 |
| 3 | `renv.lock` | 容器内 R 包版本清单（在容器里跑 `renv::snapshot()` 生成） |
| 4 | `run_cms.R` 脚本 | 输入一个矩阵文件路径，输出分型结果 CSV |
| 5 | 作者示例输入矩阵 | 从包里提取或从 GitHub 下载 |
| 6 | 作者示例的预期输出 | 作者公布的分型结果（CMS 标签） |
| 7 | 你跑出来的实际输出 | 和第 6 项对比，应该一致 |
| 8 | 一页输入契约说明 | 见下方「输入契约」一节 |
| 9 | 拒绝行为记录 | 见下方「测试拒绝行为」一节 |
| 10 | `sessionInfo()` 输出 | 容器内的 R 版本、所有包版本 |

## 第一步：装环境（Windows 本地 + 容器）

### 装 Docker Desktop

从 https://www.docker.com/products/docker-desktop/ 下载 Windows 版，安装后启动。Docker Desktop 是图形界面，装好后任务栏会有个小鲸鱼图标。

**验证**：打开 PowerShell，输入 `docker --version`，能显示版本号就说明装好了。

### 写 Dockerfile

在你要工作的目录里新建一个文件，文件名就叫 `Dockerfile`（没有后缀），内容如下：

```dockerfile
FROM rocker/r-ver:4.4

RUN R -e 'if (!require("BiocManager")) install.packages("BiocManager")'
RUN R -e 'BiocManager::install("CMSclassifier")'
RUN R -e 'install.packages("renv")'

ENTRYPOINT ["Rscript"]
```

这个文件的意思是：基于一个已经装好 R 4.4 的镜像，再装 CMSclassifier 和 renv。

### 构建 Docker 镜像

在 Dockerfile 所在目录打开 PowerShell：

```powershell
docker build -t cmsclassifier .
```

第一次构建会下载基础镜像，可能要几分钟。看到 `Successfully tagged cmsclassifier:latest` 就说明成功了。

### 在容器里跑 R

```powershell
# 交互式进入容器（用来调试）
docker run -it --rm cmsclassifier R

# 非交互式运行脚本（正式跑分析用）
docker run -v ${PWD}:/data cmsclassifier /data/run_cms.R /data/input.csv /data/output.csv
```

`-v ${PWD}:/data` 的意思是把当前目录挂载到容器里的 `/data`，这样容器里的 R 就能读到你的文件。

### 保存镜像文件

构建好后，把镜像保存成文件，方便传到 HPC：

```powershell
docker save cmsclassifier -o cmsclassifier.tar
```

这个 `.tar` 文件就是交付物之一。

### 转 Apptainer（在 HPC 上做）

把 `cmsclassifier.tar` 传到 HPC，然后在 HPC 上执行：

```bash
apptainer build cmsclassifier.sif docker-archive://cmsclassifier.tar
```

如果 HPC 登录节点不让构建，就把 `.tar` 文件给管理员帮忙转一下。

### 验证容器环境

在本地 Docker 里验证 CMSclassifier 能加载：

```powershell
docker run --rm cmsclassifier -e 'library(CMSclassifier); packageVersion("CMSclassifier")'
```

不报错、能显示版本号，就说明容器环境装好了。

### 锁定 R 包版本（renv.lock）

在容器里执行：

```powershell
docker run -v ${PWD}:/data cmsclassifier -e 'renv::init(project = "/data"); renv::snapshot(project = "/data")'
```

这会在当前目录生成 `renv.lock`，记录容器里所有 R 包的精确版本。虽然容器镜像本身已经锁定了环境，但 `renv.lock` 作为清单文件仍然要保留。

## 第二步：拿到作者的示例数据

在容器里运行 R，提取示例数据：

```powershell
# 交互式进入容器
docker run -it --rm -v ${PWD}:/data cmsclassifier R
```

然后在 R 里执行：

```r
library(CMSclassifier)

# 查看包里有哪些函数和数据
help(package = "CMSclassifier")

# 列出包自带的数据集
data(package = "CMSclassifier")

# 找到示例数据后保存（名字以实际为准）
data(example_data)
write.csv(example_data, "/data/cms_example_input.csv")

# 退出 R
q()
```

同时，去作者的 GitHub 仓库（https://github.com/Sage-Bionetworks/CMSclassifier）看看有没有测试数据和预期结果，下载下来。

## 第三步：理解输入契约

这一步是最重要的，因为后面 Agent 要根据这个判断「什么样的输入能喂给这个模型」。

用 R 打开示例数据，确认以下问题，把答案写在一页 word 或 txt 里：

| 问题 | 你要确认的 |
|------|-----------|
| 矩阵方向 | 行是基因、列是样本？还是反过来？用 `dim()` 看维度，`rownames()` 和 `colnames()` 看名字 |
| 基因 ID 类型 | 是基因名（如 `TP53`）还是 Entrez ID（一串数字）还是 Ensembl ID（`ENSG` 开头）？看 `rownames()` 的前几个值 |
| 表达值尺度 | 是 log2 转换后的值？还是原始 counts？还是 TPM？看数值范围——如果最大值只有十几，大概率是 log2；如果有几千几万，可能是 counts |
| 特征基因数 | 模型需要哪些基因？用 `CMSclassifier:::get_model_features()` 或类似函数（看帮助文档）提取特征列表 |
| 单样本还是队列 | CMSclassifier 的 RF 版本可以单样本；SSP 版本需要参考队列。我们用 **RF 版本**，确认函数名和参数 |

## 第四步：跑通并验证结果

### 跑示例数据

在容器里运行：

```powershell
docker run -v ${PWD}:/data cmsclassifier /data/run_cms.R /data/cms_example_input.csv /data/cms_example_output_mine.csv
```

或者交互式进容器跑：

```powershell
docker run -it --rm -v ${PWD}:/data cmsclassifier R
```

```r
library(CMSclassifier)

# 读取示例输入
input_matrix <- read.csv("/data/cms_example_input.csv", row.names = 1)

# 调用分类函数（函数名以实际为准，看帮助文档）
result <- classifyCMS.RF(input_matrix)

# 查看结果
head(result)

# 保存
write.csv(result, "/data/cms_example_output_mine.csv")
```

输出应该是每个样本的 CMS1 / CMS2 / CMS3 / CMS4 标签，可能还有每个亚型的预测概率。

### 验证正确性

把你自己跑出来的结果和作者公布的结果对比：

- **标签一致**：每个样本的 CMS 亚型标签应该和作者给的一样
- **概率接近**：如果作者给了概率值，你的数值应该在小数点后两位内一致

如果标签不一致，先检查输入数据的矩阵方向、基因 ID 类型、表达值尺度是否正确。

### 保存一切

在容器里保存 sessionInfo：

```r
# 保存 sessionInfo（记录 R 版本和所有包版本）
writeLines(capture.output(sessionInfo()), "/data/sessionInfo.txt")
```

或者直接用命令行：

```powershell
docker run -v ${PWD}:/data cmsclassifier -e 'writeLines(capture.output(sessionInfo()), "/data/sessionInfo.txt")'
```

## 第五步：测试拒绝行为

这步很多人会漏，但它决定了我们的 Agent 能不能正确地「拒绝不该跑的输入」。

故意构造几个坏输入，看模型怎么反应：

### 测试 1：基因缺失一半

```powershell
docker run -v ${PWD}:/data cmsclassifier -e '
input_matrix <- read.csv("/data/cms_example_input.csv", row.names = 1)
set.seed(42)
keep <- sample(rownames(input_matrix), size = nrow(input_matrix) * 0.5)
bad_input <- input_matrix[keep, ]
library(CMSclassifier)
result_bad <- classifyCMS.RF(bad_input)
print("基因缺失50%的结果：")
print(head(result_bad))
'
```

### 测试 2：矩阵方向反了

```powershell
docker run -v ${PWD}:/data cmsclassifier -e '
input_matrix <- read.csv("/data/cms_example_input.csv", row.names = 1)
bad_input2 <- t(input_matrix)
library(CMSclassifier)
result_bad2 <- classifyCMS.RF(bad_input2)
print("矩阵转置后的结果：")
print(head(result_bad2))
'
```

### 测试 3：ID 类型错了

```powershell
docker run -v ${PWD}:/data cmsclassifier -e '
input_matrix <- read.csv("/data/cms_example_input.csv", row.names = 1)
rownames(input_matrix)[1:5] <- c("TP53", "KRAS", "APC", "PIK3CA", "SMAD4")
library(CMSclassifier)
result_bad3 <- classifyCMS.RF(input_matrix)
print("ID类型错误的结果：")
print(head(result_bad3))
'
```

把每个测试的结果记录下来：模型是报错了？还是给了结果但结果明显不对？还是完全没反应？这些行为后面会写成 Agent 的 `NOT_EVALUABLE` 门禁规则。

## 第六步：写交付脚本

把你跑通的过程整理成一个可以重复运行的脚本 `run_cms.R`：

```r
#!/usr/bin/env Rscript
# CMSclassifier 分型脚本
# 用法：Rscript run_cms.R input_matrix.csv output_result.csv

args <- commandArgs(trailingOnly = TRUE)
input_file <- args[1]
output_file <- args[2]

library(CMSclassifier)

# 读取输入
input_matrix <- read.csv(input_file, row.names = 1)

# 检查输入
cat("Input dimensions:", dim(input_matrix), "\n")
cat("Gene ID type (first 5 rownames):", head(rownames(input_matrix), 5), "\n")
cat("Value range:", range(input_matrix), "\n")

# 运行分类
result <- classifyCMS.RF(input_matrix)

# 保存结果
write.csv(result, output_file)
cat("Done. Results written to", output_file, "\n")
```

然后在本地 Docker 里测试：

```powershell
docker run -v ${PWD}:/data cmsclassifier /data/run_cms.R /data/cms_example_input.csv /data/cms_example_output_mine.csv
```

在 HPC 上用 Apptainer 测试：

```bash
apptainer exec cmsclassifier.sif Rscript /data/run_cms.R /data/cms_example_input.csv /data/cms_example_output_mine.csv
```

确保脚本能从头跑到尾，不依赖 RStudio。

## 避坑提示（小白常见死法）

- **不要用 `install.packages("CMSclassifier")`**——它在 Bioconductor，不在 CRAN
- **不要拿 TCGA 原始 counts 直接喂**——CMS 要 log2 表达，counts 要先做 `log2(cpm + 1)` 转换。看包文档确认具体要求
- **不要假设最新版 R 就能装**——Bioconductor 版本和 R 版本绑定，装不上先看 `BiocManager::version()`
- **Docker 挂载路径别写错**——Windows 用 `${PWD}`，Mac/Linux 用 `$PWD` 或 `$(pwd)`；路径里有空格要用引号包起来
- **跑通了不算完**——没有容器镜像、没有示例验证、没有输入契约说明，后面接不进 Agent，等于白跑

## 下周同步要过的东西

1. 你跑出来的示例结果 vs 作者预期结果，一致吗？
2. 输入契约那一页说明，我们一起过一遍
3. 拒绝行为测试的三个结果
4. `Dockerfile`、`cmsclassifier.tar`、`renv.lock` 和 `run_cms.R`

有任何一步卡住了，先截图保存报错信息，然后来找我。

## 元信息

- 目标读者：生信初学者（无 R 语言经验、无 Docker 经验）
- 前置知识：会装软件、会打开 PowerShell
- 技术栈版本：Docker Desktop（Windows）、R 4.4、CMSclassifier（Bioconductor）、Apptainer/Singularity（HPC 侧）
- 预计阅读时间：20 分钟
