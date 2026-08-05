#!/usr/bin/env python3
"""
Generate a high-resolution RNA-seq pipeline flowchart for PPT.
"""
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
import numpy as np

# Setup high-res figure
fig, ax = plt.subplots(1, 1, figsize=(16, 10), dpi=300)
ax.set_xlim(0, 16)
ax.set_ylim(0, 10)
ax.axis('off')

# Color scheme (professional blue/teal)
colors = {
    'local': '#E8F4FD',
    'local_border': '#1976D2',
    'qc': '#FFF3E0',
    'qc_border': '#F57C00',
    'align': '#E8F5E9',
    'align_border': '#388E3C',
    'fusion': '#F3E5F5',
    'fusion_border': '#7B1FA2',
    'count': '#E0F2F1',
    'count_border': '#00796B',
    'quant': '#E8EAF6',
    'quant_border': '#303F9F',
    'output': '#FFEBEE',
    'output_border': '#C62828',
    'arrow': '#546E7A',
    'title': '#1565C0',
}

def draw_box(ax, x, y, w, h, text, facecolor, edgecolor, fontsize=9, bold=False):
    box = FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.05,rounding_size=0.15",
                          facecolor=facecolor, edgecolor=edgecolor, linewidth=2)
    ax.add_patch(box)
    weight = 'bold' if bold else 'normal'
    ax.text(x + w/2, y + h/2, text, ha='center', va='center', fontsize=fontsize,
            weight=weight, color='#212121', wrap=True)

def draw_arrow(ax, x1, y1, x2, y2, color='#546E7A'):
    ax.annotate('', xy=(x2, y2), xytext=(x1, y1),
                arrowprops=dict(arrowstyle='->', color=color, lw=1.8,
                                connectionstyle='arc3,rad=0'))

# Title
ax.text(8, 9.6, 'RNA-seq Agent 分析流程', ha='center', va='center', fontsize=22,
        weight='bold', color=colors['title'])
ax.text(8, 9.2, '从本地 FASTQ 到差异表达结果的端到端自动化', ha='center', va='center',
        fontsize=12, color='#757575')

# Row 1: Local data
draw_box(ax, 0.3, 7.5, 2.4, 1.2, '本地 FASTQ\n(双端/单端)', colors['local'], colors['local_border'], 10, True)
draw_arrow(ax, 2.7, 8.1, 3.5, 8.1)

# Row 1: Upload
draw_box(ax, 3.5, 7.7, 1.8, 0.8, '上传至服务器\nraw/', '#FFFDE7', '#FBC02D', 9, True)
draw_arrow(ax, 5.3, 8.1, 6.2, 8.1)

# Row 1: Validation badge
ax.text(1.5, 7.3, '✓ 本地校验：gzip完整性 / FASTQ结构 / R1-R2配对', ha='center', va='center',
        fontsize=8, color='#388E3C', style='italic')

# Row 2: Pipeline steps (main horizontal flow)
# fastp
draw_box(ax, 6.2, 7.5, 2.0, 1.2, 'fastp 0.24.1\n质控 & 过滤', colors['qc'], colors['qc_border'], 9, True)
draw_arrow(ax, 8.2, 8.1, 9.2, 8.1)

# STAR
draw_box(ax, 9.2, 7.5, 2.0, 1.2, 'STAR 2.7.11b\n基因组比对', colors['align'], colors['align_border'], 9, True)
draw_arrow(ax, 11.2, 8.1, 12.2, 8.1)

# Outputs from STAR
ax.text(10.2, 7.3, 'Sorted BAM | Transcriptome BAM | Chimeric | GeneCounts', ha='center', va='center',
        fontsize=7.5, color='#616161')

# Branching from STAR outputs
# Branch 1: Arriba (fusion)
draw_arrow(ax, 10.2, 7.5, 10.2, 6.2)
draw_box(ax, 9.2, 5.0, 2.0, 1.2, 'Arriba 2.5.0\n融合基因检测', colors['fusion'], colors['fusion_border'], 9, True)
draw_arrow(ax, 10.2, 5.0, 10.2, 4.2)
ax.text(10.2, 3.9, 'fusions.tsv', ha='center', va='center', fontsize=8, color=colors['fusion_border'], weight='bold')

# Branch 2: featureCounts
draw_arrow(ax, 11.2, 7.8, 12.5, 7.8)
draw_arrow(ax, 12.5, 7.8, 12.5, 6.2)
draw_box(ax, 11.5, 5.0, 2.0, 1.2, 'featureCounts 2.1.1\n基因表达计数', colors['count'], colors['count_border'], 9, True)
draw_arrow(ax, 12.5, 5.0, 12.5, 4.2)
ax.text(12.5, 3.9, 'gene_counts.txt', ha='center', va='center', fontsize=8, color=colors['count_border'], weight='bold')

# Branch 3: RSEM
draw_arrow(ax, 11.2, 8.4, 13.5, 8.4)
draw_arrow(ax, 13.5, 8.4, 13.5, 6.2)
draw_box(ax, 12.5, 5.0, 2.0, 1.2, 'RSEM 1.2.28\n转录本/基因定量', colors['quant'], colors['quant_border'], 9, True)
draw_arrow(ax, 13.5, 5.0, 13.5, 4.2)
ax.text(13.5, 3.9, '*.genes.results\n*.isoforms.results', ha='center', va='center', fontsize=7.5, color=colors['quant_border'], weight='bold')

# Result bundle
draw_box(ax, 10.2, 2.0, 3.4, 1.2, '结果打包 & 下载\ndownloads_bundle.tar.gz', colors['output'], colors['output_border'], 9, True)

# Arrows to bundle
draw_arrow(ax, 10.2, 3.7, 10.8, 3.2)
draw_arrow(ax, 12.5, 3.7, 12.0, 3.2)
draw_arrow(ax, 13.5, 3.7, 12.8, 3.2)

# Local extraction
ax.text(11.9, 1.6, '↓ 自动解压至 runs/<project>/downloads/extracted/', ha='center', va='center',
        fontsize=8, color=colors['output_border'])

# Bottom: HPC submission info
ax.text(8, 0.7, '调度器支持：Slurm (sbatch)  |  PBS (qsub)  |  Local (nohup)',
        ha='center', va='center', fontsize=10, color='#455A64', weight='bold')

# Side annotation: Agent orchestration
ax.text(0.3, 5.5, 'Agent\n编排', ha='left', va='center', fontsize=11, color='#1565C0', weight='bold',
        rotation=90)
ax.annotate('', xy=(0.8, 7.5), xytext=(0.8, 3.5),
            arrowprops=dict(arrowstyle='<->', color='#1565C0', lw=2))

# Right side: QC outputs
ax.text(14.8, 8.1, 'QC报告:\nfastp HTML/JSON\nSTAR logs', ha='center', va='center',
        fontsize=8, color='#757575', style='italic',
        bbox=dict(boxstyle='round,pad=0.3', facecolor='#FAFAFA', edgecolor='#BDBDBD'))

plt.tight_layout()
fig.savefig('D:/文件/研究生/Agent/SysuccRNAseqAgent-master/runs/rnaseq_pipeline_flowchart.png',
            bbox_inches='tight', dpi=300, facecolor='white', edgecolor='none')
plt.close()
print("Flowchart saved successfully.")
