"""Images the engineer attached to idea messages: `![<file name>](upload:<uploadId>)` in a body. Nestor only reads
text, so each reference is downloaded once (cached per idea) and replaced by the file's path, which it can Read."""

import re
from pathlib import Path

from ..config import log

REFERENCE = re.compile(r"!\[([^\]]*)\]\(upload:([A-Za-z0-9_-]+)\)")
KNOWN = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


class Images:
    def __init__(self, client, root, agent_name="nestor"):
        self.client, self.root, self.agent_name = client, Path(root), agent_name   # root: <state>/ideas

    def resolve(self, idea_id, body):
        """The body with every image reference replaced by `[image "<name>": <path>]`, or by
        `[image "<name>" could not be downloaded]` when the download fails (the turn goes on)."""
        return REFERENCE.sub(lambda m: self._one(idea_id, m.group(1), m.group(2)), body)

    def _one(self, idea_id, name, upload_id):
        label = name or "image"
        try:
            ext = Path(name).suffix.lower()
            folder = self.root / str(idea_id) / "images"
            cached = next(iter(sorted(folder.glob(f"{upload_id}.*"))), None) if folder.is_dir() else None
            if cached is None:
                data = self.client.download(upload_id)
                cached = folder / f"{upload_id}{ext if ext in KNOWN else '.png'}"
                folder.mkdir(parents=True, exist_ok=True)
                cached.write_bytes(data)
            return f'[image "{label}": {cached.resolve().as_posix()}]'
        except Exception as e:   # a missing image must not fail the turn
            log(f"idea {idea_id}: image {upload_id} could not be downloaded: {e}", self.agent_name)
            return f'[image "{label}" could not be downloaded]'
