"""
Runtime configuration store, backed by MongoDB.

Routes and their backends used to live only in .env, read once at import time
by config/settings.py. That made the admin panel's add/remove endpoints
misleading: they mutated the in-memory dict, so a change looked like it worked
and then vanished on the next restart. Anything added through the panel now
persists here instead.

DESIGN
------
The database is the source of truth once it holds anything, but it is never
required. Three rules keep a config store outage from becoming a proxy outage:

  1. The proxy starts whether or not Mongo is reachable. If the connection
     fails, .env values are used and a warning is logged - degraded, not down.
  2. On first run against an empty collection, the current .env values are
     seeded in. Upgrading an existing deployment therefore changes nothing
     visible, and there is no migration step to forget.
  3. Reads come from an in-memory cache refreshed on write, not from Mongo per
     request. Routing happens on the hot path; a database round trip there
     would tax every single proxied request.

SHAPE
-----
One document per route in the `backends` collection:

    {
      "route":        "/app1",
      "name":         "Whatsapp ChatBot",
      "health_route": "/check-health",
      "urls":         ["http://127.0.0.1:3000", "http://127.0.0.1:3001"],
      "enabled":      true,
      "updated_at":   <datetime>
    }

`urls` is always a list here, even for one backend. settings.py represents a
single backend as a bare string, which the load balancer also accepts; keeping
one shape in the database avoids that branch on every read.
"""

import os
import threading
from datetime import datetime, timezone

from proxy_server.utils.logger import get_logger

logger = get_logger(__name__)

try:
    from pymongo import MongoClient
    from pymongo.errors import PyMongoError
except ImportError:                                     # pragma: no cover
    MongoClient = None

    class PyMongoError(Exception):
        pass


# Defaults point at the same server the rest of the stack uses (the
# Proxy-Management Node service and help_chatbot both live there).
MONGO_URL = os.getenv(
    "PROXY_MONGO_URL", "mongodb://192.168.12.8:27018/poxy-management"
)
MONGO_DB = os.getenv("PROXY_MONGO_DB", "")          # else taken from the URL
COLLECTION = os.getenv("PROXY_MONGO_COLLECTION", "backends")

# Short on purpose. This runs during create_app, so a dead or firewalled Mongo
# must fail fast rather than stall startup for the driver's 30s default.
CONNECT_TIMEOUT_MS = int(os.getenv("PROXY_MONGO_TIMEOUT_MS", "3000"))

#: Set when config was loaded from Mongo; False means .env is in effect.
_from_database = False
_client = None
_lock = threading.Lock()


class ConfigStoreUnavailable(RuntimeError):
    """Mongo could not be reached. Callers fall back to .env."""


def _collection():
    """Connect lazily and reuse the client. Raises ConfigStoreUnavailable."""
    global _client

    if MongoClient is None:
        raise ConfigStoreUnavailable("pymongo is not installed")

    with _lock:
        if _client is None:
            try:
                _client = MongoClient(
                    MONGO_URL,
                    serverSelectionTimeoutMS=CONNECT_TIMEOUT_MS,
                    connectTimeoutMS=CONNECT_TIMEOUT_MS,
                )
                # The constructor is lazy, so force a round trip now: without
                # this the first real failure would surface deep in a request.
                _client.admin.command("ping")
            except Exception as exc:
                _client = None
                raise ConfigStoreUnavailable(str(exc))

    db = _client[MONGO_DB] if MONGO_DB else _client.get_default_database()
    if db is None:
        raise ConfigStoreUnavailable(
            "no database in PROXY_MONGO_URL and PROXY_MONGO_DB is unset"
        )
    return db[COLLECTION]


def is_available():
    try:
        _collection()
        return True
    except ConfigStoreUnavailable:
        return False


def loaded_from_database():
    return _from_database


def describe():
    """Where configuration came from, for /admin/config and the UI."""
    return {
        "source": "database" if _from_database else "env",
        "mongo_url": _redact(MONGO_URL),
        "collection": COLLECTION,
        "available": is_available(),
    }


