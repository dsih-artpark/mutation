"""Stage images and resource declarations for the FASTA pipeline.

Both images are shared with ont-lsdv: one snpEff image means the same variant
gets the same annotation regardless of which pipeline produced it.
"""

from __future__ import annotations

from ...runtime.images import images_for
from ...runtime.spec import StageSpec

img = images_for(__package__)

_HOUR = 3600

SNIPPY = StageSpec(image=img("shared:snippy"), cpus=2, memory_gb=4, timeout_s=_HOUR)
SNPEFF = StageSpec(image=img("shared:snpeff-lsdv"), cpus=2, memory_gb=4, timeout_s=_HOUR)
