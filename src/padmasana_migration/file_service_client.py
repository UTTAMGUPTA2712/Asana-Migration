"""Thin client for the file-service's `POST /files` (DESIGN.md §6, per
`openapi.yml`'s `FileStoreRequest`/`FileAggregateRootSchema`).

No rate limiter here on purpose - unlike Asana, there's no documented rate
limit to pace against (DESIGN.md §4: "no Asana call to pace here, only
file-service uploads"); concurrency is bounded entirely by
`upload_attachments.py`'s `--concurrency` worker count.
"""

from __future__ import annotations

import mimetypes

import requests


class FileServiceError(Exception):
    pass


class FileServiceClient:
    def __init__(self, base_url: str, token: str | None = None, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.session = requests.Session()
        if token:
            # Per DESIGN.md §2: auth isn't enforced by this instance, so a
            # token is sent only when one was actually given - never a
            # placeholder.
            self.session.headers.update({"Authorization": f"Bearer {token}"})

    def upload_file(self, *, file_path, path: str, name: str) -> dict:
        """Uploads one real file's bytes. Returns the verbatim
        `FileAggregateRootSchema` JSON body - callers store this whole and
        untouched as the eventual `attachment`/`comment_attachment` row's
        `metadata` (DESIGN.md §5.7)."""
        url = f"{self.base_url}/files"
        # Sent explicitly, the way a browser upload from padmasana-app does -
        # without it the part goes up with no Content-Type and the file
        # service stores every migrated file as a generic binary.
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        with open(file_path, "rb") as fh:
            files = {"file": (name, fh, content_type)}
            # Exactly the fields padmasana-app's own upload sends (see its
            # `use-upload-file.ts`): file, name, path. No `metadata` - a JSON
            # string there made this file service answer 500 on every upload,
            # and nothing downstream reads it back (only the returned uuid/name).
            data = {"path": path, "name": name}
            resp = self.session.post(url, files=files, data=data, timeout=self.timeout)
        if resp.status_code not in (200, 201):
            raise FileServiceError(f"POST {url} -> {resp.status_code}: {resp.text[:500]}")
        return resp.json()
