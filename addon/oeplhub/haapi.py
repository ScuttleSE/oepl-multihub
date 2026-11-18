"""Read-only access to Home Assistant states: through the Supervisor inside the addon (homeassistant_api),
or HA_URL + HA_TOKEN when run by hand."""
import json
import os
import urllib.request


class HAError(Exception):
    pass


def _supervisor_token():
    token = os.environ.get("SUPERVISOR_TOKEN")
    if token:
        return token
    # s6 in the HA base image starts run.sh without the container environment; it keeps it here
    for p in ("/run/s6/container_environment/SUPERVISOR_TOKEN", "/var/run/s6/container_environment/SUPERVISOR_TOKEN"):
        try:
            with open(p) as f:
                return f.read().strip()
        except OSError:
            continue
    return None


def _base():
    token = _supervisor_token()
    if token:
        return "http://supervisor/core/api", token
    url, token = os.environ.get("HA_URL"), os.environ.get("HA_TOKEN")
    if url and token:
        return url.rstrip("/") + "/api", token
    raise HAError("no Home Assistant access (homeassistant_api off, and no HA_URL/HA_TOKEN)")


def get_states(timeout=10) -> list:
    """Every entity's state."""
    base, token = _base()
    req = urllib.request.Request(f"{base}/states", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except (OSError, ValueError) as e:
        raise HAError(f"states: {e}") from e


def get_state(entity_id: str, timeout=10) -> dict:
    """{"state": ..., "attributes": {...}, ...} for one entity."""
    base, token = _base()
    req = urllib.request.Request(f"{base}/states/{entity_id}", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise HAError(f"{entity_id} not found") from e
        raise HAError(f"{entity_id}: HTTP {e.code}") from e
    except (OSError, ValueError) as e:
        raise HAError(f"{entity_id}: {e}") from e
