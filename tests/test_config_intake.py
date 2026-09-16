"""对话落地配置：把用户消息里的路径 / 样本 / 分组抽成可校验的配置草稿。

用户诉求（2026-09-16）：

> 帮我改对话的写盘能力，4个样本给了2个分组名因为是按ctrl，treat，ctrl，treat
> 的顺序排列的，这个不需要我多说，大语言模型应该自己会理解确定。
> 要允许特异性未知的选项先写着

背景：此前「你帮我执行」被路由成 ``None``，交给大模型；而大模型没有工具
能力，只能回「我无法直接执行」，用户在「AI 说做不到 ↔ 不知道该点哪」之间
形成死循环。这里补的是一条**确定性抽取**：模型负责理解需求，抽取结果由
代码校验并写盘，符合架构原则「LLM 理解需求，确定性代码执行」。

设计取舍：

- 抽取是**纯函数**，不碰文件系统、不发网络请求，便于单测与审计；
- 缺路径 / 缺分组时返回 ``warnings`` 而不是静默猜；
- 分组名少于样本数时，按**用户给出的顺序循环展开**（ctrl,treat,ctrl,treat），
  这是用户明确要求的语义，不要反问。
"""

from __future__ import annotations

import pytest

from rnaseq_agent.config_intake import (
    extract_config_draft,
    expand_conditions,
)


class TestExpandConditions:
    """分组名序列 → 每个样本的 condition。"""

    def test_two_names_cycle_over_four_samples(self) -> None:
        """用户原话：按 ctrl,treat,ctrl,treat 的顺序排列。"""
        result = expand_conditions(["ctrl", "treat"], 4)
        assert result.conditions == ["ctrl", "treat", "ctrl", "treat"]
        assert result.warnings == []

    def test_names_matching_sample_count_are_used_as_is(self) -> None:
        result = expand_conditions(["ctrl", "treat", "ctrl", "treat"], 4)
        assert result.conditions == ["ctrl", "treat", "ctrl", "treat"]
        assert result.warnings == []

    def test_single_name_applies_to_all(self) -> None:
        result = expand_conditions(["treat"], 3)
        assert result.conditions == ["treat", "treat", "treat"]
        # 全同组没有对照，必须警告而不是假装成功。
        assert result.warnings, "单一分组应提示无法构成对比"

    def test_uneven_cycle_warns_about_unbalanced_groups(self) -> None:
        """3 个名字铺 4 个样本会得到 2/1/1，组间不均衡要提示。"""
        result = expand_conditions(["ctrl", "treat", "other"], 4)
        assert result.conditions == ["ctrl", "treat", "other", "ctrl"]
        assert any("不均衡" in w or "重复" in w for w in result.warnings), result.warnings

    def test_empty_names_yield_empty_conditions(self) -> None:
        result = expand_conditions([], 4)
        assert result.conditions == []
        assert result.warnings

    def test_zero_samples_yield_empty_conditions(self) -> None:
        result = expand_conditions(["ctrl"], 0)
        assert result.conditions == []


