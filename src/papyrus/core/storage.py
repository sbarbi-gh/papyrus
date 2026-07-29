import hashlib
import subprocess
from pathlib import Path
from typing import Optional


def parse_storage_url(url: str) -> tuple[str, str]:
    """Parse rclone:<remote>:<path> into (remote, path)."""
    if not url.startswith("rclone:"):
        raise ValueError(f"Invalid storage URL: {url}. Must start with 'rclone:'")
    rest = url[len("rclone:"):]
    parts = rest.split(":", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid storage URL: {url}. Expected rclone:<remote>:<path>")
    return parts[0], parts[1]


def _rclone(remote: str, path: str) -> str:
    """Return rclone remote:path string."""
    return f"{remote}:{path}"


def storage_path(url: str, pmid: str) -> str:
    """Return rclone path for a PMID on the configured storage."""
    remote, base = parse_storage_url(url)
    return f"{_rclone(remote, base)}/{pmid}.pdf"


def push_pdf(pdf_path: Path, storages: list[str], pmid: str) -> list[str]:
    """Copy PDF to all external storages. Returns list of URLs that succeeded."""
    uploaded: list[str] = []
    for storage_url in storages:
        if not storage_url.startswith("rclone:"):
            raise ValueError(f"Unsupported storage: {storage_url}")
        remote, base = parse_storage_url(storage_url)
        dest = f"{_rclone(remote, base)}/{pmid}.pdf"
        try:
            subprocess.run(
                ["rclone", "copyto", str(pdf_path), dest],
                check=True, capture_output=True, text=True,
            )
            uploaded.append(storage_url)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"rclone copyto failed for {dest}: {e.stderr}") from e
    return uploaded


def pull_pdf(pmid: str, storages: list[str], dest_dir: Path) -> Optional[Path]:
    """Pull PDF from first available external storage to dest_dir.

    Returns path to the downloaded PDF, or None if not found on any storage.
    """
    for storage_url in storages:
        if not storage_url.startswith("rclone:"):
            continue
        remote, base = parse_storage_url(storage_url)
        src = f"{_rclone(remote, base)}/{pmid}.pdf"
        dest = dest_dir / "fulltext.pdf"
        try:
            subprocess.run(
                ["rclone", "copyto", src, str(dest)],
                check=True, capture_output=True, text=True,
            )
            return dest
        except subprocess.CalledProcessError:
            continue
    return None


def check_pdf(pmid: str, storages: list[str]) -> dict[str, bool]:
    """Check PDF existence on each storage. Returns {storage_url: exists}."""
    results: dict[str, bool] = {}
    for storage_url in storages:
        if not storage_url.startswith("rclone:"):
            results[storage_url] = False
            continue
        remote, base = parse_storage_url(storage_url)
        src = f"{_rclone(remote, base)}/{pmid}.pdf"
        try:
            proc = subprocess.run(
                ["rclone", "lsf", src],
                capture_output=True, text=True, timeout=10,
            )
            results[storage_url] = proc.returncode == 0
        except (subprocess.TimeoutExpired, Exception):
            results[storage_url] = False
    return results


def _source_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
