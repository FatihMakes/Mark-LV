"""Persistent, fail-closed device credential verifiers and private QR artifacts."""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import secrets
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, cast


_STORE_LOCK = threading.RLock()
_VERSION = 1
_MAX_STORE_BYTES = 1_048_576
_ID_BYTES = 16
_SECRET_BYTES = 32
_SALT_BYTES = 16
_VERIFIER_BYTES = 32
_DUMMY_SALT = b"\0" * _SALT_BYTES
_DUMMY_VERIFIER = b"\0" * _VERIFIER_BYTES


class DeviceCredentialConfigError(ValueError):
    """Raised when local credential or artifact configuration is unsafe."""


@dataclass(frozen=True)
class DeviceSummary:
    device_id: str
    label: str
    created_at: str
    revoked_at: str | None


@dataclass(frozen=True)
class CreatedDevice:
    summary: DeviceSummary
    secret: str = field(repr=False)


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: object, expected_bytes: int) -> bytes | None:
    if not isinstance(value, str) or not value or "=" in value:
        return None
    try:
        raw = base64.urlsafe_b64decode(value.encode("ascii") + b"=" * (-len(value) % 4))
    except (UnicodeEncodeError, ValueError):
        return None
    if len(raw) != expected_bytes or _encode(raw) != value:
        return None
    return raw


def _valid_label(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not 1 <= len(value) <= 80:
        return None
    if any(ord(char) <= 0x1F or 0x7F <= ord(char) <= 0x9F for char in value):
        return None
    return value


def _valid_timestamp(value: object) -> str | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        return None
    canonical = parsed.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return value if value == canonical else None


def _scrypt(secret: bytes, salt: bytes) -> bytes:
    return hashlib.scrypt(secret, salt=salt, n=2**14, r=8, p=1, dklen=_VERIFIER_BYTES)


def _summary(record: dict[str, object]) -> DeviceSummary:
    return DeviceSummary(
        device_id=cast(str, record["device_id"]),
        label=cast(str, record["label"]),
        created_at=cast(str, record["created_at"]),
        revoked_at=cast(str | None, record["revoked_at"]),
    )


def _service_uid() -> int:
    return os.geteuid()


def _is_private(mode: int, required: int | None = None) -> bool:
    permissions = stat.S_IMODE(mode)
    if required is not None:
        return permissions == required
    return not permissions & 0o022


def _open_real_directory(path: Path, *, exact_mode: int | None = None) -> int:
    """Open a checked directory without following any path component."""
    descriptor: int | None = None
    try:
        raw_path = os.fspath(path)
        if not os.path.isabs(raw_path):
            raw_path = os.path.join(os.getcwd(), raw_path)
        parts = Path(raw_path).parts
        if ".." in parts:
            raise DeviceCredentialConfigError("unsafe directory")
        descriptor = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        before: os.stat_result | None = None
        for name in parts[1:]:
            before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode):
                raise DeviceCredentialConfigError("unsafe directory")
            next_descriptor = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            opened = os.fstat(next_descriptor)
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                os.close(next_descriptor)
                raise DeviceCredentialConfigError("unsafe directory")
            os.close(descriptor)
            descriptor = next_descriptor
        after = os.fstat(descriptor)
    except DeviceCredentialConfigError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise DeviceCredentialConfigError("unsafe directory") from exc
    if (
        before is None
        or not stat.S_ISDIR(after.st_mode)
        or after.st_uid != _service_uid()
        or not _is_private(after.st_mode, exact_mode)
        or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
    ):
        os.close(descriptor)
        raise DeviceCredentialConfigError("unsafe directory")
    return descriptor


def _entry_stat(directory_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DeviceCredentialConfigError("unsafe credential path") from exc


def _checked_regular(info: os.stat_result, *, size_limit: int | None = None) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != _service_uid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or (size_limit is not None and (info.st_size == 0 or info.st_size > size_limit))
    ):
        raise DeviceCredentialConfigError("unsafe credential path")


def _write_all(descriptor: int, value: bytes) -> None:
    offset = 0
    while offset < len(value):
        offset += os.write(descriptor, value[offset:])


