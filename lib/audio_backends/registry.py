"""音频后端注册与工厂。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from lib.audio_backends.base import AudioBackend, MusicBackend

_BACKEND_FACTORIES: dict[str, Callable[..., AudioBackend]] = {}


def register_backend(name: str, factory: Callable[..., AudioBackend]) -> None:
    """注册一个音频后端工厂函数。"""
    _BACKEND_FACTORIES[name] = factory


def create_backend(name: str, **kwargs: Any) -> AudioBackend:
    """根据名称创建音频后端实例。"""
    if name not in _BACKEND_FACTORIES:
        raise ValueError(f"Unknown audio backend: {name}")
    return _BACKEND_FACTORIES[name](**kwargs)


def get_registered_backends() -> list[str]:
    """返回所有已注册的后端名称。"""
    return list(_BACKEND_FACTORIES.keys())


# 音乐后端与 TTS 分表：同一个 provider 名下两者可以只有其一（qwen3-tts 只做 TTS、
# ACE-Step 只做音乐），共用一张表会让 create_backend 拿到不满足协议的实例。
_MUSIC_BACKEND_FACTORIES: dict[str, Callable[..., MusicBackend]] = {}


def register_music_backend(name: str, factory: Callable[..., MusicBackend]) -> None:
    """注册一个音乐后端工厂函数。"""
    _MUSIC_BACKEND_FACTORIES[name] = factory


def create_music_backend(name: str, **kwargs: Any) -> MusicBackend:
    """根据名称创建音乐后端实例。"""
    if name not in _MUSIC_BACKEND_FACTORIES:
        raise ValueError(f"Unknown music backend: {name}")
    return _MUSIC_BACKEND_FACTORIES[name](**kwargs)


def get_registered_music_backends() -> list[str]:
    """返回所有已注册的音乐后端名称。"""
    return list(_MUSIC_BACKEND_FACTORIES.keys())
