"""Which adapter class handles which feed.

Replay needs to reach an adapter's `parse` without knowing anything about how
that feed is fetched, so the registry hands back a parse-only instance: no
HTTP client, no site list, no city.
"""

from __future__ import annotations

from headsup.contracts import Source
from headsup.ingest.base import Adapter, IngestError
from headsup.ingest.nws import NwsAdapter
from headsup.ingest.usgs import UsgsAdapter

#: Every feed with an adapter today. WZDx, Austin incidents and Austin signals
#: join this table as their adapters land.
ADAPTERS: dict[Source, type[Adapter]] = {
    Source.NWS: NwsAdapter,
    Source.USGS: UsgsAdapter,
}


def adapter_class(source: Source) -> type[Adapter]:
    try:
        return ADAPTERS[source]
    except KeyError:
        known = ", ".join(sorted(item.value for item in ADAPTERS))
        raise IngestError(f"no adapter for feed {source.value!r}; have: {known}") from None


def parser_for(source: Source) -> Adapter:
    """An adapter built only to parse recorded payloads of this feed."""
    return adapter_class(source)()


def parsers() -> dict[Source, Adapter]:
    """One parse-only adapter per known feed, reused across a whole replay."""
    return {source: cls() for source, cls in ADAPTERS.items()}
