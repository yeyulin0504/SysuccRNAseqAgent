# Paired two-group DESeq2 capability and airway evaluation design

## Purpose

Add one controlled paired bulk RNA-seq design to the product and the real
`bkbio-eval` Adapter so that paired datasets such as Bioconductor `airway` can
be evaluated without opening arbitrary R formulas or silently treating paired
observations as independent samples.

This design covers capability and evaluator contracts. It does not promote the
airway case to a numerical L1 until provenance/licence/batch evidence and a
fixed R/DESeq2 runtime are available.

## Scope and non-goals

The existing `independent_two_group` route remains unchanged and continues to
generate `~ condition`. The new route is named `paired_two_group` and always
generates `~ pair_id + condition`. Formula strings, arbitrary covariates,
arbitrary pair-column paths, and silent fallback to `~ condition` are outside
the capability.

The first paired release freezes `min_count_prefilter=0` and requires no
declared batch field. A non-zero prefilter or a declared batch is a structured
`NOT_EVALUABLE / unsupported_design` result until another named policy is
designed and reviewed.

Pair identifiers are sensitive project metadata. They may be entered through
the structured local workbench and retained in the local contract, but they
are not added to the default provider summary, History projection, generic
tool log, checkpoint, or the existing exact disclosure scopes. The only
currently enabled exact scope remains local `sample_ids`.

## Canonical design descriptor

All planning, rendering, contract hashing, result provenance, and Adapter
verification consume one server-generated descriptor:

```json
{
  "template": "deseq2_paired_two_group",
  "formula": "~ pair_id + condition",
  "reference_condition": "untrt",
  "contrast_condition": "trt",
  "contrast": "trt_vs_untrt",
  "pair_count": 4,
  "pair_mapping": [
    {
      "pair_id": "N052611",
      "reference_sample_id": "GSM1275866",
      "contrast_sample_id": "GSM1275867"
    }
  ],
  "min_complete_pairs": 3,
  "min_count_prefilter": 0
}
```

The mapping is canonicalized by pair identifier and then by the frozen
reference/contrast order. The descriptor is derived from validated sample
metadata; callers cannot provide a formula or replace the mapping after
validation.

## Validation and scientific gate

The gate fails closed before contract creation, credential installation,
transport, or remote scheduling when any rule fails:

1. `study.design` is exactly `paired_two_group` and differential expression is
   enabled.
2. There are exactly two non-empty condition levels. Reference and contrast
   are distinct and cover both levels.
3. Every sample has a non-empty canonical `pair_id`; sample identifiers are
   globally unique.
4. Every pair has exactly one sample for each condition. Missing mates,
   duplicate pair-condition rows, extra rows, and uneven pairs are rejected.
5. At least three complete pairs are present.
6. The generated model matrix is full rank. The R stage repeats this check
   immediately before constructing the DESeq2 dataset.
7. `min_count_prefilter` is exactly zero.
8. A declared batch field is rejected until a named paired batch policy exists.

The R renderer creates factors with deterministic levels, checks
`qr(model.matrix(...))$rank == ncol(model.matrix(...))`, and uses only the
server-generated formula. It records template, formula, pair count, matrix
rank/columns, reference, contrast, and prefilter semantics in its summary.

## Main-project integration

The capability layer and differential-design helpers add a template dispatcher
with independent and paired branches. Sample validation gains canonical
`pair_id` checks. The workbench gains a paired selector and pair-ID field for
structured local entry. Any future LLM editing tool must use bounded pair-ID
fields and the existing confirmation/approval machinery; this release does
not add a model tool that reads or invents pair mappings.

Analysis Contract fingerprints include the template, formula, canonical pair
mapping, reference/contrast, and prefilter rule. For counts execution, the
contract freezes the generated R script, generated `colData.tsv`, counts-stage
script, and submit script (or one deterministic equivalent inventory). Any
post-confirm drift fails before transport.

## Adapter integration

The Adapter first validates scalar parameters, then reads the declared pair
column while preserving it long enough to canonicalize it to `pair_id`. It
performs all pair and model preflight before creating the evaluation project,
installing credentials, probing a runtime, or contacting SSH. Refusal keeps the
strict five-field `analyzer-result@1` shape and exits zero.

The future airway numerical manifest uses:

```yaml
design_template: paired_two_group
paired: true
pair_column: cell
reference_level: untrt
contrast_level: trt
min_count_prefilter: 0
```

The current airway manifest keeps its legacy `~ cell + condition` request as an
expected refusal until this capability, metadata audit, fixed runtime, and
reviewed expected values are all complete.

## LLM and disclosure boundary

Default model context may expose only pairing presence, pair count, and gate
status. It must not contain raw pair IDs, source `cell`/`patient` values,
mapping rows, filenames, or paths. No new exact data scope is created by this
capability. A user may enter pair IDs through the local structured UI; model
assistance for pair mapping requires a separate disclosure and tool-policy
design.

## Test and release matrix

RED tests precede production changes and cover:

- valid three-pair descriptor and deterministic `~ pair_id + condition` render;
- empty/missing pair IDs, duplicate pair-condition, missing mate, extra row,
  fewer than three pairs, invalid reference, arbitrary formula, declared batch,
  non-zero prefilter, and rank deficiency;
- contract-ID changes for pair mapping/reference/contrast/prefilter drift;
- frozen counts-stage script and colData inventory plus post-confirm drift;
- Adapter refusal before project creation or transport, source-column
  canonicalization, summary-template mismatch, and no independent downgrade;
- structured UI persistence and server-side validation;
- safe model projection with no pair identifiers in context, grants,
  checkpoints, History, or generic logs.

Airway numerical promotion additionally requires a fixed immutable R 4.3.3 /
Bioconductor 3.18 runtime (or equivalent digest), source retrieval and licence
review, export-script commit, input hashes, batch/confounding assessment,
deterministic reference output, and a human-reviewed versioned baseline.