def _redact(url):
    """Hide credentials in a mongodb:// URL before it reaches an API response."""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    _, _, host = rest.partition("@")
    return f"{scheme}://***@{host}"


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def _shape(d):
    return {
        "route": d["route"],
        "name": d.get("name") or d["route"].strip("/").title(),
        "health_route": d.get("health_route") or "/health",
        "urls": [u for u in (d.get("urls") or []) if u],
        "enabled": d.get("enabled", True),
        "updated_at": d.get("updated_at").isoformat() if d.get("updated_at") else None,
    }


def load_all():
    """Every ENABLED route - what the proxy actually serves.

    Raises ConfigStoreUnavailable if Mongo is down.
    """
    docs = _collection().find({"enabled": {"$ne": False}})
    return [_shape(d) for d in docs if d.get("route")]


def list_routes(include_disabled=True):
    """Every route including disabled ones, for the admin panel.

    Kept separate from load_all: the panel has to show a disabled route in
    order to re-enable it, but the proxy must never route to one.
    """
    query = {} if include_disabled else {"enabled": {"$ne": False}}
    docs = _collection().find(query).sort("route", 1)
    return [_shape(d) for d in docs if d.get("route")]


def to_flask_config(routes):
    """Convert store documents into the three dicts settings.py exposes.

    A single-element list collapses to a bare string to match how settings.py
    builds BACKEND_ROUTES, so downstream code sees one consistent shape
    regardless of which source the config came from.
    """
    backend_routes, names, health_routes = {}, {}, {}
    for entry in routes:
        urls = entry["urls"]
        if not urls:
            continue
        backend_routes[entry["route"]] = urls[0] if len(urls) == 1 else urls
        names[entry["route"]] = entry["name"]
        health_routes[entry["route"]] = entry["health_route"]
    return backend_routes, names, health_routes


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def _now():
    return datetime.now(timezone.utc)


def add_backend(route, url, name=None, health_route=None):
    """Add a backend URL, creating the route if it does not exist yet.

    $addToSet rather than $push: adding the same URL twice is a no-op instead
    of creating a duplicate the load balancer would then weight twice.
    """
    col = _collection()
    col.update_one(
        {"route": route},
        {
            "$addToSet": {"urls": url},
            "$set": {"enabled": True, "updated_at": _now()},
            "$setOnInsert": {
                "route": route,
                "name": name or route.strip("/").title(),
                "health_route": health_route or "/health",
            },
        },
        upsert=True,
    )
    return col.find_one({"route": route})


def remove_backend(route, url):
    """Remove one backend URL. Deletes the route once its last URL is gone."""
    col = _collection()
    col.update_one(
        {"route": route}, {"$pull": {"urls": url}, "$set": {"updated_at": _now()}}
    )
    doc = col.find_one({"route": route})
    if doc is not None and not doc.get("urls"):
        col.delete_one({"route": route})
        return None
    return doc


def remove_route(route):
    _collection().delete_one({"route": route})


class DuplicateBackend(ValueError):
    """The target URL is already on this route."""


def update_backend_url(route, old_url, new_url):
    """Replace one backend URL in place.

    Position is preserved deliberately. Doing this as remove-then-add would
    move the backend to the end of the list, silently changing round-robin
    order, and would leave the route short one backend in between.

    Returns None if the route or the old URL does not exist; raises
    DuplicateBackend if new_url is already on the route.
    """
    col = _collection()
    doc = col.find_one({"route": route})
    if doc is None:
        return None

    urls = list(doc.get("urls") or [])
    if old_url not in urls:
        return None
    if new_url != old_url and new_url in urls:
        raise DuplicateBackend(f"{new_url} is already a backend of {route}")

    urls[urls.index(old_url)] = new_url
    col.update_one(
        {"route": route}, {"$set": {"urls": urls, "updated_at": _now()}}
    )
    return col.find_one({"route": route})


def upsert_route(route, name=None, health_route=None, urls=None, enabled=None):
    """Create or update a whole route in one call.

    Only fields that were actually passed are written, so editing a display
    name cannot blank out the URL list by omission.
    """
    changes = {"updated_at": _now()}
    if name is not None:
        changes["name"] = name
    if health_route is not None:
        changes["health_route"] = health_route
    if urls is not None:
        changes["urls"] = urls
    if enabled is not None:
        changes["enabled"] = bool(enabled)

    col = _collection()
    col.update_one(
        {"route": route},
        # enabled defaults to True on insert only: an explicit False passed
        # above must not be overwritten here.
        {"$set": changes, "$setOnInsert": {"route": route, "enabled": True}}
        if enabled is None
        else {"$set": changes, "$setOnInsert": {"route": route}},
        upsert=True,
    )
    return col.find_one({"route": route})


