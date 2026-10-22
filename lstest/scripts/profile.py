"""Versioned project profile loading and command policy helpers."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

try:
    from .core import sha256_file
except ImportError:  # direct execution fallback
    from core import sha256_file


class ProfileError(ValueError):
    pass


EVENT_REGISTRY: Mapping[str, Mapping[str, str]] = {
    # These are test-flow categories. They deliberately do not name project
    # markers, regex groups, protocol fields, or any business value.
    "wake_result": {"key": "WAKE", "tag": "DEVICE", "label": "唤醒"},
    "offline_result": {"key": "OFFLINE_ASR", "tag": "DEVICE", "label": "离线"},
    "online_request": {"key": "REQUEST_ID", "tag": "ONLINE", "label": "在线请求"},
    "online_response": {"key": "RESPONSE_ID", "tag": "ONLINE", "label": "在线响应"},
    "online_result": {"key": "ONLINE_ASR", "tag": "ONLINE", "label": "在线"},
    "online_intent": {"key": "ONLINE_INTENT", "tag": "ONLINE", "label": "在线意图"},
    "player_url": {"key": "PLAY_URL", "tag": "PLAYER", "label": "播放地址"},
    "player_id": {"key": "DEVICE_BROADCAST_ID", "tag": "PLAYER", "label": "设备播报"},
    "player_state": {"key": "PLAYER", "tag": "PLAYER", "label": "播放器"},
    "player_control_action": {"key": "PLAYER_CONTROL", "tag": "PLAYER", "label": "播放控制"},
    "initialization_ready": {"key": "INIT_READY", "tag": "SYSTEM", "label": "初始化"},
    "restart": {"key": "RESTART", "tag": "SYSTEM", "label": "重启"},
    "command_ack": {"key": "COMMAND_ACK", "tag": "COMMAND", "label": "命令回执"},
    "command_evidence": {"key": "COMMAND_EVIDENCE", "tag": "COMMAND", "label": "命令旁证"},
    "device_exception": {"key": "DEVICE_EXCEPTION", "tag": "ERROR", "label": "设备异常"},
}

PLAYER_STATE_CLASSES = frozenset({"preparation", "active", "terminal", "error", "unknown"})
MIRROR_POLICIES = frozenset({"distinct", "mirror", "reject_ambiguous"})


@dataclass(frozen=True)
class RawLogRecord:
    """One source record submitted by a project adapter.

    The adapter owns collection only.  Project interpretation lives in the
    profile rule, while rendering and case accounting remain in lstest.
    """

    text: str
    source: str = ""
    port: str = ""
    role: str = ""
    cursor: int | None = None
    observed_at: str = ""
    sequence: int = 0
    epoch: int = 0
    phase: str = ""
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProfileFact:
    """A profile-extracted fact whose display text is still device-native."""

    event_type: str
    key: str
    tag: str
    presentation_value: str
    display_fields: tuple[tuple[str, str], ...]
    captures: Mapping[str, str]
    source: str
    port: str
    role: str
    phase: str
    identity: str = ""
    mirror_policy: str = "distinct"
    state_class: str = ""
    render_policy: str = "all"
    evidence: tuple[str, ...] = ()
    rule_id: str = ""
    sequence: int = 0
    epoch: int = 0
    profile_version: int = 0


@dataclass(frozen=True)
class FixtureResult:
    name: str
    passed: bool
    detail: str = ""


class ProfileFixtureRunner:
    """Replay profile-owned fixtures without any device or project semantics."""

    def __init__(self, profile: "DeviceProfile") -> None:
        self.profile = profile

    @staticmethod
    def _sample(value: Any) -> Mapping[str, Any]:
        return dict(value) if isinstance(value, Mapping) else {"text": str(value)}

    def run(self) -> list[FixtureResult]:
        results: list[FixtureResult] = []
        for rule in self.profile.event_rules:
            rule_id = str(rule["rule_id"])
            fixtures = rule.get("fixtures", {})
            for kind, expected in (("positive", True), ("negative", False)):
                for index, sample in enumerate(fixtures.get(kind, ())):
                    item = self._sample(sample)
                    record = RawLogRecord(
                        text=str(item.get("text", "")), source=str(item.get("source", "")),
                        port=str(item.get("port", "")), role=str(item.get("role", "")),
                        phase=str(item.get("phase", "")),
                    )
                    facts = [fact for fact in self.profile.extract_facts(record) if fact.rule_id == rule_id]
                    passed = bool(facts) is expected
                    detail = ""
                    if passed and expected and item.get("presentation") is not None:
                        passed = facts[0].presentation_value == str(item["presentation"])
                        detail = "展示捕获不一致" if not passed else ""
                    results.append(FixtureResult(f"{rule_id}:{kind}:{index}", passed, detail))
            for index, chunks in enumerate(fixtures.get("segmented", ())):
                joined = "".join(str(chunk) for chunk in chunks)
                facts = [fact for fact in self.profile.extract_facts(RawLogRecord(text=joined)) if fact.rule_id == rule_id]
                results.append(FixtureResult(f"{rule_id}:segmented:{index}", bool(facts), "拆包拼接未命中" if not facts else ""))
        for index, safety in enumerate(self.profile.safety_stop):
            rule_id = str(safety.get("event_rule_id") or "")
            fixtures = safety.get("fixtures", {})
            for kind, expected in (("positive", True), ("negative", False)):
                for sample_index, sample in enumerate(fixtures.get(kind, ())):
                    item = self._sample(sample)
                    facts = [fact for fact in self.profile.extract_facts(RawLogRecord(text=str(item.get("text", "")))) if fact.rule_id == rule_id]
                    results.append(FixtureResult(
                        f"safety:{index}:{kind}:{sample_index}", bool(facts) is expected,
                        "安全停止触发 fixture 不符合预期",
                    ))
        return results

    def assert_valid(self) -> None:
        failed = [item for item in self.run() if not item.passed]
        if failed:
            raise ProfileError("fixture failed: " + ", ".join(f"{item.name} {item.detail}".strip() for item in failed))


class ProfileValidator:
    """Public formal-profile admission point used before pressure execution."""

    @staticmethod
    def validate(profile: "DeviceProfile", required_event_types: Iterable[str] = ()) -> None:
        profile.assert_formal_ready(required_event_types)


TOOL_LOG_FACT_KEYS = {
    "WAKE", "OFFLINE_TONE", "OFFLINE_ASR", "OFFLINE_INTENT", "OFFLINE_TEXT",
    "OFFLINE_NORMALIZED", "ONLINE_ASR", "REQUEST_ID", "RESPONSE_ID",
    "ASR_LATENCY_MS", "PLAY_URL", "AUDIO_FILE", "BROADCAST_ID", "PLAYER",
    "INIT_WAIT", "INIT_READY", "INIT_STABLE", "RESTART", "LOG_LEVEL", "COMMAND",
}


class MarkerGate:
    """Profile-scoped marker matcher with scope and debounce protection."""

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}

    @staticmethod
    def _items(value: Any) -> set[str]:
        if value is None or value == "":
            return set()
        return {str(item) for item in (value if isinstance(value, (list, tuple, set)) else [value])}

    def match(
        self,
        rule: str | Mapping[str, Any],
        text: str,
        *,
        port: str | None = None,
        role: str | None = None,
        phase: str | None = None,
        correlation_id: str | None = None,
        monotonic_seconds: float | None = None,
    ) -> tuple[bool, str]:
        if isinstance(rule, str):
            matched = bool(re.search(rule, text, flags=re.IGNORECASE))
            return matched, "matched" if matched else "pattern_not_matched"
        if not isinstance(rule, Mapping):
            return False, "invalid_rule"
        pattern = str(rule.get("pattern") or rule.get("regex") or "")
        if not pattern:
            return False, "pattern_missing"
        if not re.search(pattern, text, flags=re.IGNORECASE):
            return False, "pattern_not_matched"
        if self._items(rule.get("ports")) and str(port or "") not in self._items(rule.get("ports")):
            return False, "port_scope_rejected"
        if self._items(rule.get("roles")) and str(role or "") not in self._items(rule.get("roles")):
            return False, "role_scope_rejected"
        if self._items(rule.get("phases")) and str(phase or "") not in self._items(rule.get("phases")):
            return False, "phase_scope_rejected"
        required_id = str(rule.get("correlation_id") or "")
        if required_id and required_id != str(correlation_id or ""):
            return False, "correlation_scope_rejected"
        if any(re.search(str(item), text, flags=re.IGNORECASE) for item in rule.get("negative_patterns", [])):
            return False, "negative_pattern_matched"
        debounce_ms = max(0, int(rule.get("debounce_ms", 0) or 0))
        if debounce_ms and monotonic_seconds is not None:
            fingerprint = str(rule.get("fingerprint") or f"{pattern}|{port}|{role}|{correlation_id}|{text}")
            now = monotonic_seconds
            previous = self._seen.get(fingerprint)
            self._seen[fingerprint] = now
            # A predicate and its subsequent event filter commonly inspect the
            # same captured serial line.  Do not let that second inspection
            # consume the marker as a new debounce event.
            if previous == now:
                return True, "matched_same_event"
            if previous is not None and (now - previous) * 1000 < debounce_ms:
                return False, "debounced"
        return True, "matched"


def _rule_pattern(rule: str | Mapping[str, Any]) -> str:
    if isinstance(rule, str):
        return rule
    if isinstance(rule, Mapping):
        return str(rule.get("pattern") or rule.get("regex") or "")
    return ""


@dataclass(frozen=True)
class DeviceProfile:
    profile_id: str
    schema_version: int
    source: Path
    payload: Mapping[str, Any]
    sha256: str
    marker_gate: MarkerGate

    @classmethod
    def load(cls, path: Path) -> "DeviceProfile":
        path = Path(path).resolve()
        if not path.is_file():
            raise ProfileError(f"profile not found: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ProfileError(f"invalid profile: {error}") from error
        if not isinstance(payload, Mapping):
            raise ProfileError("profile root must be an object")
        profile_id = str(payload.get("profile_id") or payload.get("device_id") or "").strip()
        if not profile_id:
            raise ProfileError("profile_id is required")
        schema_version = int(payload.get("schema_version", 0))
        if schema_version <= 0:
            raise ProfileError("schema_version must be positive")
        if not isinstance(payload.get("ports", []), list):
            raise ProfileError("ports must be a list")
        if not isinstance(payload.get("commands", []), list):
            raise ProfileError("commands must be a list")
        if "wake_words" in payload and not isinstance(payload["wake_words"], list):
            raise ProfileError("wake_words must be a list")
        raw_observations = payload.get("observations", {}) or {}
        if not isinstance(raw_observations, Mapping):
            raise ProfileError("observations must be an object")
        for category, value in raw_observations.items():
            if not isinstance(value, Mapping):
                continue
            fact_map = value.get("fact_map", {})
            if not isinstance(fact_map, Mapping):
                raise ProfileError(f"observations.{category}.fact_map must be an object")
            for fact_key in fact_map.values():
                if str(fact_key).upper() not in TOOL_LOG_FACT_KEYS:
                    raise ProfileError(f"unsupported fact_map key: {fact_key}")
        for item in payload.get("commands", []):
            if not isinstance(item, Mapping) or not str(item.get("command", "")).strip():
                raise ProfileError("each command rule requires command")
            for name in ("success_patterns", "evidence_patterns"):
                if name in item and not isinstance(item[name], list):
                    raise ProfileError(f"command {name} must be a list")
            for name in ("retries",):
                if name in item and (not isinstance(item[name], int) or item[name] < 0):
                    raise ProfileError(f"command {name} must be a non-negative integer")
            for name in ("timeout_s", "evidence_timeout_s", "retry_delay_s"):
                if name in item:
                    try:
                        if float(item[name]) <= 0:
                            raise ValueError
                    except (TypeError, ValueError) as error:
                        raise ProfileError(f"command {name} must be positive") from error
            if item.get("safe_init", True) and not item.get("success_patterns") and not item.get("evidence_patterns"):
                raise ProfileError("safe initialization command requires success_patterns or evidence_patterns")
        for field in ("correlation", "player_markers", "observations", "recovery", "health_policy", "marker_fixtures"):
            if field in payload and not isinstance(payload[field], Mapping):
                raise ProfileError(f"{field} must be an object")
        recovery = payload.get("recovery", {})
        if isinstance(recovery, Mapping):
            if "stop_on_failure" in recovery and not isinstance(recovery["stop_on_failure"], bool):
                raise ProfileError("recovery stop_on_failure must be boolean")
            for name in ("initialization_timeout_s", "restart_poll_interval_s", "stable_for_s"):
                if name in recovery:
                    try:
                        if float(recovery[name]) <= 0:
                            raise ValueError
                    except (TypeError, ValueError) as error:
                        raise ProfileError(f"recovery {name} must be positive") from error
        wake_word_ids: set[str] = set()
        for item in payload.get("wake_words", []):
            if not isinstance(item, Mapping):
                raise ProfileError("each wake_words item must be an object")
            wake_word_id = str(item.get("wake_word_id") or "").strip()
            spoken_text = str(item.get("spoken_text") or "").strip()
            expected_raw = item.get("expected_raw")
            if not wake_word_id or not spoken_text or not isinstance(expected_raw, Mapping) or not expected_raw:
                raise ProfileError("each wake_words item requires wake_word_id, spoken_text, and expected_raw")
            if wake_word_id in wake_word_ids:
                raise ProfileError(f"duplicate wake_word_id: {wake_word_id}")
            wake_word_ids.add(wake_word_id)
        cls._validate_marker_rules(payload)
        cls._validate_event_rules(payload)
        return cls(profile_id, schema_version, path, payload, sha256_file(path), MarkerGate())

    @staticmethod
    def _validate_event_rules(payload: Mapping[str, Any]) -> None:
        rules = payload.get("event_rules", [])
        if rules is None:
            rules = []
        if not isinstance(rules, list):
            raise ProfileError("event_rules must be a list")
        formal = str(payload.get("contract_mode") or "").lower() == "formal"
        if formal and not rules:
            raise ProfileError("formal profile requires event_rules")
        ids: set[str] = set()
        for index, rule in enumerate(rules):
            location = f"event_rules[{index}]"
            if not isinstance(rule, Mapping):
                raise ProfileError(f"{location} must be an object")
            event_type = str(rule.get("event_type") or "").strip()
            if event_type not in EVENT_REGISTRY:
                raise ProfileError(f"{location} has unsupported event_type: {event_type}")
            rule_id = str(rule.get("rule_id") or "").strip()
            if not rule_id:
                raise ProfileError(f"{location} requires rule_id")
            if rule_id in ids:
                raise ProfileError(f"duplicate event rule_id: {rule_id}")
            ids.add(rule_id)
            pattern = str(rule.get("regex") or rule.get("pattern") or "")
            if not pattern:
                raise ProfileError(f"{location} requires regex")
            try:
                compiled = re.compile(pattern, flags=re.IGNORECASE)
            except re.error as error:
                raise ProfileError(f"invalid event regex at {location}: {error}") from error
            if not compiled.groupindex:
                raise ProfileError(f"{location} regex requires named capture groups")
            presentation_capture = str(rule.get("presentation_capture") or "").strip()
            if presentation_capture not in compiled.groupindex:
                raise ProfileError(f"{location} presentation_capture must name a regex capture")
            display_captures = rule.get("display_captures", [])
            if display_captures in (None, ""):
                display_captures = []
            if not isinstance(display_captures, list):
                raise ProfileError(f"{location}.display_captures must be a list")
            display_tags: set[str] = set()
            for display_index, display in enumerate(display_captures):
                display_location = f"{location}.display_captures[{display_index}]"
                if not isinstance(display, Mapping):
                    raise ProfileError(f"{display_location} must be an object")
                tag_name = str(display.get("tag_name") or "").strip()
                capture = str(display.get("capture") or "").strip()
                if not tag_name or not capture:
                    raise ProfileError(f"{display_location} requires tag_name and capture")
                if capture not in compiled.groupindex:
                    raise ProfileError(f"{display_location} capture must name a regex capture")
                normalized_tag = tag_name.casefold()
                if normalized_tag in display_tags:
                    raise ProfileError(f"{display_location} duplicates display tag: {tag_name}")
                display_tags.add(normalized_tag)
            source = rule.get("sources", {})
            if source and not isinstance(source, Mapping):
                raise ProfileError(f"{location}.sources must be an object")
            if formal and not source:
                raise ProfileError(f"{location} formal rule requires sources")
            stage = str(rule.get("stage") or "").strip()
            if formal and not stage:
                raise ProfileError(f"{location} formal rule requires stage")
            correlation = rule.get("correlation", {})
            if correlation and not isinstance(correlation, Mapping):
                raise ProfileError(f"{location}.correlation must be an object")
            identity_capture = str(correlation.get("identity_capture") or "") if isinstance(correlation, Mapping) else ""
            if identity_capture and identity_capture not in compiled.groupindex:
                raise ProfileError(f"{location} correlation.identity_capture must name a regex capture")
            policy = str(rule.get("mirror_policy") or "distinct").lower()
            if policy not in MIRROR_POLICIES:
                raise ProfileError(f"{location} has invalid mirror_policy: {policy}")
            if formal and "required_for" not in rule:
                raise ProfileError(f"{location} formal rule requires required_for")
            if formal and "empty_placeholder" not in rule:
                raise ProfileError(f"{location} formal rule requires empty_placeholder")
            if event_type == "player_state":
                state_class = str(rule.get("state_class") or "").lower()
                if state_class not in PLAYER_STATE_CLASSES:
                    raise ProfileError(f"{location} player_state requires a valid state_class")
                policy_name = str(rule.get("render_policy") or "").lower()
                if policy_name not in {"all", "first_per_state", "terminal_and_error"}:
                    raise ProfileError(f"{location} has invalid player render_policy")
            fixtures = rule.get("fixtures")
            if formal and not isinstance(fixtures, Mapping):
                raise ProfileError(f"{location} formal rule requires fixtures")
            if isinstance(fixtures, Mapping):
                positive = fixtures.get("positive", [])
                negative = fixtures.get("negative", [])
                if formal and (not positive or not negative):
                    raise ProfileError(f"{location} formal rule requires positive and negative fixtures")
                for sample in positive:
                    text = str(sample.get("text", "")) if isinstance(sample, Mapping) else str(sample)
                    if not compiled.search(text):
                        raise ProfileError(f"{location} positive fixture does not match")
                for sample in negative:
                    text = str(sample.get("text", "")) if isinstance(sample, Mapping) else str(sample)
                    if compiled.search(text):
                        raise ProfileError(f"{location} negative fixture matches")
                for sample in positive:
                    if not isinstance(sample, Mapping) or not isinstance(sample.get("display"), Mapping):
                        continue
                    match = compiled.search(str(sample.get("text", "")))
                    if match is None:
                        continue
                    captures = {name: str(value or "") for name, value in match.groupdict().items()}
                    for display in display_captures:
                        tag_name = str(display.get("tag_name"))
                        capture = str(display.get("capture"))
                        if tag_name in sample["display"] and str(sample["display"][tag_name]) != captures[capture]:
                            raise ProfileError(f"{location} positive fixture display does not match: {tag_name}")
        safety = payload.get("safety_stop", [])
        if safety and not isinstance(safety, list):
            raise ProfileError("safety_stop must be a list")
        for index, item in enumerate(safety or []):
            if not isinstance(item, Mapping):
                raise ProfileError(f"safety_stop[{index}] must be an object")
            trigger = str(item.get("event_rule_id") or "").strip()
            reason = str(item.get("reason") or "").strip()
            fixtures = item.get("fixtures")
            if not trigger or trigger not in ids or not reason or not isinstance(fixtures, Mapping):
                raise ProfileError(f"safety_stop[{index}] requires event_rule_id, reason, and fixtures")
            if not fixtures.get("positive") or not fixtures.get("negative"):
                raise ProfileError(f"safety_stop[{index}] requires positive and negative fixtures")
            risk = str(item.get("risk_category") or "").lower()
            if risk not in {"device", "data", "person"}:
                raise ProfileError(f"safety_stop[{index}] requires risk_category device/data/person")
            target = next(rule for rule in rules if str(rule.get("rule_id")) == trigger)
            if not bool(target.get("safety_eligible", False)):
                raise ProfileError(f"safety_stop[{index}] trigger rule must explicitly declare safety_eligible")

    @staticmethod
    def _validate_marker_rules(payload: Mapping[str, Any]) -> None:
        def validate(rule: Any, location: str) -> None:
            if isinstance(rule, str):
                try:
                    re.compile(rule, flags=re.IGNORECASE)
                except re.error as error:
                    raise ProfileError(f"invalid marker regex at {location}: {error}") from error
                return
            if not isinstance(rule, Mapping):
                raise ProfileError(f"marker rule at {location} must be a string or object")
            pattern = str(rule.get("pattern") or rule.get("regex") or "")
            if not pattern:
                raise ProfileError(f"marker rule at {location} requires pattern")
            try:
                re.compile(pattern, flags=re.IGNORECASE)
                for negative in rule.get("negative_patterns", []):
                    re.compile(str(negative), flags=re.IGNORECASE)
            except re.error as error:
                raise ProfileError(f"invalid marker regex at {location}: {error}") from error
            if "debounce_ms" in rule and int(rule["debounce_ms"]) < 0:
                raise ProfileError(f"marker debounce_ms at {location} must be non-negative")
            fact_map = rule.get("fact_map", {})
            if fact_map and not isinstance(fact_map, Mapping):
                raise ProfileError(f"marker fact_map at {location} must be an object")
            for fact_key in fact_map.values() if isinstance(fact_map, Mapping) else ():
                if str(fact_key).upper() not in TOOL_LOG_FACT_KEYS:
                    raise ProfileError(f"unsupported fact_map key at {location}: {fact_key}")
            fixtures = rule.get("fixtures", {})
            if fixtures and not isinstance(fixtures, Mapping):
                raise ProfileError(f"marker fixtures at {location} must be an object")
            for sample in fixtures.get("positive", []) if isinstance(fixtures, Mapping) else []:
                if not re.search(pattern, str(sample), flags=re.IGNORECASE):
                    raise ProfileError(f"marker positive fixture does not match at {location}")
            for sample in fixtures.get("negative", []) if isinstance(fixtures, Mapping) else []:
                if re.search(pattern, str(sample), flags=re.IGNORECASE):
                    raise ProfileError(f"marker negative fixture matches at {location}")
            for chunks in fixtures.get("segmented", []) if isinstance(fixtures, Mapping) else []:
                joined = "".join(str(item) for item in chunks)
                if not re.search(pattern, joined, flags=re.IGNORECASE):
                    raise ProfileError(f"marker segmented fixture does not match at {location}")

        for field in ("initialization_patterns", "restart_patterns"):
            for index, rule in enumerate(payload.get(field, [])):
                validate(rule, f"{field}[{index}]")
        player_markers = payload.get("player_markers", {})
        if isinstance(player_markers, Mapping):
            for name, value in player_markers.items():
                rules = value if isinstance(value, list) else [value]
                for index, rule in enumerate(rules):
                    if isinstance(rule, (str, Mapping)):
                        validate(rule, f"player_markers.{name}[{index}]")
        observations = payload.get("observations", {})
        if isinstance(observations, Mapping):
            for category, value in observations.items():
                if not isinstance(value, Mapping):
                    continue
                for name in ("patterns", "final_patterns"):
                    for index, rule in enumerate(value.get(name, [])):
                        validate(rule, f"observations.{category}.{name}[{index}]")

    @property
    def ports(self) -> list[Mapping[str, Any]]:
        return list(self.payload.get("ports", []))

    @property
    def initialization_patterns(self) -> list[Any]:
        return list(self.payload.get("initialization_patterns", []))

    @property
    def restart_patterns(self) -> list[Any]:
        return list(self.payload.get("restart_patterns", []))

    @property
    def correlation(self) -> Mapping[str, Any]:
        value = self.payload.get("correlation", {})
        return value if isinstance(value, Mapping) else {}

    @property
    def player_markers(self) -> Mapping[str, Any]:
        value = self.payload.get("player_markers", {})
        return value if isinstance(value, Mapping) else {}

    @property
    def observations(self) -> Mapping[str, Any]:
        value = self.payload.get("observations", {})
        return value if isinstance(value, Mapping) else {}

    @property
    def is_formal(self) -> bool:
        return str(self.payload.get("contract_mode") or "").lower() == "formal"

    @property
    def event_rules(self) -> list[Mapping[str, Any]]:
        return [dict(item) for item in self.payload.get("event_rules", []) if isinstance(item, Mapping)]

    @property
    def safety_stop(self) -> list[Mapping[str, Any]]:
        return [dict(item) for item in self.payload.get("safety_stop", []) if isinstance(item, Mapping)]

    def assert_formal_ready(self, required_event_types: Iterable[str] = ()) -> None:
        """Reject a formal run before actions when its evidence contract is incomplete."""
        if not self.is_formal:
            raise ProfileError("formal pressure runs require contract_mode=formal")
        available = {str(rule.get("event_type")) for rule in self.event_rules}
        missing = {str(item) for item in required_event_types if str(item)} - available
        if missing:
            raise ProfileError(f"BLOCKED_PROFILE_CONTRACT missing event rules: {', '.join(sorted(missing))}")
        ProfileFixtureRunner(self).assert_valid()

    def case_contract(self, metadata: Mapping[str, Any]) -> Mapping[str, Any]:
        """Return a declared case contract without inferring any project field."""
        timeline = metadata.get("timeline", {}) if isinstance(metadata, Mapping) else {}
        if not isinstance(timeline, Mapping):
            raise ProfileError("BLOCKED_PROFILE_CONTRACT case timeline must be an object")
        required = timeline.get("required_facts", ())
        if not isinstance(required, (list, tuple, set)):
            raise ProfileError("BLOCKED_PROFILE_CONTRACT required_facts must be a list")
        registry_keys = {entry["key"] for entry in EVENT_REGISTRY.values()}
        unknown = {str(item).upper() for item in required} - registry_keys
        if unknown:
            raise ProfileError("BLOCKED_PROFILE_CONTRACT unknown required facts: " + ", ".join(sorted(unknown)))
        return dict(timeline)

    @staticmethod
    def _source_matches(rule: Mapping[str, Any], record: RawLogRecord) -> bool:
        sources = rule.get("sources", {})
        if not isinstance(sources, Mapping):
            return True
        for name, actual in (("sources", record.source), ("ports", record.port), ("roles", record.role), ("phases", record.phase)):
            allowed = sources.get(name, [])
            if allowed in (None, "", []):
                continue
            if not isinstance(allowed, (list, tuple, set)):
                allowed = [allowed]
            if str(actual) not in {str(item) for item in allowed}:
                return False
        return True

    def extract_facts(self, record: RawLogRecord) -> list[ProfileFact]:
        """Apply only project-owned event rules and preserve every raw capture."""
        facts: list[ProfileFact] = []
        for rule in self.event_rules:
            if not self._source_matches(rule, record):
                continue
            pattern = str(rule.get("regex") or rule.get("pattern") or "")
            match = re.search(pattern, record.text, flags=re.IGNORECASE)
            if match is None:
                continue
            captures = {name: str(value or "") for name, value in match.groupdict().items()}
            presentation_capture = str(rule["presentation_capture"])
            presentation_value = captures[presentation_capture]
            raw_display = rule.get("display_captures", [])
            display_fields = tuple(
                (str(item["tag_name"]), captures[str(item["capture"])])
                for item in raw_display
                if isinstance(item, Mapping)
            )
            if not display_fields:
                display_fields = ((EVENT_REGISTRY[str(rule["event_type"])]["key"], presentation_value),)
            event_type = str(rule["event_type"])
            registry = EVENT_REGISTRY[event_type]
            correlation = rule.get("correlation", {})
            identity_capture = str(correlation.get("identity_capture") or "") if isinstance(correlation, Mapping) else ""
            identity = captures.get(identity_capture, "") if identity_capture else ""
            facts.append(ProfileFact(
                event_type=event_type,
                key=registry["key"],
                tag=registry["tag"],
                presentation_value=presentation_value,
                display_fields=display_fields,
                captures=captures,
                source=record.source,
                port=record.port,
                role=record.role,
                phase=str(rule.get("stage") or record.phase),
                identity=identity,
                mirror_policy=str(rule.get("mirror_policy") or "distinct").lower(),
                state_class=str(rule.get("state_class") or "").lower(),
                render_policy=str(rule.get("render_policy") or "all").lower(),
                evidence=tuple(record.evidence),
                rule_id=str(rule.get("rule_id") or ""),
                sequence=record.sequence,
                epoch=record.epoch,
                profile_version=self.schema_version,
            ))
        return facts

    def safety_stop_reason(self, facts: Iterable[ProfileFact]) -> str:
        by_rule = {item.rule_id for item in facts}
        for item in self.safety_stop:
            if str(item.get("event_rule_id") or "") in by_rule:
                return str(item.get("reason") or "")
        return ""

    @property
    def recovery(self) -> Mapping[str, Any]:
        value = self.payload.get("recovery", {})
        return value if isinstance(value, Mapping) else {}

    @property
    def health_policy(self) -> Mapping[str, Any]:
        value = self.payload.get("health_policy", {})
        return value if isinstance(value, Mapping) else {}

    @property
    def wake_words(self) -> list[Mapping[str, Any]]:
        """Ordered wakeword requirements; each test must select one entry explicitly."""
        return [dict(item) for item in self.payload.get("wake_words", []) if isinstance(item, Mapping)]

    def observation_rules(self, category: str) -> Mapping[str, Any]:
        value = self.observations.get(category, {})
        return value if isinstance(value, Mapping) else {}

    def command_rule(self, command: str, role: str | None = None) -> Mapping[str, Any] | None:
        for item in self.payload.get("commands", []):
            if not isinstance(item, Mapping) or item.get("command") != command:
                continue
            allowed_roles = item.get("roles") or []
            if role is None or not allowed_roles or role in allowed_roles:
                return item
        return None

    def assert_command_allowed(self, command: str, role: str | None = None) -> Mapping[str, Any]:
        if any(token in command for token in ("\n", "\r", ";", "&&", "||")):
            raise ProfileError("command chaining is not allowed")
        rule = self.command_rule(command, role)
        if rule is None:
            raise ProfileError(f"command is not allowed by profile: {command}")
        return rule

    def match_any(
        self,
        patterns: list[Any],
        text: str,
        *,
        port: str | None = None,
        role: str | None = None,
        phase: str | None = None,
        correlation_id: str | None = None,
        monotonic_seconds: float | None = None,
    ) -> bool:
        return any(
            self.marker_gate.match(
                rule, text, port=port, role=role, phase=phase,
                correlation_id=correlation_id, monotonic_seconds=monotonic_seconds,
            )[0]
            for rule in patterns
        )

    def match_details(self, patterns: list[Any], text: str, **scope: Any) -> list[tuple[Any, bool, str]]:
        """Return every rule decision so adapters can log scope rejections."""
        return [
            (rule, *self.marker_gate.match(rule, text, **scope))
            for rule in patterns
        ]

    def match_and_extract(self, patterns: list[Any], text: str, **scope: Any) -> list[dict[str, Any]]:
        """匹配项目规则并返回内部提取结果，正式日志不暴露规则文本。

        规则可以使用命名分组，并可通过 ``fact_map`` 将分组映射到框架事实
        key，例如 ``{"keyword": "WAKE"}``。该接口只负责事实提取和作用域
        校验，判定与人读日志由公共 runtime 完成。
        """
        results: list[dict[str, Any]] = []
        for index, rule in enumerate(patterns):
            pattern = _rule_pattern(rule)
            if not pattern:
                results.append({"rule_id": f"rule-{index}", "matched": False, "reason": "pattern_missing"})
                continue
            matched, reason = self.marker_gate.match(rule, text, **scope)
            item: dict[str, Any] = {
                "rule_id": str(rule.get("rule_id") if isinstance(rule, Mapping) else "") or f"rule-{index}",
                "matched": matched,
                "reason": reason,
                "matched_text": "",
                "captures": {},
                "facts": {},
                "evidence": scope.get("evidence", ""),
            }
            if matched:
                match = re.search(pattern, text, flags=re.IGNORECASE)
                if match is not None:
                    item["matched_text"] = match.group(0)
                    item["captures"] = dict(match.groupdict())
                    if not item["captures"]:
                        item["captures"] = {str(pos + 1): value for pos, value in enumerate(match.groups())}
                    mapping = rule.get("fact_map", {}) if isinstance(rule, Mapping) else {}
                    if isinstance(mapping, Mapping):
                        for group_name, fact_key in mapping.items():
                            value = item["captures"].get(str(group_name), "")
                            if value not in (None, ""):
                                item["facts"][str(fact_key).upper()] = value
            results.append(item)
        return results

    def marker_match_reason(self, rule: Any, text: str, **scope: Any) -> str:
        """Expose rejected/debounced marker reasons for project adapter logging."""
        return self.marker_gate.match(rule, text, **scope)[1]

    def capability_for_role(self, role: str | None) -> set[str]:
        for item in self.ports:
            if item.get("role") == role:
                return {str(value) for value in item.get("capabilities", [])}
        return set()

    def infer_role(self, text: str) -> tuple[str | None, float]:
        candidates: list[tuple[str, float]] = []
        for item in self.ports:
            role = str(item.get("role", "")).strip()
            patterns = [str(value) for value in item.get("inference_patterns", [])]
            if role and patterns and self.match_any(patterns, text):
                candidates.append((role, 1.0))
        if len(candidates) == 1:
            return candidates[0]
        return None, 0.0