class TestExtractFastqPaths:
    """从自然语言里抽出 FASTQ 目录与 `_1/_2` 配对样本。"""

    MESSAGE = (
        "你帮我填这些信息：原始 FASTQ 数据\n"
        "/hwdata/home/yeyulin/SysuccRNAseqAgent-master/runs/fastq_pair_test/fastq\n"
        "包含 8 个文件（4 个样本的双端测序）：\n"
        "SRR28119110_1.fastq.gz, SRR28119110_2.fastq.gz\n"
        "SRR28119111_1.fastq.gz, SRR28119111_2.fastq.gz\n"
        "SRR28119112_1.fastq.gz, SRR28119112_2.fastq.gz\n"
        "SRR28119113_1.fastq.gz, SRR28119113_2.fastq.gz\n"
    )

    def test_detects_the_fastq_directory(self) -> None:
        draft = extract_config_draft(self.MESSAGE)
        assert draft.fastq_dir == (
            "/hwdata/home/yeyulin/SysuccRNAseqAgent-master/runs/fastq_pair_test/fastq"
        )

    def test_detects_four_paired_samples(self) -> None:
        draft = extract_config_draft(self.MESSAGE)
        ids = [s["sample_id"] for s in draft.samples]
        assert ids == ["SRR28119110", "SRR28119111", "SRR28119112", "SRR28119113"]

    def test_samples_carry_r1_and_r2_filenames(self) -> None:
        draft = extract_config_draft(self.MESSAGE)
        first = draft.samples[0]
        assert first["fastq_1"] == "SRR28119110_1.fastq.gz"
        assert first["fastq_2"] == "SRR28119110_2.fastq.gz"

    def test_data_source_is_remote_when_dir_is_absolute(self) -> None:
        draft = extract_config_draft(self.MESSAGE)
        assert draft.data_source == "remote_path"

    def test_unmatched_files_are_reported_not_dropped(self) -> None:
        draft = extract_config_draft(
            "FASTQ 在 /data/reads\nA_1.fastq.gz\nB_1.fastq.gz\nB_2.fastq.gz\n"
        )
        assert [s["sample_id"] for s in draft.samples] == ["B"]
        assert any("A_1" in w for w in draft.warnings), draft.warnings


class TestExtractReferenceFiles:
    """参考基因组三件套：GTF / FASTA / STAR 索引。"""

    MESSAGE = (
        "参考基因组和注释文件\n"
        "基础目录：\n"
        "/hwdata/home/yeyulin/project/GSE259357_mm10_analysis/references/gencode_m25_grcm38p6\n"
        "具体文件：\n"
        "GTF 注释：\n"
        "/hwdata/home/yeyulin/project/GSE259357_mm10_analysis/references/"
        "gencode_m25_grcm38p6/gencode.vM25.annotation.gtf\n"
        "基因组 FASTA：\n"
        "/hwdata/home/yeyulin/project/GSE259357_mm10_analysis/references/"
        "gencode_m25_grcm38p6/GRCm38.primary_assembly.genome.fa\n"
        "STAR 索引目录：\n"
        "/hwdata/home/yeyulin/project/GSE259357_mm10_analysis/references/"
        "gencode_m25_grcm38p6/star_index\n"
    )

    def test_detects_gtf(self) -> None:
        draft = extract_config_draft(self.MESSAGE)
        assert draft.reference["gtf"].endswith("gencode.vM25.annotation.gtf")

    def test_detects_genome_fasta(self) -> None:
        draft = extract_config_draft(self.MESSAGE)
        assert draft.reference["genome_fasta"].endswith(
            "GRCm38.primary_assembly.genome.fa"
        )

    def test_detects_star_index_dir(self) -> None:
        draft = extract_config_draft(self.MESSAGE)
        assert draft.reference["star_index"].endswith("star_index")


