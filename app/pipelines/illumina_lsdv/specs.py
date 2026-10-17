"""Tool images and resource limits for the Illumina LSDV stages."""

from __future__ import annotations

from ...runtime.images import images_for
from ...runtime.spec import StageSpec

img = images_for(__package__)

CORE = img("core")
SNPEFF_IMAGE = img("shared:snpeff-lsdv")

_HOUR = 3600

QC = StageSpec(image=CORE, cpus=2, memory_gb=4, timeout_s=_HOUR)
ALIGN = StageSpec(image=CORE, cpus=4, memory_gb=8, timeout_s=2 * _HOUR)
SAMTOOLS = StageSpec(image=CORE, cpus=2, memory_gb=4, timeout_s=_HOUR)
PRIMER = StageSpec(image=CORE, cpus=2, memory_gb=4, timeout_s=_HOUR)
VARIANT = StageSpec(image=CORE, cpus=4, memory_gb=8, timeout_s=2 * _HOUR)
CONSENSUS = StageSpec(image=CORE, cpus=2, memory_gb=4, timeout_s=_HOUR)
SNPEFF = StageSpec(image=SNPEFF_IMAGE, cpus=2, memory_gb=4, timeout_s=_HOUR)
