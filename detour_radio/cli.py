from __future__ import annotations

import argparse
import base64
import hashlib
import json
import pathlib
import random
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any


CACHE_TTL = timedelta(hours=1)
AUTH_EXPIRY_SAFETY_MARGIN = timedelta(minutes=1)
CONFIG_DIR = pathlib.Path.home() / ".config/detour-radio"
APP_CONFIG_PATH = CONFIG_DIR / "config.json"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8989/callback"
AUTH_PATH = CONFIG_DIR / "auth.json"
QUERY_CACHE_PATH = CONFIG_DIR / "query-cache.json"
HISTORY_PATH = CONFIG_DIR / "history.json"
QUERY_BATCH_SIZE = 24
QUERY_CACHE_VERSION = 1
HISTORY_LIMIT = 500
CLAUDE_TIMEOUT_SECONDS = 120
SPOTIFY_API_BASE_URL = "https://api.spotify.com/v1"
SPOTIFY_AUTH_BASE_URL = "https://accounts.spotify.com"


@dataclass(frozen=True)
class SpotifyConfig:
    client_id: str
    redirect_uri: str


@dataclass(frozen=True)
class OAuthTokens:
    access_token: str
    refresh_token: str
    expires_at: str


@dataclass(frozen=True)
class DiscoveryQuery:
    query: str
    target_type: str
    region: str | None = None
    genre: str | None = None
    era: str | None = None
    novelty_score: int | None = None


@dataclass(frozen=True)
class SpotifyArtist:
    id: str
    name: str
    uri: str
    popularity: int | None = None
    original_rank: int | None = None


@dataclass(frozen=True)
class SpotifyAlbum:
    id: str
    name: str
    uri: str
    artist_name: str
    original_rank: int | None = None


