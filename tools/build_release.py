"""Fetch a pinned runtime and use the same package command as the CLI."""

import argparse
import json
import platform
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apee.config import ApeeError
from apee.release import package, sha256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--architecture', choices=('arm64', 'x86_64'), default=platform.machine())
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cache', type=Path, default=Path.home()/'Library/Caches/APEE/release-inputs')
    parser.add_argument('--signing-identity')
    parser.add_argument('--notary-profile')
    args = parser.parse_args()
    try:
        pins = json.loads((ROOT/'release/python-runtimes.json').read_text())
        pin = pins[args.architecture]
        args.cache.mkdir(parents=True, exist_ok=True)
        archive = args.cache/(pin['sha256']+'.tar.gz')
        if not archive.exists():
            # Never publish a partial download under the checksum's filename.
            with tempfile.NamedTemporaryFile(dir=args.cache, prefix='.download-', delete=False) as target:
                temporary = Path(target.name)
                try:
                    request = urllib.request.Request(pin['url'], headers={'User-Agent': 'APEE-release-builder'})
                    with urllib.request.urlopen(request, timeout=60) as source:
                        while True:
                            chunk = source.read(1024*1024)
                            if not chunk:
                                break
                            target.write(chunk)
                    target.flush()
                    if sha256(temporary) != pin['sha256']:
                        raise ApeeError('下載的 Python runtime SHA-256 不符。')
                    temporary.replace(archive)
                finally:
                    temporary.unlink(missing_ok=True)
        result = package(archive, pin['sha256'], args.output, args.architecture,
                         args.signing_identity, args.notary_profile)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ApeeError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
