"""Build a relocatable macOS app and, with an identity, notarize its archive."""

import hashlib
import json
import os
import platform
import plistlib
import re
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from . import __version__
from .config import ApeeError, Config
from .process import Runner


MACHO = {b'\xcf\xfa\xed\xfe', b'\xce\xfa\xed\xfe', b'\xfe\xed\xfa\xcf',
         b'\xfe\xed\xfa\xce', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca'}


def sha256(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024*1024), b''):
            result.update(chunk)
    return result.hexdigest()


def extract_runtime(archive, expected_sha256, destination):
    if not re.fullmatch(r'[a-fA-F0-9]{64}', expected_sha256 or ''):
        raise ApeeError('必須提供來源已核對的 Python runtime SHA-256。')
    if sha256(archive) != expected_sha256.lower():
        raise ApeeError('Python runtime SHA-256 不符，未解壓或執行。')
    with tarfile.open(archive, 'r:gz') as source:
        members = source.getmembers()
        names = {member.name.rstrip('/'): member for member in members}
        if len(names) != len(members) or len(members) > 50000 or sum(m.size for m in members) > 2*1024**3:
            raise ApeeError('Python runtime 封裝大小或重複項目無法核對。')
        links = {name for name, member in names.items() if member.issym() or member.islnk()}
        for member in members:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != 'python'
                    or '\\' in member.name or not (member.isfile() or member.isdir() or member.issym() or member.islnk())
                    or any(str(parent) in links for parent in path.parents)):
                raise ApeeError('Python runtime 含不安全的封裝路徑。')
            if member.issym() or member.islnk():
                target = member.linkname if member.islnk() else str(path.parent/member.linkname)
                target = os.path.normpath(target)
                if (PurePosixPath(member.linkname).is_absolute() or '\\' in member.linkname
                        or not target.startswith('python/')
                        or (member.islnk() and (target not in names or not names[target].isfile()))):
                    raise ApeeError('Python runtime 含無法核對的連結。')
            member.mode &= 0o755
        source.extractall(destination, members=members)
    python = Path(destination)/'python/bin/python3'
    if not python.is_file() or not python.resolve().is_relative_to((Path(destination)/'python').resolve()):
        raise ApeeError('Python runtime 缺少內含的 python3。')
    return Path(destination)/'python'


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    path.chmod(0o644)


def sign(app, identity, runner):
    options = ['--options', 'runtime', '--timestamp'] if identity != '-' else []
    # Sign nested interpreters/extensions first, then seal the containing app.
    for path in sorted(app.rglob('*'), key=lambda p: len(p.parts), reverse=True):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open('rb') as source:
            magic = source.read(4)
        if magic in MACHO:
            runner.run(['/usr/bin/codesign', '--force', '--sign', identity, *options, str(path)], timeout=60)
    runner.run(['/usr/bin/codesign', '--force', '--sign', identity, *options,
                '--identifier', 'com.apee.desktop', str(app)], timeout=60)
    runner.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)], timeout=60)


