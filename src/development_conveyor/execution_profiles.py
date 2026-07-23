"""Validated feature-owned execution-profile resolution.

Profile selection is deliberately independent from context discovery and
verification planning.  It consumes explicit configuration and recorded
evidence only; feature wording is never a routing signal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import ConfigurationError, QueueError


PROFILE_NAMES = (
    "mechanical",
    "repository_aware",
    "bounded_precise",
    "multi_module_precise",
    "generic_or_architectural",
    "ambiguous_or_authoritative",
    "unresolved_after_high",
)

DEFAULT_PROFILES: dict[str, dict[str, str]] = {
    "mechanical": {"model": "gpt-5.6-luna", "reasoning": "medium"},
    "repository_aware": {"model": "gpt-5.6-luna", "reasoning": "high"},
    "bounded_precise": {"model": "gpt-5.6-terra", "reasoning": "medium"},
    "multi_module_precise": {"model": "gpt-5.6-terra", "reasoning": "high"},
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