@dataclass(frozen=True)
class DiscoverySelection:
    query: DiscoveryQuery
    item_type: str
    artist: SpotifyArtist | None
    album: SpotifyAlbum | None
    query_source: str
    query_candidates_considered: int
    filtered_recent_item_count: int
    selection_reason: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discover a random Spotify artist or album outside your listening bubble."
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Force refresh the cached AI discovery queries.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the chosen query and result without starting playback.",
    )
    parser.add_argument(
        "--surprise-level",
        type=int,
        choices=range(1, 6),
        default=3,
        metavar="1-5",
        help="How far outside your usual music bubble to explore.",
    )
    parser.add_argument(
        "--type",
        choices=("artist", "album"),
        default="artist",
        help="What kind of Spotify result to discover.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    try:
        spotify_config = load_spotify_config()
        access_token = get_access_token(spotify_config)
        queries, query_source = load_discovery_queries_with_source(
            refresh=args.refresh,
            surprise_level=args.surprise_level,
            discovery_type=args.type,
        )
        history = load_history()
        if args.type == "album":
            selection = choose_album_discovery(
                access_token=access_token,
                queries=queries,
                history=history,
                query_source=query_source,
            )
        else:
            selection = choose_artist_discovery(
                access_token=access_token,
                queries=queries,
                history=history,
                query_source=query_source,
            )
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 1

    print(f"Query: {selection.query.query}")
    print_selection(selection)

    if args.dry_run:
        print(f"Type: {selection.item_type}")
        print(f"Query source: {selection.query_source}")
        print(f"Queries considered: {selection.query_candidates_considered}")
        print(
            f"Recently seen {selection.item_type}s filtered: "
            f"{selection.filtered_recent_item_count}"
        )
        print(f"Selection reason: {selection.selection_reason}")
        return 0

    try:
        launch_spotify_desktop_app()
        if selection.item_type == "album":
            if selection.album is None:
                raise RuntimeError("Album selection was missing album details.")
            start_album_playback(selection.album.id)
        else:
            if selection.artist is None:
                raise RuntimeError("Artist selection was missing artist details.")
            open_artist_page(selection.artist.id)
        record_history_entry(selection)
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 1

    return 0


def load_spotify_config() -> SpotifyConfig:
    try:
        payload = json.loads(APP_CONFIG_PATH.read_text())
    except FileNotFoundError as error:
        try:
            return prompt_for_spotify_config()
        except RuntimeError as prompt_error:
            raise prompt_error from error
    except json.JSONDecodeError as error:
        raise RuntimeError("Spotify app config is invalid JSON.") from error

    if not isinstance(payload, dict):
        raise RuntimeError("Spotify app config must be a JSON object.")

    client_id = payload.get("client_id")
    redirect_uri = payload.get("redirect_uri")

    if not isinstance(client_id, str) or not client_id.strip():
        return prompt_for_spotify_config()

    if redirect_uri is None:
        redirect_uri = DEFAULT_REDIRECT_URI
    elif not isinstance(redirect_uri, str) or not redirect_uri.strip():
        return prompt_for_spotify_config(existing_client_id=client_id.strip())

    return SpotifyConfig(
        client_id=client_id.strip(),
        redirect_uri=redirect_uri.strip(),
    )


def prompt_for_spotify_config(existing_client_id: str | None = None) -> SpotifyConfig:
    if not sys.stdin.isatty():
        raise RuntimeError(
            "Spotify app config is missing or incomplete.\n"
            "Create a Spotify app at https://developer.spotify.com/dashboard and "
            "copy its client_id.\n"
            f"Create {APP_CONFIG_PATH} with contents like:\n"
            "{\n"
            '  "client_id": "your-spotify-client-id",\n'
            f'  "redirect_uri": "{DEFAULT_REDIRECT_URI}"\n'
            "}\n"
            "redirect_uri is optional and defaults to the value above."
        )

    print(f"Spotify app config not found or incomplete: {APP_CONFIG_PATH}")
    print("Get your Spotify client_id from https://developer.spotify.com/dashboard")
    client_id_prompt = "Spotify client_id"
    if existing_client_id:
        client_id_prompt = f"{client_id_prompt} [{existing_client_id}]"
    entered_client_id = input(f"{client_id_prompt}: ").strip()
    client_id = entered_client_id or existing_client_id or ""
    if not client_id:
        raise RuntimeError("Spotify setup requires a non-empty client_id.")

    entered_redirect_uri = input(
        f"Spotify redirect_uri [{DEFAULT_REDIRECT_URI}]: "
    ).strip()
    redirect_uri = entered_redirect_uri or DEFAULT_REDIRECT_URI

    config = SpotifyConfig(
        client_id=client_id,
        redirect_uri=redirect_uri,
    )
    write_spotify_app_config(config)
    print(f"Saved Spotify app config to {APP_CONFIG_PATH}")
    return config


def write_spotify_app_config(config: SpotifyConfig) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    APP_CONFIG_PATH.write_text(
        json.dumps(
            {
                "client_id": config.client_id,
                "redirect_uri": config.redirect_uri,
            },
            indent=2,
        )
        + "\n"
    )


def get_access_token(spotify_config: SpotifyConfig) -> str:
    tokens = load_tokens()
    if tokens is not None and token_is_valid(tokens):
        return tokens.access_token

    if tokens is not None and tokens.refresh_token:
        try:
            refreshed_tokens = refresh_access_token(spotify_config, tokens)
        except RuntimeError:
            authenticated_tokens = authenticate_user(spotify_config)
            save_tokens(authenticated_tokens)
            return authenticated_tokens.access_token

        save_tokens(refreshed_tokens)
        return refreshed_tokens.access_token

    authenticated_tokens = authenticate_user(spotify_config)
    save_tokens(authenticated_tokens)
    return authenticated_tokens.access_token


def load_tokens() -> OAuthTokens | None:
    try:
        payload = json.loads(AUTH_PATH.read_text())
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as error:
        raise RuntimeError("Spotify auth cache is invalid JSON.") from error

    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    expires_at = payload.get("expires_at")

    if not isinstance(access_token, str):
        return None

    if not isinstance(refresh_token, str):
        refresh_token = ""

    if not isinstance(expires_at, str):
        return None

    return OAuthTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
    )


def token_is_valid(tokens: OAuthTokens) -> bool:
    try:
        expires_at = datetime.fromisoformat(tokens.expires_at)
    except ValueError:
        return False

    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    return expires_at - AUTH_EXPIRY_SAFETY_MARGIN > now_utc()


def authenticate_user(spotify_config: SpotifyConfig) -> OAuthTokens:
    verifier = build_code_verifier()
    challenge = build_code_challenge(verifier)
    authorize_state = secrets.token_urlsafe(24)
    redirect_uri = spotify_config.redirect_uri

    authorization_url = build_authorization_url(
        client_id=spotify_config.client_id,
        redirect_uri=redirect_uri,
        state=authorize_state,
        challenge=challenge,
    )

    authorization_code = wait_for_authorization_code(
        authorization_url=authorization_url,
        redirect_uri=redirect_uri,
        expected_state=authorize_state,
    )

    token_payload = exchange_authorization_code(
        spotify_config=spotify_config,
        code=authorization_code,
        verifier=verifier,
    )

    return build_tokens_from_payload(
        payload=token_payload,
        existing_refresh_token="",
    )


def build_code_verifier() -> str:
    return secrets.token_urlsafe(64)


