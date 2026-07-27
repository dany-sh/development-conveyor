# Accepted Functionality per Codex Usage Policy

- Policy version: 1
- Status: governing controller guidance
- Optimization target: accepted functionality delivered per unit of total Codex usage
- Executable configuration: `config/execution-profiles.yaml`

## Central governing rule

Luna prepares the plan. Terra executes a known plan. Sol determines or changes the plan.

The controller should ship a feature with the least expensive profile likely to finish it acceptably in one bounded implementation-and-test cycle. It should not optimize for the cheapest first response, and it should not use the strongest available model merely because a feature description contains risk-associated words.

Expected total usage includes the initial run, validation, necessary repair runs, review, and integration. A bounded Terra execution may therefore be more efficient than a broader Sol investigation when the contract, affected area, tests, prohibited side effects, and acceptance commands are already explicit. Sol is appropriate when the plan itself, shared authority, or architecture must be determined or changed.

## Governing guidance

Use Luna for mechanical preparation, repository-aware discovery, inventory, classification, documentation, and other planning inputs. Use Terra for precisely specified implementation along known paths. Use Sol for generic controller behavior, reusable architecture, unclear ownership, competing authorities, or material ambiguity. Reserve Sol xhigh for a recorded unresolved reasoning problem after high-reasoning work with complete evidence.

Complexity and uncertainty are distinct. Complexity may increase reasoning within a model family. Structural uncertainty may change the model family. Persistence, migration, recovery, lifecycle, concurrency, or similarly consequential vocabulary does not establish uncertainty by itself.

Context discovery and verification planning are separate concerns from profile selection:

- Context discovery identifies the focused source, tests, specification, architecture, and adapter contracts needed to execute the selected feature.
- Verification planning preserves the feature's complete acceptance criteria and repository-configured gates, then identifies the focused implementation-loop checks.
- Neither process scores a feature or changes its execution profile.

## Executable profile configuration

The executable authority is deterministic and ordered:

1. Deterministic zero-model route.
2. Explicit user or run profile override.
3. Selected feature `execution_policy` or its controller-owned reconciled feature policy.
4. Validated workflow fallback for a historical feature without policy metadata.
5. Evidence-based escalation permitted by the selected policy.

The named profiles are:

| Profile | Model | Reasoning | Governing use |
| --- | --- | --- | --- |
| `mechanical` | `gpt-5.6-luna` | medium | deterministic-adjacent metadata or formatting requiring semantic transformation |
| `repository_aware` | `gpt-5.6-luna` | high | repository discovery, classification, and plan preparation |
| `bounded_precise` | `gpt-5.6-terra` | medium | one bounded, precisely specified implementation path |
| `multi_module_precise` | `gpt-5.6-terra` | high | several known modules under a complete contract |
| `application_feature_implementation` | `gpt-5.6-terra` | medium | compatibility name for a bounded application implementation |
| `generic_or_architectural` | `gpt-5.6-sol` | medium | reusable or generic behavior, or architecture that must be determined |
| `ambiguous_or_authoritative` | `gpt-5.6-sol` | high | material ownership or authority ambiguity |
| `unresolved_after_high` | `gpt-5.6-sol` | xhigh | unresolved reasoning after complete high-reasoning evidence |

Each newly readied feature must declare:

```json
{
  "execution_policy": {
    "profile": "multi_module_precise",
    "parent_sessions": 1,
    "child_sessions": 0,
    "escalation": {
      "trigger": "material_architecture_or_authority_ambiguity",
      "profile": "generic_or_architectural"
    }
  }
}
```

`escalation` is optional. Historical ready entries may use a validated workflow fallback. The controller does not mass-edit historical feature metadata.

The resolved profile, source, model, reasoning, parent and child budgets, escalation trigger, and escalation target are inspectable in dry-run. The resolved model and reasoning are passed explicitly to Codex argv. Parent and child budgets fail closed at the launcher boundary; a zero-child feature launch mechanically removes collaboration tools.

Child sessions default to zero and the initial maximum is one. A positive
budget requires a strictly cheaper configured child, an independently bounded
task, materially smaller expected input context, a compact output contract,
exclusive file ownership or read-only scope, and a concrete cost-saving
justification. Delegation is not assumed to save usage merely because a child
exists.

Every Conveyor model launch also receives a workflow-specific capability
allowlist and compact terminal report contract. The pinned CLI must prove the
effective model-visible skill set after session-only skill, plugin, and MCP
overrides. Prompt-only isolation is not enforcement; an unprovable allowlist
blocks before model launch.

## Escalation guidance

Escalation requires a recorded evidence object whose trigger exactly matches the feature policy, identifies a stable evidence record, and confirms that the focused context is complete. An explicit user/run override remains authoritative.

Do not escalate when:

- required files or context were missing;
- an environment, toolchain, permission, or test-infrastructure failure occurred;
- a focused test exposed a localized omission;
- the only signal is feature wording such as persistence, migration, recovery, lifecycle, or concurrency;
- the proposed run merely repeats a failed attempt without new evidence.

Repair missing context or environment failures at the same profile. Repair localized omissions at the same profile. Escalate from Terra to Sol only when recorded evidence shows material architecture or authority ambiguity. Use `unresolved_after_high` only after complete evidence shows that high reasoning remained genuinely inconclusive.

## Non-executable guidance

Decision trees, scoring matrices, complexity arithmetic, expected-usage formulas, examples, and two-choice comparisons are explanatory tools for people preparing feature metadata. They are not executable controller logic. The controller does not calculate per-run complexity or uncertainty scores, infer escalation from keywords, or override explicit feature metadata with a broad automatic selector.
