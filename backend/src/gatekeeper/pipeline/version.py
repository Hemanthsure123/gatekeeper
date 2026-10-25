"""Pipeline version. Bump it when the pipeline's output for the same input can change
(steps, prompts, thresholds); skip-unchanged only reuses analyses of the same version."""

from typing import Final

PIPELINE_VERSION: Final = "1"