def _cleanup_private_files(directory_fd: int, names: list[str]) -> None:
    cleanup_error: OSError | None = None
    for name in names:
        try:
            # unlink removes a symlink itself and never follows it to its target.
            os.unlink(name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            cleanup_error = cleanup_error or exc
    if cleanup_error is not None:
        raise DeviceCredentialConfigError("unable to clean up device artifact") from cleanup_error


class DeviceCredentialStore:
    """A process-local serialized registry of individually revocable credentials."""

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self._clock = clock

    def _load(self) -> list[dict[str, object]]:
        directory_fd = _open_real_directory(self.path.parent)
        try:
            before = _entry_stat(directory_fd, self.path.name)
            if before is None:
                return []
            _checked_regular(before, size_limit=_MAX_STORE_BYTES)
            source_fd: int | None = None
            try:
                source_fd = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
            except OSError as exc:
                raise DeviceCredentialConfigError("unsafe credential path") from exc
            try:
                opened = os.fstat(source_fd)
                _checked_regular(opened, size_limit=_MAX_STORE_BYTES)
                if (before.st_dev, before.st_ino, before.st_size) != (opened.st_dev, opened.st_ino, opened.st_size):
                    raise DeviceCredentialConfigError("credential store changed during read")
                with os.fdopen(source_fd, "rb", closefd=True) as source:
                    source_fd = None
                    raw = source.read(_MAX_STORE_BYTES + 1)
            finally:
                if source_fd is not None:
                    os.close(source_fd)
            after = _entry_stat(directory_fd, self.path.name)
            if after is None or (opened.st_dev, opened.st_ino, opened.st_size) != (after.st_dev, after.st_ino, after.st_size):
                raise DeviceCredentialConfigError("credential store changed during read")
        finally:
            os.close(directory_fd)
        if len(raw) > _MAX_STORE_BYTES or len(raw) != opened.st_size:
            raise DeviceCredentialConfigError("invalid credential store")
        try:
            document = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DeviceCredentialConfigError("invalid credential store") from exc
        if not isinstance(document, dict) or set(document) != {"version", "devices"}:
            raise DeviceCredentialConfigError("invalid credential store")
        if document["version"] != _VERSION or not isinstance(document["devices"], list):
            raise DeviceCredentialConfigError("invalid credential store")
        records: list[dict[str, object]] = []
        device_ids: set[str] = set()
        for record in document["devices"]:
            if not isinstance(record, dict) or set(record) != {
                "device_id", "label", "created_at", "salt", "verifier", "revoked_at"
            }:
                raise DeviceCredentialConfigError("invalid credential store")
            device_id = record["device_id"]
            label = _valid_label(record["label"])
            created_at = _valid_timestamp(record["created_at"])
            revoked_at = record["revoked_at"]
            if (
                not isinstance(device_id, str)
                or _decode(device_id, _ID_BYTES) is None
                or label is None
                or label != record["label"]
                or created_at is None
                or _decode(record["salt"], _SALT_BYTES) is None
                or _decode(record["verifier"], _VERIFIER_BYTES) is None
                or (revoked_at is not None and _valid_timestamp(revoked_at) is None)
                or device_id in device_ids
            ):
                raise DeviceCredentialConfigError("invalid credential store")
            device_ids.add(device_id)
            records.append(record)
        return records

    def _write(self, records: list[dict[str, object]]) -> None:
        data = json.dumps({"version": _VERSION, "devices": records}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(data) > _MAX_STORE_BYTES:
            raise DeviceCredentialConfigError("credential store exceeds size limit")
        directory_fd = _open_real_directory(self.path.parent)
        temporary_name = f".device-credentials-{secrets.token_hex(16)}"
        temporary_fd: int | None = None
        try:
            temporary_fd = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            _checked_regular(os.fstat(temporary_fd))
            _write_all(temporary_fd, data)
            os.fsync(temporary_fd)
            os.close(temporary_fd)
            temporary_fd = None
            os.replace(temporary_name, self.path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
        except OSError as exc:
            raise DeviceCredentialConfigError("unable to write credential store") from exc
        finally:
            if temporary_fd is not None:
                os.close(temporary_fd)
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            except OSError:
                pass
            os.close(directory_fd)

    def _timestamp(self) -> str:
        return dt.datetime.fromtimestamp(self._clock(), tz=dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    def create(self, label: str) -> CreatedDevice:
        normalized = _valid_label(label)
        if normalized is None:
            raise ValueError("invalid device label")
        with _STORE_LOCK:
            records = self._load()
            device_id = _encode(secrets.token_bytes(_ID_BYTES))
            secret_bytes = secrets.token_bytes(_SECRET_BYTES)
            salt = secrets.token_bytes(_SALT_BYTES)
            record: dict[str, object] = {
                "device_id": device_id,
                "label": normalized,
                "created_at": self._timestamp(),
                "salt": _encode(salt),
                "verifier": _encode(_scrypt(secret_bytes, salt)),
                "revoked_at": None,
            }
            records.append(record)
            self._write(records)
            return CreatedDevice(_summary(record), _encode(secret_bytes))

    def verify(self, device_id: str, secret: str) -> DeviceSummary | None:
        with _STORE_LOCK:
            records = self._load()
            secret_bytes = _decode(secret, _SECRET_BYTES)
            if secret_bytes is None or _decode(device_id, _ID_BYTES) is None:
                return None
            record = next((item for item in records if item["device_id"] == device_id), None)
            if record is None:
                calculated = _scrypt(secret_bytes, _DUMMY_SALT)
                hmac.compare_digest(calculated, _DUMMY_VERIFIER)
                return None
            salt = _decode(record["salt"], _SALT_BYTES)
            verifier = _decode(record["verifier"], _VERIFIER_BYTES)
            if salt is None or verifier is None:  # _load already validates this.
                raise DeviceCredentialConfigError("invalid credential store")
            matches = hmac.compare_digest(_scrypt(secret_bytes, salt), verifier)
            if not matches or record["revoked_at"] is not None:
                return None
            return _summary(record)

    def list_devices(self) -> list[DeviceSummary]:
        with _STORE_LOCK:
            return [_summary(record) for record in self._load()]

    def revoke(self, device_id: str) -> DeviceSummary | None:
        with _STORE_LOCK:
            records = self._load()
            if _decode(device_id, _ID_BYTES) is None:
                return None
            for record in records:
                if record["device_id"] == device_id:
                    if record["revoked_at"] is None:
                        record["revoked_at"] = self._timestamp()
                        self._write(records)
                    return _summary(record)
            return None


def _open_or_create_qr_directory(path: Path) -> int:
    try:
        return _open_real_directory(path, exact_mode=0o700)
    except DeviceCredentialConfigError:
        if path.exists() or path.is_symlink():
            raise
    parent_fd = _open_real_directory(path.parent)
    try:
        try:
            os.mkdir(path.name, 0o700, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except FileExistsError:
            pass
        except OSError as exc:
            raise DeviceCredentialConfigError("unable to create QR directory") from exc
    finally:
        os.close(parent_fd)
    return _open_real_directory(path, exact_mode=0o700)


def _create_private_file(directory_fd: int, name: str) -> int:
    try:
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise DeviceCredentialConfigError("unable to create device artifact") from exc
    try:
        _checked_regular(os.fstat(descriptor))
    except BaseException:
        close_error: OSError | None = None
        try:
            os.close(descriptor)
        except OSError as exc:
            close_error = exc
        try:
            _cleanup_private_files(directory_fd, [name])
        except DeviceCredentialConfigError:
            raise
        if close_error is not None:
            raise DeviceCredentialConfigError("unable to clean up device artifact") from close_error
        raise
    return descriptor


def validate_device_artifact_directory(output_dir: Path) -> None:
    """Create or validate the private QR directory without exposing a path FD."""
    directory_fd = _open_or_create_qr_directory(Path(output_dir))
    try:
        os.close(directory_fd)
    except OSError as exc:
        raise DeviceCredentialConfigError("unable to validate QR directory") from exc


def write_device_artifacts(created: CreatedDevice, public_url: str, output_dir: Path) -> tuple[Path, Path]:
    """Create private URL and PNG files without returning or logging their payload."""
    if not isinstance(public_url, str) or not public_url.rstrip("/"):
        raise ValueError("invalid public URL")
    if _decode(created.summary.device_id, _ID_BYTES) is None or _decode(created.secret, _SECRET_BYTES) is None:
        raise ValueError("invalid device credential")
    payload = f"{public_url.rstrip('/')}/login#device={created.summary.device_id}.{created.secret}"
    output_dir = Path(output_dir)
    url_name = f"{created.summary.device_id}.url"
    png_name = f"{created.summary.device_id}.png"
    directory_fd = _open_or_create_qr_directory(output_dir)
    made: list[str] = []
    try:
        url_fd = _create_private_file(directory_fd, url_name)
        made.append(url_name)
        with os.fdopen(url_fd, "wb", closefd=True) as artifact:
            artifact.write(payload.encode("utf-8"))
            artifact.flush()
            os.fsync(artifact.fileno())

        png_fd = _create_private_file(directory_fd, png_name)
        made.append(png_name)
        import qrcode

        qr = qrcode.QRCode()
        qr.add_data(payload)
        qr.make(fit=True)
        image = qr.make_image()
        with os.fdopen(png_fd, "wb", closefd=True) as artifact:
            image.save(artifact, format="PNG")
            artifact.flush()
            os.fsync(artifact.fileno())
        os.fsync(directory_fd)
    except BaseException as exc:
        _cleanup_private_files(directory_fd, made)
        if isinstance(exc, DeviceCredentialConfigError):
            raise
        if isinstance(exc, Exception):
            raise RuntimeError("unable to create device artifact") from None
        raise
    finally:
        os.close(directory_fd)
    return output_dir / url_name, output_dir / png_name
