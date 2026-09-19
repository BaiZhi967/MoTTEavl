"""Deterministic, project-owned Chinese benchmark generation for Direct LLM v2.

The generator is deliberately offline and side-effect free. It creates structured facts first,
then derives a gold value and renders a prompt. A separate oracle recomputes every gold value
without calling the primary gold implementation.
"""

from __future__ import annotations

import calendar
import hashlib
import ipaddress
import json
import random
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from typing import Any, Iterable

from motte_contracts.direct_llm_v2 import (
    CONTRACT_VERSION,
    DATASET_ID,
    DATASET_VERSION,
    SELECTION_ALL,
    SUITE,
    DirectLlmDatasetV2,
    case_ids_sha256,
    converter_config_sha256,
    dataset_fingerprint_v2,
    scorer_config_sha256,
)
from motte_contracts.identity import canonical_sha256

CANONICAL_SEED = 20260919
GENERATOR_VERSION = "motte-core-zh-generator-v1"
CONVERTER_VERSION = "motte-core-zh-converter-v1"
PROMPT_VERSION = "motte-core-zh-prompt-v1"
SCORER_PROFILE_VERSION = "motte-core-zh-scorers-v1"
PROFILE_VERSION = "motte-core-zh-profiles-v1"
DATASET_NAME = "motte-core-zh"
DATASET_RECORD_VERSION = "1"
LANGUAGE = "zh-CN"
SPLIT = "generated"


@dataclass(frozen=True)
class CategoryDefinition:
    key: str
    quota: int
    subject: str


@dataclass(frozen=True)
class CoreZhFact:
    """A generated fact contains no rendered prompt and no stored gold answer."""

    category: str
    ordinal: int
    template_family: str
    difficulty: str
    payload: dict[str, Any]


CATEGORIES: tuple[CategoryDefinition, ...] = (
    CategoryDefinition("arithmetic", 160, "reasoning"),
    CategoryDefinition("datetime", 90, "reasoning"),
    CategoryDefinition("unit_conversion", 80, "reasoning"),
    CategoryDefinition("set_logic", 70, "reasoning"),
    CategoryDefinition("ticket_intent", 200, "business-operations"),
    CategoryDefinition("json_extraction", 200, "business-operations"),
    CategoryDefinition("format_instruction", 120, "instruction-following"),
    CategoryDefinition("noise_boundary", 80, "instruction-following"),
)
CATEGORY_QUOTAS = {item.key: item.quota for item in CATEGORIES}
TOTAL_CASES = sum(CATEGORY_QUOTAS.values())
SMOKE_PER_CATEGORY = 10
REGRESSION_QUOTAS = {key: value // 2 for key, value in CATEGORY_QUOTAS.items()}

_CHOICE_LABELS_4 = ["A", "B", "C", "D"]
_CHOICE_LABELS_7 = ["A", "B", "C", "D", "E", "F", "G"]
_INTENT_LABELS = {
    "duplicate_charge": "A",
    "invoice_amount": "A",
    "client_crash": "B",
    "upload_timeout": "B",
    "password_reset": "C",
    "account_locked": "C",
    "business_hours": "D",
    "feature_question": "D",
}
_INTENT_TEXT = {
    "duplicate_charge": "同一笔测试订单在账单中出现了两次扣费",
    "invoice_amount": "测试发票上的金额与订单金额不一致",
    "client_crash": "测试客户端启动后立即报错 E_TEST_CRASH",
    "upload_timeout": "上传测试附件时一直提示 E_TEST_TIMEOUT",
    "password_reset": "忘记测试账号密码，需要重置登录凭据",
    "account_locked": "测试账号连续验证失败后被锁定",
    "business_hours": "想咨询测试服务台的工作时间",
    "feature_question": "想了解演示版是否支持批量导出",
}
_DOC_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32")
)
_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@([A-Z0-9.-]+)\b")
_RESERVED_EMAIL_DOMAINS = frozenset({"example.com", "example.net", "example.org"})
_RESERVED_EMAIL_TLDS = frozenset({"invalid", "test", "example", "localhost"})
_IP_RE = re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")
_IPV6_RE = re.compile(
    r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![0-9A-Fa-f:])"
)
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_NATIONAL_ID_RE = re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)")
_SECRET_PATTERNS = (
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{16,}=*"),
)


def _category(key: str) -> CategoryDefinition:
    for definition in CATEGORIES:
        if definition.key == key:
            return definition
    raise ValueError(f"unknown core zh category: {key}")


def _rng_for(seed: int, category: str, ordinal: int) -> random.Random:
    material = f"{GENERATOR_VERSION}:{seed}:{category}:{ordinal}".encode()
    return random.Random(int.from_bytes(hashlib.sha256(material).digest(), "big"))


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("generated numeric answer must be finite")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _fraction_text(value: Fraction) -> str:
    denominator = value.denominator
    reduced = denominator
    for factor in (2, 5):
        while reduced % factor == 0:
            reduced //= factor
    if reduced != 1:
        raise ValueError("generated fraction has a non-terminating decimal expansion")
    return _decimal_text(Decimal(value.numerator) / Decimal(denominator))


def _weekday_manual(value: str) -> int:
    year, month, day = (int(part) for part in value.split("-"))
    offsets = (0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4)
    adjusted_year = year - 1 if month < 3 else year
    sunday_zero = (
        adjusted_year
        + adjusted_year // 4
        - adjusted_year // 100
        + adjusted_year // 400
        + offsets[month - 1]
        + day
    ) % 7
    return (sunday_zero - 1) % 7


