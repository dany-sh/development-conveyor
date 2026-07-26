"""Validated feature-owned execution-profile resolution.

Profile selection is deliberately independent from context discovery and
verification planning.  It consumes explicit configuration and recorded
evidence only; feature wording is never a routing signal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .errors import ConfigurationError, QueueError


PROFILE_NAMES = (
    "mechanical",
    "repository_aware",
    "bounded_precise",
    "multi_module_precise",
    "application_feature_implementation",
    "generic_or_architectural",
    "ambiguous_or_authoritative",
    "unresolved_after_high",
)

DEFAULT_PROFILES: dict[str, dict[str, str]] = {
    "mechanical": {"model": "gpt-5.6-luna", "reasoning": "medium"},
    "repository_aware": {"model": "gpt-5.6-luna", "reasoning": "high"},
    "bounded_precise": {"model": "gpt-5.6-terra", "reasoning": "medium"},
    "multi_module_precise": {"model": "gpt-5.6-terra", "reasoning": "high"},
    "application_feature_implementation": {
        "model": "gpt-5.3-codex",
        "reasoning": "high",
    },
    "generic_or_architectural": {"model": "gpt-5.6-sol", "reasoning": "medium"},
    "ambiguous_or_authoritative": {"model": "gpt-5.6-sol", "reasoning": "high"},
    "unresolved_after_high": {"model": "gpt-5.6-sol", "reasoning": "xhigh"},
}

DEFAULT_WORKFLOW_FALLBACKS = {
    "queue_reconciliation": "repository_aware",
    "application_feature": "generic_or_architectural",
    "controller_repair": "generic_or_architectural",
    "metadata": "mechanical",
}


@dataclass(frozen=True)
class FeatureExecutionPolicy:
    profile: str
    parent_sessions: int
    child_sessions: int
    escalation_trigger: str | None = None
    escalation_profile: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "profile": self.profile,
            "parent_sessions": self.parent_sessions,
            "child_sessions": self.child_sessions,
        }
        if self.escalation_trigger is not None and self.escalation_profile is not None:
            value["escalation"] = {
                "trigger": self.escalation_trigger,
                "profile": self.escalation_profile,
            }
        return value


@dataclass(frozen=True)
class ResolvedExecutionProfile:
    profile: str | None
    resolution_source: str
    model: str | None
    reasoning: str | None
    parent_sessions: int
    child_sessions: int
    escalation_trigger: str | None
    escalation_profile: str | None
    escalated: bool = False
    escalation_evidence_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "resolution_source": self.resolution_source,
            "model": self.model,
            "reasoning": self.reasoning,
            "parent_sessions": self.parent_sessions,
            "child_sessions": self.child_sessions,
            "escalation_trigger": self.escalation_trigger,
            "escalation_profile": self.escalation_profile,
            "escalated": self.escalated,
            "escalation_evidence_id": self.escalation_evidence_id,
        }


def validate_feature_execution_policy(
    value: Any,
    *,
    path: str = "$.execution_policy",
    error_type: type[Exception] = QueueError,
) -> FeatureExecutionPolicy:
    if not isinstance(value, dict):
        raise error_type(f"{path}: expected an object")
    allowed = {"profile", "parent_sessions", "child_sessions", "escalation"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise error_type(f"{path}: unsupported keys: {', '.join(unknown)}")
    profile = value.get("profile")
    if profile not in PROFILE_NAMES:
        raise error_type(f"{path}.profile: unsupported profile {profile!r}")
    parent = value.get("parent_sessions")
    child = value.get("child_sessions")
    if not isinstance(parent, int) or isinstance(parent, bool) or parent < 1:
        raise error_type(f"{path}.parent_sessions: expected an integer of at least 1")
    if not isinstance(child, int) or isinstance(child, bool) or child < 0:
        raise error_type(f"{path}.child_sessions: expected a non-negative integer")
    trigger = None
    escalation_profile = None
    escalation = value.get("escalation")
    if escalation is not None:
        if not isinstance(escalation, dict) or set(escalation) != {"trigger", "profile"}:
            raise error_type(f"{path}.escalation: expected exactly trigger and profile")
        trigger = escalation.get("trigger")
        escalation_profile = escalation.get("profile")
        if not isinstance(trigger, str) or not trigger.strip():
            raise error_type(f"{path}.escalation.trigger: expected a non-empty string")
        if escalation_profile not in PROFILE_NAMES:
            raise error_type(
                f"{path}.escalation.profile: unsupported profile {escalation_profile!r}"
            )
        if escalation_profile == profile:
            raise error_type(f"{path}.escalation.profile: must change the profile")
    return FeatureExecutionPolicy(
        profile=profile,
        parent_sessions=parent,
        child_sessions=child,
        escalation_trigger=trigger.strip() if isinstance(trigger, str) else None,
        escalation_profile=escalation_profile,
    )


def validate_execution_profile_configuration(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError("execution profile configuration must be an object")
    profiles = value.get("profiles")
    if not isinstance(profiles, dict) or set(profiles) != set(PROFILE_NAMES):
        raise ConfigurationError("execution profile configuration must define every named profile exactly once")
    for name, expected in DEFAULT_PROFILES.items():
        configured = profiles.get(name)
        if not isinstance(configured, dict) or set(configured) != {"model", "reasoning"}:
            raise ConfigurationError(f"execution profile {name} must define exactly model and reasoning")
        if configured != expected:
            raise ConfigurationError(
                f"execution profile {name} must resolve to {expected['model']}/{expected['reasoning']}"
            )
    fallbacks = value.get("workflow_fallbacks")
    if not isinstance(fallbacks, dict):
        raise ConfigurationError("workflow_fallbacks must be an object")
    for workflow, profile in fallbacks.items():
        if not isinstance(workflow, str) or profile not in PROFILE_NAMES:
            raise ConfigurationError(f"workflow fallback {workflow!r} references an unsupported profile")
    policies = value.get("feature_policies", [])
    if not isinstance(policies, list):
        raise ConfigurationError("feature_policies must be an array")
    identities: set[tuple[str, str]] = set()
    for index, item in enumerate(policies):
        if not isinstance(item, dict) or set(item) != {"project_id", "feature_id", "execution_policy"}:
            raise ConfigurationError(
                f"feature_policies[{index}] must define exactly project_id, feature_id, and execution_policy"
            )
        project_id = item.get("project_id")
        feature_id = item.get("feature_id")
        if not isinstance(project_id, str) or not project_id.strip():
            raise ConfigurationError(f"feature_policies[{index}].project_id must be non-empty")
        if not isinstance(feature_id, str) or not feature_id.strip():
            raise ConfigurationError(f"feature_policies[{index}].feature_id must be non-empty")
        identity = (project_id, feature_id)
        if identity in identities:
            raise ConfigurationError(f"duplicate feature execution policy for {project_id}/{feature_id}")
        identities.add(identity)
        validate_feature_execution_policy(
            item.get("execution_policy"),
            path=f"$.feature_policies[{index}].execution_policy",
            error_type=ConfigurationError,
        )
    return value


def _configuration(value: dict[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {
            "profiles": DEFAULT_PROFILES,
            "workflow_fallbacks": DEFAULT_WORKFLOW_FALLBACKS,
            "feature_policies": [],
        }
    return validate_execution_profile_configuration(value)


def _feature_policy(
    feature: dict[str, Any] | None,
    *,
    project_id: str | None,
    configuration: dict[str, Any],
) -> tuple[FeatureExecutionPolicy | None, str | None]:
    embedded = feature.get("execution_policy") if isinstance(feature, dict) else None
    configured = None
    if isinstance(feature, dict) and project_id:
        feature_id = feature.get("id")
        matches = [
            item for item in configuration.get("feature_policies", [])
            if item.get("project_id") == project_id and item.get("feature_id") == feature_id
        ]
        configured = matches[0].get("execution_policy") if matches else None
    if embedded is not None and configured is not None and embedded != configured:
        raise ConfigurationError("queue and controller feature execution policies disagree")
    if embedded is not None:
        return validate_feature_execution_policy(embedded), "selected_feature_profile"
    if configured is not None:
        return (
            validate_feature_execution_policy(
                configured,
                path="$.feature_policies.execution_policy",
                error_type=ConfigurationError,
            ),
            "reconciled_feature_profile",
        )
    return None, None


def _eligible_escalation(
    policy: FeatureExecutionPolicy,
    evidence: dict[str, Any] | None,
) -> tuple[bool, str | None]:
    if not policy.escalation_trigger or not policy.escalation_profile or not isinstance(evidence, dict):
        return False, None
    disqualifying = (
        evidence.get("missing_context") is True
        or evidence.get("environment_failure") is True
        or evidence.get("localized_test_omission") is True
    )
    eligible = (
        evidence.get("recorded") is True
        and evidence.get("trigger") == policy.escalation_trigger
        and evidence.get("context_complete") is True
        and not disqualifying
        and isinstance(evidence.get("evidence_id"), str)
        and bool(evidence.get("evidence_id").strip())
    )
    return eligible, evidence.get("evidence_id") if eligible else None


def resolve_execution_profile(
    *,
    workflow: str,
    deterministic: bool,
    feature: dict[str, Any] | None = None,
    project_id: str | None = None,
    configuration: dict[str, Any] | None = None,
    override_profile: str | None = None,
    escalation_evidence: dict[str, Any] | None = None,
) -> ResolvedExecutionProfile:
    configured = _configuration(configuration)
    if deterministic:
        return ResolvedExecutionProfile(None, "deterministic_zero_model", None, None, 0, 0, None, None)
    feature_policy, feature_source = _feature_policy(
        feature, project_id=project_id, configuration=configured
    )
    if override_profile is not None:
        if override_profile not in PROFILE_NAMES:
            raise ConfigurationError(f"unsupported explicit execution profile: {override_profile}")
        policy = FeatureExecutionPolicy(
            override_profile,
            feature_policy.parent_sessions if feature_policy else 1,
            feature_policy.child_sessions if feature_policy else 0,
            feature_policy.escalation_trigger if feature_policy else None,
            feature_policy.escalation_profile if feature_policy else None,
        )
        source = "explicit_run_override"
    elif feature_policy is not None:
        policy = feature_policy
        source = str(feature_source)
    else:
        fallback = configured.get("workflow_fallbacks", {}).get(workflow)
        if fallback not in PROFILE_NAMES:
            raise ConfigurationError(f"workflow {workflow!r} has no validated execution-profile fallback")
        policy = FeatureExecutionPolicy(fallback, 1, 0)
        source = "workflow_fallback"

    escalated, evidence_id = _eligible_escalation(policy, escalation_evidence)
    # An explicit human/run choice is authoritative over automatic escalation.
    if source == "explicit_run_override":
        escalated, evidence_id = False, None
    resolved_profile = policy.escalation_profile if escalated else policy.profile
    profile = configured["profiles"][resolved_profile]
    return ResolvedExecutionProfile(
        profile=resolved_profile,
        resolution_source="evidence_based_escalation" if escalated else source,
        model=profile["model"],
        reasoning=profile["reasoning"],
        parent_sessions=policy.parent_sessions,
        child_sessions=policy.child_sessions,
        escalation_trigger=policy.escalation_trigger,
        escalation_profile=policy.escalation_profile,
        escalated=escalated,
        escalation_evidence_id=evidence_id,
    )


_YAML_FENCE = re.compile(
    r"(?ms)^```(?:yaml|yml)[ \t]*\r?\n(?P<body>.*?)^```[ \t]*\r?$"
)


def markdown_execution_policy(
    feature_id: str,
    text: str,
    *,
    required: bool,
) -> dict[str, Any] | None:
    """Read one bounded fenced-YAML execution policy from a feature contract."""

    blocks: list[tuple[list[str], int, int]] = []
    for fence in _YAML_FENCE.finditer(text):
        lines = fence.group("body").splitlines()
        for index, line in enumerate(lines):
            match = re.fullmatch(r"(?P<indent> *)execution_policy:[ \t]*", line)
            if match is not None:
                blocks.append((lines, index, len(match.group("indent"))))
    if not blocks and not required:
        return None
    if len(blocks) != 1:
        raise QueueError(
            f"feature contract must contain exactly one fenced-YAML "
            f"execution_policy block for {feature_id}"
        )

    lines, start, base_indent = blocks[0]
    policy: dict[str, Any] = {}
    escalation: dict[str, Any] | None = None
    for line in lines[start + 1:]:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= base_indent:
            break
        if "\t" in line[:indent] or indent not in {base_indent + 2, base_indent + 4}:
            raise QueueError(
                f"feature contract contains malformed execution_policy YAML "
                f"for {feature_id}"
            )
        key_value = re.fullmatch(r" *([a-z_]+):[ \t]*(.*?)[ \t]*", line)
        if key_value is None:
            raise QueueError(
                f"feature contract contains malformed execution_policy YAML "
                f"for {feature_id}"
            )
        key, value = key_value.groups()
        if indent == base_indent + 2:
            if key == "escalation" and value == "":
                if "escalation" in policy:
                    raise QueueError(
                        f"feature contract contains duplicate execution_policy "
                        f"keys for {feature_id}"
                    )
                escalation = {}
                policy["escalation"] = escalation
            elif key in {"profile", "parent_sessions", "child_sessions"}:
                if key in policy:
                    raise QueueError(
                        f"feature contract contains duplicate execution_policy "
                        f"keys for {feature_id}"
                    )
                policy[key] = (
                    int(value)
                    if key.endswith("_sessions") and value.isdigit()
                    else value
                )
            else:
                raise QueueError(
                    f"feature contract contains unsupported execution_policy "
                    f"YAML for {feature_id}"
                )
        elif escalation is None or key not in {"trigger", "profile"}:
            raise QueueError(
                f"feature contract contains malformed execution_policy "
                f"escalation for {feature_id}"
            )
        else:
            if key in escalation:
                raise QueueError(
                    f"feature contract contains duplicate execution_policy "
                    f"escalation keys for {feature_id}"
                )
            escalation[key] = value

    validate_feature_execution_policy(
        policy,
        path=f"feature {feature_id}.spec.execution_policy",
    )
    return policy


def render_markdown_execution_policy(value: dict[str, Any]) -> str:
    """Render the canonical feature-contract execution-policy section."""

    policy = validate_feature_execution_policy(value)
    lines = [
        "## Execution policy",
        "",
        "```yaml",
        "execution_policy:",
        f"  profile: {policy.profile}",
        f"  parent_sessions: {policy.parent_sessions}",
        f"  child_sessions: {policy.child_sessions}",
    ]
    if policy.escalation_trigger is not None and policy.escalation_profile is not None:
        lines.extend([
            "  escalation:",
            f"    trigger: {policy.escalation_trigger}",
            f"    profile: {policy.escalation_profile}",
        ])
    lines.extend(["```", ""])
    return "\n".join(lines)


def resolve_feature_execution_policy(
    *,
    feature: dict[str, Any],
    project_id: str,
    specification_policy: dict[str, Any] | None,
    configuration: dict[str, Any] | None,
) -> dict[str, Any]:
    """Resolve one feature policy through the normal application-feature route.

    Explicit valid queue/spec metadata is preserved. When both are absent, the
    same canonical workflow fallback used by a future feature dry-run supplies
    the required executable policy.
    """

    feature_id = feature.get("id")
    if not isinstance(feature_id, str) or not feature_id:
        raise ConfigurationError("feature execution-policy resolution requires a feature ID")
    queue_policy = feature.get("execution_policy")
    if queue_policy is not None:
        queue_policy = validate_feature_execution_policy(
            queue_policy,
            path=f"feature {feature_id}.execution_policy",
        ).to_dict()
    if specification_policy is not None:
        specification_policy = validate_feature_execution_policy(
            specification_policy,
            path=f"feature {feature_id}.spec.execution_policy",
        ).to_dict()
    if (
        queue_policy is not None
        and specification_policy is not None
        and queue_policy != specification_policy
    ):
        raise ConfigurationError(
            f"queue and feature specification execution policies disagree for {feature_id}"
        )

    explicit_policy = queue_policy or specification_policy
    candidate = dict(feature)
    if explicit_policy is not None:
        candidate["execution_policy"] = explicit_policy
    resolved = resolve_execution_profile(
        workflow="application_feature",
        deterministic=False,
        feature=candidate,
        project_id=project_id,
        configuration=configuration,
    )
    if (
        resolved.profile is None
        or resolved.model is None
        or resolved.reasoning is None
        or resolved.parent_sessions < 1
        or resolved.child_sessions < 0
        or resolved.resolution_source
        not in {
            "selected_feature_profile",
            "reconciled_feature_profile",
            "workflow_fallback",
        }
    ):
        raise ConfigurationError(
            f"feature {feature_id} does not resolve to one unambiguous executable policy"
        )

    execution_policy = (
        explicit_policy
        or FeatureExecutionPolicy(
            profile=resolved.profile,
            parent_sessions=resolved.parent_sessions,
            child_sessions=resolved.child_sessions,
            escalation_trigger=resolved.escalation_trigger,
            escalation_profile=resolved.escalation_profile,
        ).to_dict()
    )
    validate_feature_execution_policy(
        execution_policy,
        path=f"feature {feature_id}.execution_policy",
    )
    return {
        "feature_id": feature_id,
        "execution_policy": execution_policy,
        "resolved_execution_profile": resolved.to_dict(),
        "policy_source": resolved.resolution_source,
        "explicit_policy_preserved": explicit_policy is not None,
    }
