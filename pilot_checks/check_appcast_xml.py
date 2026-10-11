"""Independent acceptance check for the codex-pool-manager pilot task.

Run from its checkout. This script only generates feeds with synthetic data into
system temp; it does not build, sign, publish, access accounts, or call a model.
"""

import os
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path


def main():
    generator = Path.cwd() / "scripts" / "generate_sparkle_appcast.sh"
    if not generator.is_file():
        raise SystemExit("Run this check from the codex-pool-manager checkout.")
    sparkle = "{http://www.andymatuschak.org/xml-namespaces/sparkle}"
    fixtures = [
        dict(name='Codex & <Pool> "Manager"', title='Update & <fix> "A"',
             notes="修正 A & B，保留 ]]> 分隔符號。", query="?arch=arm64&download=1"),
        dict(name="CodexPoolManager", title="Plain release", notes="Ordinary release notes.", query=""),
    ]
    for fixture in fixtures:
        with tempfile.TemporaryDirectory(prefix="apee-appcast-acceptance-") as temporary:
            arm_url = "https://example.com/apple-silicon.dmg" + fixture["query"]
            intel_url = "https://example.com/intel.dmg?arch=x86_64&download=1"
            notes_url = "https://example.com/notes?language=zh-Hant&full=1"
            env = dict(os.environ)
            env.update(
                OUTPUT_DIR=temporary, APP_NAME=fixture["name"], RELEASE_TAG="v9.8.7",
                RELEASE_NAME=fixture["title"], RELEASE_NOTES=fixture["notes"],
                BUILD_VERSION="112", PUBLISHED_AT="2026-06-09T09:05:30Z",
                ARM64_URL=arm_url, X86_64_URL=intel_url, ARM64_SIZE="123", X86_64_SIZE="456",
                LOCALIZED_RELEASE_NOTES_LINKS="zh-Hant=" + notes_url, MAX_NOTES_CHARS="2800",
            )
            subprocess.run(["bash", str(generator)], env=env, check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
            for arch, url, size in (("arm64", arm_url, "123"), ("x86_64", intel_url, "456")):
                path = Path(temporary) / f"appcast-{arch}.xml"
                feed = ET.parse(path).getroot()
                channel = feed.find("channel")
                item = channel.find("item")
                enclosure = item.find("enclosure")
                assert channel.findtext("title") == f"{fixture['name']} Updates ({arch})"
                assert channel.findtext("description") == f"Latest {fixture['name']} updates for {arch}"
                assert item.findtext("title") == fixture["title"]
                assert item.findtext("description") == fixture["notes"]
                assert item.findtext(sparkle + "releaseNotesLink") == notes_url
                assert enclosure.get("url") == url
                assert enclosure.get("length") == size
                assert enclosure.get(sparkle + "version") == "112"
                assert enclosure.get(sparkle + "shortVersionString") == "9.8.7"
    print("PASS: both architectures parse as XML and preserve special characters, URLs, CDATA text and ordinary input")


if __name__ == "__main__":
    main()
