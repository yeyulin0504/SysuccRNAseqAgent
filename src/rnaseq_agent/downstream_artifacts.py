from __future__ import annotations

import hashlib
import json
from typing import Any

from .downstream import normalized_downstream_config
from .shell import shell_quote


DOWNSTREAM_CONFIG_FILENAME = "downstream_config.json"
DOWNSTREAM_SCRIPT_FILENAME = "run_downstream.R"


def render_downstream_config(config: dict[str, Any]) -> str:
    rendered = normalized_downstream_config(config)
    if rendered.get("source_mode") == "standalone_count_matrix":
        rendered["input"] = {
            "kind": "standalone_count_matrix_tsv_v1",
            "path": "inputs/counts.tsv",
        }
        rendered["metadata_input"] = {"path": "inputs/metadata.tsv"}
    gmt = rendered.get("enrichment", {}).get("gmt", {})
    if gmt.get("enabled", False):
        gmt["source_path"] = f"scripts/{gmt['source_filename']}"
    return json.dumps(
        rendered,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def downstream_script_artifacts(config: dict[str, Any]) -> dict[str, str]:
    if not config.get("downstream", {}).get("enabled", False):
        return {}
    return {
        DOWNSTREAM_CONFIG_FILENAME: render_downstream_config(config),
        DOWNSTREAM_SCRIPT_FILENAME: render_downstream_r_script(),
    }


def downstream_gmt_source(config: dict[str, Any]) -> tuple[str, str] | None:
    gmt = config.get("downstream", {}).get("enrichment", {}).get("gmt", {})
    if not gmt.get("enabled", False):
        return None
    return str(gmt["source_path"]), str(gmt["source_filename"])


def downstream_script_hashes(config: dict[str, Any]) -> dict[str, str]:
    return {
        name: hashlib.sha256(content.encode("utf-8")).hexdigest()
        for name, content in downstream_script_artifacts(config).items()
    }


def render_downstream_command(config: dict[str, Any]) -> str:
    runtime = config["downstream"]["runtime"]
    image = shell_quote(runtime["image_path"])
    return (
        f"apptainer exec {image} Rscript {shell_quote('scripts/' + DOWNSTREAM_SCRIPT_FILENAME)} "
        f"--config {shell_quote('scripts/' + DOWNSTREAM_CONFIG_FILENAME)}"
    )


def render_downstream_r_script() -> str:
    return r'''#!/usr/bin/env Rscript
args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 2L || args[[1L]] != "--config") {
  stop("Usage: run_downstream.R --config scripts/downstream_config.json")
}

required_packages <- c(
  "jsonlite", "DESeq2", "ggplot2", "pheatmap", "clusterProfiler",
  "enrichplot", "DOSE", "AnnotationDbi", "org.Hs.eg.db", "org.Mm.eg.db", "matrixStats"
)
missing_packages <- required_packages[!vapply(required_packages, requireNamespace, logical(1), quietly = TRUE)]
if (length(missing_packages)) {
  stop("Required downstream R packages are unavailable: ", paste(missing_packages, collapse = ", "))
}

config <- jsonlite::fromJSON(args[[2L]], simplifyVector = FALSE)
dir.create("downstream", recursive = TRUE, showWarnings = FALSE)
dir.create("downstream/qc", recursive = TRUE, showWarnings = FALSE)
dir.create("downstream/de", recursive = TRUE, showWarnings = FALSE)
dir.create("downstream/enrichment", recursive = TRUE, showWarnings = FALSE)

if (identical(config$source_mode, "standalone_count_matrix")) {
  metadata_path <- config$metadata_input$path
  if (!file.exists(metadata_path)) stop("Standalone metadata file not found: ", metadata_path)
  metadata <- read.delim(metadata_path, check.names = FALSE, stringsAsFactors = FALSE)
  expected_metadata_columns <- c("sample_id", "condition", if (identical(config$design$formula, "~ batch + condition")) "batch" else character())
  if (!setequal(names(metadata), expected_metadata_columns)) stop("Standalone metadata columns do not match the configured design.")
  metadata <- metadata[, expected_metadata_columns, drop = FALSE]
} else {
  metadata_rows <- config$metadata$samples
  metadata <- data.frame(
    sample_id = vapply(metadata_rows, `[[`, character(1), "sample_id"),
    condition = vapply(metadata_rows, `[[`, character(1), "condition"),
    stringsAsFactors = FALSE,
    check.names = FALSE
  )
  if (identical(config$design$formula, "~ batch + condition")) {
    metadata$batch <- vapply(metadata_rows, `[[`, character(1), "batch")
  }
}
rownames(metadata) <- metadata$sample_id
metadata$condition <- factor(metadata$condition)
if ("batch" %in% names(metadata)) metadata$batch <- factor(metadata$batch)

if (identical(config$source_mode, "standalone_count_matrix")) {
  counts_path <- config$input$path
  if (!file.exists(counts_path)) stop("Standalone count matrix not found: ", counts_path)
  counts_raw <- read.delim(counts_path, check.names = FALSE, stringsAsFactors = FALSE)
  if (length(names(counts_raw)) < 3L || !identical(names(counts_raw)[[1L]], "gene_id")) stop("Standalone count matrix header is invalid.")
  count_columns <- names(counts_raw)[-1L]
  if (!setequal(count_columns, metadata$sample_id)) stop("Standalone count columns do not exactly match metadata sample IDs.")
  counts <- as.matrix(counts_raw[, metadata$sample_id, drop = FALSE])
  original_ids <- counts_raw$gene_id
} else {
  counts_path <- config$input$path
  if (!file.exists(counts_path)) stop("featureCounts count file not found: ", counts_path)
  counts_raw <- read.delim(counts_path, comment.char = "#", check.names = FALSE, stringsAsFactors = FALSE)
  required_columns <- c("Geneid", "Chr", "Start", "End", "Strand", "Length")
  if (!all(required_columns %in% names(counts_raw))) stop("Unexpected featureCounts header.")
  count_columns <- setdiff(names(counts_raw), required_columns)
  if (!setequal(count_columns, metadata$sample_id)) stop("featureCounts columns do not exactly match downstream metadata.")
  counts <- as.matrix(counts_raw[, metadata$sample_id, drop = FALSE])
  original_ids <- counts_raw$Geneid
}
storage.mode(counts) <- "numeric"
if (any(!is.finite(counts)) || any(counts < 0) || any(counts != round(counts))) stop("Count matrix must contain nonnegative integer counts.")
normalized_ids <- sub("\\.[0-9]+$", "", original_ids)
duplicate_ids <- normalized_ids[duplicated(normalized_ids)]
if (length(duplicate_ids)) stop("Ensembl ID version normalization introduced duplicate IDs.")
rownames(counts) <- normalized_ids

keep <- rowSums(counts >= config$filtering$min_count) >= config$filtering$min_samples
counts <- counts[keep, , drop = FALSE]
if (!nrow(counts)) stop("Low-expression filtering removed all genes.")
write.csv(data.frame(original_gene_id = original_ids, normalized_gene_id = normalized_ids, kept = keep), "downstream/id_mapping_audit.csv", row.names = FALSE)
write.csv(data.frame(original_gene_count = length(original_ids), retained_gene_count = nrow(counts), duplicate_after_normalization = length(duplicate_ids)), "downstream/count_filter_audit.csv", row.names = FALSE)

formula <- if (identical(config$design$formula, "~ batch + condition")) ~ batch + condition else ~ condition
if (qr(model.matrix(formula, metadata))$rank < ncol(model.matrix(formula, metadata))) stop("Configured design matrix is rank deficient.")
dds <- DESeq2::DESeqDataSetFromMatrix(countData = round(counts), colData = metadata, design = formula)
dds <- DESeq2::DESeq(dds)
vst_counts <- DESeq2::assay(DESeq2::vst(dds, blind = FALSE))
write.csv(vst_counts, "downstream/vst_counts.csv")

pca <- prcomp(t(vst_counts), scale. = FALSE)
pca_table <- data.frame(sample_id = rownames(pca$x), condition = metadata[rownames(pca$x), "condition"], pca$x[, 1:2, drop = FALSE])
write.csv(pca_table, "downstream/qc/pca.csv", row.names = FALSE)
pca_plot <- ggplot2::ggplot(pca_table, ggplot2::aes(x = PC1, y = PC2, color = condition, label = sample_id)) + ggplot2::geom_point(size = 3) + ggplot2::theme_minimal()
ggplot2::ggsave("downstream/qc/pca.png", pca_plot, width = 7, height = 5, dpi = 160)
correlation <- cor(vst_counts, method = "spearman")
write.csv(correlation, "downstream/qc/sample_spearman_correlation.csv")
png("downstream/qc/sample_spearman_correlation.png", width = 1200, height = 1000, res = 160)
pheatmap::pheatmap(correlation)
dev.off()
variable_genes <- head(order(matrixStats::rowVars(vst_counts), decreasing = TRUE), min(50L, nrow(vst_counts)))
png("downstream/qc/variable_gene_heatmap.png", width = 1200, height = 1000, res = 160)
pheatmap::pheatmap(vst_counts[variable_genes, , drop = FALSE], scale = "row")
dev.off()

annotation_db <- if (identical(config$enrichment$enabled, TRUE) && identical(config$enrichment$id_type, "ENSEMBL")) {
  if (identical(config$enrichment$organism, "mouse")) org.Mm.eg.db::org.Mm.eg.db else org.Hs.eg.db::org.Hs.eg.db
} else NULL
map_ensembl_to_entrez <- function(gene_ids) {
  mapping <- AnnotationDbi::select(
    annotation_db,
    keys = unique(gene_ids),
    keytype = "ENSEMBL",
    columns = "ENTREZID"
  )
  mapping <- mapping[!is.na(mapping$ENTREZID) & nzchar(mapping$ENTREZID), , drop = FALSE]
  mapping <- mapping[!duplicated(mapping$ENSEMBL), , drop = FALSE]
  mapping
}
write_enrichment_table <- function(result, path) {
  table <- if (is.null(result)) data.frame() else as.data.frame(result)
  write.csv(table, path, row.names = FALSE)
  nrow(table)
}
pathway_term2gene <- function() {
  pathway_map <- if (identical(config$enrichment$organism, "mouse")) org.Mm.eg.db::org.Mm.egPATH2EG else org.Hs.eg.db::org.Hs.egPATH2EG
  mapping <- AnnotationDbi::toTable(pathway_map)
  names(mapping) <- c("term", "gene")
  mapping
}
gmt_term2gene <- function() {
  gmt <- config$enrichment$gmt
  if (!identical(gmt$enabled, TRUE)) return(NULL)
  if (!file.exists(gmt$source_path)) stop("Configured GMT file is unavailable: ", gmt$source_path)
  clusterProfiler::read.gmt(gmt$source_path)
}
write_enrichment_plot <- function(result, path) {
  if (is.null(result) || !nrow(as.data.frame(result))) return(FALSE)
  ggplot2::ggsave(path, enrichplot::dotplot(result, showCategory = min(20L, nrow(as.data.frame(result)))), width = 8, height = 6, dpi = 160)
  TRUE
}
summary <- list(profile_id = config$profile_id, design = config$design$formula, contrasts = list(), filtering = config$filtering, runtime = config$runtime, package_versions = vapply(required_packages, function(pkg) as.character(utils::packageVersion(pkg)), character(1)))
for (contrast in config$contrasts) {
  result <- DESeq2::results(dds, contrast = c("condition", contrast$numerator, contrast$denominator))
  result_table <- data.frame(gene_id = rownames(result), as.data.frame(result), check.names = FALSE)
  prefix <- file.path("downstream/de", contrast$id)
  write.csv(result_table, paste0(prefix, "_results.csv"), row.names = FALSE)
  significant <- subset(result_table, !is.na(padj) & padj <= config$differential_expression$padj_threshold & abs(log2FoldChange) >= config$differential_expression$abs_log2_fold_change)
  write.csv(significant, paste0(prefix, "_deg.csv"), row.names = FALSE)
  png(paste0(prefix, "_ma.png"), width = 1200, height = 900, res = 160)
  DESeq2::plotMA(result, ylim = c(-5, 5))
  dev.off()
  volcano <- ggplot2::ggplot(result_table, ggplot2::aes(x = log2FoldChange, y = -log10(pmax(padj, .Machine$double.xmin)))) + ggplot2::geom_point(alpha = 0.4) + ggplot2::theme_minimal()
  ggplot2::ggsave(paste0(prefix, "_volcano.png"), volcano, width = 7, height = 5, dpi = 160)
  enrichment_summary <- list()
  if (identical(config$enrichment$enabled, TRUE)) {
    mapping <- map_ensembl_to_entrez(result_table$gene_id)
    write.csv(mapping, paste0(prefix, "_ensembl_entrez_mapping.csv"), row.names = FALSE)
    significant_entrez <- unique(mapping$ENTREZID[match(significant$gene_id, mapping$ENSEMBL)])
    background_entrez <- unique(mapping$ENTREZID)
    if (identical(config$enrichment$go_ora, TRUE)) {
      go <- if (length(significant_entrez)) clusterProfiler::enrichGO(gene = significant_entrez, universe = background_entrez, OrgDb = annotation_db, keyType = "ENTREZID", ont = "ALL", pAdjustMethod = "BH", readable = FALSE) else NULL
      go_path <- paste0("downstream/enrichment/", contrast$id, "_go_ora.csv")
      enrichment_summary$go_ora <- list(table = go_path, term_count = write_enrichment_table(go, go_path), plot = write_enrichment_plot(go, paste0("downstream/enrichment/", contrast$id, "_go_ora.png")))
    }
    if (identical(config$enrichment$kegg_ora, TRUE)) {
      kegg <- if (length(significant_entrez)) clusterProfiler::enricher(gene = significant_entrez, universe = background_entrez, TERM2GENE = pathway_term2gene(), pAdjustMethod = "BH") else NULL
      kegg_path <- paste0("downstream/enrichment/", contrast$id, "_kegg_ora.csv")
      enrichment_summary$kegg_ora <- list(table = kegg_path, term_count = write_enrichment_table(kegg, kegg_path), plot = write_enrichment_plot(kegg, paste0("downstream/enrichment/", contrast$id, "_kegg_ora.png")))
    }
    if (identical(config$enrichment$gsea, TRUE)) {
      ranked <- result_table$stat
      names(ranked) <- mapping$ENTREZID[match(result_table$gene_id, mapping$ENSEMBL)]
      ranked <- ranked[is.finite(ranked) & !is.na(names(ranked)) & nzchar(names(ranked))]
      ranked <- sort(tapply(ranked, names(ranked), max), decreasing = TRUE)
      gsea <- if (length(ranked) >= 10L) clusterProfiler::GSEA(geneList = ranked, TERM2GENE = pathway_term2gene(), pAdjustMethod = "BH", verbose = FALSE) else NULL
      gsea_path <- paste0("downstream/enrichment/", contrast$id, "_kegg_gsea.csv")
      enrichment_summary$kegg_gsea <- list(table = gsea_path, term_count = write_enrichment_table(gsea, gsea_path), plot = write_enrichment_plot(gsea, paste0("downstream/enrichment/", contrast$id, "_kegg_gsea.png")))
      term2gene <- gmt_term2gene()
      if (!is.null(term2gene)) {
        gmt_gsea <- if (length(ranked) >= 10L) clusterProfiler::GSEA(geneList = ranked, TERM2GENE = term2gene, pAdjustMethod = "BH", verbose = FALSE) else NULL
        gmt_path <- paste0("downstream/enrichment/", contrast$id, "_gmt_gsea.csv")
        enrichment_summary$gmt_gsea <- list(table = gmt_path, term_count = write_enrichment_table(gmt_gsea, gmt_path), plot = write_enrichment_plot(gmt_gsea, paste0("downstream/enrichment/", contrast$id, "_gmt_gsea.png")))
      }
    }
  }
  summary$contrasts[[contrast$id]] <- list(result_table = paste0(prefix, "_results.csv"), deg_table = paste0(prefix, "_deg.csv"), significant_gene_count = nrow(significant), enrichment = enrichment_summary)
}
writeLines(capture.output(sessionInfo()), "downstream/session_info.txt")
jsonlite::write_json(summary, "downstream/summary.json", pretty = TRUE, auto_unbox = TRUE)
'''
