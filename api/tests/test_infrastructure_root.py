from pathlib import Path
from unittest.mock import patch

from aria.config import Settings, _default_infrastructure_root


def test_mac_uses_canonical_model_host_tree():
    with patch("aria.config.sys.platform", "darwin"), patch("aria.config.Path.home", return_value=Path("/Users/fixture")):
        assert _default_infrastructure_root() == "/Users/fixture/Development/Infrastructure/CorsairModelHost"


def test_linux_uses_deployed_model_host_tree():
    with patch("aria.config.sys.platform", "linux"):
        assert _default_infrastructure_root() == "/home/ben/Development/infrastructure"


def test_explicit_infrastructure_setting_still_wins():
    assert Settings(infrastructure_root="/custom/model-host", _env_file=None).infrastructure_root == "/custom/model-host"