def build_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("utf-8")


def build_authorization_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    challenge: str,
) -> str:
    params = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "state": state,
        }
    )
    return f"{SPOTIFY_AUTH_BASE_URL}/authorize?{params}"


def wait_for_authorization_code(
    *,
    authorization_url: str,
    redirect_uri: str,
    expected_state: str,
) -> str:
    parsed_redirect_uri = urllib.parse.urlparse(redirect_uri)
    host = parsed_redirect_uri.hostname
    port = parsed_redirect_uri.port
    callback_path = parsed_redirect_uri.path

    if host is None or port is None or not callback_path:
        raise RuntimeError("Spotify redirect_uri must include host, port, and path.")

    server_state: dict[str, str | None] = {"code": None, "state": None, "error": None}

    class SpotifyOAuthCallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed_path = urllib.parse.urlparse(self.path)
            if parsed_path.path != callback_path:
                self.send_response(404)
                self.end_headers()
                return

            query_parameters = urllib.parse.parse_qs(parsed_path.query)
            server_state["code"] = first_query_value(query_parameters, "code")
            server_state["state"] = first_query_value(query_parameters, "state")
            server_state["error"] = first_query_value(query_parameters, "error")

            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"Spotify authentication received. You can close this window."
            )

    httpd = HTTPServer((host, port), SpotifyOAuthCallbackHandler)
    httpd.timeout = 120

    if not webbrowser.open(authorization_url):
        try:
            subprocess.run(
                ["open", authorization_url],
                check=True,
                capture_output=True,
                text=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as error:
            raise RuntimeError(
                "Failed to open Spotify authorization in a browser."
            ) from error

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        httpd.handle_request()
        if server_state["error"] is not None:
            raise RuntimeError(
                f"Spotify authorization failed: {server_state['error']}"
            )
        if server_state["code"] is not None:
            if server_state["state"] != expected_state:
                raise RuntimeError("Spotify authorization state mismatch.")
            return server_state["code"]

    raise RuntimeError("Timed out waiting for Spotify authorization.")


def first_query_value(values: dict[str, list[str]], key: str) -> str | None:
    query_values = values.get(key)
    if not query_values:
        return None
    return query_values[0]


def exchange_authorization_code(
    *,
    spotify_config: SpotifyConfig,
    code: str,
    verifier: str,
) -> dict[str, Any]:
    payload = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": spotify_config.redirect_uri,
            "client_id": spotify_config.client_id,
            "code_verifier": verifier,
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        f"{SPOTIFY_AUTH_BASE_URL}/api/token",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    return perform_json_request(
        request,
        failure_message="Failed to exchange Spotify authorization code.",
    )


def refresh_access_token(
    spotify_config: SpotifyConfig,
    tokens: OAuthTokens,
) -> OAuthTokens:
    payload = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
            "client_id": spotify_config.client_id,
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        f"{SPOTIFY_AUTH_BASE_URL}/api/token",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    token_payload = perform_json_request(
        request,
        failure_message="Failed to refresh Spotify access token.",
    )

    return build_tokens_from_payload(
        payload=token_payload,
        existing_refresh_token=tokens.refresh_token,
    )


def build_tokens_from_payload(
    *,
    payload: dict[str, Any],
    existing_refresh_token: str,
) -> OAuthTokens:
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token", existing_refresh_token)
    expires_in = payload.get("expires_in")

    if not isinstance(access_token, str) or not isinstance(refresh_token, str):
        raise RuntimeError("Spotify token response was missing required fields.")

    if not isinstance(expires_in, int):
        raise RuntimeError("Spotify token response had an invalid expiry value.")

    expires_at = now_utc() + timedelta(seconds=expires_in)

    return OAuthTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at.isoformat(),
    )


def save_tokens(tokens: OAuthTokens) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    AUTH_PATH.write_text(json.dumps(asdict(tokens), indent=2) + "\n")


def load_discovery_queries(
    *,
    refresh: bool,
    surprise_level: int,
    discovery_type: str,
) -> list[DiscoveryQuery]:
    queries, _ = load_discovery_queries_with_source(
        refresh=refresh,
        surprise_level=surprise_level,
        discovery_type=discovery_type,
    )
    return queries


