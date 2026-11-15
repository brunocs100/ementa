import ementa
import ementa_kit


def test_packages_import_and_share_version() -> None:
    assert ementa.__version__ == ementa_kit.__version__ == "0.1.0"
