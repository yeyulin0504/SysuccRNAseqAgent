FROM rocker/r-ver:4.3.3

LABEL org.opencontainers.image.title="SYSU RNA-seq downstream analysis"
LABEL org.opencontainers.image.version="2026.09-bioc3.18"

RUN apt-get update && apt-get install -y --no-install-recommends \
      libcurl4-openssl-dev libssl-dev libxml2-dev libgit2-dev \
    && rm -rf /var/lib/apt/lists/*

RUN R -q -e "install.packages('BiocManager', repos='https://cloud.r-project.org'); BiocManager::install(version='3.18', ask=FALSE); BiocManager::install(c('DESeq2','CMScaller'), ask=FALSE, update=FALSE)"

COPY verify_packages.R /opt/rnaseq-agent/verify_packages.R
RUN Rscript /opt/rnaseq-agent/verify_packages.R

ENTRYPOINT ["Rscript"]
