from __future__ import annotations

DEFAULT_REFERENCE = {
    "name": "GENCODE_R47_GRCh38p14_ALL",
    "species": "human",
    "release": "GENCODE v47",
    "assembly": "GRCh38.p14",
    "regions": "ALL",
    "gtf_url": "https://ftp.ebi.ac.uk/pub/databases/gencode/Gencode_human/release_47/gencode.v47.chr_patch_hapl_scaff.annotation.gtf.gz",
    "genome_fasta_url": "https://ftp.ebi.ac.uk/pub/databases/gencode/Gencode_human/release_47/GRCh38.p14.genome.fa.gz",
    "remote_ref_dir": "/data/ref/gencode/human/release_47_all",
    "remote_gtf_path": "/data/ref/gencode/human/release_47_all/gencode.v47.chr_patch_hapl_scaff.annotation.gtf",
    "remote_genome_fasta_path": "/data/ref/gencode/human/release_47_all/GRCh38.p14.genome.fa",
    "star_index_dir": "/data/ref/gencode/human/release_47_all/star_2.7.11b",
    "rsem_index_prefix": "/data/ref/gencode/human/release_47_all/rsem/rsem_gencode_v47",
    "arriba_blacklist_path": "",
    "arriba_known_fusions_path": "",
}

# 框架 15.3：DESeq2 差异表达（条件开放）的冻结设置。
DEFAULT_DIFFEXP = {
    "formula": "~ condition",  # 冻结公式，不允许任意统计公式
    "reference_condition": "",  # 空 = 取字典序较小的 condition 作为参考
    "min_replicates_per_group": 3,  # 每组最少生物学重复
    "padj_cutoff": 0.05,
    "lfc_cutoff": 1.0,
}

# 框架 15.3：CMScaller CMS 分子分型（条件开放：已注册分型模型）的冻结设置。
# 输入固定为 featureCounts 原始 counts，RNAseq=TRUE 由 CMScaller 内部
# log2(x+.25)+quantile normalize；doPlot 关闭（仅落盘 CSV/JSON）。
DEFAULT_CMS = {
    "n_perm": 1000,       # p 值置换次数
    "fdr": 0.05,          # 预测置信阈值
    "seed": 20260907,     # 固定 seed 保证 p 值可复现
    "do_plot": False,     # 不生成 subHeatmap，只出结果表
    "min_samples": 30,    # CMScaller 低于 30 样本提示高方差
    "run_mode": "pipeline",  # "pipeline"= 接 featurecounts 跑完整 FASTQ 流程；"counts"= 直接上传 counts 矩阵即跑 CMS
    "reference_condition": "",  # counts 直入时用于分组富集分析的参考组（可选）
}

# 框架 15.3 黄金路线 A：diffexp 为“条件开放”阶段 ——
# DESeq2 两组差异表达（冻结公式 ~ condition），默认关闭；
# 由 DEG 设计门禁（单样本/不足重复/混杂 -> NOT_EVALUABLE）批准后启用。
# cms 同为条件开放阶段（CMS 结直肠癌分子分型），默认关闭，
# 由 CMS 门禁（癌种=CRC / 样本≥30 / featureCounts 前置）批准后启用。
DEFAULT_PIPELINE = {
    "fastp": {"enabled": True, "version": "0.24.1"},
    "star": {"enabled": True, "version": "2.7.11b"},
    "arriba": {"enabled": True, "version": "2.5.0"},
    "featurecounts": {"enabled": True, "version": "Subread 2.1.1"},
    "rsem": {"enabled": True, "version": "1.2.28"},
    "diffexp": {"enabled": False, "version": "DESeq2 1.40+ (R 4.2+)"},
    "cms": {"enabled": False, "version": "CMScaller 2.0"},
}

DEFAULT_CONTAINER = {
    "enabled": True,
    "engine": "apptainer",
    "image_uri": "docker://ghcr.io/sysucc/rnaseq-downstream:2026.09",
    "image_path": "/hwdata/home/yeyulin/containers/rnaseq-downstream-2026.09.sif",
    "bind_paths": [],
}
