"""M5-T06：Skill 三类形态、不可变版本与发布校验。

SkillVersion 是**已发布**的不可变资源记录（仓库主键 skill_id+version）；草稿由
SkillDraft 表示，只有 publish_skill 才把草稿冻结成版本。契约、内容 hash、发布/
弃用与选择校验同住本模块：持久仓库（resources.skills）只负责存储，本模块不依赖
任何具体实现，只要求 duck-typed 的 put(record, expected_generation=None) /
get(*keys) / list() 语义。

设计约束（roadmap 第 6 节、M5 执行计划 M5-T06）：

* kind 只有 instruction / instruction_with_resources / executable；只有
  executable 要求 entrypoint，且 entrypoint 是固定 interpreter+argv（列表），
  永不接受 shell 字符串，并携带 motte_sandbox.SandboxPolicy。
* 多个 Skill 的**顺序**是身份的一部分：SkillSetRef 保留有序绑定，
  selection_hash 对顺序敏感。
* content_hash 排除 lifecycle/published_at/content_hash/deprecated_*：草稿编辑
  与 deprecated 转换都不会改变已发布版本的内容身份。
* 声明权限不等于授权：requested_permissions 只描述申请，实际执行权限由 Run
  policy、资源与 runtime 可强制能力的交集决定（M5-G07/T07）。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Sequence

from pydantic import Field, field_validator, model_validator

from motte_contracts.identity import canonical_sha256
from motte_contracts.messages import Contract
from motte_sandbox.policy import SandboxPolicy

from .content_store import CONTENT_ROOT_ENV, ContentStoreCorruption, is_content_store

SKILL_SCHEMA_VERSION = 1

SKILL_KINDS: tuple[str, ...] = ("instruction", "instruction_with_resources", "executable")
#: 仓库只存已发布/已弃用记录；draft 用 SkillDraft 表示，不进入版本仓库。
SKILL_LIFECYCLES: tuple[str, ...] = ("published", "deprecated")
INJECTION_MODES: tuple[str, ...] = ("none", "system-prompt", "context-section", "native-loader")
SKILL_DEPENDENCY_KINDS: tuple[str, ...] = ("python", "node", "binary", "oci", "git")

MAX_SKILL_RESOURCES = 512
MAX_RESOURCE_PATH_DEPTH = 16
MAX_INLINE_INSTRUCTION_BYTES = 64 * 1024

#: 这些解释器本身是 shell：entrypoint 不得借助它们拼装任意命令行。
SHELL_INTERPRETERS = frozenset({
    "sh", "bash", "zsh", "dash", "ksh", "csh", "tcsh", "fish", "ash", "busybox",
    "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe", "wsl",
})
#: 解释器代码求值开关（旧常量，保留给外部调用方）：把代码字符串塞进 argv 等于
#: 绕过资源 hash 校验。发布期的真实判定见 INTERPRETER_GRAMMARS / parse_entrypoint_argv。
INTERPRETER_EVAL_FLAGS = frozenset({"-c", "-e", "--eval", "/c", "/k", "-Command", "-EncodedCommand"})


@dataclass(frozen=True)
class InterpreterGrammar:
    """某个允许的解释器家族的前置 argv 语法（F13）。

    * inline_code：开关本身携带代码字符串（python -c、node -e）。任何以它开头的
      短 token（含 -cprint(42) 这种连写）或 --name[=值] 写法都拒绝。
    * by_name：按名字加载模块/包（python -m、node -r）；被加载的入口不在已发布
      资源里，因此同样拒绝。
    * value：这些开关后面跟一个值；值不是入口文件，解析入口时必须跳过
      （python -X dev run.py 的入口是 run.py）。

    元素写法：短开关取首字母（"c"），长开关带前导 --（"--eval"）。
    """

    inline_code: frozenset[str] = frozenset()
    by_name: frozenset[str] = frozenset()
    value: frozenset[str] = frozenset()


#: 明确列出语法的解释器家族。未列出的解释器只应用保守的通用规则：入口文件必须
#: 紧跟解释器（第一个 token），任何前置开关都会让入口判定失败（fail closed）。
INTERPRETER_GRAMMARS: dict[str, InterpreterGrammar] = {
    "python": InterpreterGrammar(
        inline_code=frozenset({"c"}),
        by_name=frozenset({"m"}),
        value=frozenset({"X", "W", "Q", "--check-hash-based-pycs"}),
    ),
    "node": InterpreterGrammar(
        inline_code=frozenset({"e", "p", "--eval", "--print"}),
        by_name=frozenset({
            "r", "--require", "--import", "--loader", "--experimental-loader",
        }),
        value=frozenset({
            "C", "--conditions", "--max-old-space-size", "--title", "--openssl-config",
            "--stack-trace-limit", "--input-type",
        }),
    ),
    "ruby": InterpreterGrammar(
        inline_code=frozenset({"e"}),
        by_name=frozenset({"r"}),
        value=frozenset({"I", "K"}),
    ),
    "perl": InterpreterGrammar(
        inline_code=frozenset({"e"}),
        by_name=frozenset({"M", "m"}),
        value=frozenset({"I"}),
    ),
    "lua": InterpreterGrammar(inline_code=frozenset({"e"}), by_name=frozenset({"l"})),
    "php": InterpreterGrammar(inline_code=frozenset({"r"}), value=frozenset({"d"})),
}
#: 未列出解释器的保守语法：沿用旧规则覆盖的写法（-c/-e/--eval，含连写）。
_GENERIC_GRAMMAR = InterpreterGrammar(inline_code=frozenset({"c", "e", "--eval"}))
#: 常见别名归一到上表的家族名。
_INTERPRETER_ALIASES = {"nodejs": "node", "pypy": "python", "pypy3": "python", "cpython": "python"}
#: 加载器注入类环境变量永不放行（env allowlist 只允许名字，不允许这些名字）。
FORBIDDEN_ENV_NAMES = frozenset({
    "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME",
    "NODE_OPTIONS", "NODE_PATH", "BASH_ENV", "ENV", "GIT_SSH_COMMAND", "PERL5OPT",
})
SHELL_METACHARACTERS: tuple[str, ...] = (
    ";", "|", "&", "$(", "`", ">", "<", "\n", "\r", "\x00",
)

_REFERENCE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,127}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_MEDIA_TYPE = re.compile(r"^[a-z]+/[A-Za-z0-9][A-Za-z0-9.+-]{0,127}$")
_FIXED_VERSION = re.compile(r"^\d+(?:\.\d+)*(?:[-+][A-Za-z0-9._-]+)?$")
_DIGEST_PIN = re.compile(r"^(?:sha256:[0-9a-f]{64}|[0-9a-f]{40})$")
_CONTENT_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_SCRIPT_SUFFIXES = (".py", ".js", ".mjs", ".cjs", ".ts", ".sh", ".rb", ".pl", ".lua")
#: 与 M4 规则 6 一致：凭据引用只能是档案名，出现密钥前缀即拒绝。
_SECRET_PREFIXES = (
    "sk-", "pk-", "rk-", "gsk-", "ghp_", "gho_", "ghu_", "xoxb-", "xoxp-",
    "AKIA", "AIza", "Bearer ", "eyJ",
)
#: 内容身份排除的存储地址与生命周期元数据。
_CONTENT_EXCLUDED = frozenset({
    "lifecycle", "published_at", "content_hash", "deprecated_at", "deprecated_reason",
})
#: 生命周期转换唯一允许改写的字段（F20）：其余发布元数据必须保持原值。
_LIFECYCLE_FIELDS = frozenset({"lifecycle", "deprecated_at", "deprecated_reason"})


class SkillError(ValueError):
    """Skill 发布/选择错误的基类；每个子类有稳定错误码。"""

    code = "skill_error"


class SkillVersionConflict(SkillError):
    code = "skill_version_conflict"


class SkillNotFound(SkillError):
    code = "skill_not_found"


class SkillDeprecated(SkillError):
    code = "skill_deprecated"


class SkillContentHashMismatch(SkillError):
    code = "skill_content_hash_mismatch"


class SkillResourceMissing(SkillError):
    code = "skill_resource_missing"


class SkillResourceMismatch(SkillError):
    code = "skill_resource_mismatch"


class SkillDependencyNotPinned(SkillError):
    code = "unpinned_dependency"


class SkillDependencyUnavailable(SkillError):
    """依赖虽然固定了版本，但解析器证明该版本不存在（发布拒绝）。"""

    code = "skill_dependency_unavailable"


class SkillResourceStoreRequired(SkillError):
    """非空资源清单必须有可按内容地址读回字节的存储；缺失时发布一律拒绝。"""

    code = "skill_resource_store_required"


def _canonical_utc(value: Any, field: str) -> str:
    """只接受规范 UTC RFC3339 时间戳，避免同一时刻多种写法产生不同记录。"""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as error:
        raise ValueError(f"{field} must be timezone-aware ISO 8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware ISO 8601")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _relative_path(value: Any, field: str) -> str:
    """相对 POSIX 路径：无盘符、无反斜杠、无 . / .. 段，深度有上限。"""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty relative path")
    if "\\" in value:
        raise ValueError(f"{field} must use forward slashes: {value!r}")
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value) is not None:
        raise ValueError(f"{field} must be relative: {value!r}")
    segments = value.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise ValueError(f"{field} must not contain empty or traversal segments: {value!r}")
    if len(segments) > MAX_RESOURCE_PATH_DEPTH:
        raise ValueError(f"{field} exceeds {MAX_RESOURCE_PATH_DEPTH} path segments")
    return value


def _content_hash_shape(value: str) -> str:
    if _CONTENT_HASH.fullmatch(value) is None:
        raise ValueError("content_hash must be a canonical sha256 digest")
    return value


def is_exact_pin(version: Any) -> bool:
    """依赖必须显式固定版本；范围、通配、latest 与 URL 全部不算固定。"""
    if not isinstance(version, str) or not version or version != version.strip():
        return False
    if _DIGEST_PIN.fullmatch(version) is not None:
        return True
    return _FIXED_VERSION.fullmatch(version) is not None


class SkillResource(Contract):
    """资源清单条目：内容寻址（sha256）+ 字节数 + 媒体类型。"""

    path: str
    sha256: str
    size_bytes: int = Field(strict=True)
    media_type: str = "text/plain"

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        return _relative_path(value, "resource.path")

    @field_validator("sha256")
    @classmethod
    def _sha256(cls, value: str) -> str:
        return _content_hash_shape(value)

    @field_validator("size_bytes", mode="before")
    @classmethod
    def _size(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("resource.size_bytes must be a non-negative integer")
        return value

    @field_validator("media_type")
    @classmethod
    def _media_type(cls, value: str) -> str:
        if not isinstance(value, str) or _MEDIA_TYPE.fullmatch(value) is None:
            raise ValueError(f"resource.media_type is not a media type: {value!r}")
        return value


class SkillDependency(Contract):
    """依赖引用：名称 + **精确**版本（或 digest/commit），不允许范围与 latest。"""

    name: str
    version: str
    kind: str = "python"
    integrity: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if _REFERENCE.fullmatch(value) is None:
            raise ValueError(f"dependency.name is not a valid name: {value!r}")
        return value

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        if value not in SKILL_DEPENDENCY_KINDS:
            raise ValueError(f"unknown dependency kind: {value!r}")
        return value

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        if not is_exact_pin(value):
            raise ValueError(
                "dependency.version must be an exact pin (no ranges, wildcards or latest): "
                f"{value!r}"
            )
        return value

    @field_validator("integrity")
    @classmethod
    def _integrity(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _content_hash_shape(value)


class SkillFixtureRef(Contract):
    """对已发布 Fixture 版本的固定引用（fixture 内容归 M5-T02）。"""

    fixture_id: str
    version: int = Field(default=1, strict=True)
    content_hash: str | None = None

    @field_validator("fixture_id")
    @classmethod
    def _fixture_id(cls, value: str) -> str:
        if _REFERENCE.fullmatch(value) is None:
            raise ValueError(f"fixture_id is not a valid resource name: {value!r}")
        return value

    @field_validator("version", mode="before")
    @classmethod
    def _version(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("fixture version must be a positive integer")
        return value

    @field_validator("content_hash")
    @classmethod
    def _hash(cls, value: str | None) -> str | None:
        return None if value is None else _content_hash_shape(value)


class SkillPermissions(Contract):
    """Skill 申请的权限；声明不是授权，deny 优先由执行层交集决定。"""

    tools: tuple[str, ...] = ()
    filesystem_read: tuple[str, ...] = ()
    filesystem_write: tuple[str, ...] = ()
    network: Literal["none"] = "none"
    credential_refs: tuple[str, ...] = ()

    @field_validator("tools")
    @classmethod
    def _tools(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for name in value:
            if _REFERENCE.fullmatch(name) is None:
                raise ValueError(f"permission tool is not a valid name: {name!r}")
        return _unique(value, "requested_permissions.tools")

    @field_validator("filesystem_read", "filesystem_write")
    @classmethod
    def _paths(cls, value: tuple[str, ...], info: Any) -> tuple[str, ...]:
        for path in value:
            _relative_path(path, f"requested_permissions.{info.field_name}")
        return _unique(value, f"requested_permissions.{info.field_name}")

    @field_validator("credential_refs")
    @classmethod
    def _credentials(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for name in value:
            if _REFERENCE.fullmatch(name) is None or name.lower().startswith(
                tuple(prefix.lower() for prefix in _SECRET_PREFIXES)
            ):
                raise ValueError(
                    "requested_permissions.credential_refs are profile names, never secrets"
                )
        return _unique(value, "requested_permissions.credential_refs")


def _unique(values: tuple[str, ...], field: str) -> tuple[str, ...]:
    if len(values) != len(set(values)):
        raise ValueError(f"{field} must be unique")
    return values


def interpreter_family(interpreter: Any) -> str:
    """把 python3.12 / nodejs / lua5.4 归一成家族名（python / node / lua）。"""
    program = str(interpreter).strip().lower()
    program = re.sub(r"[.-]?\d+(?:\.\d+)*$", "", program) or program
    return _INTERPRETER_ALIASES.get(program, program)


def interpreter_grammar(interpreter: Any) -> InterpreterGrammar:
    """该解释器的 argv 语法；未列出的解释器使用保守的通用语法。"""
    if not isinstance(interpreter, str):
        return _GENERIC_GRAMMAR
    return INTERPRETER_GRAMMARS.get(interpreter_family(interpreter), _GENERIC_GRAMMAR)


def _reject_code_flag(grammar: InterpreterGrammar, flag: str, token: str) -> None:
    if flag in grammar.inline_code or flag in grammar.by_name:
        raise ValueError(
            "entrypoint.argv must not evaluate an inline code string or load a module by "
            "name (no -c/-e/-p/-m/-r/--eval/--print/--require): keep code in hashed "
            f"resources: {token!r}"
        )


def parse_entrypoint_argv(interpreter: Any, argv: Sequence[str]) -> str:
    """按解释器语法解析入口 argv，返回入口文件 token；违规写法抛 ValueError。

    规则（M5-T06 规则 1、R5/F13）：

    * 内联代码（-c、-e、-p、--eval，含 -cprint(42) 连写）与按名加载模块（-m、
      -r、--require）一律拒绝：它们把未发布、未校验的代码带进进程。
    * "-" 表示从 stdin 读程序，同样拒绝。
    * 第一个位置参数是入口文件；已知解释器的取值开关会跳过它的值。清单层面的
      "入口必须是已发布资源"由 SkillVersion._entrypoint_resources 核对。
    """
    tokens = list(argv)
    if not tokens:
        raise ValueError("entrypoint.argv must be a non-empty argument list")
    grammar = interpreter_grammar(interpreter)
    entry: str | None = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not isinstance(token, str) or token == "":
            raise ValueError("entrypoint.argv parts must be non-empty strings")
        if token == "-":
            raise ValueError("entrypoint.argv must not read the program from stdin")
        if token.startswith("--"):
            name = "--" + token[2:].split("=", 1)[0]
            _reject_code_flag(grammar, name, token)
            index += 2 if name in grammar.value and "=" not in token else 1
            continue
        if token.startswith("-"):
            short = token[1:]
            _reject_code_flag(grammar, short[0], token)
            if short[0] in grammar.value:
                index += 2 if len(short) == 1 else 1
            else:
                index += 1
            continue
        if entry is None:
            entry = token
        index += 1
    if entry is None:
        raise ValueError(
            "entrypoint.argv must name a declared resource entry file as its first "
            "positional argument: inline code is not a published resource"
        )
    return entry


class SkillEntrypoint(Contract):
    """executable Skill 的受控入口：固定 interpreter + argv 列表，不经 shell。"""

    interpreter: str
    argv: tuple[str, ...] = Field(min_length=1)
    cwd: str = "."
    env_allowlist: tuple[str, ...] = ()
    sandbox: SandboxPolicy = SandboxPolicy()

    @field_validator("interpreter")
    @classmethod
    def _interpreter(cls, value: str) -> str:
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError("entrypoint.interpreter must be a non-empty program name")
        if "/" in value or "\\" in value:
            raise ValueError("entrypoint.interpreter must be a bare program name, not a path")
        if value.lower() in SHELL_INTERPRETERS:
            raise ValueError(
                f"entrypoint.interpreter must not be a shell: {value!r}; declare fixed argv instead"
            )
        _reject_shell_fragment(value, "entrypoint.interpreter")
        return value

    @field_validator("argv")
    @classmethod
    def _argv(cls, value: tuple[str, ...], info: Any) -> tuple[str, ...]:
        if not value:
            raise ValueError("entrypoint.argv must be a non-empty argument list")
        for part in value:
            if not isinstance(part, str) or part == "":
                raise ValueError("entrypoint.argv parts must be non-empty strings")
            _reject_shell_fragment(part, "entrypoint.argv")
        # 解释器语法在契约层就生效：内联代码（含 -cprint(42) 连写）与按名加载模块
        # 一律拒绝，绝不等到发布或执行期。interpreter 校验失败时退回通用语法。
        parse_entrypoint_argv(info.data.get("interpreter", ""), value)
        return tuple(value)

    @field_validator("cwd")
    @classmethod
    def _cwd(cls, value: str) -> str:
        if value == ".":
            return value  # 就地执行（workspace root），不是穿越
        return _relative_path(value, "entrypoint.cwd")

    @field_validator("env_allowlist")
    @classmethod
    def _env(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for name in value:
            if _ENV_NAME.fullmatch(name) is None:
                raise ValueError(f"entrypoint.env_allowlist entry is not an env name: {name!r}")
            if name in FORBIDDEN_ENV_NAMES:
                raise ValueError(f"entrypoint.env_allowlist must not pass {name!r}")
        return _unique(value, "entrypoint.env_allowlist")

    @model_validator(mode="after")
    def _consistent_with_sandbox(self) -> SkillEntrypoint:
        allowed = self.sandbox.commands.allowed_programs
        if allowed is not None and self.interpreter not in allowed:
            raise ValueError(
                "entrypoint interpreter is not in sandbox.commands.allowed_programs: "
                f"{self.interpreter!r}"
            )
        undeclared = sorted(set(self.sandbox.environment) - set(self.env_allowlist))
        if undeclared:
            raise ValueError(
                "sandbox environment names must be declared in env_allowlist: " + repr(undeclared)
            )
        return self

    def command(self) -> tuple[str, ...]:
        """实际命令行 = interpreter + argv；调用方不得再做 shell 拼接。"""
        return (self.interpreter, *self.argv)

    def entry_file(self) -> str:
        """入口文件 token（相对路径规范化为资源清单里的写法）。"""
        return parse_entrypoint_argv(self.interpreter, list(self.argv)).removeprefix("./")

    def validated_command(self) -> tuple[str, ...]:
        """用 argv 语法与 SandboxPolicy 再校验一次，fail closed（含 forbidden_programs）。

        构造期已经校验过；这里重跑一次是为了挡住 model_construct / 直接改字段等绕过
        pydantic 的调用方（F13）。
        """
        parse_entrypoint_argv(self.interpreter, list(self.argv))
        return tuple(self.sandbox.commands.validate_command(list(self.command())))


def _reject_shell_fragment(value: str, field: str) -> None:
    for fragment in SHELL_METACHARACTERS:
        if fragment in value:
            raise ValueError(f"{field} contains a shell fragment {fragment!r}: {value!r}")


class SkillBinding(Contract):
    """对一个已发布 Skill 版本的固定引用（可选择携带内容 hash）。"""

    skill_id: str
    version: str
    content_hash: str | None = None

    @field_validator("skill_id")
    @classmethod
    def _skill_id(cls, value: str) -> str:
        if _REFERENCE.fullmatch(value) is None:
            raise ValueError(f"skill_id is not a valid resource name: {value!r}")
        return value

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError("skill version must be a non-empty string")
        return value

    @field_validator("content_hash")
    @classmethod
    def _hash(cls, value: str | None) -> str | None:
        return None if value is None else _content_hash_shape(value)

    @property
    def ref(self) -> str:
        return f"{self.skill_id}@{self.version}"


class SkillSetRef(Contract):
    """有序 Skill 集合引用：顺序是身份的一部分，selection_hash 对顺序敏感。"""

    skills: tuple[SkillBinding, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_skills(self) -> SkillSetRef:
        seen = [binding.skill_id for binding in self.skills]
        if len(seen) != len(set(seen)):
            raise ValueError("a skill set must reference each skill_id at most once")
        return self

    def selection_hash(self) -> str:
        return canonical_sha256([binding.model_dump(mode="json") for binding in self.skills])


class SkillVersion(Contract):
    """已发布的不可变 Skill 版本资源（仓库主键 skill_id+version）。

    只有 executable 需要 entrypoint；instruction / instruction_with_resources
    声明 entrypoint 反而是错误：那会把静态校验谎称为可执行入口验证（M5-A10）。
    """

    skill_id: str
    version: str
    schema_version: int = Field(default=SKILL_SCHEMA_VERSION, strict=True)
    kind: Literal["instruction", "instruction_with_resources", "executable"]
    description: str | None = None
    instruction: str | None = None
    instruction_ref: str | None = None
    resource_manifest: tuple[SkillResource, ...] = ()
    dependency_refs: tuple[SkillDependency, ...] = ()
    requested_permissions: SkillPermissions = Field(default_factory=SkillPermissions)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    fixture_refs: tuple[SkillFixtureRef, ...] = ()
    injection_mode: str = "system-prompt"
    entrypoint: SkillEntrypoint | None = None
    lifecycle: Literal["published", "deprecated"] = "published"
    published_at: str
    deprecated_at: str | None = None
    deprecated_reason: str | None = None
    content_hash: str | None = None

    @field_validator("skill_id")
    @classmethod
    def _skill_id(cls, value: str) -> str:
        if _REFERENCE.fullmatch(value) is None:
            raise ValueError(f"skill_id is not a valid resource name: {value!r}")
        return value

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError("skill version must be a non-empty string")
        return value

    @field_validator("instruction_ref")
    @classmethod
    def _instruction_ref(cls, value: str | None) -> str | None:
        return None if value is None else _relative_path(value, "instruction_ref")

    @field_validator("instruction")
    @classmethod
    def _instruction(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("inline instruction must be non-empty")
        if len(value.encode("utf-8")) > MAX_INLINE_INSTRUCTION_BYTES:
            raise ValueError(
                f"inline instruction exceeds {MAX_INLINE_INSTRUCTION_BYTES} bytes; "
                "use instruction_ref with a hashed resource"
            )
        return value

    @field_validator("injection_mode")
    @classmethod
    def _injection_mode(cls, value: str) -> str:
        if value not in INJECTION_MODES:
            raise ValueError(f"unknown injection mode: {value!r}")
        return value

    @field_validator("published_at")
    @classmethod
    def _published_at(cls, value: str | None) -> str | None:
        # SkillDraft 允许 None；非草稿必须给出规范 UTC 时间戳（_structure 再收紧）。
        return None if value is None else _canonical_utc(value, "published_at")

    @field_validator("deprecated_at")
    @classmethod
    def _deprecated_at(cls, value: str | None) -> str | None:
        return None if value is None else _canonical_utc(value, "deprecated_at")

    @field_validator("content_hash")
    @classmethod
    def _content_hash(cls, value: str | None) -> str | None:
        return None if value is None else _content_hash_shape(value)

    @model_validator(mode="after")
    def _structure(self) -> SkillVersion:
        if self.schema_version != SKILL_SCHEMA_VERSION:
            raise ValueError(f"unsupported skill schema_version {self.schema_version}")
        if len(self.resource_manifest) > MAX_SKILL_RESOURCES:
            raise ValueError(f"skill declares more than {MAX_SKILL_RESOURCES} resources")
        paths = [entry.path for entry in self.resource_manifest]
        if len(paths) != len(set(paths)):
            raise ValueError("resource_manifest paths must be unique")
        if len({ref.name for ref in self.dependency_refs}) != len(self.dependency_refs):
            raise ValueError("dependency_refs must reference distinct dependency names")
        if len({ref.fixture_id for ref in self.fixture_refs}) != len(self.fixture_refs):
            raise ValueError("fixture_refs must reference distinct fixture ids")
        if self.instruction is not None and self.instruction_ref is not None:
            raise ValueError("declare either inline instruction or instruction_ref, not both")
        if self.instruction_ref is not None and self.instruction_ref not in paths:
            raise ValueError(
                f"instruction_ref must be a declared resource: {self.instruction_ref!r}"
            )
        if self.kind == "instruction":
            if self.resource_manifest:
                raise ValueError(
                    "instruction skills carry no resources; use instruction_with_resources"
                )
            if self.entrypoint is not None:
                raise ValueError("instruction skills cannot declare an entrypoint")
            if self.instruction is None and self.instruction_ref is None:
                raise ValueError("instruction skills require inline instruction or instruction_ref")
            if self.injection_mode == "none":
                raise ValueError("instruction skills must declare an injection mode")
        elif self.kind == "instruction_with_resources":
            if not self.resource_manifest:
                raise ValueError("instruction_with_resources skills require a resource_manifest")
            if self.entrypoint is not None:
                raise ValueError("instruction_with_resources skills cannot declare an entrypoint")
            if self.instruction is None and self.instruction_ref is None:
                raise ValueError(
                    "instruction_with_resources skills require inline instruction or instruction_ref"
                )
            if self.injection_mode == "none":
                raise ValueError("instruction_with_resources skills must declare an injection mode")
        else:  # executable
            if self.entrypoint is None:
                raise ValueError("executable skills require a declared entrypoint")
            if not self.input_schema or not self.output_schema:
                raise ValueError("executable skills require input_schema and output_schema")
            self._entrypoint_resources(paths)
        if self.lifecycle == "published":
            if not self.published_at:
                raise ValueError("published skills require published_at")
            if self.deprecated_at is not None or self.deprecated_reason is not None:
                raise ValueError("published skills cannot carry deprecation metadata")
        elif self.lifecycle == "deprecated":
            if not self.published_at or self.deprecated_at is None:
                raise ValueError("deprecated skills require published_at and deprecated_at")
        elif self.deprecated_at is not None or self.deprecated_reason is not None:
            raise ValueError("draft skills cannot carry deprecation metadata")
        return self

    def _entrypoint_resources(self, paths: list[str]) -> None:
        """入口必须绑定到已发布、已核验的资源文件（F13）。

        第一位置参数就是入口文件，必须在 resource_manifest 里；其余位置参数里凡是
        脚本后缀或带路径分隔符的 token（它们同样可能是被执行的代码）也必须已声明。
        """
        assert self.entrypoint is not None
        entry = self.entrypoint.entry_file()
        if entry not in paths:
            raise ValueError(
                f"entrypoint entry file must be a declared resource: {entry!r}"
            )
        for part in self.entrypoint.argv:
            token = part.removeprefix("./")
            if token in paths or part.startswith("-"):
                continue
            if token.endswith(_SCRIPT_SUFFIXES) or "/" in token or "\\" in token:
                raise ValueError(f"entrypoint script must be a declared resource: {token!r}")

    @property
    def ref(self) -> str:
        return f"{self.skill_id}@{self.version}"

    @property
    def name(self) -> str:
        """旧调用方兼容别名（SkillManifest.name）。"""
        return self.skill_id

    @property
    def resource_paths(self) -> tuple[str, ...]:
        return tuple(entry.path for entry in self.resource_manifest)

    def effective_content_hash(self) -> str:
        computed = skill_content_hash(self)
        if self.content_hash is not None and self.content_hash != computed:
            raise SkillContentHashMismatch("skill content_hash does not match its content")
        return computed


class SkillDraft(SkillVersion):
    """可编辑草稿；published() 时才冻结成带 published_at 的 SkillVersion。"""

    lifecycle: Literal["draft"] = "draft"
    published_at: str | None = None
    deprecated_at: None = None
    deprecated_reason: None = None

    def edited(self, **changes: Any) -> SkillDraft:
        """受校验的编辑：返回新草稿，绝不静默接受非法字段。"""
        return SkillDraft.model_validate({**self.model_dump(mode="json"), **changes})

    def published(self, published_at: str | None = None) -> SkillVersion:
        payload = {
            **self.model_dump(mode="json"),
            "lifecycle": "published",
            "published_at": published_at or _utc_now(),
        }
        return SkillVersion.model_validate(payload)


def _skill_model(record: Any) -> SkillVersion | SkillDraft:
    """把任意记录（模型/映射）规范化为契约模型；未知字段与非法字段都会被拒绝。"""
    if isinstance(record, (SkillVersion, SkillDraft)):
        return record
    if hasattr(record, "model_dump"):
        record = record.model_dump(mode="json")
    if not isinstance(record, dict):
        raise TypeError("skill record must be a contract model or a mapping")
    payload = dict(record)
    if payload.get("lifecycle", "published") == "draft":
        return SkillDraft.model_validate(payload)
    return SkillVersion.model_validate(payload)


def _content_payload(record: SkillVersion | SkillDraft) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.model_dump(mode="json").items()
        if key not in _CONTENT_EXCLUDED
    }


def skill_content_hash(record: Any) -> str:
    """内容身份：排除存储地址与生命周期元数据。

    草稿与已发布版本对同一内容得到同一 hash，因此编辑草稿不会改变已发布记录
    的 hash（已发布行是不可变的）；反过来任何内容改动都会改变 hash。
    """
    return canonical_sha256(_content_payload(_skill_model(record)))


def _publication_payload(record: SkillVersion | SkillDraft) -> dict[str, Any]:
    """发布元数据（不可改写）：除生命周期三字段外的全部字段。

    F20：published_at、content_hash、资源清单等都属于发布身份；只有 lifecycle 与
    deprecated_* 允许在弃用转换里变化。注意它和 skill_content_hash 用的
    _content_payload 不同：后者必须允许草稿（无 published_at）与已发布版本同 hash。
    """
    return {
        key: value
        for key, value in record.model_dump(mode="json").items()
        if key not in _LIFECYCLE_FIELDS
    }


def skill_version_transition(existing: Any, record: Any) -> str:
    """同一 (skill_id, version) 上允许的转换：identical / deprecation / conflict。

    只有 lifecycle/deprecated_* 可以变化；published_at 与 content_hash 等发布元数据
    必须保持原值，否则一律 conflict（F20）。
    """
    before = _skill_model(existing)
    after = _skill_model(record)
    if _publication_payload(before) != _publication_payload(after):
        return "conflict"
    if (
        before.lifecycle == after.lifecycle
        and before.deprecated_at == after.deprecated_at
        and before.deprecated_reason == after.deprecated_reason
    ):
        return "identical"
    if before.lifecycle == "published" and after.lifecycle == "deprecated":
        return "deprecation"
    return "conflict"


def is_deprecation_transition(existing: Any, record: Any) -> bool:
    """存储层可用的单行判定：只有 lifecycle/deprecated_* 变化时才允许写入。"""
    return skill_version_transition(existing, record) == "deprecation"


def require_content_store(store: Any) -> Any:
    """非空资源清单的发布前置条件：必须有可按内容地址读回字节的存储（F12）。

    旧实现把 resource_store=None 当作"没有字节要验"，于是 Memory 与 SQLite 都能发布
    声明任意 hash/size 的资源。这里 fail closed：没有可用存储就不发布。
    """
    if not is_content_store(store):
        raise SkillResourceStoreRequired(
            "skill resources require a readable content-addressed store; configure "
            f"{CONTENT_ROOT_ENV} (motte_skill.content_store.create_content_store) or pass "
            "resource_store=... to publish_skill"
        )
    return store


def verify_dependency_refs(refs: Any, resolver: Any = None) -> tuple[SkillDependency, ...]:
    """发布期再核验依赖：固定版本（契约之外的第二道防线）+ 版本确实存在。

    resolver 是可选的可调用对象（接收 SkillDependency，返回它是否存在）。没有配置
    resolver 时只核验 pin；resolver 报不存在或自己失败都拒绝发布（fail closed）。
    """
    verified: list[SkillDependency] = []
    for item in refs:
        raw = item if isinstance(item, dict) else getattr(item, "__dict__", {})
        version = raw.get("version") if isinstance(raw, dict) else None
        if version is None and isinstance(item, SkillDependency):
            version = item.version
        if not is_exact_pin(version):
            name = raw.get("name") if isinstance(raw, dict) else getattr(item, "name", None)
            raise SkillDependencyNotPinned(
                f"dependency {name!r} is not pinned to an exact version: {version!r}"
            )
        dependency = (
            item if isinstance(item, SkillDependency) else SkillDependency.model_validate(item)
        )
        verified.append(dependency)
    if resolver is not None:
        for dependency in verified:
            try:
                available = bool(resolver(dependency))
            except Exception as error:  # noqa: BLE001 - 解析失败按"依赖不可用"处理
                raise SkillDependencyUnavailable(
                    f"dependency {dependency.name}@{dependency.version} could not be resolved: "
                    f"{error}"
                ) from error
            if not available:
                raise SkillDependencyUnavailable(
                    f"dependency {dependency.name}@{dependency.version} "
                    f"({dependency.kind}) does not exist"
                )
    return tuple(verified)


def verify_resources(store: Any, entries: Any) -> tuple[SkillResource, ...]:
    """按内容寻址重新读取并核验字节；缺失、漂移或损坏都拒绝发布。"""
    verified: list[SkillResource] = []
    for item in entries:
        entry = item if isinstance(item, SkillResource) else SkillResource.model_validate(item)
        try:
            data = require_content_store(store).get(entry.sha256)
        except KeyError as error:
            raise SkillResourceMissing(
                f"resource bytes are missing from the content store: {entry.sha256}"
            ) from error
        except ContentStoreCorruption as error:
            raise SkillResourceMismatch(
                f"resource bytes are corrupted for {entry.path}: expected {entry.sha256}"
            ) from error
        if data is None:
            raise SkillResourceMissing(
                f"resource bytes are missing from the content store: {entry.sha256}"
            )
        digest = "sha256:" + hashlib.sha256(bytes(data)).hexdigest()
        if digest != entry.sha256 or len(data) != entry.size_bytes:
            raise SkillResourceMismatch(
                f"resource bytes drifted for {entry.path}: expected {entry.sha256}"
            )
        verified.append(entry)
    return tuple(verified)


def _revalidated(record: SkillVersion | SkillDraft) -> SkillVersion | SkillDraft:
    """发布前从 JSON 形状重跑一次契约校验。

    调用方可能用 model_construct 或直接改字段绕过 pydantic；发布是最后一道边界，
    这里重新验证 argv 语法、entrypoint 与资源的绑定、kind/lifecycle 规则。
    """
    payload = record.model_dump(mode="json")
    if payload.get("lifecycle") == "draft":
        return SkillDraft.model_validate(payload)
    return SkillVersion.model_validate(payload)


def publish_skill(
    repository: Any,
    record: Any,
    *,
    resource_store: Any = None,
    published_at: str | None = None,
    dependency_resolver: Any = None,
) -> dict[str, Any]:
    """把一个草稿/内容发布进版本仓库，返回仓库中的记录。

    规则由本函数自身强制（不依赖存储实现）：

    * 同 (skill_id, version) 同内容 => 幂等，返回既有记录，不重写；
    * 同版本异内容 => SkillVersionConflict；
    * published -> deprecated 的纯生命周期转换允许写入（弃用）；
    * 非空资源清单必须有可读写字节的内容存储，且逐个核验（F12）；
    * 依赖必须固定版本；配置了 resolver 时还必须能解析到（版本不存在即拒绝）。
    """
    model = _revalidated(_skill_model(record))
    if model.lifecycle == "draft":
        model = _revalidated(model.published(published_at))
    elif published_at is not None and model.published_at != published_at:
        raise SkillError("published_at is fixed when a version is first published")
    computed = skill_content_hash(model)
    if model.content_hash is not None and model.content_hash != computed:
        raise SkillContentHashMismatch("skill content_hash does not match its content")
    verify_dependency_refs(model.dependency_refs, resolver=dependency_resolver)
    if model.resource_manifest:
        verify_resources(require_content_store(resource_store), model.resource_manifest)
    payload = {**model.model_dump(mode="json"), "content_hash": computed}
    existing = repository.get(model.skill_id, model.version)
    if existing is None:
        return repository.put(payload)
    transition = skill_version_transition(existing, payload)
    if transition == "identical":
        return existing
    if transition == "deprecation":
        return repository.put(payload)
    raise SkillVersionConflict(
        f"skill version {model.ref} already exists with different content; use a new version"
    )


def deprecate_skill(
    repository: Any,
    skill_id: str,
    version: str,
    *,
    deprecated_at: str | None = None,
    reason: str | None = None,
    expected_generation: int | None = None,
) -> dict[str, Any]:
    """弃用已发布版本：停止新选择，但历史读取仍拿到同一内容 hash 与资源。"""
    existing = repository.get(skill_id, version)
    if existing is None:
        raise SkillNotFound(f"skill version {skill_id}@{version} is not published")
    if existing.get("lifecycle") == "deprecated":
        return existing
    record = {
        **existing,
        "lifecycle": "deprecated",
        "deprecated_at": _canonical_utc(deprecated_at or _utc_now(), "deprecated_at"),
        "deprecated_reason": reason,
    }
    if not is_deprecation_transition(existing, record):
        raise SkillVersionConflict("only published skill versions can be deprecated")
    return repository.put(record, expected_generation=expected_generation)


def select_skills(
    repository: Any,
    selection: Any,
    *,
    allow_deprecated: bool = False,
) -> tuple[SkillVersion, ...]:
    """按声明顺序解析一个 Skill 集合；deprecated 阻止**新选择**。

    allow_deprecated=True 只用于历史 Run 重读冻结快照，不表示可以重新选中。
    """
    if isinstance(selection, SkillSetRef):
        set_ref = selection
    elif isinstance(selection, (list, tuple)):
        set_ref = SkillSetRef(skills=tuple(selection))
    else:
        set_ref = SkillSetRef.model_validate(selection)
    selected: list[SkillVersion] = []
    for binding in set_ref.skills:
        raw = repository.get(binding.skill_id, binding.version)
        if raw is None:
            raise SkillNotFound(f"skill version {binding.ref} is not published")
        record = SkillVersion.model_validate(raw)
        if record.lifecycle == "deprecated" and not allow_deprecated:
            raise SkillDeprecated(
                f"skill version {binding.ref} is deprecated; select a new version"
            )
        if binding.content_hash is not None and record.content_hash != binding.content_hash:
            raise SkillContentHashMismatch(
                f"skill version {binding.ref} content hash does not match the selection"
            )
        selected.append(record)
    return tuple(selected)
