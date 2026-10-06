# IVY11_GRRM_V1 GitHub Runtime Bridge

## Purpose

This repository is **not** a new investment model.

It is an execution bridge for the already-frozen V4 research architecture.

It computes, after market close:

- MSP Market
- MSP CONTROL Credit
- MSP Volatility
- Fast Sentinel 65/70
- CLEAN3F frozen 243-model vote
- Sentinel 70/75
- 200D gap

and writes:

- `docs/latest_fast_snapshot.json`
- `docs/latest_fast_snapshot.md`

The Custom GPT reads this compact snapshot instead of trying to reconstruct the full frozen model from ad-hoc web snippets.

## Safety

- no Strategic Cash authority
- no threshold retuning
- no IVY11 ranking changes
- source failure => workflow failure / old snapshot remains visibly dated
- target market date is explicit
- frozen anchor = 2026-10-02

## Deployment

1. Create a GitHub repository.
2. Upload this repository content preserving folders.
3. In Settings → Actions → General, allow workflows to read/write repository contents.
4. Run the workflow manually once.
5. Inspect `docs/latest_fast_snapshot.json`.
6. Optionally enable GitHub Pages from the `/docs` folder.
7. Put the resulting fixed snapshot URL into the V4 GPT Instructions.

The next implementation block adds the BASE full-risk runtime.