class TestExtractGrouping:
    """分组：用户会给分组名序列，也可能写「1是control 2是treat」。"""

    def test_ordered_condition_names_cycle_over_samples(self) -> None:
        message = (
            "FASTQ 在 /data/reads\n"
            "SRR1_1.fastq.gz\nSRR1_2.fastq.gz\n"
            "SRR2_1.fastq.gz\nSRR2_2.fastq.gz\n"
            "SRR3_1.fastq.gz\nSRR3_2.fastq.gz\n"
            "SRR4_1.fastq.gz\nSRR4_2.fastq.gz\n"
            "分组按 ctrl, treat, ctrl, treat 顺序\n"
        )
        draft = extract_config_draft(message)
        assert [s["condition"] for s in draft.samples] == [
            "ctrl", "treat", "ctrl", "treat",
        ]

    def test_indexed_assignments_are_honoured(self) -> None:
        """“1是control 2是treat” 这种写法要按样本序号落到对应样本。"""
        message = (
            "FASTQ 在 /data/reads\n"
            "SRR1_1.fastq.gz\nSRR1_2.fastq.gz\n"
            "SRR2_1.fastq.gz\nSRR2_2.fastq.gz\n"
            "1是control 2是treat\n"
        )
        draft = extract_config_draft(message)
        assert [s["condition"] for s in draft.samples] == ["control", "treat"]

    def test_missing_grouping_blocks_the_write(self) -> None:
        """分组是必需字段：缺了它写出的会话必然过不了 Gate-A。

        因此必须**阻断**而不是只警告——否则「先写一半、再补分组」会被
        「已有会话，不覆盖」挡住，用户反而卡得更死。
        """
        message = (
            "FASTQ 在 /data/reads\nSRR1_1.fastq.gz\nSRR1_2.fastq.gz\n"
        )
        draft = extract_config_draft(message)
        assert draft.samples
        assert all(not s.get("condition") for s in draft.samples)
        assert any("分组" in b for b in draft.blockers), draft.blockers
        assert draft.ready is False

    def test_blocker_tells_the_user_how_to_specify_groups(self) -> None:
        message = "FASTQ 在 /data/reads\nSRR1_1.fastq.gz\nSRR1_2.fastq.gz\n"
        draft = extract_config_draft(message)
        blocker = " ".join(draft.blockers)
        # 给出可照抄的写法，避免用户又陷进「不知道该怎么表达」。
        assert "顺序" in blocker or "1是" in blocker, blocker


class TestExtractStrandedness:
    """链特异性未知时也要能落盘（用户明确要求「先写着」）。"""

    @pytest.mark.parametrize("word", ["链特异性未知", "strandedness 未知", "不知道链特异性"])
    def test_unknown_is_recorded_as_unknown(self, word: str) -> None:
        draft = extract_config_draft(f"FASTQ 在 /data/reads\n{word}\n")
        assert draft.strandedness == "unknown"

    @pytest.mark.parametrize(
        "word,expected",
        [
            ("链特异性是 reverse", "reverse"),
            ("链特异性为 forward", "forward"),
            ("链特异性 unstranded", "unstranded"),
        ],
    )
    def test_known_values_are_recognised(self, word: str, expected: str) -> None:
        draft = extract_config_draft(f"FASTQ 在 /data/reads\n{word}\n")
        assert draft.strandedness == expected

    def test_defaults_to_unknown_when_unmentioned(self) -> None:
        draft = extract_config_draft("FASTQ 在 /data/reads\n")
        assert draft.strandedness == "unknown"


