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
    "rsem": {"enabled": True, "version": "1.2.28"},
}

DEFAULT_CONTAINER = {
    "enabled": True,
    "engine": "apptainer",
    "image_path": "/data/containers/rnaseq-agent-star-rsem.sif",
    "bind_paths": [],
}
