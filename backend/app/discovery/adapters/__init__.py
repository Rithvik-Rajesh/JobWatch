"""Platform adapter registry — explicit, no plugin magic.

All shared-platform entries are pre-registered by the lead so adapter agents never edit this file
concurrently. Bespoke adapters (Phase 3.4) are added as "custom:<module>" entries. Lookup:
`get_adapter(platform, module=None)`.
"""

from app.discovery.adapters.base import Adapter
from app.models.enums import ExtractorKind

_REGISTRY: dict[str, str] = {
    # platform key -> "module.path:ClassName"  (lazy import keeps startup light)
    "workday": "app.discovery.adapters.workday:WorkdayAdapter",
    "greenhouse": "app.discovery.adapters.greenhouse:GreenhouseAdapter",
    "lever": "app.discovery.adapters.lever:LeverAdapter",
    "ashby": "app.discovery.adapters.ashby:AshbyAdapter",
    "oracle_hcm": "app.discovery.adapters.oracle_hcm:OracleHcmAdapter",
    "eightfold": "app.discovery.adapters.eightfold:EightfoldAdapter",
    "custom:amazon": "app.discovery.adapters.amazon:AmazonAdapter",
    "custom:atlassian": "app.discovery.adapters.public_feeds:AtlassianAdapter",
    "custom:grab": "app.discovery.adapters.public_feeds:GrabAdapter",
    "custom:amd": "app.discovery.adapters.public_feeds:IcimsJibeAdapter",
}


def registry_key(platform: ExtractorKind | str, module: str | None = None) -> str:
    platform = ExtractorKind(platform)
    return f"custom:{module}" if platform == ExtractorKind.custom else platform.value


def get_adapter(platform: ExtractorKind | str, module: str | None = None) -> Adapter:
    import importlib

    key = registry_key(platform, module)
    target = _REGISTRY.get(key)
    if target is None:
        raise KeyError(f"No adapter registered for '{key}'")
    mod_name, cls_name = target.split(":")
    return getattr(importlib.import_module(mod_name), cls_name)()


def supported_platforms() -> list[str]:
    return sorted(_REGISTRY)