class TestRealWorldMessage:
    """用户在真实对话里贴出的完整消息（2026-09-16）。

    这条消息是缺陷复现的原始素材：FASTQ 段在前、参考基因组段在后，
    末尾是「链特异性未知，1是control2是treat」。早期实现把参考段的
    ``star_index`` 当成了 FASTQ 目录，并且只给 2 个样本分到组。
    """

    MESSAGE = (
        "你帮我填这些信息：原始 FASTQ 数据\n"
        "/hwdata/home/yeyulin/SysuccRNAseqAgent-master/runs/fastq_pair_test/fastq\n"
        "包含 8 个文件（4 个样本的双端测序）：\n"
        "SRR28119110_1.fastq.gz, SRR28119110_2.fastq.gz\n"
        "SRR28119111_1.fastq.gz, SRR28119111_2.fastq.gz\n"
        "SRR28119112_1.fastq.gz, SRR28119112_2.fastq.gz\n"
        "SRR28119113_1.fastq.gz, SRR28119113_2.fastq.gz\n"
        "参考基因组和注释文件\n"
        "基础目录：\n"
        "/hwdata/home/yeyulin/project/GSE259357_mm10_analysis/references/gencode_m25_grcm38p6\n"
        "具体文件：\n"
        "GTF 注释：\n"
        "/hwdata/home/yeyulin/project/GSE259357_mm10_analysis/references/"
        "gencode_m25_grcm38p6/gencode.vM25.annotation.gtf\n"
        "基因组 FASTA：\n"
        "/hwdata/home/yeyulin/project/GSE259357_mm10_analysis/references/"
        "gencode_m25_grcm38p6/GRCm38.primary_assembly.genome.fa\n"
        "STAR 索引目录：\n"
        "/hwdata/home/yeyulin/project/GSE259357_mm10_analysis/references/"
        "gencode_m25_grcm38p6/star_index\n"
        "链特异性未知，1是control2是treat\n"
    )

    def test_fastq_dir_is_not_the_star_index(self) -> None:
        """参考段排在 FASTQ 段之后，不能把 star_index 当成 FASTQ 目录。"""
        draft = extract_config_draft(self.MESSAGE)
        assert draft.fastq_dir == (
            "/hwdata/home/yeyulin/SysuccRNAseqAgent-master/runs/fastq_pair_test/fastq"
        )
        assert "star_index" not in draft.fastq_dir

    def test_all_four_samples_get_conditions(self) -> None:
        """「1是control2是treat」是 4 个样本的两组循环，不能只填前两个。"""
        draft = extract_config_draft(self.MESSAGE)
        assert [s["condition"] for s in draft.samples] == [
            "control", "treat", "control", "treat",
        ]

    def test_message_is_ready_to_write(self) -> None:
        draft = extract_config_draft(self.MESSAGE)
        assert draft.blockers == []
        assert draft.ready is True

    def test_reference_is_extracted_alongside_fastq(self) -> None:
        draft = extract_config_draft(self.MESSAGE)
        assert draft.reference["gtf"].endswith("gencode.vM25.annotation.gtf")
        assert draft.reference["genome_fasta"].endswith(
            "GRCm38.primary_assembly.genome.fa"
        )
        assert draft.reference["star_index"].endswith("star_index")


class TestPartialIndexedGroupingIsCycled:
    """指派数少于样本数时按用户给出的序列循环补齐，不反问。"""

    def test_two_assignments_cycle_over_four_samples(self) -> None:
        message = (
            "FASTQ 在 /data/reads\n"
            "A_1.fastq.gz\nA_2.fastq.gz\n"
            "B_1.fastq.gz\nB_2.fastq.gz\n"
            "C_1.fastq.gz\nC_2.fastq.gz\n"
            "D_1.fastq.gz\nD_2.fastq.gz\n"
            "1是control 2是treat\n"
        )
        draft = extract_config_draft(message)
        assert [s["condition"] for s in draft.samples] == [
            "control", "treat", "control", "treat",
        ]

    def test_explicit_assignments_are_not_overwritten(self) -> None:
        """已明确指派的序号保持原样，只补空缺位。"""
        message = (
            "FASTQ 在 /data/reads\n"
            "A_1.fastq.gz\nA_2.fastq.gz\n"
            "B_1.fastq.gz\nB_2.fastq.gz\n"
            "C_1.fastq.gz\nC_2.fastq.gz\n"
            "1是control 3是other\n"
        )
        draft = extract_config_draft(message)
        conditions = [s["condition"] for s in draft.samples]
        assert conditions[0] == "control"
        assert conditions[2] == "other"
        # 空缺位按已指派序列循环补，不会反写已确认的序号。
        assert conditions[1] == "control"



