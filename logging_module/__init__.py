"""
Custom logger — file + console output.
"""

import logging
import os
from pathlib import Path

logs_dir = Path(__file__).resolve().parents[1] / "logs"
logs_dir.mkdir(exist_ok=True)

logger = logging.getLogger("lendflow")
logger.setLevel(logging.DEBUG)

# File handler
fh = logging.FileHandler(logs_dir / "lendflow.log")
fh.setLevel(logging.DEBUG)

# Console handler
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)

formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
fh.setFormatter(formatter)
ch.setFormatter(formatter)

logger.addHandler(fh)
logger.addHandler(ch)
