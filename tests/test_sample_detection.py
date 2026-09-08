from rnaseq_agent.sample_detection import detect_fastq_pairs, read_counts_samples


def test_detects_common_paired_fastq_names() -> None:
    result = detect_fastq_pairs([
        "/data/tumor_R1.fastq.gz", "/data/tumor_R2.fastq.gz",
        "/data/normal_1.fq.gz", "/data/normal_2.fq.gz",
    ])
    assert [row["sample_id"] for row in result["samples"]] == ["normal", "tumor"]
    assert result["unmatched"] == []


def test_reports_unmatched_fastq() -> None:
    result = detect_fastq_pairs(["/data/lone_R1.fastq.gz"])
    assert result["samples"] == []
    assert result["unmatched"] == ["/data/lone_R1.fastq.gz"]


def test_reads_counts_header_without_gene_column() -> None:
    content = b"gene_id\tCTRL_1\tCTRL_2\tCASE_1\nENSG1\t1\t2\t3\n"
    assert read_counts_samples(content) == ["CTRL_1", "CTRL_2", "CASE_1"]
