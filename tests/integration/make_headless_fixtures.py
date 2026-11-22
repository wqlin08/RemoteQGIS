# SPDX-License-Identifier: GPL-3.0-or-later

from pathlib import Path
import sys

from workspace_scenario import fixtures

fixtures(Path(sys.argv[1]), int(sys.argv[2]) if len(sys.argv) > 2 else 2)
