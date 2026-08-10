from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import re
from typing import Any


@dataclass(frozen=True)
class ReferenceCatalogEntry:
    catalog_id: str
    species: str
    assembly: str
    annotation_release: str
    provider: str
    gtf_url: str
    genome_fasta_url: str
    remote_ref_dir: str
    gtf_filename: str
    genome_fasta_filename: str
    star_subdir: str
    rsem_subdir: str
    rsem_basename: str

    @property
    def manifest_sha256(self) -> str:
        values = asdict(self)
        canonical = "\n".join(f"{key}={values[key]}" for key in sorted(values))
        return sha256(canonical.encode("utf-8")).hexdigest()

    def reference_fields(self) -> dict[str, str]:
        return {
            "name": self.catalog_id,
            "species": self.species,
            "release": self.annotation_release,
            "assembly": self.assembly,
            "provider": self.provider,
            "catalog_id": self.catalog_id,
            "catalog_manifest_sha256": self.manifest_sha256,
            "gtf_url": self.gtf_url,
            "genome_fasta_url": self.genome_fasta_url,
            "index_state": "unconfigured",
        }


CATALOG: dict[str, ReferenceCatalogEntry] = {
    "GENCODE_R47_GRCh38p14_ALL": ReferenceCatalogEntry(
        catalog_id="GENCODE_R47_GRCh38p14_ALL",
        species="human",
        assembly="GRCh38.p14",
        annotation_release="GENCODE v47",
        provider="GENCODE",
        gtf_url="https://ftp.ebi.ac.uk/pub/databases/gencode/Gencode_human/release_47/gencode.v47.chr_patch_hapl_scaff.annotation.gtf.gz",
        genome_fasta_url="https://ftp.ebi.ac.uk/pub/databases/gencode/Gencode_human/release_47/GRCh38.p14.genome.fa.gz",
        remote_ref_dir="/data/ref/gencode/human/release_47_all",
        gtf_filename="gencode.v47.chr_patch_hapl_scaff.annotation.gtf.gz",
        genome_fasta_filename="GRCh38.p14.genome.fa.gz",
        star_subdir="star_2.7.11b",
        rsem_subdir="rsem",
        rsem_basename="rsem_gencode_v47",
    ),
    "GENCODE_M35_GRCm39_PRIMARY": ReferenceCatalogEntry(
        catalog_id="GENCODE_M35_GRCm39_PRIMARY",
        species="mouse",
        assembly="GRCm39",
        annotation_release="GENCODE M35",
        provider="GENCODE",
        gtf_url="https://ftp.ebi.ac.uk/pub/databases/gencode/Gencode_mouse/release_M35/gencode.vM35.primary_assembly.annotation.gtf.gz",
        genome_fasta_url="https://ftp.ebi.ac.uk/pub/databases/gencode/Gencode_mouse/release_M35/GRCm39.primary_assembly.genome.fa.gz",
        remote_ref_dir="/data/ref/gencode/mouse/release_M35_primary",
        gtf_filename="gencode.vM35.primary_assembly.annotation.gtf.gz",
        genome_fasta_filename="GRCm39.primary_assembly.genome.fa.gz",
        star_subdir="star_2.7.11b",
        rsem_subdir="rsem",
        rsem_basename="rsem_gencode_M35",
    ),
}


@dataclass(frozen=True)
class ReferenceCandidate:
    catalog_id: str
    confidence: str
    evidence: tuple[str, ...]

    @property
    def entry(self) -> ReferenceCatalogEntry:
        return CATALOG[self.catalog_id]


_SPECIES_PATTERNS = {
    "mouse": (r"\bmouse\b", r"\bmus musculus\b", r"小鼠", r"鼠源", r"mm10", r"mm39", r"grcm38", r"grcm39"),
    "human": (r"\bhuman\b", r"\bhomo sapiens\b", r"人类", r"人源", r"hg38", r"grch38"),
}

_EXPLICIT_SPECIES_ALIASES = {
    "human": "human",
    "homo sapiens": "human",
    "人类": "human",
    "人": "human",
    "mouse": "mouse",
    "mus musculus": "mouse",
    "小鼠": "mouse",
    "鼠": "mouse",
}

_ASSEMBLY_ALIASES = {
    "GRCh38.p14": ("grch38.p14", "grch38", "hg38"),
    "GRCm39": ("grcm39", "mm39"),
}

_UNSUPPORTED_ASSEMBLY_ALIASES = (
    "grcm38",
    "mm10",
)


def catalog_ids_for_species(species: str) -> tuple[str, ...]:
    return tuple(
        entry.catalog_id
        for entry in CATALOG.values()
        if entry.species == species
    )


def normalize_explicit_species(answer: str) -> str | None:
    return _EXPLICIT_SPECIES_ALIASES.get(" ".join(answer.lower().split()))


def candidates_for_species(species: str) -> tuple[ReferenceCandidate, ...]:
    return tuple(
        ReferenceCandidate(entry.catalog_id, "user_confirmed", (species,))
        for entry in CATALOG.values()
        if entry.species == species
    )


def reference_catalog_ids() -> tuple[str, ...]:
    return tuple(CATALOG)


def catalog_entry(catalog_id: str) -> ReferenceCatalogEntry:
    try:
        return CATALOG[catalog_id]
    except KeyError as exc:
        raise ValueError("Unknown approved reference catalog entry.") from exc


def has_unsupported_assembly(metadata: str) -> bool:
    text = metadata.lower()
    return any(alias in text for alias in _UNSUPPORTED_ASSEMBLY_ALIASES)


def infer_reference_candidates(metadata: str) -> tuple[ReferenceCandidate, ...]:
    text = metadata.lower()
    found = {
        species
        for species, patterns in _SPECIES_PATTERNS.items()
        if any(
            re.search(pattern, text, flags=re.IGNORECASE)
            for pattern in patterns
        )
    }
    if len(found) != 1:
        return ()

    species = found.pop()
    if has_unsupported_assembly(metadata):
        return ()
    candidates: list[ReferenceCandidate] = []
    for entry in CATALOG.values():
        if entry.species != species:
            continue
        assembly_aliases = _ASSEMBLY_ALIASES.get(
            entry.assembly,
            (entry.assembly.lower(),),
        )
        matched_aliases = tuple(
            alias for alias in assembly_aliases if alias in text
        )
        release = entry.annotation_release.lower()
        evidence = matched_aliases
        if release in text:
            evidence += (release,)
        candidates.append(
            ReferenceCandidate(
                entry.catalog_id,
                "high" if evidence else "medium",
                evidence or (species,),
            )
        )
    return tuple(candidates)


def catalog_reference_fields(catalog_id: str) -> dict[str, str]:
    return catalog_entry(catalog_id).reference_fields()
