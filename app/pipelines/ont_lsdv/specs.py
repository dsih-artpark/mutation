"""Tool images and per-stage resource declarations.

Images are named by logical key and resolved through the manifest and lock file
(see app/runtime/images.py), so this module contains no image strings and needs
no edit when an image is repinned.

Tool isolation by image is the reason this design works: Medaka and Clair3 have
irreconcilable Python, PyTorch/TensorFlow and htslib requirements, and ARTIC
align_trim pulls a CPython of its own. minimap2, samtools, bcftools and NanoPack
share `core` because the pipeline pipes between them.
"""

from __future__ import annotations

from ...runtime.images import images_for
from ...runtime.spec import StageSpec

img = images_for(__package__)

CORE_IMAGE = img("core")

_HOUR = 3600

# Memory figures are starting estimates for a ~150 kb genome. The run_stages
# table records real peak usage per stage -- after ~20 runs, replace these with
# measured values rather than guesses.
NANOPLOT = StageSpec(image=CORE_IMAGE, cpus=2, memory_gb=4, timeout_s=_HOUR)
FASTP = StageSpec(image=CORE_IMAGE, cpus=2, memory_gb=2, timeout_s=_HOUR)
NANOFILT = StageSpec(image=CORE_IMAGE, cpus=2, memory_gb=2, timeout_s=_HOUR)
ALIGN = StageSpec(image=CORE_IMAGE, cpus=4, memory_gb=6, timeout_s=2 * _HOUR)
SAMTOOLS = StageSpec(image=CORE_IMAGE, cpus=2, memory_gb=2, timeout_s=_HOUR)

# Carries its own samtools at the same pin as `core`, so the align_trim ->
# samtools sort pipe and the index that follows it stay in one container.
ALIGN_TRIM = StageSpec(image=img("align-trim"), cpus=2, memory_gb=4, timeout_s=_HOUR)

BCFTOOLS = StageSpec(image=CORE_IMAGE, cpus=2, memory_gb=4, timeout_s=2 * _HOUR)

# Neural-network callers. CPU-only inference on Lightsail (no GPU available),
# so these dominate wall-clock; budget accordingly.
MEDAKA = StageSpec(image=img("medaka"), cpus=4, memory_gb=8, timeout_s=4 * _HOUR)
CLAIR3 = StageSpec(image=img("clair3"), cpus=4, memory_gb=8, timeout_s=4 * _HOUR)

SNPEFF = StageSpec(image=img("shared:snpeff-lsdv"), cpus=2, memory_gb=4, timeout_s=_HOUR)
