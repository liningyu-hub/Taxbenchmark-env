"""SHA-256 of every file in a workspace. Used to detect tampering and to prove a reset is identical."""
import hashlib

from .rubyisms import glob_files


def scan(root) -> dict:
    files = {}
    for rel in glob_files(root, dotmatch=True):
        with open(f"{root}/{rel}", "rb") as f:
            files[rel] = hashlib.sha256(f.read()).hexdigest()
    return files


def fingerprint(files: dict) -> str:
    return hashlib.sha256("\n".join(f"{rel}:{sha}" for rel, sha in files.items()).encode("utf-8")).hexdigest()
