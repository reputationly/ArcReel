"""剧本结构校验器（纯函数）。

把「一个剧本 dict 是否结构良构」这个判断收敛到唯一一处：喂入 dict、返回
`ValidationResult`，不读磁盘、不依赖项目状态。写盘统一入口、测试、未来其它写入面都复用它。

校验对象是结构层 Pydantic 模型（`lib.script_models`），而非 FS 感知的 `DataValidator`
——后者会读磁盘并拒绝合法的半成品草稿（分镜图尚未生成）。模型已编码所有结构约束
（必填、`duration_seconds` 范围、id 格式、prompt 形状、参考单元 shots↔duration 一致性），
本校验器只负责「按模式选对模型」并把 Pydantic 的 `ValidationError` 转成 `ValidationResult`，
不复制任何约束——模型变更即校验变更（单一真相源）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError
from pydantic_core import ErrorDetails

from lib.data_validator import ValidationResult
from lib.script_models import (
    AdEpisodeScript,
    DramaEpisodeScript,
    MVEpisodeScript,
    NarrationEpisodeScript,
    ReferenceVideoScript,
)
from lib.script_skeleton import resolve_script_kind


class ScriptStructureValidationError(ValueError):
    """剧本结构校验失败。携带 `ValidationResult`，供 router 转 i18n 4xx 响应。"""

    def __init__(self, result: ValidationResult) -> None:
        self.result = result
        super().__init__("; ".join(result.errors) or "script structure invalid")


_KIND_MODEL: dict[str, type[BaseModel]] = {
    "video_units": ReferenceVideoScript,
    "scenes": DramaEpisodeScript,
    "segments": NarrationEpisodeScript,
    "shots": AdEpisodeScript,
}

#: ``shots`` 骨架下按 content_mode 再分的模型。ad 与 mv 的镜头数据形状相同（平铺数组 +
#: shot_id），字段不同（mv 有 start_seconds / section / lyrics_line / is_performance），
#: 故骨架同、模型异。骨架表只描述形状，这一层分派留在校验本地。
_SHOTS_MODEL_BY_CONTENT_MODE: dict[str, type[BaseModel]] = {
    "mv": MVEpisodeScript,
}


def model_for_kind(kind: str, content_mode: str | None) -> type[BaseModel]:
    """``(骨架种类, content_mode)`` → 剧本 Pydantic 模型。

    两个消费方共用这一张表，各自按合法途径拿 ``kind``：结构校验走取证解析
    （``resolve_script_kind``，数据形状优先），剧本生成走规范解析
    （``resolve_declared_kind``，它本就持有项目声明）。

    ``content_mode`` 必须参与：``ad`` 与 ``mv`` 同为 ``shots`` 骨架但字段不同，只按 kind 取模型
    会让 MV 的响应被拿去校验 ``AdEpisodeScript``——7 条字段错误、静默回落原始数据，于是校验对
    MV 恒为空转，还每次刷一条指不到真因的 warning。
    """
    if kind == "shots" and isinstance(content_mode, str):
        override = _SHOTS_MODEL_BY_CONTENT_MODE.get(content_mode)
        if override is not None:
            return override
    return _KIND_MODEL[kind]


def _select_model(script: dict[str, Any]) -> type[BaseModel]:
    """结构校验侧的模型选择：kind 走取证解析（数据形状优先，见 `resolve_script_kind`）。

    ``generation_mode`` 不参与判别——caller 端的生成路径(enqueue_videos 等)自己按
    generation_mode 分流;此函数只决定**结构校验**用哪个 Pydantic 模型。
    """
    return model_for_kind(resolve_script_kind(script), script.get("content_mode"))


def _format_error(err: ErrorDetails) -> str:
    loc = ".".join(str(part) for part in err.get("loc", ()))
    msg = err.get("msg", "")
    return f"{loc}: {msg}" if loc else str(msg)


def validate_script_structure(script: dict[str, Any]) -> ValidationResult:
    """校验剧本 dict 的结构是否良构，返回 `ValidationResult`。

    纯函数：不读磁盘、不查文件引用、不查跨 project.json 的角色/场景名一致性
    （那些是 `DataValidator.validate_project_tree` 的归档层职责）。
    """
    model = _select_model(script)
    try:
        model.model_validate(script)
    except ValidationError as exc:
        return ValidationResult(valid=False, errors=[_format_error(e) for e in exc.errors()])
    return ValidationResult(valid=True)
