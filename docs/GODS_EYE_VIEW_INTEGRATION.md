# God's Eye View — pinned for future integration

This is a reference-only source pin for future Brahma work. It is deliberately **not wired into Brahma's startup, permissions, UI, or background services** in this change.

## Selected source

- Upstream: https://github.com/bilawalsidhu/gods-eye-view
- Local submodule path: `external/gods-eye-view`
- Pinned upstream `main` commit: `5597c81e7c064414cc43b1fa8ebe0d6bac65e6a8` (latest main commit observed on 2026-10-19)
- Latest formal release observed at pin time: `v0.2.1`, published 2026-10-03: https://github.com/bilawalsidhu/gods-eye-view/releases/tag/v0.2.1
- The pinned `main` commit is newer than that release tag; this is a source snapshot, not a claim that the commit is a tagged release.

The existing `dragonballls/gods-eye-view` mirror was last observed at `759652207fd1279ece97f0f19af566feb9a82146` (2026-09-05), so this pin targets the original upstream rather than that older mirror.

## Get the source after cloning Brahma

From the Brahma repository root:

```powershell
git submodule update --init --recursive external/gods-eye-view
```

For a fresh clone, use `git clone --recurse-submodules <Brahma-repository-url>`. The submodule pointer is fixed to the exact commit recorded in this repository; updating it later should be an explicit reviewed change.

## Integration boundary and checks

- Do not import, launch, or bundle this project into Brahma automatically. The submodule is stored for later evaluation.
- Before integration, inspect upstream `README.md`, `SECURITY.md`, `DATA_SOURCES.md`, `LICENSE`, and `public/models/README.md`.
- Upstream identifies its source code as MIT-licensed, but its license explicitly excludes several third-party datasets, live provider data, and 3D assets. The MIT grant must not be treated as a blanket license for those materials. Respect their individual attribution, privacy, provider terms, and commercial-use limits.
- The project is a separate web application with its own supported Node.js versions and dependencies. It has not been tested as a Brahma subsystem by this pin.
- Any future bridge should be opt-in, local-only by default, and separately tested; do not enable monitoring or data collection silently.

This change only adds a versioned submodule reference and this note. It does not change Brahma runtime behavior or claim that God's Eye View has passed Brahma's tests.
