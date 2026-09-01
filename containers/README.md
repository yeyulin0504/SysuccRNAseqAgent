# Apptainer RNA-seq Runtime

This directory defines the controlled RNA-seq runtime used by the agent on
shared HPC systems.

## Build

Build the image on an authorized machine with Apptainer:

```bash
apptainer build rnaseq-agent-star-rsem.sif rnaseq-agent-star-rsem.def
```

If the HPC has no internet access, build the image on an approved networked
Linux machine, verify it, then transfer only the `.sif` image to the HPC.

## Verify

```bash
apptainer test rnaseq-agent-star-rsem.sif
apptainer exec rnaseq-agent-star-rsem.sif fastp --version
apptainer exec rnaseq-agent-star-rsem.sif STAR --version
apptainer exec rnaseq-agent-star-rsem.sif featureCounts -v
apptainer exec rnaseq-agent-star-rsem.sif rsem-calculate-expression --version
```

## Deploy

Place the image in a shared read-only location, for example:

```bash
/data/containers/rnaseq-agent-star-rsem.sif
```

Then set the project configuration:

```json
{
  "container": {
    "enabled": true,
    "engine": "apptainer",
    "image_path": "/data/containers/rnaseq-agent-star-rsem.sif",
    "bind_paths": []
  }
}
```

The agent runs workflow tools through `apptainer exec`, so ordinary users do
not need to know where `fastp`, `STAR`, `featureCounts`, or `RSEM` are installed.