class TestDraftToPayload:
    """草稿 → 写盘端点所需的 payload。"""

    def test_to_payload_marks_remote_and_carries_samples(self) -> None:
        message = (
            "FASTQ 在 /data/reads\n"
            "SRR1_1.fastq.gz\nSRR1_2.fastq.gz\n"
            "SRR2_1.fastq.gz\nSRR2_2.fastq.gz\n"
            "分组 ctrl, treat\n"
        )
        draft = extract_config_draft(message)
        payload = draft.to_payload()
        assert payload["data_source"] == "remote_path"
        assert payload["remote_fastq_dir"] == "/data/reads"
        assert len(payload["samples"]) == 2
        assert payload["strandedness"] == "unknown"

    def test_ready_is_false_without_dir_or_samples(self) -> None:
        draft = extract_config_draft("你好，帮我看看数据")
        assert draft.ready is False
        assert draft.blockers

    def test_ready_is_true_for_a_complete_request(self) -> None:
        message = (
            "FASTQ 在 /data/reads\n"
            "SRR1_1.fastq.gz\nSRR1_2.fastq.gz\n"
            "SRR2_1.fastq.gz\nSRR2_2.fastq.gz\n"
            "分组 ctrl, treat\n"
        )
        draft = extract_config_draft(message)
        assert draft.ready is True
        assert draft.blockers == []


class TestStrandednessDownstream:
    """链特异性「未知」不能静默当成无链特异性。"""

    def test_unknown_maps_to_auto_for_featurecounts(self) -> None:
        from rnaseq_agent.pipeline import _featurecounts_strand

        # featureCounts 只接受 0/1/2；unknown 必须先落到安全默认值。
        assert _featurecounts_strand("unknown") == 0

    def test_pipeline_notes_unknown_strandedness_in_plan(self, tmp_path) -> None:
        """计划里必须如实说明链特异性未知，而不是只写 -s 0。"""
        from rnaseq_agent.pipeline import render_remote_pipeline_script

        config = _minimal_config(tmp_path, strandedness="unknown")
        script = render_remote_pipeline_script(config)
        assert "-s 0" in script
        # 脚本需带注释说明这是「未知 → 默认无链特异性」，不可静默。
        assert "unknown" in script.lower() or "未知" in script


def _minimal_config(tmp_path, *, strandedness: str = "unknown") -> dict:
    """Smallest config render_pipeline_script accepts (2 samples, paired)."""
    return {
        "schema_version": 1,
        "project": {"id": "p", "title": "t", "owner": "o"},
        "study": {"cancer_type": "pan_cancer", "design": "independent_two_group"},
        "server": {
            "profile": "mvp_local",
            "host": "localhost",
            "user": "u",
            "remote_base_dir": str(tmp_path / "remote"),
            "remote_workdir": str(tmp_path / "remote" / "w"),
            "scheduler": "local",
            "threads": 4,
            "memory_gb": 16,
            "shell": "bash",
            "init_commands": [],
        },
        "reference": {
            "name": "ref",
            "remote_gtf_path": "/ref/a.gtf",
            "remote_genome_fasta_path": "/ref/a.fa",
            "star_index_dir": "/ref/star",
            "rsem_index_prefix": "/ref/rsem",
        },
        "sequencing": {
            "layout": "paired",
            "reads_per_sample_million": 40,
            "strandedness": strandedness,
        },
        "samples": {
            "source": "remote_path",
            "local_data_dir": str(tmp_path / "fastq"),
            "remote_data_dir": "/data/reads",
            "remote_prestaged": True,
            "items": [
                {"sample_id": "a", "condition": "ctrl", "fastq_1": "a_1.fastq.gz", "fastq_2": "a_2.fastq.gz"},
                {"sample_id": "b", "condition": "treat", "fastq_1": "b_1.fastq.gz", "fastq_2": "b_2.fastq.gz"},
            ],
        },
        "pipeline": {
            "fastp": {"enabled": True, "version": "0.24.1"},
            "star": {"enabled": True, "version": "2.7.11b"},
            "arriba": {"enabled": True, "version": "2.5.0"},
            "featurecounts": {"enabled": True, "version": "Subread 2.1.1"},
            "rsem": {"enabled": True, "version": "1.28"},
        },
        "polling": {"interval_seconds": 300, "timeout_hours": 24},
        "notification": {"email_enabled": False},
    }
