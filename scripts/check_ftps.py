"""Check your Shutterstock FTPS login from your own computer before enabling uploads.

Logs in, lists the upload folder, and logs out. Uploads nothing unless you pass --upload.

    python scripts/check_ftps.py --user you@example.com
    python scripts/check_ftps.py --user you@example.com --upload D:/Photos/test.jpg
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from shuto.ftps import DEFAULT_HOST, connect, upload_file  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True, help="Shutterstock contributor email/username")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--upload", help="optional: also upload this JPEG (it appears in your portal)")
    args = ap.parse_args()
    password = getpass.getpass("Shutterstock password: ")

    ftp = connect(args.host, args.user, password)
    print("Login OK:", ftp.getwelcome())
    print("Files currently in the upload area:", ftp.nlst()[:20])
    ftp.quit()

    if args.upload:
        path = Path(args.upload)
        upload_file(args.host, args.user, password, path.name, path.read_bytes())
        print(f"Uploaded {path.name}: check 'Not submitted' on submit.shutterstock.com")


if __name__ == "__main__":
    main()
