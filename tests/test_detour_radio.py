from __future__ import annotations

import importlib
import builtins
import json
import sys
from datetime import timedelta


def load_module():
    sys.modules.pop("detour_radio", None)
    sys.modules.pop("detour_radio.cli", None)
    importlib.invalidate_caches()
    return importlib.import_module("detour_radio.cli")


def test_module_exposes_main_entrypoint():
    module = load_module()

    assert callable(module.main)


def test_load_spotify_config_requires_local_config_file(tmp_path, monkeypatch):
    module = load_module()
    monkeypatch.setattr(module, "APP_CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(module.sys.stdin, "isatty", lambda: False)

    try:
        module.load_spotify_config()
    except RuntimeError as error:
        assert "config.json" in str(error)
        assert module.DEFAULT_REDIRECT_URI in str(error)
    else:
        raise AssertionError("Expected missing config to fail.")


def test_load_spotify_config_defaults_redirect_uri_when_missing(tmp_path, monkeypatch):
    module = load_module()
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"client_id": "abc"}) + "\n")
    monkeypatch.setattr(module, "APP_CONFIG_PATH", config_path)

    config = module.load_spotify_config()

    assert config.client_id == "abc"
    assert config.redirect_uri == module.DEFAULT_REDIRECT_URI


def test_load_spotify_config_prompts_and_writes_file_when_missing(tmp_path, monkeypatch):
    module = load_module()
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(module, "APP_CONFIG_PATH", config_path)
    monkeypatch.setattr(module, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(module.sys.stdin, "isatty", lambda: True)

    answers = iter(["client-123", ""])
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(answers))

    config = module.load_spotify_config()

    assert config.client_id == "client-123"
    assert config.redirect_uri == module.DEFAULT_REDIRECT_URI
    saved_payload = json.loads(config_path.read_text())
    assert saved_payload == {
        "client_id": "client-123",
        "redirect_uri": module.DEFAULT_REDIRECT_URI,
    }


def test_cache_entry_is_valid_uses_one_hour_ttl():
    module = load_module()
    generated_at = (module.now_utc() - timedelta(minutes=59)).isoformat()

    cache_entry = {
        "generated_at": generated_at,
        "schema_version": module.QUERY_CACHE_VERSION,
        "queries": [{"query": "ethiopian jazz", "target_type": "artist"}],
    }

    assert module.cache_entry_is_valid(cache_entry) is True


def test_cache_entry_is_invalid_after_one_hour_ttl_expires():
    module = load_module()
    generated_at = (module.now_utc() - timedelta(hours=2)).isoformat()

    cache_entry = {
        "generated_at": generated_at,
        "schema_version": module.QUERY_CACHE_VERSION,
        "queries": [{"query": "ethiopian jazz", "target_type": "artist"}],
    }

    assert module.cache_entry_is_valid(cache_entry) is False


def test_build_query_cache_key_separates_artist_and_album_modes():
    module = load_module()

    artist_key = module.build_query_cache_key("artist", 3)
    album_key = module.build_query_cache_key("album", 3)

    assert artist_key == "artist:v1:surprise:3"
    assert album_key == "album:v1:surprise:3"


def test_order_queries_by_history_prefers_unseen_queries():
    module = load_module()
    queries = [
        module.DiscoveryQuery(query="ethiopian jazz", target_type="artist"),
        module.DiscoveryQuery(query="zamrock 1970s", target_type="artist"),
        module.DiscoveryQuery(query="thai molam", target_type="artist"),
    ]
    history = [{"query": "ethiopian jazz", "used_at": module.now_utc().isoformat()}]

    ordered_queries = module.order_queries_by_history(queries, history)

    assert ordered_queries[-1].query == "ethiopian jazz"
    assert {query.query for query in ordered_queries[:2]} == {
        "zamrock 1970s",
        "thai molam",
    }


def test_choose_artist_discovery_skips_recent_artist_ids_when_possible(monkeypatch):
    module = load_module()
    queries = [module.DiscoveryQuery(query="ethiopian jazz", target_type="artist")]
    history = {
        "queries": {"artist": [], "album": []},
        "artists": [{"id": "seen-artist", "used_at": "now"}],
        "albums": [],
    }

    artists = [
        module.SpotifyArtist(
            id="seen-artist",
            name="Seen Artist",
            uri="spotify:artist:seen-artist",
        ),
        module.SpotifyArtist(
            id="new-artist",
            name="New Artist",
            uri="spotify:artist:new-artist",
        ),
    ]

    monkeypatch.setattr(module, "search_artists", lambda access_token, query: artists)
    monkeypatch.setattr(module.random, "choice", lambda items: items[0])

    selection = module.choose_artist_discovery(
        access_token="token",
        queries=queries,
        history=history,
    )

    assert selection.artist.id == "new-artist"


def test_choose_album_discovery_skips_recent_album_ids_when_possible(monkeypatch):
    module = load_module()
    queries = [module.DiscoveryQuery(query="ethiopian jazz", target_type="album")]
    history = {
        "queries": {"artist": [], "album": []},
        "artists": [],
        "albums": [{"id": "seen-album", "used_at": "now"}],
    }

    albums = [
        module.SpotifyAlbum(
            id="seen-album",
            name="Seen Album",
            uri="spotify:album:seen-album",
            artist_name="Seen Artist",
        ),
        module.SpotifyAlbum(
            id="new-album",
            name="New Album",
            uri="spotify:album:new-album",
            artist_name="New Artist",
        ),
    ]

    monkeypatch.setattr(module, "search_albums", lambda access_token, query: albums)
    monkeypatch.setattr(module.random, "choice", lambda items: items[0])

    selection = module.choose_album_discovery(
        access_token="token",
        queries=queries,
        history=history,
    )

    assert selection.album.id == "new-album"


def test_parse_discovery_query_rejects_non_artist_results():
    module = load_module()

    parsed_query = module.parse_discovery_query(
        {"query": "ethiopian jazz", "target_type": "album", "novelty_score": 4},
        "artist",
    )

    assert parsed_query is None


def test_parse_spotify_album_requires_primary_artist_name():
    module = load_module()

    album = module.parse_spotify_album(
        {
            "id": "album-id",
            "name": "Album Name",
            "uri": "spotify:album:album-id",
            "artists": [{"id": "artist-id"}],
        }
    )

    assert album is None


def test_parse_spotify_artist_preserves_original_rank():
    module = load_module()

    artist = module.parse_spotify_artist(
        {
            "id": "artist-id",
            "name": "Artist Name",
            "uri": "spotify:artist:artist-id",
            "popularity": 42,
        },
        3,
    )

    assert artist.original_rank == 3


def test_parse_claude_query_payload_accepts_direct_json_array():
    module = load_module()

    payload = module.parse_claude_query_payload(
        '[{"query":"ethiopian jazz","target_type":"album"}]'
    )

    assert payload == [{"query": "ethiopian jazz", "target_type": "album"}]


def test_parse_claude_query_payload_accepts_fenced_json_array():
    module = load_module()

    payload = module.parse_claude_query_payload(
        '```json\n[{"query":"ethiopian jazz","target_type":"album"}]\n```'
    )

    assert payload == [{"query": "ethiopian jazz", "target_type": "album"}]


def test_parse_claude_query_payload_accepts_prose_wrapped_json_array():
    module = load_module()

    payload = module.parse_claude_query_payload(
        'Here are the queries:\n[{"query":"ethiopian jazz","target_type":"album"}]'
    )

    assert payload == [{"query": "ethiopian jazz", "target_type": "album"}]
