"""Stable typed-value hashing, independent of Arrow/Parquet buffer layout."""
import json


def update_frame(digest, frame):
    # IPC bytes are not a logical content identity: identical frames can have
    # different null buffers/dictionaries after a Parquet roundtrip. Preserve
    # schema, order, null, NaN/Inf and exact Python float/Decimal values instead.
    digest.update(json.dumps([frame.height, [(name, str(dtype)) for name, dtype in frame.schema.items()]],
                             ensure_ascii=False).encode())
    digest.update(b"\0")
    for part in frame.iter_slices(8192):
        digest.update(json.dumps(part.rows(), ensure_ascii=False, default=str,
                                 sort_keys=True, allow_nan=True, separators=(",", ":")).encode())
        digest.update(b"\0")
