"""Display one device QR artifact as a terminal QR for on-site scanning.

The QR content never leaves the SSH session: this renders the private
`config/device_qr/<device-id>.png`-paired `.url` text as an ASCII QR block.
The HTTPS URL is never printed as text and never appears in logs.

Usage:
    python scripts/show_device_qr.py --label 'LaCasa iPhone 1'   \
        [--store config/device_credentials.json] [--qr-dir config/device_qr]

Exit codes: 0 rendered; 1 device unknown/revoked; 2 unsafe/missing artifacts.
"""
import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core.device_credentials import DeviceCredentialStore, DeviceCredentialConfigError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or '').splitlines()[0])
    parser.add_argument('--label', required=True)
    parser.add_argument('--store', type=Path,
                        default=Path(os_get('JARVIS_DEVICE_CREDENTIALS',
                                            BASE_DIR / 'config' / 'device_credentials.json')))
    parser.add_argument('--qr-dir', type=Path,
                        default=Path(os_get('JARVIS_DEVICE_QR_DIR',
                                            BASE_DIR / 'config' / 'device_qr')))
    args = parser.parse_args()
    try:
        matches = [summary for summary
                   in DeviceCredentialStore(args.store).list_devices()
                   if summary.label == args.label and summary.revoked_at is None]
    except DeviceCredentialConfigError as exc:
        print('QR display refused an unsafe store (value not shown).')
        return 2
    if len(matches) != 1:
        # Zero or ambiguous: either way, refuse to display anything.
        return 1
    device_id = matches[0].device_id
    url_file = args.qr_dir / f'{device_id}.url'
    try:
        payload = url_file.read_text(encoding='utf-8')
    except OSError:
        print('QR artifact missing or unreadable (content not shown).')
        return 2
    import qrcode
    qr = qrcode.QRCode(border=2)
    qr.add_data(payload)
    qr.make(fit=True)
    qr.print_ascii(invert=True)
    return 0


def os_get(name: str, default):
    import os
    return os.environ.get(name, str(default))


if __name__ == '__main__':
    raise SystemExit(main())