def load_discovery_queries_with_source(
    *,
    refresh: bool,
    surprise_level: int,
    discovery_type: str,
) -> tuple[list[DiscoveryQuery], str]:
    cache_key = build_query_cache_key(discovery_type, surprise_level)
    cache_payload = load_query_cache_payload()
    cached_entry = cache_payload.get(cache_key)

    if not refresh and cache_entry_is_valid(cached_entry):
        return parse_cached_queries(cached_entry["queries"], discovery_type), "cache"

    try:
        print(
            f"Calling Claude to generate {discovery_type} discovery keywords...",
            file=sys.stderr,
        )
        generated_queries = generate_discovery_queries(
            surprise_level=surprise_level,
            discovery_type=discovery_type,
        )
    except RuntimeError:
        if not refresh and cache_entry_is_valid(cached_entry):
            return parse_cached_queries(cached_entry["queries"], discovery_type), "cache"
        raise

    cache_payload[cache_key] = {
        "generated_at": now_utc().isoformat(),
        "queries": [asdict(query) for query in generated_queries],
        "schema_version": QUERY_CACHE_VERSION,
    }
    write_query_cache_payload(cache_payload)
    return generated_queries, "fresh"


def build_query_cache_key(discovery_type: str, surprise_level: int) -> str:
    return f"{discovery_type}:v{QUERY_CACHE_VERSION}:surprise:{surprise_level}"


def load_query_cache_payload() -> dict[str, Any]:
    try:
        payload = json.loads(QUERY_CACHE_PATH.read_text())
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as error:
        raise RuntimeError("Discovery query cache is invalid JSON.") from error

    if not isinstance(payload, dict):
        return {}

    return payload


def cache_entry_is_valid(cache_entry: Any) -> bool:
    if not isinstance(cache_entry, dict):
        return False

    generated_at = cache_entry.get("generated_at")
    schema_version = cache_entry.get("schema_version")
    queries = cache_entry.get("queries")

    if schema_version != QUERY_CACHE_VERSION or not isinstance(queries, list):
        return False

    if not isinstance(generated_at, str):
        return False

    try:
        generated_at_dt = datetime.fromisoformat(generated_at)
    except ValueError:
        return False

    if generated_at_dt.tzinfo is None:
        generated_at_dt = generated_at_dt.replace(tzinfo=timezone.utc)

    return generated_at_dt + CACHE_TTL > now_utc()


def parse_cached_queries(items: list[Any], discovery_type: str) -> list[DiscoveryQuery]:
    queries = [
        query
        for item in items
        if (query := parse_discovery_query(item, discovery_type)) is not None
    ]
    if not queries:
        raise RuntimeError("Discovery query cache did not contain usable queries.")
    return queries


def generate_discovery_queries(
    *,
    surprise_level: int,
    discovery_type: str,
) -> list[DiscoveryQuery]:
    prompt = build_claude_prompt(
        surprise_level=surprise_level,
        discovery_type=discovery_type,
    )
    command = [
        "claude",
        "--print",
        prompt,
    ]

    try:
        completed_process = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as error:
        raise RuntimeError("Claude CLI is not installed or not on PATH.") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Timed out generating discovery queries with Claude.") from error
    except subprocess.CalledProcessError as error:
        stderr = error.stderr.strip()
        stdout = error.stdout.strip()
        details = stderr or stdout or "Claude query generation failed."
        raise RuntimeError(
            f"Failed to generate discovery queries with Claude.\n{details}"
        ) from error

    try:
        payload = parse_claude_query_payload(completed_process.stdout)
    except ValueError as error:
        raise RuntimeError("Claude returned invalid JSON for discovery queries.") from error

    if not isinstance(payload, list):
        raise RuntimeError("Claude returned an unexpected discovery query format.")

    queries = [
        query
        for item in payload
        if (query := parse_discovery_query(item, discovery_type)) is not None
    ]

    if not queries:
        raise RuntimeError("Claude returned no usable discovery queries.")

    return queries


def parse_claude_query_payload(output: str) -> list[Any]:
    stripped_output = output.strip()
    if not stripped_output:
        raise ValueError("Claude returned an empty response.")

    direct_payload = try_load_json_array(stripped_output)
    if direct_payload is not None:
        return direct_payload

    fenced_payload = extract_fenced_json_payload(stripped_output)
    if fenced_payload is not None:
        return fenced_payload

    embedded_payload = extract_embedded_json_array(stripped_output)
    if embedded_payload is not None:
        return embedded_payload

    raise ValueError("Claude response did not contain a JSON array.")


def try_load_json_array(text: str) -> list[Any] | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None

    if not isinstance(payload, list):
        return None

    return payload


def extract_fenced_json_payload(text: str) -> list[Any] | None:
    fence_prefixes = ("```json", "```JSON", "```")
    for fence_prefix in fence_prefixes:
        start_index = text.find(fence_prefix)
        if start_index == -1:
            continue

        content_start = start_index + len(fence_prefix)
        end_index = text.find("```", content_start)
        if end_index == -1:
            continue

        fenced_content = text[content_start:end_index].strip()
        payload = try_load_json_array(fenced_content)
        if payload is not None:
            return payload

    return None


