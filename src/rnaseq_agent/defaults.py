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

# 框架 15.3 黄金路线 A：diffexp 为“条件开放”阶段 ——
# DESeq2 两组差异表达（冻结公式 ~ condition），默认关闭；
# 由 DEG 设计门禁（单样本/不足重复/混杂 -> NOT_EVALUABLE）批准后启用。
DEFAULT_PIPELINE = {
    "fastp": {"enabled": True, "version": "0.24.1"},
    "star": {"enabled": True, "version": "2.7.11b"},
    "arriba": {"enabled": True, "version": "2.5.0"},
    "featurecounts": {"enabled": True, "version": "Subread 2.1.1"},
    "rsem": {"enabled": True, "version": "1.2.28"},
    "diffexp": {"enabled": False, "version": "DESeq2 1.40+ (R 4.2+)"},
}

DEFAULT_CONTAINER = {
    "enabled": True,
    "engine": "apptainer",
    "image_path": "/data/containers/rnaseq-agent-star-rsem.sif",
    "bind_paths": [],
}