def set_enabled(route, enabled):
    """Take a route out of rotation without deleting its configuration."""
    col = _collection()
    col.update_one(
        {"route": route},
        {"$set": {"enabled": bool(enabled), "updated_at": _now()}},
    )
    return col.find_one({"route": route})


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

def seed_from_config(backend_routes, names, health_routes):
    """Populate an empty collection from .env values.

    Only runs when the collection is empty, so it never overwrites something an
    operator set through the admin panel.
    """
    col = _collection()
    if col.estimated_document_count() > 0:
        return 0

    docs = []
    for route, backends in (backend_routes or {}).items():
        urls = [backends] if isinstance(backends, str) else list(backends or [])
        urls = [u for u in urls if u]
        if not urls:
            continue
        docs.append({
            "route": route,
            "name": names.get(route) or route.strip("/").title(),
            "health_route": health_routes.get(route) or "/health",
            "urls": urls,
            "enabled": True,
            "updated_at": _now(),
        })

    if docs:
        col.insert_many(docs)
        col.create_index("route", unique=True)
    return len(docs)


def apply_to_app(app):
    """Point the running app at the database, seeding it on first run.

    Returns True when the database is now authoritative, False when .env
    remains in effect. Never raises: a config store that is down must not stop
    the proxy from serving traffic.
    """
    global _from_database

    try:
        seeded = seed_from_config(
            app.config.get("BACKEND_ROUTES", {}),
            app.config.get("BACKEND_ROUTE_NAMES", {}),
            app.config.get("BACKEND_HEALTH_ROUTES", {}),
        )
        if seeded:
            logger.info(f"config store: seeded {seeded} route(s) from .env")

        routes = load_all()
        if not routes:
            logger.warning("config store: reachable but empty - keeping .env values")
            _from_database = False
            return False

        backend_routes, names, health_routes = to_flask_config(routes)
        app.config["BACKEND_ROUTES"] = backend_routes
        app.config["BACKEND_ROUTE_NAMES"] = names
        app.config["BACKEND_HEALTH_ROUTES"] = health_routes

        _from_database = True
        logger.info(
            f"config store: loaded {len(backend_routes)} route(s) from MongoDB "
            f"({_redact(MONGO_URL)})"
        )
        return True

    except ConfigStoreUnavailable as exc:
        logger.warning(f"config store unavailable ({exc}) - using .env values")
        _from_database = False
        return False
    except PyMongoError as exc:
        logger.error(f"config store error ({exc}) - using .env values")
        _from_database = False
        return False


def refresh_app(app):
    """Re-read routes after a write and rebuild health monitoring.

    Health status is keyed by "route:index", so a backend added or removed
    without this leaves the health service watching a stale set.
    """
    try:
        routes = load_all()
    except (ConfigStoreUnavailable, PyMongoError) as exc:
        logger.error(f"config store: refresh failed ({exc})")
        return False

    backend_routes, names, health_routes = to_flask_config(routes)
    app.config["BACKEND_ROUTES"] = backend_routes
    app.config["BACKEND_ROUTE_NAMES"] = names
    app.config["BACKEND_HEALTH_ROUTES"] = health_routes

    health = getattr(app, "health_service", None)
    if health is not None:
        health.config = app.config
        health.backend_status = {}
        health._initialize_backend_status()
        # Re-initialising resets every backend to 'unknown' with no last_check.
        # Without an immediate probe the whole dashboard would show as failed
        # for up to HEALTH_CHECK_INTERVAL after any edit, which looks like the
        # edit broke something. Threaded so the admin request still returns at
        # once - a probe of several backends can take seconds.
        try:
            import threading

            threading.Thread(
                target=health._check_all_backends, daemon=True
            ).start()
        except Exception as exc:
            logger.warning(f"config store: immediate health re-check failed: {exc}")
    return True