def package(runtime_archive, runtime_sha256, output, architecture=None,
            signing_identity=None, notary_profile=None, runner=None):
    if sys.platform != 'darwin':
        raise ApeeError('macOS 安裝包只能在 macOS 編譯。')
    architecture = architecture or platform.machine()
    if architecture not in ('arm64', 'x86_64'):
        raise ApeeError('安裝包架構只支援 arm64 或 x86_64。')
    if signing_identity and not signing_identity.startswith('Developer ID Application:'):
        raise ApeeError('對外簽章必須指定 Developer ID Application 憑證。')
    if notary_profile and not signing_identity:
        raise ApeeError('公證需要 Developer ID Application 憑證。')
    output = Path(output).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise ApeeError('安裝包輸出位置已存在，未覆寫。')
    source = Path(__file__).resolve().parents[1]
    config = Config(source/'apee.json')
    if config.projects:
        raise ApeeError('公開版預設設定必須沒有啟用或個人專案。')
    runner = runner or Runner()
    tracked = runner.git(source, 'ls-files', '--', 'apee', 'release/python-licenses').splitlines()
    modules = [name for name in tracked if re.fullmatch(r'apee/[A-Za-z0-9_]+\.(?:py|swift)', name)]
    licenses = [name for name in tracked if re.fullmatch(r'release/python-licenses/[A-Za-z0-9_.-]+', name)]
    if not {'apee/__init__.py', 'apee/portable.py', 'apee/desktop.swift'}.issubset(modules):
        raise ApeeError('公開封裝需要完整且由 Git 追蹤的程式碼。')
    if 'release/python-licenses/origins.json' not in licenses:
        raise ApeeError('缺少 Python 與其相依元件的原始授權聲明。')
    for name in [*modules, *licenses, 'apee.json', 'LICENSE', 'THIRD_PARTY_NOTICES.md']:
        if (source/name).is_symlink():
            raise ApeeError('封裝來源不可使用指向其他位置的符號連結。')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.apee-package-', dir=output.parent) as temporary:
        stage = Path(temporary)
        app = stage/'APEE.app'
        resources = app/'Contents/Resources'
        resources.mkdir(parents=True)
        extract_runtime(runtime_archive, runtime_sha256, resources)
        python = resources/'python/bin/python3'
        runtime_arch = runner.run(['/usr/bin/lipo', '-archs', str(python)]).strip().split()
        if architecture not in runtime_arch:
            raise ApeeError('Python runtime 與安裝包架構不一致。')
        # Never sweep ignored local settings, virtualenvs, or other private files
        # into a public artifact, even if they are placed inside the package.
        for name in modules:
            write(resources/'backend'/name, (source/name).read_bytes())
        write(resources/'portable.json', b'{"schema":1}\n')
        write(resources/'default-config.json', (source/'apee.json').read_bytes())
        for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md'):
            if (source/name).is_file():
                write(resources/name, (source/name).read_bytes())
        for name in licenses:
            write(resources/'python-licenses'/Path(name).name, (source/name).read_bytes())
        payload = {str(path.relative_to(resources/'backend')): sha256(path)
                   for path in (resources/'backend/apee').rglob('*') if path.is_file()}
        payload['apee.json'] = sha256(resources/'default-config.json')
        for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md'):
            if (resources/name).is_file():
                payload[name] = sha256(resources/name)
        for path in (resources/'python-licenses').rglob('*'):
            if path.is_file():
                payload['release/python-licenses/'+str(path.relative_to(resources/'python-licenses'))] = sha256(path)
        write(resources/'source-payload.json', (json.dumps(payload, sort_keys=True, indent=2)+'\n').encode())
        info = dict(CFBundleIdentifier='com.apee.desktop', CFBundleName='APEE',
                    CFBundleDisplayName='APEE', CFBundleExecutable='APEE',
                    CFBundlePackageType='APPL', CFBundleShortVersionString=__version__,
                    CFBundleVersion=__version__, LSMinimumSystemVersion='14.0',
                    NSHighResolutionCapable=True)
        write(app/'Contents/Info.plist', plistlib.dumps(info))
        binary = app/'Contents/MacOS/APEE'
        binary.parent.mkdir()
        runner.run(['/usr/bin/xcrun', 'swiftc', '-swift-version', '5', '-parse-as-library', '-O',
                    '-file-prefix-map', str(source)+'=/APEE-source',
                    '-file-prefix-map', str(stage)+'=/APEE-build',
                    '-target', architecture+'-apple-macosx14.0', '-framework', 'SwiftUI',
                    '-framework', 'AppKit', '-framework', 'UserNotifications',
                    str(resources/'backend/apee/desktop.swift'), '-o', str(binary)], timeout=240)
        binary.chmod(0o755)
        sign(app, signing_identity or '-', runner)
        notarized = False
        upload = stage/'notary-upload.zip'
        if notary_profile:
            runner.run(['/usr/bin/ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', str(app), str(upload)], timeout=180)
            result = json.loads(runner.run(['/usr/bin/xcrun', 'notarytool', 'submit', str(upload),
                                '--keychain-profile', notary_profile, '--output-format', 'json', '--wait'], timeout=1800))
            if result.get('status') != 'Accepted':
                raise ApeeError('Apple 公證未通過；提交 ID：'+str(result.get('id', 'unknown')))
            runner.run(['/usr/bin/xcrun', 'stapler', 'staple', str(app)], timeout=120)
            runner.run(['/usr/bin/xcrun', 'stapler', 'validate', str(app)], timeout=120)
            runner.run(['/usr/sbin/spctl', '--assess', '--type', 'execute', '--verbose', str(app)], timeout=60)
            notarized = True
            upload.unlink()
        suffix = '' if notarized else '-preview'
        archive_name = f'APEE-{__version__}-macos-{architecture}{suffix}.zip'
        archive = stage/archive_name
        runner.run(['/usr/bin/ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', str(app), str(archive)], timeout=180)
        try:
            commit = runner.git(source, 'rev-parse', 'HEAD')
            dirty = bool(runner.git(source, 'status', '--porcelain', '--untracked-files=normal'))
        except ApeeError:
            commit, dirty = None, None
        payload_matches = all((source/name).is_file() and sha256(source/name) == value for name, value in payload.items())
        manifest = dict(version=__version__, architecture=architecture, archive=archive_name,
                        sha256=sha256(archive), python_archive_sha256=runtime_sha256.lower(),
                        source_commit=commit, source_dirty=dirty, notarized=notarized,
                        source_payload_sha256=sha256(resources/'source-payload.json'),
                        source_payload_matches_checkout=payload_matches,
                        signing='Developer ID' if signing_identity else 'ad-hoc preview',
                        license_included=(resources/'LICENSE').is_file(),
                        minimum_macos='14.0', bundled_python=True,
                        public_app_ready=notarized and (resources/'LICENSE').is_file() and dirty is False and payload_matches)
        write(stage/'release.json', (json.dumps(manifest, indent=2)+'\n').encode())
        write(stage/'SHA256SUMS', (manifest['sha256']+'  '+archive_name+'\n').encode())
        if (source/'docs/INSTALL.md').is_file():
            write(stage/'INSTALL.md', (source/'docs/INSTALL.md').read_bytes())
        # Keep only reviewable deliverables; no build-time settings or login.
        stage.rename(output)
    return dict(output=str(output), app=str(output/'APEE.app'), **manifest)
