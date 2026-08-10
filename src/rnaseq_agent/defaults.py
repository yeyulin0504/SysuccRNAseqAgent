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

DEFAULT_PIPELINE = {
    "fastp": {"enabled": True, "version": "0.24.1"},
    "star": {"enabled": True, "version": "2.7.11b"},
    "arriba": {"enabled": True, "version": "2.5.0"},
    "featurecounts": {"enabled": True, "version": "Subread 2.1.1"},
    "rsem": {"enabled": False, "version": "1.2.28"},
}

DEFAULT_DOWNSTREAM = {
    "enabled": False,
    "profile_id": "bulk_rnaseq_deseq2_v1",
    "source_mode": "pipeline_featurecounts",
    "input_root": "",
    "input": {
        "kind": "featurecounts_raw_counts",
        "path": "featurecounts/gene_counts.txt",
        "source_filename": "",
        "source_path": "",
    },
    "metadata_input": {"source_filename": "", "source_path": ""},
    "metadata": {"samples": []},
    "design": {"condition_column": "condition", "batch_column": "", "formula": "~ condition"},
    "contrasts": [],
    "filtering": {"min_count": 10, "min_samples": 2},
    "differential_expression": {"padj_threshold": 0.05, "abs_log2_fold_change": 1.0},
    "enrichment": {
        "enabled": True,
        "go_ora": True,
        "kegg_ora": True,
        "gsea": True,
        "id_type": "ENSEMBL",
        "organism": "",
        "gmt": {"enabled": False, "source_filename": "", "source_path": "", "sha256": ""},
    },
    "runtime": {
        "environment_kind": "apptainer",
        "image_path": "",
        "image_sha256": "",
        "rscript_path": "Rscript",
    },
}


DEFAULT_CONTAINER = {
    "enabled": True,
    "engine": "apptainer",
    "image_path": "/data/containers/rnaseq-agent-star-rsem.sif",
    "bind_paths": [],
}
