## Visibility changes

1. Audit ownership, callers, scripts, assets, connectors, and tool dependencies.
2. Archive user-owned state and configuration before replacement or retirement.
3. Internalize only reusable instructions needed by a surviving router, preserving provenance. Do
   not pretend copied documentation preserves a connector or executable capability.
4. Update every router and validate reachability.
5. Disable a local Codex skill with an exact `[[skills.config]]` path or disable its owning plugin.
   Never patch bundled or plugin caches.
6. Restart the affected platform and compare the picker with the declared allowlist.
