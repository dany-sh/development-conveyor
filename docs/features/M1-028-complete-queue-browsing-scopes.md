# M1-028: Complete queue browsing scopes

## Status

Integrated historically as
`b7ac3641564c9827e426bc08b8cfe131917faa74`, whose sole parent is the
M1-027 implementation commit
`e46a6a92f620b0f984503a70a5d1fb2997a28b6c`.

## Goal

Let the operator browse active, unfinished, or complete feature inventory
without widening the authority that selects executable work.

## Contract

- `queue --scope active` remains the default and reports the active milestone.
- `queue --scope unfinished` reports every nonterminal feature, and
  `queue --scope all` reports every known feature.
- An optional milestone filter narrows visible rows without changing the
  active milestone or the controller's next-feature selection.
- Rows outside the active milestone are explicitly execution-ineligible and
  explain the active-milestone restriction.
- Unknown milestones return a stable structured read-only error.
- Counts, milestone summaries, queue positions, derived columns, and execution
  eligibility remain presentation evidence only.

## Acceptance evidence

- Focused disposable queue-control tests cover all scopes, milestone filtering,
  future-milestone execution reasons, deterministic counts and ordering, and
  structured unknown-milestone output.
- Queue inspection launches zero models and zero children and does not change
  the queue, repository HEAD, or execution selection.
