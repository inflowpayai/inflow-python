from importlib.metadata import version
from importlib.resources import files

import inflowpay


def test_distribution_version() -> None:
    assert inflowpay.__version__ == version("inflowpay")
    assert inflowpay.__all__ == ["__version__"]


def test_type_marker() -> None:
    assert files("inflowpay").joinpath("py.typed").is_file()
