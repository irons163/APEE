"""Build an unsigned-for-distribution, locally signed native macOS control app."""

import json
import plistlib
import platform
import sys
import tempfile
from pathlib import Path

from . import __version__
from .config import ApeeError
from .process import Runner
from .service import Service, atomic_write


def build(config, output=None, runner=None):
    if sys.platform != 'darwin':
        raise ApeeError('桌面介面目前只支援 macOS。')
    output = Path(output or Path.home()/'Applications/APEE.app').expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise ApeeError('桌面 app 輸出位置已存在，未覆寫。請使用新的 --output 位置。')
    source = Path(__file__).with_name('desktop.swift')
    if not source.is_file():
        raise ApeeError('缺少桌面介面原始碼。')
    output.parent.mkdir(parents=True, exist_ok=True)
    runner = runner or Runner()
    with tempfile.TemporaryDirectory(prefix='.apee-desktop-', dir=output.parent) as temporary:
        app = Path(temporary)/'APEE.app'
        binary = app/'Contents/MacOS/APEE'
        binary.parent.mkdir(parents=True)
        resources = app/'Contents/Resources'
        resources.mkdir()
        service = Service(config.path)
        backend = dict(python=str(Path(sys.executable).resolve()), root=str(source.parent.parent),
                       config=str(config.path), environment=service.environment())
        atomic_write(resources/'backend.json', (json.dumps(backend, indent=2)+'\n').encode())
        info = dict(CFBundleIdentifier='com.apee.desktop', CFBundleName='APEE',
                    CFBundleDisplayName='APEE', CFBundleExecutable='APEE',
                    CFBundlePackageType='APPL', CFBundleShortVersionString=__version__,
                    CFBundleVersion=__version__, LSMinimumSystemVersion='14.0',
                    NSHighResolutionCapable=True)
        atomic_write(app/'Contents/Info.plist', plistlib.dumps(info))
        runner.run(['/usr/bin/xcrun', 'swiftc', '-swift-version', '5', '-parse-as-library', '-O',
                    '-target', platform.machine() + '-apple-macosx14.0',
                    '-framework', 'SwiftUI', '-framework', 'AppKit', str(source), '-o', str(binary)], timeout=180)
        binary.chmod(0o700)
        runner.run(['/usr/bin/codesign', '--force', '--sign', '-', '--identifier', 'com.apee.desktop', str(app)], timeout=30)
        runner.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)], timeout=30)
        app.rename(output)
    return dict(app=str(output), version=__version__, config=str(config.path),
                signing='local ad-hoc; no distribution notarization')


def open_app(path):
    Runner().run(['/usr/bin/open', str(path)])
