"""The SAR application metadata stays publishable."""

import re
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent.parent / "template.yaml"


def _sar_description():
    text = TEMPLATE.read_text()
    block = text.split("AWS::ServerlessRepo::Application:", 1)[1]
    m = re.search(r"\n    Description: >-\n((?:      .*\n)+)", block)
    assert m, "no folded Description under AWS::ServerlessRepo::Application"
    return " ".join(line.strip() for line in m.group(1).splitlines())


def test_sar_description_fits_the_publish_limit():
    # SAR rejects 0 or more than 256 characters at publish time, after the
    # tests and the package have passed.
    assert 1 <= len(_sar_description()) <= 256