def _manual_ordinal(value: str) -> int:
    year, month, day = (int(part) for part in value.split("-"))
    days = 0
    for current_year in range(1, year):
        days += 366 if calendar.isleap(current_year) else 365
    month_days = [31, 29 if calendar.isleap(year) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    days += sum(month_days[: month - 1])
    return days + day


def _manual_shift_local(value: str, delta_hours: int) -> str:
    date_text, time_text = value.split("T")
    year, month, day = (int(part) for part in date_text.split("-"))
    hour, minute = (int(part) for part in time_text.split(":"))
    total_minutes = hour * 60 + minute + delta_hours * 60
    day_shift, minute_of_day = divmod(total_minutes, 24 * 60)
    day += day_shift
    while True:
        month_days = [
            31,
            29 if calendar.isleap(year) else 28,
            31,
            30,
            31,
            30,
            31,
            31,
            30,
            31,
            30,
            31,
        ]
        if day > month_days[month - 1]:
            day -= month_days[month - 1]
            month += 1
            if month == 13:
                month = 1
                year += 1
            continue
        if day <= 0:
            month -= 1
            if month == 0:
                month = 12
                year -= 1
            previous_month_days = [
                31,
                29 if calendar.isleap(year) else 28,
                31,
                30,
                31,
                30,
                31,
                31,
                30,
                31,
                30,
                31,
            ]
            day += previous_month_days[month - 1]
            continue
        break
    target_hour, target_minute = divmod(minute_of_day, 60)
    return f"{year:04d}-{month:02d}-{day:02d} {target_hour:02d}:{target_minute:02d}"


def _arithmetic_fact(ordinal: int, rng: random.Random) -> CoreZhFact:
    variant = ordinal % 4
    if variant == 0:
        quantities = [rng.randint(2, 18) for _ in range(3)]
        unit_prices = [rng.randint(5, 90) for _ in range(3)]
        payload = {"operation": "line_total", "quantities": quantities, "unit_prices": unit_prices}
        family, difficulty = "arithmetic-line-total-v1", "medium"
    elif variant == 1:
        payload = {
            "operation": "discount",
            "list_price": rng.randint(40, 900),
            "discount_percent": rng.choice([5, 10, 15, 20, 25, 30, 40]),
        }
        family, difficulty = "arithmetic-discount-v1", "medium"
    elif variant == 2:
        denominator = rng.choice([2, 4, 5])
        payload = {
            "operation": "ratio",
            "base": rng.randint(10, 100) * denominator,
            "numerator": rng.choice([3, 5, 7]),
            "denominator": denominator,
        }
        family, difficulty = "arithmetic-ratio-v1", "hard"
    else:
        center = rng.randint(20, 500)
        delta = rng.randint(2, 30)
        payload = {"operation": "average", "values": [center - delta, center, center + delta]}
        family, difficulty = "arithmetic-average-v1", "easy"
    return CoreZhFact("arithmetic", ordinal, family, difficulty, payload)


def _datetime_fact(ordinal: int, rng: random.Random) -> CoreZhFact:
    variant = ordinal % 3
    base = date(2023, 1, 1) + timedelta(days=rng.randint(0, 1460))
    if variant == 0:
        end = base + timedelta(days=rng.randint(1, 120))
        payload = {"operation": "day_difference", "start": base.isoformat(), "end": end.isoformat()}
        family, difficulty = "datetime-day-difference-v1", "medium"
    elif variant == 1:
        payload = {"operation": "weekday", "date": base.isoformat()}
        family, difficulty = "datetime-weekday-v1", "easy"
    else:
        hour = rng.choice([0, 1, 22, 23])
        minute = rng.choice([0, 15, 30, 45])
        source_offset = rng.choice([8, 9, 0, -5])
        target_offset = rng.choice([offset for offset in (8, 9, 0, -5) if offset != source_offset])
        local = f"{base.isoformat()}T{hour:02d}:{minute:02d}"
        target = _manual_shift_local(local, target_offset - source_offset)
        target_dt = datetime.strptime(target, "%Y-%m-%d %H:%M")
        candidates = [
            target,
            (target_dt + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M"),
            (target_dt - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M"),
            (target_dt + timedelta(days=1)).strftime("%Y-%m-%d %H:%M"),
        ]
        rng.shuffle(candidates)
        payload = {
            "operation": "timezone",
            "local": local,
            "source_offset": source_offset,
            "target_offset": target_offset,
            "options": candidates,
        }
        family, difficulty = "datetime-timezone-boundary-v1", "hard"
    return CoreZhFact("datetime", ordinal, family, difficulty, payload)


def _unit_fact(ordinal: int, rng: random.Random) -> CoreZhFact:
    variant = ordinal % 4
    if variant == 0:
        payload = {
            "operation": "scale",
            "amount": rng.randint(100, 9000),
            "numerator": 1,
            "denominator": 100,
            "source_unit": "厘米",
            "target_unit": "米",
        }
        family = "unit-length-v1"
    elif variant == 1:
        payload = {
            "operation": "scale",
            "amount": rng.randint(2, 900),
            "numerator": 1000,
            "denominator": 1,
            "source_unit": "千克",
            "target_unit": "克",
        }
        family = "unit-mass-v1"
    elif variant == 2:
        payload = {
            "operation": "scale",
            "amount": rng.randint(20, 2400) * 3,
            "numerator": 1,
            "denominator": 60,
            "source_unit": "分钟",
            "target_unit": "小时",
        }
        family = "unit-duration-v1"
    else:
        denominator = rng.choice([2, 4, 5])
        payload = {
            "operation": "scale",
            "amount": rng.randint(10, 500) * denominator,
            "numerator": rng.choice([3, 5, 7]),
            "denominator": denominator,
            "source_unit": "甲币",
            "target_unit": "乙币",
        }
        family = "unit-fictional-currency-v1"
    return CoreZhFact("unit_conversion", ordinal, family, "medium", payload)


def _set_logic_fact(ordinal: int, rng: random.Random) -> CoreZhFact:
    variant = ordinal % 3
    if variant == 0:
        values = [rng.randint(1, 30) for _ in range(7)]
        values.append(values[rng.randrange(len(values))])
        payload = {"operation": "sort_unique", "values": values}
        family, difficulty = "set-sort-unique-v1", "easy"
    elif variant == 1:
        left = sorted({rng.randint(1, 24) for _ in range(7)})
        shared = rng.sample(left, min(3, len(left)))
        right = shared + [rng.randint(25, 40) for _ in range(4)]
        rng.shuffle(right)
        payload = {"operation": "intersection", "left": left, "right": right}
        family, difficulty = "set-intersection-v1", "medium"
    else:
        parity = rng.choice(["odd", "even"])
        matching = rng.sample(range(1 if parity == "odd" else 2, 40, 2), 2)
        other = rng.sample(range(2 if parity == "odd" else 1, 40, 2), 2)
        values = matching + other
        rng.shuffle(values)
        payload = {"operation": "ranked_parity", "values": values, "parity": parity, "rank": 2}
        family, difficulty = "set-ranked-condition-v1", "hard"
    return CoreZhFact("set_logic", ordinal, family, difficulty, payload)


def _ticket_fact(ordinal: int, rng: random.Random) -> CoreZhFact:
    by_label = {
        "A": ["duplicate_charge", "invoice_amount"],
        "B": ["client_crash", "upload_timeout"],
        "C": ["password_reset", "account_locked"],
        "D": ["business_hours", "feature_question"],
    }
    label = _CHOICE_LABELS_4[ordinal % 4]
    issue = by_label[label][(ordinal // 4) % 2]
    style = (ordinal // 8) % 4
    payload = {
        "operation": "ticket_intent",
        "issue": issue,
        "style": style,
        "ticket_id": f"TEST-TICKET-{ordinal + 1:04d}",
    }
    if label == "C":
        payload["account"] = f"test-user-{ordinal + 1:04d}@example.invalid"
    return CoreZhFact(
        "ticket_intent",
        ordinal,
        f"ticket-intent-style-{style + 1}-v1",
        "medium" if style < 3 else "hard",
        payload,
    )


def _json_fact(ordinal: int, rng: random.Random) -> CoreZhFact:
    variant = ordinal % 4
    if variant == 0:
        payload = {
            "operation": "order",
            "order_id": f"TEST-ORDER-{ordinal + 1:05d}",
            "item": rng.choice(["样品甲", "样品乙", "样品丙"]),
            "quantity": rng.randint(1, 20),
            "priority": bool((ordinal // 4) % 2),
        }
        family = "json-order-v1"
    elif variant == 1:
        payload = {
            "operation": "log",
            "event_id": f"TEST-EVENT-{ordinal + 1:05d}",
            "level": rng.choice(["INFO", "WARN", "ERROR"]),
            "code": f"E_TEST_{rng.randint(10, 99)}",
            "source_ip": f"192.0.2.{ordinal % 250 + 1}",
        }
        family = "json-log-v1"
    elif variant == 2:
        payload = {
            "operation": "notice",
            "notice_id": f"TEST-NOTICE-{ordinal + 1:05d}",
            "effective_date": (date(2026, 1, 1) + timedelta(days=rng.randint(0, 364))).isoformat(),
            "active": bool((ordinal // 4) % 2),
        }
        family = "json-notice-v1"
    else:
        payload = {
            "operation": "missing",
            "record_id": f"TEST-RECORD-{ordinal + 1:05d}",
            "amount": rng.randint(10, 999),
            "owner": None,
        }
        family = "json-missing-null-v1"
    return CoreZhFact("json_extraction", ordinal, family, "medium", payload)


def _format_fact(ordinal: int, rng: random.Random) -> CoreZhFact:
    variant = ordinal % 3
    if variant == 0:
        payload = {
            "operation": "status_line",
            "record_id": f"TEST-FMT-{ordinal + 1:04d}",
            "status": rng.choice(["OK", "WAIT", "STOP"]),
        }
        family, difficulty = "format-status-line-v1", "easy"
    elif variant == 1:
        tokens = [f"T{rng.randint(10, 99)}" for _ in range(3)]
        payload = {"operation": "token_sequence", "tokens": tokens}
        family, difficulty = "format-token-sequence-v1", "medium"
    else:
        payload = {
            "operation": "json_envelope",
            "batch": f"TEST-BATCH-{ordinal + 1:04d}",
            "accepted": bool(ordinal % 2),
            "count": rng.randint(1, 50),
        }
        family, difficulty = "format-json-envelope-v1", "medium"
    return CoreZhFact("format_instruction", ordinal, family, difficulty, payload)


def _noise_fact(ordinal: int, rng: random.Random) -> CoreZhFact:
    variant = ordinal % 4
    if variant == 0:
        denied = rng.choice(["duplicate_charge", "client_crash"])
        actual = rng.choice(["password_reset", "business_hours"])
        payload = {
            "operation": "negated_intent",
            "denied_issue": denied,
            "actual_issue": actual,
            "ticket_id": f"TEST-NOISE-{ordinal + 1:04d}",
        }
        family, difficulty = "noise-negated-intent-v1", "hard"
    elif variant == 1:
        original = rng.randint(1, 20)
        corrected = original + rng.choice([-1, 1, 2])
        payload = {
            "operation": "corrected_order",
            "order_id": f"TEST-NOISE-ORDER-{ordinal + 1:04d}",
            "original_quantity": original,
            "corrected_quantity": corrected,
        }
        family, difficulty = "noise-corrected-value-v1", "hard"
    elif variant == 2:
        payload = {
            "operation": "explicit_missing",
            "record_id": f"TEST-NOISE-RECORD-{ordinal + 1:04d}",
            "department": rng.choice(["演示一组", "演示二组"]),
        }
        family, difficulty = "noise-explicit-missing-v1", "medium"
    else:
        value = rng.randint(0, 20)
        threshold = rng.randint(5, 15)
        payload = {"operation": "inclusive_boundary", "value": value, "threshold": threshold}
        family, difficulty = "noise-inclusive-boundary-v1", "hard"
    return CoreZhFact("noise_boundary", ordinal, family, difficulty, payload)


_FACTORIES = {
    "arithmetic": _arithmetic_fact,
    "datetime": _datetime_fact,
    "unit_conversion": _unit_fact,
    "set_logic": _set_logic_fact,
    "ticket_intent": _ticket_fact,
    "json_extraction": _json_fact,
    "format_instruction": _format_fact,
    "noise_boundary": _noise_fact,
}


def generate_fact(category: str, ordinal: int, *, seed: int = CANONICAL_SEED) -> CoreZhFact:
    definition = _category(category)
    if type(ordinal) is not int or ordinal < 0:
        raise ValueError("fact ordinal must be a non-negative integer")
    rng = _rng_for(seed, definition.key, ordinal)
    return _FACTORIES[definition.key](ordinal, rng)


def generate_facts(*, seed: int = CANONICAL_SEED) -> list[CoreZhFact]:
    """Generate the canonical structured facts in stable category/ordinal order."""
    return [
        generate_fact(definition.key, ordinal, seed=seed)
        for definition in CATEGORIES
        for ordinal in range(definition.quota)
    ]


def sample_random_facts(count: int, *, seed: int = CANONICAL_SEED + 1) -> list[CoreZhFact]:
    """Generate deterministic property-test facts without using canonical case ordinals."""
    if type(count) is not int or count < 0:
        raise ValueError("sample count must be a non-negative integer")
    chooser = random.Random(seed)
    facts = []
    for index in range(count):
        category = chooser.choice(CATEGORIES).key
        ordinal = chooser.randrange(10_000, 2_000_000) + index * 2_000_000
        facts.append(generate_fact(category, ordinal, seed=seed))
    return facts


def _sorted_distinct(values: Iterable[int]) -> list[int]:
    return sorted(set(values))


def _manual_sorted_distinct(values: Iterable[int]) -> list[int]:
    result: list[int] = []
    for value in values:
        if value in result:
            continue
        insert_at = 0
        while insert_at < len(result) and result[insert_at] < value:
            insert_at += 1
        result.insert(insert_at, value)
    return result


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def primary_expected(fact: CoreZhFact) -> str:
    """Compute the generation-path gold from structured facts."""
    payload = fact.payload
    operation = payload["operation"]
    if fact.category == "arithmetic":
        if operation == "line_total":
            value = sum(
                Decimal(quantity) * Decimal(price)
                for quantity, price in zip(
                    payload["quantities"], payload["unit_prices"], strict=True
                )
            )
        elif operation == "discount":
            value = (
                Decimal(payload["list_price"])
                * (Decimal(100) - Decimal(payload["discount_percent"]))
                / Decimal(100)
            )
        elif operation == "ratio":
            value = (
                Decimal(payload["base"])
                * Decimal(payload["numerator"])
                / Decimal(payload["denominator"])
            )
        else:
            values = payload["values"]
            value = sum(Decimal(item) for item in values) / Decimal(len(values))
        return _decimal_text(value)
    if fact.category == "datetime":
        if operation == "day_difference":
            return str(
                (date.fromisoformat(payload["end"]) - date.fromisoformat(payload["start"])).days
            )
        if operation == "weekday":
            return _CHOICE_LABELS_7[date.fromisoformat(payload["date"]).weekday()]
        local = datetime.fromisoformat(payload["local"])
        source_zone = timezone(timedelta(hours=payload["source_offset"]))
        target_zone = timezone(timedelta(hours=payload["target_offset"]))
        target = (
            local.replace(tzinfo=source_zone).astimezone(target_zone).strftime("%Y-%m-%d %H:%M")
        )
        return _CHOICE_LABELS_4[payload["options"].index(target)]
    if fact.category == "unit_conversion":
        value = (
            Decimal(payload["amount"])
            * Decimal(payload["numerator"])
            / Decimal(payload["denominator"])
        )
        return _decimal_text(value)
    if fact.category == "set_logic":
        if operation == "sort_unique":
            return ",".join(str(item) for item in _sorted_distinct(payload["values"]))
        if operation == "intersection":
            common = sorted(set(payload["left"]) & set(payload["right"]))
            return ",".join(str(item) for item in common)
        parity = 1 if payload["parity"] == "odd" else 0
        candidates = sorted(value for value in payload["values"] if value % 2 == parity)
        target = candidates[payload["rank"] - 1]
        return _CHOICE_LABELS_4[payload["values"].index(target)]
    if fact.category == "ticket_intent":
        return _INTENT_LABELS[payload["issue"]]
    if fact.category == "json_extraction":
        if operation == "order":
            value = {key: payload[key] for key in ("order_id", "item", "quantity", "priority")}
        elif operation == "log":
            value = {key: payload[key] for key in ("event_id", "level", "code", "source_ip")}
        elif operation == "notice":
            value = {key: payload[key] for key in ("notice_id", "effective_date", "active")}
        else:
            value = {key: payload[key] for key in ("record_id", "amount", "owner")}
        return _json_text(value)
    if fact.category == "format_instruction":
        if operation == "status_line":
            return f"[RESULT|id={payload['record_id']}|status={payload['status']}]"
        if operation == "token_sequence":
            return " > ".join(payload["tokens"])
        return _json_text({key: payload[key] for key in ("batch", "accepted", "count")})
    if operation == "negated_intent":
        return _INTENT_LABELS[payload["actual_issue"]]
    if operation == "corrected_order":
        return _json_text(
            {"order_id": payload["order_id"], "quantity": payload["corrected_quantity"]}
        )
    if operation == "explicit_missing":
        return _json_text({"record_id": payload["record_id"], "owner": None})
    return "A" if payload["value"] <= payload["threshold"] else "B"


def recompute_expected(fact: CoreZhFact) -> str:
    """Independently recompute gold without calling or sharing the primary result."""
    payload = fact.payload
    operation = payload["operation"]
    if fact.category == "arithmetic":
        if operation == "line_total":
            answer = Fraction(0)
            for index in range(len(payload["quantities"])):
                answer += Fraction(payload["quantities"][index] * payload["unit_prices"][index])
        elif operation == "discount":
            answer = Fraction(payload["list_price"] * (100 - payload["discount_percent"]), 100)
        elif operation == "ratio":
            answer = Fraction(payload["base"] * payload["numerator"], payload["denominator"])
        else:
            answer = Fraction(sum(payload["values"]), len(payload["values"]))
        return _fraction_text(answer)
    if fact.category == "datetime":
        if operation == "day_difference":
            return str(_manual_ordinal(payload["end"]) - _manual_ordinal(payload["start"]))
        if operation == "weekday":
            return _CHOICE_LABELS_7[_weekday_manual(payload["date"])]
        shifted = _manual_shift_local(
            payload["local"], payload["target_offset"] - payload["source_offset"]
        )
        for index, candidate in enumerate(payload["options"]):
            if candidate == shifted:
                return _CHOICE_LABELS_4[index]
        raise ValueError("timezone options do not contain the independently computed answer")
    if fact.category == "unit_conversion":
        return _fraction_text(
            Fraction(payload["amount"] * payload["numerator"], payload["denominator"])
        )
    if fact.category == "set_logic":
        if operation == "sort_unique":
            return ",".join(str(item) for item in _manual_sorted_distinct(payload["values"]))
        if operation == "intersection":
            common = [
                item
                for item in _manual_sorted_distinct(payload["left"])
                if item in payload["right"]
            ]
            return ",".join(str(item) for item in common)
        wanted_remainder = 1 if payload["parity"] == "odd" else 0
        matches = _manual_sorted_distinct(
            item for item in payload["values"] if item % 2 == wanted_remainder
        )
        wanted = matches[payload["rank"] - 1]
        for index, item in enumerate(payload["values"]):
            if item == wanted:
                return _CHOICE_LABELS_4[index]
        raise ValueError("ranked condition answer is absent")
    if fact.category == "ticket_intent":
        issue = payload["issue"]
        if issue in {"duplicate_charge", "invoice_amount"}:
            return "A"
        if issue in {"client_crash", "upload_timeout"}:
            return "B"
        if issue in {"password_reset", "account_locked"}:
            return "C"
        if issue in {"business_hours", "feature_question"}:
            return "D"
        raise ValueError("unknown ticket intent")
    if fact.category == "json_extraction":
        value: dict[str, Any] = {}
        if operation == "order":
            for key in ("order_id", "item", "quantity", "priority"):
                value[key] = payload.get(key)
        elif operation == "log":
            for key in ("event_id", "level", "code", "source_ip"):
                value[key] = payload.get(key)
        elif operation == "notice":
            for key in ("notice_id", "effective_date", "active"):
                value[key] = payload.get(key)
        else:
            value["record_id"] = payload.get("record_id")
            value["amount"] = payload.get("amount")
            value["owner"] = payload.get("owner")
        return _json_text(value)
    if fact.category == "format_instruction":
        if operation == "status_line":
            pieces = ["[RESULT", f"id={payload['record_id']}", f"status={payload['status']}]"]
            return "|".join(pieces)
        if operation == "token_sequence":
            output = payload["tokens"][0]
            for token in payload["tokens"][1:]:
                output += " > " + token
            return output
        value = {
            "accepted": payload.get("accepted"),
            "batch": payload.get("batch"),
            "count": payload.get("count"),
        }
        return _json_text(value)
    if operation == "negated_intent":
        issue = payload["actual_issue"]
        if issue in {"password_reset", "account_locked"}:
            return "C"
        if issue in {"business_hours", "feature_question"}:
            return "D"
        if issue in {"duplicate_charge", "invoice_amount"}:
            return "A"
        return "B"
    if operation == "corrected_order":
        value = {"quantity": payload.get("corrected_quantity"), "order_id": payload.get("order_id")}
        return _json_text(value)
    if operation == "explicit_missing":
        value = {"owner": None, "record_id": payload.get("record_id")}
        return _json_text(value)
    difference = payload["value"] - payload["threshold"]
    return "A" if difference <= 0 else "B"


def _choice_lines(options: list[str]) -> str:
    return "\n".join(f"{_CHOICE_LABELS_7[index]}. {value}" for index, value in enumerate(options))


def render_prompt(fact: CoreZhFact) -> str:
    """Render a Chinese prompt from facts only; this function never reads a gold value."""
    payload = fact.payload
    operation = payload["operation"]
    task_id = f"TEST-{fact.category.upper().replace('_', '-')}-{fact.ordinal + 1:04d}"
    if fact.category == "arithmetic":
        if operation == "line_total":
            rows = "；".join(
                f"{quantity}件，每件{price}元"
                for quantity, price in zip(
                    payload["quantities"], payload["unit_prices"], strict=True
                )
            )
            question = f"测试采购批次 {task_id} 有三行：{rows}。总金额是多少？"
        elif operation == "discount":
            question = (
                f"测试报价 {task_id} 的原价为{payload['list_price']}元，"
                f"统一优惠{payload['discount_percent']}%。优惠后金额是多少？"
            )
        elif operation == "ratio":
            question = (
                f"测试仓库 {task_id} 有基准数量{payload['base']}，目标数量是基准的"
                f"{payload['numerator']}/{payload['denominator']}。目标数量是多少？"
            )
        else:
            question = f"测试批次 {task_id} 的数值为{payload['values']}。算术平均数是多少？"
        return question + "\n只在最后一行输出 [ANSWER:<十进制数字>]，不要附加单位或解释。"
    if fact.category == "datetime":
        if operation == "day_difference":
            question = (
                f"测试日程 {task_id} 从 {payload['start']} 到 {payload['end']}，"
                "按结束日期减开始日期计算相差多少个自然日？"
            )
            return question + "\n只在最后一行输出 [ANSWER:<整数>]，不要附加其他文字。"
        if operation == "weekday":
            weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
            return (
                f"测试日期 {task_id} 为 {payload['date']}。它是星期几？\n"
                f"{_choice_lines(weekdays)}\n"
                "只在最后一行输出 [ANSWER:<选项字母>]。"
            )
        options = _choice_lines(payload["options"])
        return (
            f"测试会议 {task_id} 的本地时间为 {payload['local'].replace('T', ' ')} "
            f"(UTC{payload['source_offset']:+d})。换算到 UTC{payload['target_offset']:+d} 是哪项？\n"
            f"{options}\n只在最后一行输出 [ANSWER:<选项字母>]。"
        )
    if fact.category == "unit_conversion":
        question = (
            f"测试换算 {task_id}：{payload['amount']}{payload['source_unit']}，"
            f"固定换算关系为 1{payload['source_unit']} = "
            f"{payload['numerator']}/{payload['denominator']}{payload['target_unit']}。"
            f"换算结果是多少{payload['target_unit']}？"
        )
        return question + "\n只在最后一行输出 [ANSWER:<十进制数字>]，不要附加单位或解释。"
    if fact.category == "set_logic":
        if operation == "sort_unique":
            return (
                f"测试集合 {task_id} 的原始序列为 {payload['values']}。去重并按整数升序排列。\n"
                "只输出数字，并用英文逗号连接；不要输出空格、括号或解释。"
            )
        if operation == "intersection":
            return (
                f"测试集合 {task_id}：左集合={payload['left']}，右集合={payload['right']}。"
                "求交集并按整数升序排列。\n"
                "只输出数字，并用英文逗号连接；不要输出空格、括号或解释。"
            )
        parity = "奇数" if payload["parity"] == "odd" else "偶数"
        options = [str(value) for value in payload["values"]]
        return (
            f"测试条件题 {task_id} 的候选值如下：\n{_choice_lines(options)}\n"
            f"选择其中第{payload['rank']}小的{parity}。\n"
            "只在最后一行输出 [ANSWER:<选项字母>]。"
        )
    if fact.category == "ticket_intent":
        mapping = "A=账单，B=技术，C=账号，D=其他"
        prefix = ["收到一条测试工单", "请路由以下演示工单", "仅判断主要诉求", "忽略礼貌用语并分类"][
            payload["style"]
        ]
        account = f"，测试账号为 {payload['account']}" if payload.get("account") else ""
        return (
            f"{prefix} {payload['ticket_id']}：{_INTENT_TEXT[payload['issue']]}{account}。\n"
            f"分类规则：{mapping}。\n只在最后一行输出 [ANSWER:<选项字母>]。"
        )
    if fact.category == "json_extraction":
        if operation == "order":
            source = (
                f"测试订单 {payload['order_id']} 包含{payload['quantity']}件{payload['item']}，"
                f"优先处理={'是' if payload['priority'] else '否'}。"
            )
            fields = "order_id、item、quantity、priority"
        elif operation == "log":
            source = (
                f"测试事件 {payload['event_id']}：级别={payload['level']}，"
                f"代码={payload['code']}，来源地址={payload['source_ip']}。"
            )
            fields = "event_id、level、code、source_ip"
        elif operation == "notice":
            source = (
                f"测试公告 {payload['notice_id']} 于 {payload['effective_date']} 生效，"
                f"active={'true' if payload['active'] else 'false'}。"
            )
            fields = "notice_id、effective_date、active"
        else:
            source = f"测试记录 {payload['record_id']} 的金额为 {payload['amount']}；负责人未提供。"
            fields = "record_id、amount、owner，其中缺失值使用 null"
        return (
            f"{source}\n抽取字段：{fields}。\n"
            "只输出一个合法 JSON 对象；不得使用 Markdown 代码块或附加解释。"
        )
    if fact.category == "format_instruction":
        if operation == "status_line":
            return (
                f"格式测试 {task_id}：记录号={payload['record_id']}，状态={payload['status']}。\n"
                "严格只输出 `[RESULT|id=<记录号>|status=<状态>]`，替换尖括号内容，"
                "不得增加空格或换行说明。"
            )
        if operation == "token_sequence":
            return (
                f"格式测试 {task_id}：按给定顺序输出标记 {payload['tokens']}。\n"
                "严格使用 ` > `（两侧各一个空格）连接，只输出连接后的文本。"
            )
        return (
            f"格式测试 {task_id}：batch={payload['batch']}，"
            f"accepted={'true' if payload['accepted'] else 'false'}，count={payload['count']}。\n"
            "只输出包含 batch、accepted、count 的合法 JSON 对象，不得附加解释。"
        )
    if operation == "negated_intent":
        return (
            f"测试工单 {payload['ticket_id']}：这不是“{_INTENT_TEXT[payload['denied_issue']]}”，"
            f"实际诉求是“{_INTENT_TEXT[payload['actual_issue']]}”。\n"
            "分类规则：A=账单，B=技术，C=账号，D=其他。\n"
            "只在最后一行输出 [ANSWER:<选项字母>]。"
        )
    if operation == "corrected_order":
        return (
            f"噪声记录：测试订单 {payload['order_id']} 最初写为"
            f"{payload['original_quantity']}件；注意，不是{payload['original_quantity']}件，"
            f"复核后的实际数量是{payload['corrected_quantity']}件。\n"
            "只输出包含 order_id 和最终 quantity 的合法 JSON 对象。"
        )
    if operation == "explicit_missing":
        return (
            f"噪声记录：{payload['record_id']} 属于{payload['department']}。文本没有提供负责人，"
            "也不要从部门名称猜测。\n只输出 record_id 和 owner；owner 必须为 null。"
        )
    return (
        f"边界测试 {task_id}：测量值={payload['value']}，阈值={payload['threshold']}。"
        "规则是“小于或等于阈值算通过”。\nA. 通过\nB. 不通过\nC. 信息不足\nD. 忽略\n"
        "只在最后一行输出 [ANSWER:<选项字母>]。"
    )


def _scorer_for(fact: CoreZhFact) -> dict[str, Any]:
    operation = fact.payload["operation"]
    if fact.category in {"arithmetic", "unit_conversion"} or (
        fact.category == "datetime" and operation == "day_difference"
    ):
        scorer_id, version = "numeric", "1"
        config = {
            "mode": "exact",
            "absolute_tolerance": None,
            "relative_tolerance": None,
            "allow_sign": True,
            "allow_thousands": False,
            "allow_scientific": False,
            "allow_fraction": False,
            "allow_non_finite": False,
        }
    elif (
        fact.category in {"datetime", "ticket_intent"}
        or (fact.category == "set_logic" and operation == "ranked_parity")
        or (
            fact.category == "noise_boundary"
            and operation in {"negated_intent", "inclusive_boundary"}
        )
    ):
        labels = _CHOICE_LABELS_7 if operation == "weekday" else _CHOICE_LABELS_4
        scorer_id, version = "choice", "1"
        config = {"labels": labels, "allow_bare_final_label": False}
    elif (
        fact.category == "json_extraction"
        or (fact.category == "format_instruction" and operation == "json_envelope")
        or (
            fact.category == "noise_boundary"
            and operation in {"corrected_order", "explicit_missing"}
        )
    ):
        scorer_id, version = "json_equal", "1"
        config = {}
    else:
        scorer_id, version = "exact", "1"
        config = {"normalize_whitespace": False, "case_sensitive": True}
    return {
        "id": scorer_id,
        "version": version,
        "config": config,
        "config_sha256": scorer_config_sha256(config),
    }


def _case_id(fact: CoreZhFact) -> str:
    return f"motte-core-zh-{fact.category.replace('_', '-')}-{fact.ordinal + 1:04d}"


def _source_id(fact: CoreZhFact) -> str:
    return f"{GENERATOR_VERSION}:{fact.category}:{fact.ordinal + 1:04d}"


def _build_cases(facts: list[CoreZhFact]) -> list[dict[str, Any]]:
    cases = []
    for source_line, fact in enumerate(facts, 1):
        definition = _category(fact.category)
        scorer = _scorer_for(fact)
        record = {
            "case_id": _case_id(fact),
            "input": render_prompt(fact),
            "expected": primary_expected(fact),
            "metadata": {
                "source_line": source_line,
                "source_id": _source_id(fact),
                "language": LANGUAGE,
                "subject": definition.subject,
                "category": fact.category,
                "difficulty": fact.difficulty,
                "split": SPLIT,
                "tags": ["synthetic", "project-owned", fact.category, scorer["id"]],
                "template_family": fact.template_family,
                "scorer": scorer,
            },
        }
        cases.append(record)
    return cases


def _profile_ids(
    cases: list[dict[str, Any]],
    counts: dict[str, int],
    *,
    minimum_per_template_family: int = 0,
) -> list[str]:
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        by_category[case["metadata"]["category"]].append(case)
    selected_ids: set[str] = set()
    for category, target_count in counts.items():
        candidates = by_category[category]
        if minimum_per_template_family:
            by_family: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for case in candidates:
                by_family[case["metadata"]["template_family"]].append(case)
            for family_cases in by_family.values():
                if len(family_cases) < minimum_per_template_family:
                    raise ValueError("template family cannot satisfy profile minimum")
                selected_ids.update(
                    case["case_id"] for case in family_cases[:minimum_per_template_family]
                )
        category_selected = sum(case["case_id"] in selected_ids for case in candidates)
        if category_selected > target_count:
            raise ValueError("template-family minimum exceeds category profile quota")
        for case in candidates:
            if category_selected >= target_count:
                break
            if case["case_id"] not in selected_ids:
                selected_ids.add(case["case_id"])
                category_selected += 1
        if category_selected != target_count:
            raise ValueError(
                f"profile quota mismatch for {category!r}: "
                f"expected {target_count}, got {category_selected}"
            )
    selected = [case["case_id"] for case in cases if case["case_id"] in selected_ids]
    if len(selected) != sum(counts.values()):
        raise ValueError("profile contains unexpected category cases")
    return selected


def _build_profiles(cases: list[dict[str, Any]], seed: int) -> list[dict[str, Any]]:
    smoke_counts = {definition.key: SMOKE_PER_CATEGORY for definition in CATEGORIES}
    definitions = (
        (
            "smoke",
            "stratified-family-covered-v1",
            _profile_ids(cases, smoke_counts, minimum_per_template_family=2),
            f"{seed}:{PROFILE_VERSION}:smoke",
        ),
        (
            "regression",
            "stratified-half-fixed-prefix-v1",
            _profile_ids(cases, REGRESSION_QUOTAS),
            f"{seed}:{PROFILE_VERSION}:regression",
        ),
        ("full", "all-fixed-ids", [case["case_id"] for case in cases], None),
    )
    return [
        {
            "name": name,
            "strategy": strategy,
            "count": len(case_ids),
            "case_ids": case_ids,
            "case_ids_sha256": case_ids_sha256(case_ids),
            "dimensions": ["category", "difficulty", "template_family", "scorer"],
            "seed": profile_seed,
        }
        for name, strategy, case_ids, profile_seed in definitions
    ]


def build_dataset(*, seed: int = CANONICAL_SEED) -> dict[str, Any]:
    """Build and validate the complete in-memory v2 dataset; never write an artifact."""
    facts = generate_facts(seed=seed)
    cases = _build_cases(facts)
    profiles = _build_profiles(cases, seed)
    recipe = {
        "generator_version": GENERATOR_VERSION,
        "converter_version": CONVERTER_VERSION,
        "prompt_version": PROMPT_VERSION,
        "scorer_profile_version": SCORER_PROFILE_VERSION,
        "profile_version": PROFILE_VERSION,
        "seed": seed,
        "quotas": CATEGORY_QUOTAS,
    }
    recipe_bytes = json.dumps(
        recipe, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    artifacts = [
        {
            "logical_name": "generation-recipe",
            "url": f"urn:motteavl:generator:{DATASET_NAME}:{GENERATOR_VERSION}:{seed}",
            "sha256": hashlib.sha256(recipe_bytes).hexdigest(),
            "bytes": len(recipe_bytes),
        }
    ]
    converter_config = {
        "generator_version": GENERATOR_VERSION,
        "prompt_version": PROMPT_VERSION,
        "scorer_profile_version": SCORER_PROFILE_VERSION,
        "profile_version": PROFILE_VERSION,
        "seed": seed,
        "quotas": CATEGORY_QUOTAS,
    }
    default_config = {"normalize_whitespace": False, "case_sensitive": True}
    record: dict[str, Any] = {
        "name": DATASET_NAME,
        "version": DATASET_RECORD_VERSION,
        "contract_version": CONTRACT_VERSION,
        "eval": {
            "suite": SUITE,
            "id": DATASET_ID,
            "version": DATASET_VERSION,
            "selected_count": len(cases),
            "selection": SELECTION_ALL,
            "scorer": {
                "id": "exact",
                "version": "1",
                "config": default_config,
                "config_sha256": scorer_config_sha256(default_config),
            },
            "prompt_version": PROMPT_VERSION,
            "max_output_tokens": 256,
            "max_retries": 0,
        },
        "provenance": {
            "source_id": DATASET_NAME,
            "source_kind": "generated-internal",
            "homepage": None,
            "upstream_revision": GENERATOR_VERSION,
            "artifacts": artifacts,
            "artifact_manifest_sha256": canonical_sha256(artifacts),
            "license": {
                "id": "project-owned-internal",
                "status": "pending",
                "evidence_urls": [],
                "commercial_use": "unknown",
                "redistribution": "unknown",
                "reviewed_at": None,
            },
            "converter": {
                "id": "motte-core-zh-generator",
                "version": CONVERTER_VERSION,
                "config": converter_config,
                "config_sha256": converter_config_sha256(converter_config),
            },
            "synthetic": True,
        },
        "cases": cases,
        "cases_sha256": canonical_sha256(cases),
        "profiles": profiles,
        "profiles_sha256": canonical_sha256(profiles),
    }
    record["dataset_fingerprint"] = dataset_fingerprint_v2(record)
    return DirectLlmDatasetV2.model_validate(record).model_dump(mode="json")


def _is_reserved_email_domain(domain: str) -> bool:
    normalized = domain.lower().rstrip(".")
    return (
        normalized in _RESERVED_EMAIL_DOMAINS
        or any(normalized.endswith(f".{item}") for item in _RESERVED_EMAIL_DOMAINS)
        or normalized.rsplit(".", 1)[-1] in _RESERVED_EMAIL_TLDS
    )


def scan_sensitive_values(cases: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    """Reject secrets, real-looking PII, non-reserved emails, and non-documentation IPs."""
    issues: list[dict[str, str]] = []
    for case in cases:
        case_id = str(case.get("case_id", "<unknown>"))
        texts = [str(case.get("input", "")), str(case.get("expected", ""))]
        for text in texts:
            for match in _EMAIL_RE.finditer(text):
                if not _is_reserved_email_domain(match.group(1)):
                    issues.append(
                        {"case_id": case_id, "kind": "non-reserved-email", "value": match.group(0)}
                    )
            for pattern in (_IP_RE, _IPV6_RE):
                for match in pattern.finditer(text):
                    try:
                        address = ipaddress.ip_address(match.group(0))
                    except ValueError:
                        issues.append(
                            {"case_id": case_id, "kind": "invalid-ip", "value": match.group(0)}
                        )
                        continue
                    if not any(address in network for network in _DOC_NETWORKS):
                        issues.append(
                            {
                                "case_id": case_id,
                                "kind": "non-documentation-ip",
                                "value": match.group(0),
                            }
                        )
            for pattern in _SECRET_PATTERNS:
                match = pattern.search(text)
                if match:
                    issues.append(
                        {"case_id": case_id, "kind": "secret-like-value", "value": match.group(0)}
                    )
            for kind, pattern in (
                ("phone-like-value", _PHONE_RE),
                ("national-id-like-value", _NATIONAL_ID_RE),
            ):
                match = pattern.search(text)
                if match:
                    issues.append({"case_id": case_id, "kind": kind, "value": match.group(0)})
    return issues


def answer_distribution(cases: Iterable[dict[str, Any]]) -> dict[str, dict[str, int]]:
    distributions: dict[str, Counter[str]] = defaultdict(Counter)
    for case in cases:
        category = case["metadata"]["category"]
        scorer_id = case["metadata"]["scorer"]["id"]
        key = f"{category}:{scorer_id}"
        distributions[key][case["expected"]] += 1
    return {key: dict(sorted(values.items())) for key, values in sorted(distributions.items())}


def build_fixture(
    *, per_template_family: int = 1, seed: int = CANONICAL_SEED
) -> list[dict[str, Any]]:
    """Return a small in-memory fixture with at most two cases per template family."""
    if type(per_template_family) is not int or not 1 <= per_template_family <= 2:
        raise ValueError("per_template_family must be 1 or 2")
    dataset = build_dataset(seed=seed)
    counts: Counter[str] = Counter()
    fixture = []
    for case in dataset["cases"]:
        family = case["metadata"]["template_family"]
        if counts[family] < per_template_family:
            fixture.append(case)
            counts[family] += 1
    return fixture


def _review_category_counts(count: int) -> dict[str, int]:
    counts = {definition.key: 1 for definition in CATEGORIES}
    remaining = count - len(CATEGORIES)
    total_capacity = TOTAL_CASES - len(CATEGORIES)
    remainders: list[tuple[int, int, str]] = []
    allocated = 0
    for index, definition in enumerate(CATEGORIES):
        capacity = definition.quota - 1
        quotient, remainder = divmod(remaining * capacity, total_capacity)
        counts[definition.key] += quotient
        allocated += quotient
        remainders.append((remainder, -index, definition.key))
    for _, _, category in sorted(remainders, reverse=True)[: remaining - allocated]:
        counts[category] += 1
    return counts


def build_review_sample(
    *, dataset: dict[str, Any] | None = None, count: int = 200, seed: int = CANONICAL_SEED + 200
) -> dict[str, Any]:
    """Create a machine-readable stratified sample; no review is marked complete."""
    source = (
        build_dataset()
        if dataset is None
        else DirectLlmDatasetV2.model_validate(dataset).model_dump(mode="json")
    )
    if type(count) is not int or count < len(CATEGORIES) or count > len(source["cases"]):
        raise ValueError(
            f"review count must be between {len(CATEGORIES)} and {len(source['cases'])}"
        )
    category_counts = _review_category_counts(count)
    selected: list[dict[str, Any]] = []
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in source["cases"]:
        by_category[case["metadata"]["category"]].append(case)
    for definition in CATEGORIES:
        category_count = category_counts[definition.key]
        rng = random.Random(f"{seed}:{definition.key}:review-v1")
        choices = sorted(
            rng.sample(by_category[definition.key], category_count),
            key=lambda item: item["case_id"],
        )
        for case in choices:
            selected.append(
                {
                    "case_id": case["case_id"],
                    "input": case["input"],
                    "expected": case["expected"],
                    "metadata": case["metadata"],
                    "review": {
                        "status": "pending-human",
                        "required_reviewers": 2,
                        "completed_reviewers": 0,
                        "decision": None,
                        "notes": None,
                    },
                }
            )
    sample_payload = [item["case_id"] for item in selected]
    return {
        "schema_version": 1,
        "dataset_fingerprint": source["dataset_fingerprint"],
        "selection": {
            "strategy": "category-stratified-v1",
            "seed": str(seed),
            "count": len(selected),
            "case_ids_sha256": canonical_sha256(sample_payload),
        },
        "status": "pending-human",
        "items": selected,
    }


def build_quality_report(
    *, seed: int = CANONICAL_SEED, oracle_sample_count: int = 10_000
) -> dict[str, Any]:
    """Report automated evidence while keeping human and model gates explicitly pending."""
    dataset = build_dataset(seed=seed)
    facts = generate_facts(seed=seed)
    canonical_mismatches = [
        _case_id(fact)
        for fact, case in zip(facts, dataset["cases"], strict=True)
        if recompute_expected(fact) != case["expected"]
    ]
    random_mismatches = [
        f"{fact.category}:{fact.ordinal}"
        for fact in sample_random_facts(oracle_sample_count, seed=seed + 1)
        if primary_expected(fact) != recompute_expected(fact)
    ]
    ids = [case["case_id"] for case in dataset["cases"]]
    prompts = [case["input"] for case in dataset["cases"]]
    quota_counts = Counter(case["metadata"]["category"] for case in dataset["cases"])
    profiles = {profile["name"]: profile for profile in dataset["profiles"]}
    smoke_ids = set(profiles["smoke"]["case_ids"])
    all_families = {case["metadata"]["template_family"] for case in dataset["cases"]}
    smoke_families = Counter(
        case["metadata"]["template_family"]
        for case in dataset["cases"]
        if case["case_id"] in smoke_ids
    )
    ticket_distribution = answer_distribution(dataset["cases"]).get("ticket_intent:choice", {})
    json_boolean_distribution = {}
    for field in ("priority", "active"):
        distribution = Counter(
            fact.payload[field]
            for fact in facts
            if fact.category == "json_extraction" and field in fact.payload
        )
        json_boolean_distribution[field] = {
            "false": distribution[False],
            "true": distribution[True],
        }
    automated_checks = {
        "total_cases_exact": len(dataset["cases"]) == TOTAL_CASES,
        "category_quotas_exact": dict(quota_counts) == CATEGORY_QUOTAS,
        "case_ids_unique": len(ids) == len(set(ids)),
        "prompts_unique": len(prompts) == len(set(prompts)),
        "canonical_oracle_mismatches": len(canonical_mismatches),
        "random_oracle_sample_count": oracle_sample_count,
        "random_oracle_mismatches": len(random_mismatches),
        "sensitive_value_violations": len(scan_sensitive_values(dataset["cases"])),
        "profiles_exact": {name: profiles[name]["count"] for name in profiles}
        == {"smoke": 80, "regression": 500, "full": 1000},
        "smoke_covers_all_template_families": set(smoke_families) == all_families,
        "smoke_minimum_per_template_family": min(smoke_families.values()),
        "ticket_choice_distribution": ticket_distribution,
        "ticket_choice_balanced": ticket_distribution == {"A": 50, "B": 50, "C": 50, "D": 50},
        "json_boolean_distribution": json_boolean_distribution,
        "json_booleans_balanced": all(
            distribution == {"false": 25, "true": 25}
            for distribution in json_boolean_distribution.values()
        ),
    }
    passed = (
        automated_checks["total_cases_exact"]
        and automated_checks["category_quotas_exact"]
        and automated_checks["case_ids_unique"]
        and automated_checks["prompts_unique"]
        and automated_checks["canonical_oracle_mismatches"] == 0
        and automated_checks["random_oracle_mismatches"] == 0
        and automated_checks["sensitive_value_violations"] == 0
        and automated_checks["profiles_exact"]
        and automated_checks["smoke_covers_all_template_families"]
        and automated_checks["smoke_minimum_per_template_family"] >= 2
        and automated_checks["ticket_choice_balanced"]
        and automated_checks["json_booleans_balanced"]
    )
    review_sample = build_review_sample(dataset=dataset)
    return {
        "schema_version": 1,
        "generator_version": GENERATOR_VERSION,
        "dataset_fingerprint": dataset["dataset_fingerprint"],
        "automated": {"status": "passed" if passed else "failed", "checks": automated_checks},
        "human_review": {
            "status": "pending-human",
            "required_cases": 200,
            "required_reviewers_per_case": 2,
            "completed_cases": 0,
            "sample_case_ids_sha256": review_sample["selection"]["case_ids_sha256"],
        },
        "model_calibration": {
            "status": "pending-human",
            "required_reference_models": 3,
            "completed_reference_models": 0,
            "floor_threshold": 0.10,
            "ceiling_threshold": 0.95,
        },
        "release": {
            "status": "blocked",
            "stable_eligible": False,
            "blockers": [
                "Stratified 200-case dual human review is pending.",
                "Three-reference-model calibration is pending.",
                "Ownership and privacy approval is pending; publication remains blocked.",
            ],
        },
    }