def extract_embedded_json_array(text: str) -> list[Any] | None:
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "[":
            continue

        try:
            payload, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue

        if isinstance(payload, list):
            return payload

    return None


def build_claude_prompt(*, surprise_level: int, discovery_type: str) -> str:
    surprise_guide = {
        1: "Keep results accessible and only mildly outside common English-language listening.",
        2: "Push into less mainstream regions and scenes while staying broadly approachable.",
        3: "Aim for meaningful novelty with strong geography, genre, and era variation.",
        4: "Favor deep cuts, niche scenes, smaller markets, and older or stranger combinations.",
        5: "Maximize surprise with obscure scenes, underrepresented regions, and unusual hybrids.",
    }[surprise_level]
    target_noun = "artists" if discovery_type == "artist" else "albums"

    return (
        f"Generate Spotify search queries for discovering {target_noun} outside a "
        "mainstream English-language music bubble. "
        f"Return only a JSON array with exactly {QUERY_BATCH_SIZE} objects. "
        "Do not wrap the JSON in markdown. "
        "Use a hybrid strategy: most queries should be short, structured, and "
        "Spotify-searchable combinations such as region + genre + era, while a "
        "smaller number can be freer combinations for surprise. "
        "Do not include quotation marks inside query text. "
        f"Prefer {discovery_type} discovery rather than other Spotify result types. "
        f"{surprise_guide} "
        "Each object must have these fields: "
        f"query (string), target_type ('{discovery_type}'), region (string), genre (string), "
        "era (string), novelty_score (integer 1-5). "
        "Examples of the style we want include ethiopian jazz, zamrock 1970s, "
        "thai molam, turkish psych rock, colombian cumbia rebajada, or minimal wave belgium. "
        "Avoid generic prompts like world music, indie, or chill unless they are grounded "
        "by region, era, scene, or instrument."
    )


def parse_discovery_query(item: Any, discovery_type: str) -> DiscoveryQuery | None:
    if not isinstance(item, dict):
        return None

    query = item.get("query")
    target_type = item.get("target_type")
    region = item.get("region")
    genre = item.get("genre")
    era = item.get("era")
    novelty_score = item.get("novelty_score")

    if not isinstance(query, str) or not query.strip():
        return None

    if target_type != discovery_type:
        return None

    if region is not None and not isinstance(region, str):
        return None

    if genre is not None and not isinstance(genre, str):
        return None

    if era is not None and not isinstance(era, str):
        return None

    if novelty_score is not None and not isinstance(novelty_score, int):
        return None

    return DiscoveryQuery(
        query=query.strip(),
        target_type=target_type,
        region=region,
        genre=genre,
        era=era,
        novelty_score=novelty_score,
    )


