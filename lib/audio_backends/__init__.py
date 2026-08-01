"""语音合成（TTS）服务层公共 API。"""

from lib.audio_backends.base import (
    AudioBackend,
    AudioCapability,
    AudioSynthesisRequest,
    AudioSynthesisResult,
    MusicBackend,
    MusicGenerationRequest,
    MusicGenerationResult,
    SingingSynthesisRequest,
    SingingSynthesisResult,
)
from lib.audio_backends.registry import (
    create_backend,
    create_music_backend,
    get_registered_backends,
    get_registered_music_backends,
    register_backend,
    register_music_backend,
)

__all__ = [
    "AudioBackend",
    "AudioCapability",
    "AudioSynthesisRequest",
    "AudioSynthesisResult",
    "MusicBackend",
    "MusicGenerationRequest",
    "MusicGenerationResult",
    "SingingSynthesisRequest",
    "SingingSynthesisResult",
    "create_backend",
    "create_music_backend",
    "get_registered_backends",
    "get_registered_music_backends",
    "register_backend",
    "register_music_backend",
]

# Backend auto-registration
from lib.audio_backends.dashscope import DashScopeAudioBackend
from lib.audio_backends.newapi_music import PROVIDER_NEWAPI_MUSIC, NewAPIMusicBackend
from lib.providers import PROVIDER_DASHSCOPE

register_backend(PROVIDER_DASHSCOPE, DashScopeAudioBackend)
register_music_backend(PROVIDER_NEWAPI_MUSIC, NewAPIMusicBackend)
