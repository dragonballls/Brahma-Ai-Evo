# Brahma-Ai-Evo Graveyard / Reference Archive

This branch is a **reference shelf**, not a product integration branch. Nothing in this folder is wired into Brahma's runtime. Do not merge this branch into `main` as an app feature. Pick individual files or commits later only after review and tests.

## Preserved references

### 1. Original Brahma-Ai-Evo creator source

- Original repository: https://github.com/titechprabhasolutions/Brahma-Ai-Evo
- Exact upstream snapshot inspected: [`c8d9c0f8f1b14b67baf4c258a15be631793a7381`](https://github.com/titechprabhasolutions/Brahma-Ai-Evo/tree/c8d9c0f8f1b14b67baf4c258a15be631793a7381)
- Full fix/feature commit index: [`BRAHMA_UPSTREAM_REFERENCE.md`](./BRAHMA_UPSTREAM_REFERENCE.md)

**License note:** the original repository's current `LICENSE` is a source-available personal-use license that prohibits redistribution. To avoid republishing its source in this public fork, this archive stores the source URL, exact commit, and commit index—not copied upstream source files. The upstream repository remains the place to inspect its code and fixes. Review the license and obtain the creator's permission before redistributing that code.

### 2. AbacusAI-Omni work completed in our repo

- Pinned source repo: https://github.com/dragonballls/abacusai-omni
- Snapshot: [`4f4bc61f56167a9512ab2b208b18bf9fb5915e63`](https://github.com/dragonballls/abacusai-omni/tree/4f4bc61f56167a9512ab2b208b18bf9fb5915e63)
- Summary and checks: [`ABACUSAI_OMNI_REFERENCE.md`](./ABACUSAI_OMNI_REFERENCE.md)
- Submodule path: `graveyard/abacusai-omni` (pinned to the exact commit above)

The AbacusAI-Omni snapshot is kept as a separate repository reference; it is **not merged into Brahma's app**. On a local clone, fetch it with:

```powershell
git checkout archive/graveyard-sources-2026-10-18
git submodule update --init --recursive
```

## Archive rules

1. Keep this branch separate from `main`.
2. Never treat these archived projects as integrated features.
3. Before moving a chosen component into Brahma, identify its dependencies, configuration, license, security boundaries, and tests.
4. Run the relevant tests and packaged-app smoke tests after any future selection.
