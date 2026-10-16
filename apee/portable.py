"""Resolve a bundled app at launch without storing the builder's paths or login."""

import json
import os
import sys
import tempfile
from pathlib import Path

# This file is also the entry point for the bundled Python's isolated mode.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apee.config import ApeeError, Config
from apee.service import Service


def initialize(instance, defaults):
    instance = Path(instance).expanduser()
    if not instance.is_absolute():
        raise ApeeError('APEE_INSTANCE_ROOT 必須是絕對路徑。')
    instance.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = instance/'apee.local.json'
    if not path.exists() and not path.is_symlink():
        # Publish a complete file only if another first launch has not won.
        fd, temporary = tempfile.mkstemp(prefix='.apee-initial-', dir=instance)
        try:
            with os.fdopen(fd, 'wb') as output:
                output.write(Path(defaults).read_bytes())
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            Path(temporary).unlink(missing_ok=True)
    Config(path)  # Invalid existing settings are reported, never replaced.
    return path.resolve()


def describe(app, instance=None):
    app = Path(app).resolve()
    resources = app/'Contents/Resources'
    marker = json.loads((resources/'portable.json').read_text())
    if marker != {'schema': 1}:
        raise ApeeError('無法辨識可攜式 app 格式。')
    root = resources/'backend'
    if root.resolve() != Path(__file__).resolve().parents[1]:
        raise ApeeError('app 與執行中的引擎不一致。')
    if instance is None:
        instance = os.environ.get('APEE_INSTANCE_ROOT')
    if instance is None:
        instance = Path.home()/'Library/Application Support/APEE/instances/default'
    config = initialize(instance, resources/'default-config.json')
    return dict(python=str(Path(sys.executable).resolve()), root=str(root),
                config=str(config), environment=Service(config).environment(),
                bootstrap=str(Path(__file__).resolve()))


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(args) == 2 and args[0] == 'describe':
            print(json.dumps(describe(args[1]), ensure_ascii=False))
            return 0
        if len(args) >= 3 and args[0] == 'run':
            from apee.cli import main as cli
            return cli(['--config', args[1], *args[2:]])
        raise ApeeError('無法辨識 app 執行參數。')
    except (ApeeError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
