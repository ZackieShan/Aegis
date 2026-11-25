"""Connected services: the outside APIs the assistant may use, with one set of keys for the whole house.

    catalog   what services there are and what each is for (code, no keys)
    store     the keys an administrator typed in (data folder, encrypted) and the count of paid use
    http      the one way a connected service is asked
    checks    "try it" for each service, for the Integrations screen

The keys belong to the house, not to a person: every account's assistant draws on them. Only an
administrator sees the Integrations screen. See ``docs/integrations.md``.
"""

from . import catalog, store
from .http import Problem
from .store import Refused, count, has_key, limit, on, over_limit, ready, usable, used, value

__all__ = ["catalog", "store", "Problem", "Refused", "count", "has_key", "limit", "on", "over_limit", "ready",
           "usable", "used", "value"]
