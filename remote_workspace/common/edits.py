# SPDX-License-Identifier: GPL-3.0-or-later

import json
import os
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

IDENTITY = "rq_uuid"


def edit_delta(base, working):
    prior = {row["id"]: row for row in base["features"]}
    current = {row["id"]: row for row in working["features"]}
    fields = working["fields"]
    changed_schema = fields != base["fields"]
    return {
        "base": base["revision"], "transaction": str(uuid4()), "fields": fields,
        "delete": sorted(prior.keys() - current.keys()),
        "add": [row for key, row in current.items() if key not in prior],
        "update": [row for key, row in current.items()
                   if key in prior and (changed_schema or row != prior[key])],
    }


class Recovery:
    def __init__(self, directory: Path):
        self.directory = directory

    def path(self, origin, descriptor):
        identity = (origin, descriptor["path"], descriptor["table"])
        if descriptor.get("workspace_scope"):
            identity += (descriptor["workspace_scope"],)
        key = sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
        return self.directory / f"{key}.json"

    def read(self, path):
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def store(self, path, data):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = None
        try:
            with NamedTemporaryFile("w", encoding="utf-8", dir=self.directory, delete=False) as file:
                temp = Path(file.name)
                json.dump(data, file, ensure_ascii=False, allow_nan=False)
                file.flush()
                os.fsync(file.fileno())
            temp.replace(path)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
