"""Every snapshot folder is a known source, with URL, retrieval date and hash."""

from ementa.ingest.corpus import DEFAULT_CORPUS_DIR, SOURCE_IDS, load_statute


def test_every_snapshot_folder_is_a_known_source() -> None:
    folders = {p.parent.name for p in (DEFAULT_CORPUS_DIR / "raw").glob("*/source.json")}
    assert folders == set(SOURCE_IDS)


def test_every_source_has_url_date_and_hash() -> None:
    for source_id in SOURCE_IDS:
        source = load_statute(source_id).source
        assert source.url.startswith("https://"), source_id
        assert source.retrieved_at == "2026-11-15"
        assert len(source.sha256) == 64
        assert all(f.url.startswith("https://") for f in source.files)
