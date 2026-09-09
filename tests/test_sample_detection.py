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


from rnaseq_agent.sample_detection import preview_expression_matrix


def test_preview_expression_matrix_detects_raw_counts() -> None:
    content = b"gene\tCTRL_1\tCTRL_2\tCASE_1\nENSG1\t1\t2\t3\nENSG2\t0\t4\t9\n"

    preview = preview_expression_matrix(content, filename="counts.tsv")

    assert preview["ok"] is True
    assert preview["matrix_type"] == "raw_counts"
    assert preview["samples"] == ["CTRL_1", "CTRL_2", "CASE_1"]
    assert preview["can_run_deseq2"] is True


def test_preview_expression_matrix_detects_normalized_expression() -> None:
    content = b"gene\tA\tB\nGene1\t6.23\t7.81\nGene2\t4.01\t3.98\n"

    preview = preview_expression_matrix(content, filename="expr.tsv")

    assert preview["ok"] is True
    assert preview["matrix_type"] == "normalized_expression"
    assert preview["can_run_deseq2"] is False
    assert "不是 raw counts" in " ".join(preview["warnings"])


def test_preview_expression_matrix_detects_geo_series_matrix_like() -> None:
    content = (
        b"!Series_title\tExample\n"
        b"!series_matrix_table_begin\n"
        b"ID_REF\tGSM1\tGSM2\n"
        b"1007_s_at\t5.1\t6.2\n"
        b"1053_at\t7.0\t8.4\n"
        b"!series_matrix_table_end\n"
    )

    preview = preview_expression_matrix(content, filename="GSE_series_matrix.txt")

    assert preview["ok"] is True
    assert preview["matrix_type"] == "geo_series_matrix_like"
    assert preview["gene_id_column"] == "ID_REF"
    assert preview["samples"] == ["GSM1", "GSM2"]
    assert preview["can_run_deseq2"] is False


def test_read_counts_samples_drops_empty_middle_header_columns() -> None:
    content = b"gene\tCTRL_1\t\tCASE_1\nENSG1\t1\t2\t3\n"

    samples = read_counts_samples(content)

    assert "" not in samples
    assert samples == ["CTRL_1", "CASE_1"]


def test_read_counts_samples_header_only_matrix_still_returns_sample_names() -> None:
    content = b"gene\tCTRL_1\tCTRL_2\tCASE_1\n"

    samples = read_counts_samples(content)

    assert samples == ["CTRL_1", "CTRL_2", "CASE_1"]
