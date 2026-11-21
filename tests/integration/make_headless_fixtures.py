# SPDX-License-Identifier: GPL-3.0-or-later

from pathlib import Path
import sys

from workspace_scenario import fixtures

fixtures(Path(sys.argv[1]), 2)
