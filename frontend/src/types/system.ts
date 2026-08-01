export interface SystemConfigSettings {
  default_video_backend: string;
  default_image_backend: string;
  default_image_backend_t2i?: string;
  default_image_backend_i2i?: string;
  default_text_backend: string;
  default_audio_backend?: string;
  narration_voice?: string;
  narration_speed?: number | null;
  text_backend_simple: string;
  text_backend_complex: string;
  /** 作曲模型。与旁白 TTS 分列——ACE-Step 只会作曲，配了 TTS 不等于能作曲。 */
  default_music_backend: string;
  /** 歌声合成模型。与作曲再分列——SoulX-Singer 只会唱、ACE-Step 只会作曲。 */
  default_singing_backend: string;
  /** 口型驱动（数字人 s2v）模型。MV 演唱镜头用它，其余镜头走常规视频模型。 */
  default_lip_sync_backend: string;
  video_generate_audio: boolean;
  video_frame_interpolation: boolean;
  image_quality_mode: boolean;
  anthropic_api_key: { is_set: boolean; masked: string | null };
  anthropic_base_url: string;
  anthropic_model: string;
  anthropic_default_haiku_model: string;
  anthropic_default_opus_model: string;
  anthropic_default_sonnet_model: string;
  claude_code_subagent_model: string;
  agent_session_cleanup_delay_seconds: number;
  agent_max_concurrent_sessions: number;
}

export interface SystemConfigOptions {
  video_backends: string[];
  image_backends: string[];
  text_backends: string[];
  // audio 桶按能力拆三份（与后端 _AUDIO_CAP_TO_BUCKET 同口径）：TTS / 作曲 / 歌声是互不
  // 兼容的协议，共用一个列表会让下拉框互相提供对方用不了的模型。
  audio_backends?: string[];
  music_backends?: string[];
  singing_backends?: string[];
  provider_names?: Record<string, string>;
}

export interface GetSystemConfigResponse {
  settings: SystemConfigSettings;
  options: SystemConfigOptions;
}

export interface SystemVersionReleaseInfo {
  version: string;
  tag_name: string;
  name: string;
  body: string;
  html_url: string;
  published_at: string;
}

export interface GetSystemVersionResponse {
  current: { version: string };
  latest: SystemVersionReleaseInfo | null;
  has_update: boolean;
  checked_at: string;
  update_check_error: string | null;
}

/** 首次使用引导的「已看过」状态 —— 实例级，未设置视为未看过。 */
export interface OnboardingStatus {
  seen: boolean;
}

export interface SystemConfigPatch {
  default_video_backend?: string;
  default_image_backend?: string;
  default_image_backend_t2i?: string;
  default_image_backend_i2i?: string;
  default_text_backend?: string;
  default_audio_backend?: string;
  narration_voice?: string;
  narration_speed?: number | null;
  text_backend_simple?: string;
  text_backend_complex?: string;
  default_music_backend?: string;
  default_singing_backend?: string;
  default_lip_sync_backend?: string;
  video_generate_audio?: boolean;
  video_frame_interpolation?: boolean;
  image_quality_mode?: boolean;
  anthropic_api_key?: string;
  anthropic_base_url?: string;
  anthropic_model?: string;
  anthropic_default_haiku_model?: string;
  anthropic_default_opus_model?: string;
  anthropic_default_sonnet_model?: string;
  claude_code_subagent_model?: string;
  agent_session_cleanup_delay_seconds?: number;
  agent_max_concurrent_sessions?: number;
}
