# AbacusAI-Omni reference snapshot

- **Repository:** https://github.com/dragonballls/abacusai-omni
- **Pinned commit:** [`4f4bc61f56167a9512ab2b208b18bf9fb5915e63`](https://github.com/dragonballls/abacusai-omni/tree/4f4bc61f56167a9512ab2b208b18bf9fb5915e63)
- **Main CI run:** https://github.com/dragonballls/abacusai-omni/actions/runs/39192248087
- **Archive submodule path:** `graveyard/abacusai-omni`

## Work kept for future component selection

- OmniRoute gateway integration with an in-app, movable/resizable provider window.
- Gateway runtime packaging with the Windows app build, pinned OmniRoute runtime, and Node.js runtime.
- API-key-first provider setup (BYOK) and removal of the Abacus credit gate for supported API-only flows.
- A regression fix so an Abacus account key alone cannot falsely mark the API-only automatic route as configured.
- Tests and packaging checks covering the gateway/auth flow and Windows, Linux, and macOS package matrix.

The post-merge CI run for the pinned commit completed successfully: type-check/lint/format, unit tests on Windows/Linux/macOS, renderer screenshots, and packaging on all three platforms. This verifies automated checks; it does **not** by itself prove a live external provider responds using an end-user API key.

## Selection checklist

Before lifting a component into Brahma, review its dependencies and license; map the provider contracts and IPC boundaries; remove product-specific assumptions; add or adapt tests; then run Brahma's own CI and packaged-app smoke tests. This snapshot is not wired into Brahma and makes no claim that these capabilities currently work inside Brahma.
