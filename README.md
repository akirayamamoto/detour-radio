# detour-radio

`detour-radio` is a small CLI for exploratory Spotify discovery outside your
usual listening bubble.

It generates Spotify-friendly discovery queries with `claude`, then searches
Spotify for artists or albums using phrases such as:
- `ethiopian jazz`
- `zamrock 1970s`
- `thai molam`
- `turkish psych rock`
- `colombian cumbia rebajada`
- `minimal wave belgium`

For each run, it searches Spotify, filters out recent repeats when possible, and then randomly picks from the remaining candidates.

After selecting a result, it:
- opens the artist page for artist mode
- starts full album playback for album mode

## Install

Install from GitHub:

```bash
uv tool install git+https://github.com/akirayamamoto/detour-radio
```

For contributors working from a local checkout:

```bash
uv tool install -e .
```

## Requirements

- `uv`
- `claude`
- a Spotify developer app with an allowed redirect URI
- Spotify desktop app for playback actions

## Setup

Create a Spotify app in the Spotify Developer Dashboard and copy its
`client_id`:

- https://developer.spotify.com/dashboard

Create a local config file at `~/.config/detour-radio/config.json`:

```json
{
  "client_id": "your-spotify-client-id"
}
```

Notes:
- `redirect_uri` is optional. If omitted, `detour-radio` defaults to
  `http://127.0.0.1:8989/callback`.
- If you set `redirect_uri`, it must exactly match one of the allowed redirect
  URIs in your Spotify developer app.
- OAuth tokens, query cache, and history are stored outside the repo under
  `~/.config/detour-radio/`.

## Usage

```bash
detour-radio [--refresh] [--dry-run] [--surprise-level 1-5] [--type artist|album]
```

Examples:

```bash
# Discover and open a random artist page
detour-radio

# Discover and play a random album
detour-radio --type album

# Push further outside your usual listening bubble
detour-radio --surprise-level 5

# Force regeneration of cached AI discovery queries
detour-radio --refresh

# Preview the chosen query and result without starting playback
detour-radio --dry-run
```

`--dry-run` also reports:
- whether the query came from cache
- how many queries were considered
- how many recent artists or albums were filtered
- why the final result was selected
- the original Spotify search rank of the chosen result

## Behavior

- Authenticates directly with Spotify Web API using a browser-based OAuth flow
- Generates Spotify-friendly discovery queries with `claude`
- Caches AI-generated discovery queries for 1 hour
- Searches Spotify for artists or albums and avoids recent repeats when possible
- Opens the artist page for artist mode
- Starts full album playback for album mode
- Sets repeat off and shuffle on after album playback starts

## Local State

- `~/.config/detour-radio/config.json`
- `~/.config/detour-radio/auth.json`
- `~/.config/detour-radio/query-cache.json`
- `~/.config/detour-radio/history.json`

## Testing

```bash
uv run --group test pytest
```