def write_query_cache_payload(payload: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    QUERY_CACHE_PATH.write_text(json.dumps(payload, indent=2) + "\n")


def load_history() -> dict[str, Any]:
    default_history = {
        "queries": {"artist": [], "album": []},
        "artists": [],
        "albums": [],
    }

    try:
        payload = json.loads(HISTORY_PATH.read_text())
    except FileNotFoundError:
        return default_history
    except json.JSONDecodeError as error:
        raise RuntimeError("Discovery history file is invalid JSON.") from error

    if not isinstance(payload, dict):
        return default_history

    query_history = payload.get("queries")
    artist_history = payload.get("artists")
    album_history = payload.get("albums")

    if isinstance(query_history, dict):
        artist_query_history = (
            query_history.get("artist") if isinstance(query_history.get("artist"), list) else []
        )
        album_query_history = (
            query_history.get("album") if isinstance(query_history.get("album"), list) else []
        )
    elif isinstance(query_history, list):
        artist_query_history = query_history
        album_query_history = []
    else:
        artist_query_history = []
        album_query_history = []

    return {
        "queries": {
            "artist": artist_query_history,
            "album": album_query_history,
        },
        "artists": artist_history if isinstance(artist_history, list) else [],
        "albums": album_history if isinstance(album_history, list) else [],
    }


def choose_artist_discovery(
    *,
    access_token: str,
    queries: list[DiscoveryQuery],
    history: dict[str, Any],
    query_source: str = "unknown",
) -> DiscoverySelection:
    query_order = order_queries_by_history(queries, history["queries"]["artist"])
    recent_artist_ids = recent_history_values(history["artists"], "id")

    fallback_selection: DiscoverySelection | None = None
    filtered_recent_artist_count = 0

    for query_index, query in enumerate(query_order, start=1):
        artists = search_artists(access_token, query.query)
        if not artists:
            continue

        unseen_artists = [
            artist for artist in artists if artist.id not in recent_artist_ids
        ]
        filtered_recent_artist_count += len(artists) - len(unseen_artists)
        candidate_artists = unseen_artists or artists

        if fallback_selection is None:
            fallback_selection = DiscoverySelection(
                query=query,
                item_type="artist",
                artist=random.choice(candidate_artists),
                album=None,
                query_source=query_source,
                query_candidates_considered=query_index,
                filtered_recent_item_count=filtered_recent_artist_count,
                selection_reason=(
                    "All matching artists had recent history, so the first fallback "
                    "candidate was kept in reserve."
                ),
            )

        if unseen_artists:
            return DiscoverySelection(
                query=query,
                item_type="artist",
                artist=random.choice(unseen_artists),
                album=None,
                query_source=query_source,
                query_candidates_considered=query_index,
                filtered_recent_item_count=filtered_recent_artist_count,
                selection_reason="Selected from artists not found in recent history.",
            )

    if fallback_selection is not None:
        return fallback_selection

    raise RuntimeError("Spotify search did not return any usable artists.")


def choose_album_discovery(
    *,
    access_token: str,
    queries: list[DiscoveryQuery],
    history: dict[str, Any],
    query_source: str = "unknown",
) -> DiscoverySelection:
    query_order = order_queries_by_history(queries, history["queries"]["album"])
    recent_album_ids = recent_history_values(history["albums"], "id")

    fallback_selection: DiscoverySelection | None = None
    filtered_recent_album_count = 0

    for query_index, query in enumerate(query_order, start=1):
        albums = search_albums(access_token, query.query)
        if not albums:
            continue

        unseen_albums = [album for album in albums if album.id not in recent_album_ids]
        filtered_recent_album_count += len(albums) - len(unseen_albums)
        candidate_albums = unseen_albums or albums

        if fallback_selection is None:
            fallback_selection = DiscoverySelection(
                query=query,
                item_type="album",
                artist=None,
                album=random.choice(candidate_albums),
                query_source=query_source,
                query_candidates_considered=query_index,
                filtered_recent_item_count=filtered_recent_album_count,
                selection_reason=(
                    "All matching albums had recent history, so the first fallback "
                    "candidate was kept in reserve."
                ),
            )

        if unseen_albums:
            return DiscoverySelection(
                query=query,
                item_type="album",
                artist=None,
                album=random.choice(unseen_albums),
                query_source=query_source,
                query_candidates_considered=query_index,
                filtered_recent_item_count=filtered_recent_album_count,
                selection_reason="Selected from albums not found in recent history.",
            )

    if fallback_selection is not None:
        return fallback_selection

    raise RuntimeError("Spotify search did not return any usable albums.")


def order_queries_by_history(
    queries: list[DiscoveryQuery],
    query_history: list[dict[str, str]],
) -> list[DiscoveryQuery]:
    used_queries = recent_history_values(query_history, "query")
    unseen_queries = [query for query in queries if query.query not in used_queries]
    seen_queries = [query for query in queries if query.query in used_queries]

    random.shuffle(unseen_queries)
    random.shuffle(seen_queries)
    return unseen_queries + seen_queries


def recent_history_values(
    items: list[dict[str, str]],
    field_name: str,
) -> set[str]:
    values: set[str] = set()
    for item in reversed(items):
        value = item.get(field_name)
        if isinstance(value, str):
            values.add(value)
        if len(values) >= HISTORY_LIMIT:
            break
    return values


def search_artists(access_token: str, query: str) -> list[SpotifyArtist]:
    search_url = (
        f"{SPOTIFY_API_BASE_URL}/search?"
        f"{urllib.parse.urlencode({'q': query, 'type': 'artist', 'limit': 10})}"
    )
    request = urllib.request.Request(
        search_url,
        headers={"Authorization": f"Bearer {access_token}"},
    )

    payload = perform_json_request(
        request,
        failure_message=f"Failed to search Spotify artists for '{query}'.",
        allow_rate_limit_retry=True,
    )

    artists_block = payload.get("artists")
    if not isinstance(artists_block, dict):
        return []

    items = artists_block.get("items")
    if not isinstance(items, list):
        return []

    artists_by_id: dict[str, SpotifyArtist] = {}
    for rank, item in enumerate(items, start=1):
        artist = parse_spotify_artist(item, rank)
        if artist is not None:
            artists_by_id[artist.id] = artist

    return list(artists_by_id.values())


def search_albums(access_token: str, query: str) -> list[SpotifyAlbum]:
    search_url = (
        f"{SPOTIFY_API_BASE_URL}/search?"
        f"{urllib.parse.urlencode({'q': query, 'type': 'album', 'limit': 10})}"
    )
    request = urllib.request.Request(
        search_url,
        headers={"Authorization": f"Bearer {access_token}"},
    )

    payload = perform_json_request(
        request,
        failure_message=f"Failed to search Spotify albums for '{query}'.",
        allow_rate_limit_retry=True,
    )

    albums_block = payload.get("albums")
    if not isinstance(albums_block, dict):
        return []

    items = albums_block.get("items")
    if not isinstance(items, list):
        return []

    albums_by_id: dict[str, SpotifyAlbum] = {}
    for rank, item in enumerate(items, start=1):
        album = parse_spotify_album(item, rank)
        if album is not None:
            albums_by_id[album.id] = album

    return list(albums_by_id.values())


def parse_spotify_artist(item: Any, original_rank: int | None = None) -> SpotifyArtist | None:
    if not isinstance(item, dict):
        return None

    artist_id = item.get("id")
    artist_name = item.get("name")
    artist_uri = item.get("uri")
    popularity = item.get("popularity")

    if not isinstance(artist_id, str):
        return None

    if not isinstance(artist_name, str) or not isinstance(artist_uri, str):
        return None

    if popularity is not None and not isinstance(popularity, int):
        popularity = None

    return SpotifyArtist(
        id=artist_id,
        name=artist_name,
        uri=artist_uri,
        popularity=popularity,
        original_rank=original_rank,
    )


def parse_spotify_album(item: Any, original_rank: int | None = None) -> SpotifyAlbum | None:
    if not isinstance(item, dict):
        return None

    album_id = item.get("id")
    album_name = item.get("name")
    album_uri = item.get("uri")
    artists = item.get("artists")

    if not isinstance(album_id, str):
        return None

    if not isinstance(album_name, str) or not isinstance(album_uri, str):
        return None

    if not isinstance(artists, list) or not artists:
        return None

    primary_artist = artists[0]
    if not isinstance(primary_artist, dict):
        return None

    primary_artist_name = primary_artist.get("name")
    if not isinstance(primary_artist_name, str):
        return None

    return SpotifyAlbum(
        id=album_id,
        name=album_name,
        uri=album_uri,
        artist_name=primary_artist_name,
        original_rank=original_rank,
    )


def perform_json_request(
    request: urllib.request.Request,
    *,
    failure_message: str,
    allow_rate_limit_retry: bool = False,
) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        response_body = error.read().decode("utf-8", errors="replace").strip()
        if error.code == 429 and allow_rate_limit_retry:
            retry_after = error.headers.get("Retry-After")
            if retry_after is not None and retry_after.isdigit():
                time.sleep(int(retry_after))
                with urllib.request.urlopen(request) as response:
                    return json.loads(response.read().decode("utf-8"))
            raise RuntimeError(
                f"{failure_message}\nSpotify rate limited the request (HTTP 429). "
                "Wait a bit and try again."
            ) from error

        raise RuntimeError(
            build_http_error_message(
                failure_message=failure_message,
                status_code=error.code,
                response_body=response_body,
            )
        ) from error
    except urllib.error.URLError as error:
        raise RuntimeError(
            f"{failure_message}\nNetwork error while contacting Spotify: {error.reason}"
        ) from error
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{failure_message}\nReceived invalid JSON.") from error


def build_http_error_message(
    *,
    failure_message: str,
    status_code: int,
    response_body: str,
) -> str:
    if status_code == 400:
        return (
            f"{failure_message}\nSpotify rejected the request (HTTP 400). "
            "Check the request parameters or redirect URI configuration."
        )

    if status_code == 401:
        return (
            f"{failure_message}\nSpotify authentication failed (HTTP 401). "
            "The saved token may be expired or revoked. Re-run the command to "
            "trigger a fresh browser login."
        )

    if status_code == 403:
        return (
            f"{failure_message}\nSpotify refused the request (HTTP 403). "
            "This can happen if the app configuration or account permissions do not "
            "allow the requested operation."
        )

    if status_code == 404:
        return (
            f"{failure_message}\nSpotify returned HTTP 404. "
            "The requested resource or endpoint was not found."
        )

    if status_code >= 500:
        return (
            f"{failure_message}\nSpotify had a server-side problem (HTTP {status_code}). "
            "Try again in a moment."
        )

    if response_body:
        return f"{failure_message}\nSpotify returned HTTP {status_code}.\n{response_body}"

    return f"{failure_message}\nSpotify returned HTTP {status_code}."


def launch_spotify_desktop_app() -> None:
    try:
        subprocess.run(
            ["open", "-a", "Spotify"],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise RuntimeError("The macOS 'open' command is not available.") from error
    except subprocess.CalledProcessError as error:
        stderr = error.stderr.strip()
        stdout = error.stdout.strip()
        details = stderr or stdout or "Could not open Spotify."
        raise RuntimeError(
            "Failed to launch the Spotify desktop app.\n"
            f"{details}"
        ) from error

    time.sleep(2)


def open_artist_page(artist_id: str) -> None:
    artist_uri = f"spotify:artist:{artist_id}"

    try:
        subprocess.run(
            ["open", artist_uri],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise RuntimeError("The macOS 'open' command is not available.") from error
    except subprocess.CalledProcessError as error:
        stderr = error.stderr.strip()
        stdout = error.stdout.strip()
        details = stderr or stdout or "Could not open the Spotify artist page."
        raise RuntimeError(
            "Failed to open the Spotify artist page.\n"
            f"{details}"
        ) from error


def start_album_playback(album_id: str) -> None:
    album_uri = f"spotify:album:{album_id}"

    run_osascript(
        f'tell application "Spotify" to play track "{album_uri}"',
        failure_message="Failed to start Spotify album playback.",
    )
    time.sleep(2)
    set_spotify_repeat(False)
    set_spotify_shuffle(True)


def set_spotify_repeat(enabled: bool) -> None:
    repeat_value = "true" if enabled else "false"
    run_osascript(
        f'tell application "Spotify" to set repeating to {repeat_value}',
        failure_message="Failed to set Spotify repeat state.",
    )


def set_spotify_shuffle(enabled: bool) -> None:
    shuffle_value = "true" if enabled else "false"
    run_osascript(
        f'tell application "Spotify" to set shuffling to {shuffle_value}',
        failure_message="Failed to set Spotify shuffle state.",
    )


def run_osascript(script: str, *, failure_message: str) -> str:
    try:
        completed_process = subprocess.run(
            ["osascript", "-e", script],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise RuntimeError("osascript is not available on this machine.") from error
    except subprocess.CalledProcessError as error:
        stderr = error.stderr.strip()
        stdout = error.stdout.strip()
        details = stderr or stdout or "osascript command failed."
        raise RuntimeError(f"{failure_message}\n{details}") from error

    return completed_process.stdout


def record_history_entry(selection: DiscoverySelection) -> None:
    history = load_history()
    timestamp = now_utc().isoformat()
    history["queries"][selection.item_type].append(
        {"query": selection.query.query, "used_at": timestamp}
    )

    if selection.item_type == "album":
        if selection.album is None:
            raise RuntimeError("Album history entry was missing album details.")
        history["albums"].append(
            {
                "id": selection.album.id,
                "name": selection.album.name,
                "artist_name": selection.album.artist_name,
                "used_at": timestamp,
            }
        )
    else:
        if selection.artist is None:
            raise RuntimeError("Artist history entry was missing artist details.")
        history["artists"].append(
            {
                "id": selection.artist.id,
                "name": selection.artist.name,
                "used_at": timestamp,
            }
        )

    history["queries"]["artist"] = history["queries"]["artist"][-HISTORY_LIMIT:]
    history["queries"]["album"] = history["queries"]["album"][-HISTORY_LIMIT:]
    history["artists"] = history["artists"][-HISTORY_LIMIT:]
    history["albums"] = history["albums"][-HISTORY_LIMIT:]

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(history, indent=2) + "\n")


def print_selection(selection: DiscoverySelection) -> None:
    if selection.item_type == "album":
        if selection.album is None:
            raise RuntimeError("Album selection was missing album details.")
        print(format_ranked_label("Album", selection.album.name, selection.album.id, selection.album.original_rank))
        print(f"Album Artist: {selection.album.artist_name}")
        return

    if selection.artist is None:
        raise RuntimeError("Artist selection was missing artist details.")
    print(
        format_ranked_label(
            "Artist",
            selection.artist.name,
            selection.artist.id,
            selection.artist.original_rank,
        )
    )


def format_ranked_label(
    label: str,
    name: str,
    spotify_id: str,
    original_rank: int | None,
) -> str:
    if original_rank is None:
        return f"{label}: {name} ({spotify_id})"

    return f"{label}: {name} ({spotify_id}) [Spotify rank: {original_rank}]"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


if __name__ == "__main__":
    raise SystemExit(main())
