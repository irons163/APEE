# Bundled and external components

The portable macOS app contains CPython 3.12.15 from
[Astral's python-build-standalone release 20261013](https://github.com/astral-sh/python-build-standalone/releases/tag/20261013).
The exact archive URLs and SHA-256 digests are pinned in
`release/python-runtimes.json` and verified before extraction or execution.

The complete Python runtime is retained, including its original license files.
In an app bundle, the CPython license is at
`Contents/Resources/python/lib/python3.12/LICENSE.txt`; bundled packages retain
their notices in `site-packages`, including `*.dist-info/licenses` directories.
The upstream build's complete `LICENSE*` collection and `python-licenses.rst`
are also preserved in `release/python-licenses` in this repository and
`Contents/Resources/python-licenses` in the app. `origins.json` records each
upstream URL, Git blob ID, and SHA-256. This includes licenses for native runtime
dependencies that are omitted from the install-only archive's root.
Those components retain their own licenses. APEE's license does not replace them.

APEE source is available at <https://github.com/irons163/APEE>. Python runtime
sources, dependency download URLs, checksums, and build instructions are
available from the pinned upstream source tag
<https://github.com/astral-sh/python-build-standalone/tree/20261013>.
The upstream license collection covers multiple platforms; inclusion of a
license text alone does not indicate that every listed component is linked
into this macOS runtime.

Git, GitHub CLI, and Codex CLI are external prerequisites. They are not copied
into APEE's source release or app bundle. Users install and authenticate their
own copies. APEE does not distribute credentials, CLI account configuration,
model logs, project checkouts, or task databases in release artifacts.
