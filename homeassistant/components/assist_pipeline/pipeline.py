"""Classes for voice assistant pipelines."""

from __future__ import annotations

import array
import asyncio
from collections import defaultdict, deque
from collections.abc import AsyncGenerator, AsyncIterable, Callable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
import functools
import logging
import math
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
import time
from typing import TYPE_CHECKING, Any, TypedDict, cast
import wave

import hass_nabucasa
import voluptuous as vol

from homeassistant.components import conversation, stt, tts, wake_word, websocket_api
from homeassistant.const import ATTR_SUPPORTED_FEATURES, MATCH_ALL
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    chat_session,
    device_registry as dr,
    entity_registry as er,
    intent,
)
from homeassistant.helpers.collection import (
    CHANGE_UPDATED,
    CollectionError,
    ItemNotFound,
    SerializedStorageCollection,
    StorageCollection,
    StorageCollectionWebsocket,
)
from homeassistant.helpers.singleton import singleton
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import UNDEFINED, UndefinedType, VolDictType
from homeassistant.util import (
    dt as dt_util,
    language as language_util,
    ulid as ulid_util,
)
from homeassistant.util.hass_dict import HassKey
from homeassistant.util.limited_size_dict import LimitedSizeDict

from .audio_enhancer import AudioEnhancer, EnhancedAudioChunk, MicroVadSpeexEnhancer
from .const import (
    ACKNOWLEDGE_PATH,
    BYTES_PER_CHUNK,
    CONF_DEBUG_RECORDING_DIR,
    DATA_CONFIG,
    DATA_LAST_WAKE_UP,
    DOMAIN,
    MS_PER_CHUNK,
    SAMPLE_CHANNELS,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    SAMPLES_PER_CHUNK,
    WAKE_WORD_COOLDOWN,
)
from .error import (
    DuplicateWakeUpDetectedError,
    IntentRecognitionError,
    PipelineError,
    PipelineNotFound,
    SpeechToTextError,
    TextToSpeechError,
    WakeWordDetectionAborted,
    WakeWordDetectionError,
    WakeWordTimeoutError,
)
from .vad import AudioBuffer, VoiceActivityTimeout, VoiceCommandSegmenter, chunk_samples

if TYPE_CHECKING:
    from hassil.recognize import RecognizeResult

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = f"{DOMAIN}.pipelines"
STORAGE_VERSION = 1
STORAGE_VERSION_MINOR = 2

ENGINE_LANGUAGE_PAIRS = (
    ("stt_engine", "stt_language"),
    ("tts_engine", "tts_language"),
)

KEY_ASSIST_PIPELINE: HassKey[PipelineData] = HassKey(DOMAIN)
KEY_PIPELINE_CONVERSATION_DATA: HassKey[dict[str, PipelineConversationData]] = HassKey(
    "pipeline_conversation_data"
)
# Number of response parts to handle before streaming the response
STREAM_RESPONSE_CHARS = 60


def validate_language(data: dict[str, Any]) -> Any:
    """Validate language settings."""
    for engine, language in ENGINE_LANGUAGE_PAIRS:
        if data[engine] is not None and data[language] is None:
            raise vol.Invalid(f"Need language {language} for {engine} {data[engine]}")
    return data


PIPELINE_FIELDS: VolDictType = {
    vol.Required("conversation_engine"): str,
    vol.Required("conversation_language"): str,
    vol.Required("language"): str,
    vol.Required("name"): str,
    vol.Required("stt_engine"): vol.Any(str, None),
    vol.Required("stt_language"): vol.Any(str, None),
    vol.Required("tts_engine"): vol.Any(str, None),
    vol.Required("tts_language"): vol.Any(str, None),
    vol.Required("tts_voice"): vol.Any(str, None),
    vol.Required("wake_word_entity"): vol.Any(str, None),
    vol.Required("wake_word_id"): vol.Any(str, None),
    vol.Optional("prefer_local_intents"): bool,
    vol.Optional("acknowledge_media_id"): str,
}

STORED_PIPELINE_RUNS = 10

SAVE_DELAY = 10


@callback
def _async_local_fallback_intent_filter(result: RecognizeResult) -> bool:
    """Filter out intents that are not local fallback."""
    return result.intent.name in (intent.INTENT_GET_STATE)


@callback
def _resolve_conversation(
    hass: HomeAssistant, engine_id: str | None
) -> tuple[str, str, str]:
    """Return (conversation_engine_id, conversation_language, pipeline_language)."""
    engine_id = engine_id or conversation.HOME_ASSISTANT_AGENT

    conversation_language = "en"
    pipeline_language = "en"

    languages = language_util.matches(
        hass.config.language,
        conversation.async_get_conversation_languages(hass, engine_id),
        country=hass.config.country,
    )
    if languages:
        pipeline_language = hass.config.language
        conversation_language = languages[0]

    return engine_id, conversation_language, pipeline_language


@callback
def _resolve_stt(
    hass: HomeAssistant, pipeline_language: str, engine_id: str | None
) -> tuple[str | None, str | None]:
    """Return (stt_engine_id, stt_language)."""
    engine_id = engine_id or stt.async_default_engine(hass)
    if engine_id is None:
        return None, None

    engine = stt.async_get_speech_to_text_engine(hass, engine_id)
    if engine is None:
        return None, None

    languages = language_util.matches(
        pipeline_language, engine.supported_languages, country=hass.config.country
    )
    if languages:
        return engine_id, languages[0]

    _LOGGER.debug(
        "Speech-to-text engine '%s' does not support language '%s'",
        engine_id,
        pipeline_language,
    )
    return None, None


@callback
def _resolve_tts(
    hass: HomeAssistant, pipeline_language: str, engine_id: str | None
) -> tuple[str | None, str | None, str | None]:
    """Return (tts_engine_id, tts_language, tts_voice)."""
    engine_id = engine_id or tts.async_default_engine(hass)
    if engine_id is None:
        return None, None, None

    engine = tts.get_engine_instance(hass, engine_id)
    if engine is None:
        return None, None, None

    languages = language_util.matches(
        pipeline_language, engine.supported_languages, country=hass.config.country
    )
    if not languages:
        _LOGGER.debug(
            "Text-to-speech engine '%s' does not support language '%s'",
            engine_id,
            pipeline_language,
        )
        return None, None, None

    lang = languages[0]
    voice: str | None = None
    voices = engine.async_get_supported_voices(lang)
    if voices:
        voice = voices[0].voice_id

    return engine_id, lang, voice


# ---- main function with reduced complexity ----


@callback
def _async_resolve_default_pipeline_settings(
    hass: HomeAssistant,
    *,
    conversation_engine_id: str | None = None,
    stt_engine_id: str | None = None,
    tts_engine_id: str | None = None,
    pipeline_name: str,
) -> dict[str, str | None]:
    """Resolve settings for a default pipeline.

    The default pipeline will use the homeassistant conversation agent and the
    default stt / tts engines if none are specified.
    """
    conv_engine_id, conversation_language, pipeline_language = _resolve_conversation(
        hass, conversation_engine_id
    )
    stt_engine_id, stt_language = _resolve_stt(hass, pipeline_language, stt_engine_id)
    tts_engine_id, tts_language, tts_voice = _resolve_tts(
        hass, pipeline_language, tts_engine_id
    )

    return {
        "conversation_engine": conv_engine_id,
        "conversation_language": conversation_language,
        "language": hass.config.language,
        "name": pipeline_name,
        "stt_engine": stt_engine_id,
        "stt_language": stt_language,
        "tts_engine": tts_engine_id,
        "tts_language": tts_language,
        "tts_voice": tts_voice,
        "wake_word_entity": None,
        "wake_word_id": None,
    }


async def _async_create_default_pipeline(
    hass: HomeAssistant, pipeline_store: PipelineStorageCollection
) -> Pipeline:
    """Create a default pipeline.

    The default pipeline will use the homeassistant conversation agent and the
    default stt / tts engines.
    """
    pipeline_settings = _async_resolve_default_pipeline_settings(
        hass, pipeline_name="Home Assistant"
    )
    return await pipeline_store.async_create_item(pipeline_settings)


async def async_create_default_pipeline(
    hass: HomeAssistant,
    stt_engine_id: str,
    tts_engine_id: str,
    pipeline_name: str,
) -> Pipeline | None:
    """Create a pipeline with default settings.

    The default pipeline will use the homeassistant conversation agent and the
    specified stt / tts engines.
    """
    pipeline_data = hass.data[KEY_ASSIST_PIPELINE]
    pipeline_store = pipeline_data.pipeline_store
    pipeline_settings = _async_resolve_default_pipeline_settings(
        hass,
        stt_engine_id=stt_engine_id,
        tts_engine_id=tts_engine_id,
        pipeline_name=pipeline_name,
    )
    if (
        pipeline_settings["stt_engine"] != stt_engine_id
        or pipeline_settings["tts_engine"] != tts_engine_id
    ):
        return None
    return await pipeline_store.async_create_item(pipeline_settings)


@callback
def _async_get_pipeline_from_conversation_entity(
    hass: HomeAssistant, entity_id: str
) -> Pipeline:
    """Get a pipeline by conversation entity ID."""
    entity = hass.states.get(entity_id)
    settings = _async_resolve_default_pipeline_settings(
        hass,
        pipeline_name=entity.name if entity else entity_id,
        conversation_engine_id=entity_id,
    )
    settings["id"] = entity_id

    return Pipeline.from_json(settings)


@callback
def async_get_pipeline(hass: HomeAssistant, pipeline_id: str | None = None) -> Pipeline:
    """Get a pipeline by id or the preferred pipeline."""
    pipeline_data = hass.data[KEY_ASSIST_PIPELINE]

    if pipeline_id is None:
        # A pipeline was not specified, use the preferred one
        pipeline_id = pipeline_data.pipeline_store.async_get_preferred_item()

    if pipeline_id.startswith("conversation."):
        return _async_get_pipeline_from_conversation_entity(hass, pipeline_id)

    pipeline = pipeline_data.pipeline_store.data.get(pipeline_id)

    # If invalid pipeline ID was specified
    if pipeline is None:
        raise PipelineNotFound(
            "pipeline_not_found", f"Pipeline {pipeline_id} not found"
        )

    return pipeline


@callback
def async_get_pipelines(hass: HomeAssistant) -> list[Pipeline]:
    """Get all pipelines."""
    pipeline_data = hass.data[KEY_ASSIST_PIPELINE]

    return list(pipeline_data.pipeline_store.data.values())


class PipelineUpdateTD(TypedDict, total=False):
    """TypedDict for optional pipeline update fields."""

    conversation_engine: str | UndefinedType
    conversation_language: str | UndefinedType
    language: str | UndefinedType
    name: str | UndefinedType
    stt_engine: str | None | UndefinedType
    stt_language: str | None | UndefinedType
    tts_engine: str | None | UndefinedType
    tts_language: str | None | UndefinedType
    tts_voice: str | None | UndefinedType
    wake_word_entity: str | None | UndefinedType
    wake_word_id: str | None | UndefinedType
    prefer_local_intents: bool | UndefinedType


async def async_update_pipeline(
    hass: HomeAssistant,
    pipeline: Pipeline,
    *,
    update: PipelineUpdateTD,
) -> None:
    """Update an existing Assist pipeline with the provided data."""
    pipeline_data = hass.data[KEY_ASSIST_PIPELINE]

    updates: dict[str, Any] = pipeline.to_json()
    updates.pop("id", None)

    updates.update({k: v for k, v in update.items() if v is not UNDEFINED})

    await pipeline_data.pipeline_store.async_update_item(pipeline.id, updates)


class PipelineEventType(StrEnum):
    """Event types emitted during a pipeline run."""

    RUN_START = "run-start"
    RUN_END = "run-end"
    WAKE_WORD_START = "wake_word-start"
    WAKE_WORD_END = "wake_word-end"
    STT_START = "stt-start"
    STT_VAD_START = "stt-vad-start"
    STT_VAD_END = "stt-vad-end"
    STT_END = "stt-end"
    INTENT_START = "intent-start"
    INTENT_PROGRESS = "intent-progress"
    INTENT_END = "intent-end"
    TTS_START = "tts-start"
    TTS_END = "tts-end"
    ERROR = "error"


@dataclass(frozen=True)
class PipelineEvent:
    """Events emitted during a pipeline run."""

    type: PipelineEventType
    data: dict[str, Any] | None = None
    timestamp: str = field(default_factory=lambda: dt_util.utcnow().isoformat())


type PipelineEventCallback = Callable[[PipelineEvent], None]


@dataclass(frozen=True)
class Pipeline:
    """A voice assistant pipeline."""

    conversation_engine: str
    conversation_language: str
    language: str
    name: str
    stt_engine: str | None
    stt_language: str | None
    tts_engine: str | None
    tts_language: str | None
    tts_voice: str | None
    wake_word_entity: str | None
    wake_word_id: str | None
    prefer_local_intents: bool = False

    id: str = field(default_factory=ulid_util.ulid_now)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Pipeline:
        """Create an instance from a JSON serialization.

        This function was added in HA Core 2023.10, previous versions will raise
        if there are unexpected items in the serialized data.
        """
        return cls(
            conversation_engine=data["conversation_engine"],
            conversation_language=data["conversation_language"],
            id=data["id"],
            language=data["language"],
            name=data["name"],
            stt_engine=data["stt_engine"],
            stt_language=data["stt_language"],
            tts_engine=data["tts_engine"],
            tts_language=data["tts_language"],
            tts_voice=data["tts_voice"],
            wake_word_entity=data["wake_word_entity"],
            wake_word_id=data["wake_word_id"],
            prefer_local_intents=data.get("prefer_local_intents", False),
        )

    def to_json(self) -> dict[str, Any]:
        """Return a JSON serializable representation for storage."""
        return {
            "conversation_engine": self.conversation_engine,
            "conversation_language": self.conversation_language,
            "id": self.id,
            "language": self.language,
            "name": self.name,
            "stt_engine": self.stt_engine,
            "stt_language": self.stt_language,
            "tts_engine": self.tts_engine,
            "tts_language": self.tts_language,
            "tts_voice": self.tts_voice,
            "wake_word_entity": self.wake_word_entity,
            "wake_word_id": self.wake_word_id,
            "prefer_local_intents": self.prefer_local_intents,
        }


class PipelineStage(StrEnum):
    """Stages of a pipeline."""

    WAKE_WORD = "wake_word"
    STT = "stt"
    INTENT = "intent"
    TTS = "tts"
    END = "end"


PIPELINE_STAGE_ORDER = [
    PipelineStage.WAKE_WORD,
    PipelineStage.STT,
    PipelineStage.INTENT,
    PipelineStage.TTS,
]


class PipelineRunValidationError(Exception):
    """Error when a pipeline run is not valid."""


class InvalidPipelineStagesError(PipelineRunValidationError):
    """Error when given an invalid combination of start/end stages."""

    def __init__(
        self,
        start_stage: PipelineStage,
        end_stage: PipelineStage,
    ) -> None:
        """Set error message."""
        super().__init__(
            f"Invalid stage combination: start={start_stage}, end={end_stage}"
        )


@dataclass(frozen=True)
class WakeWordSettings:
    """Settings for wake word detection."""

    timeout: float | None = None
    """Seconds of silence before detection times out."""

    audio_seconds_to_buffer: float = 0
    """Seconds of audio to buffer before detection and forward to STT."""


@dataclass(frozen=True)
class AudioSettings:
    """Settings for pipeline audio processing."""

    noise_suppression_level: int = 0
    """Level of noise suppression (0 = disabled, 4 = max)"""

    auto_gain_dbfs: int = 0
    """Amount of automatic gain in dbFS (0 = disabled, 31 = max)"""

    volume_multiplier: float = 1.0
    """Multiplier used directly on PCM samples (1.0 = no change, 2.0 = twice as loud)"""

    is_vad_enabled: bool = True
    """True if VAD is used to determine the end of the voice command."""

    silence_seconds: float = 0.7
    """Seconds of silence after voice command has ended."""

    def __post_init__(self) -> None:
        """Verify settings post-initialization."""
        if (self.noise_suppression_level < 0) or (self.noise_suppression_level > 4):
            raise ValueError("noise_suppression_level must be in [0, 4]")

        if (self.auto_gain_dbfs < 0) or (self.auto_gain_dbfs > 31):
            raise ValueError("auto_gain_dbfs must be in [0, 31]")

    @property
    def needs_processor(self) -> bool:
        """True if an audio processor is needed."""
        return (
            self.is_vad_enabled
            or (self.noise_suppression_level > 0)
            or (self.auto_gain_dbfs > 0)
        )


def _wake_word_metadata_dict() -> dict[str, Any]:
    """Create wake-word metadata (language removed)."""
    metadata = asdict(
        stt.SpeechMetadata(
            language="",
            format=stt.AudioFormats.WAV,
            codec=stt.AudioCodecs.PCM,
            bit_rate=stt.AudioBitRates.BITRATE_16,
            sample_rate=stt.AudioSampleRates.SAMPLERATE_16000,
            channel=stt.AudioChannels.CHANNEL_MONO,
        )
    )
    metadata.pop("language", None)  # not used for wake words
    return metadata


def _maybe_create_wake_word_vad(
    settings: WakeWordSettings,
) -> VoiceActivityTimeout | None:
    """Create VAD only when a positive timeout is configured."""
    if (settings.timeout is not None) and (settings.timeout > 0):
        return VoiceActivityTimeout(settings.timeout)
    return None


def _maybe_create_stt_buffer(
    settings: WakeWordSettings,
) -> deque[EnhancedAudioChunk] | None:
    """Create a ring buffer for pre-detection audio, if requested."""
    num_chunks = int(
        (settings.audio_seconds_to_buffer * SAMPLE_RATE) / SAMPLES_PER_CHUNK
    )
    return deque(maxlen=num_chunks) if num_chunks > 0 else None


@dataclass
class PipelineRun:
    """Running context for a pipeline."""

    hass: HomeAssistant
    context: Context
    pipeline: Pipeline
    start_stage: PipelineStage
    end_stage: PipelineStage
    event_callback: PipelineEventCallback
    language: str | None = None
    runner_data: Any | None = None
    intent_agent: conversation.AgentInfo | None = None
    tts_audio_output: str | dict[str, Any] | None = None
    wake_word_settings: WakeWordSettings | None = None
    audio_settings: AudioSettings = field(default_factory=AudioSettings)

    id: str = field(default_factory=ulid_util.ulid_now)
    stt_provider: stt.SpeechToTextEntity | stt.Provider = field(init=False, repr=False)
    tts_stream: tts.ResultStream | None = field(init=False, default=None)
    wake_word_entity_id: str | None = field(init=False, default=None, repr=False)
    wake_word_entity: wake_word.WakeWordDetectionEntity = field(init=False, repr=False)

    abort_wake_word_detection: bool = field(init=False, default=False)

    debug_recording_thread: Thread | None = None
    """Thread that records audio to debug_recording_dir"""

    debug_recording_queue: Queue[str | bytes | None] | None = None
    """Queue to communicate with debug recording thread"""

    audio_enhancer: AudioEnhancer | None = None
    """VAD/noise suppression/auto gain"""

    audio_chunking_buffer: AudioBuffer = field(
        default_factory=lambda: AudioBuffer(BYTES_PER_CHUNK)
    )
    """Buffer used when splitting audio into chunks for audio processing"""

    _device_id: str | None = None
    """Optional device id set during run start."""

    _satellite_id: str | None = None
    """Optional satellite id set during run start."""

    _conversation_data: PipelineConversationData | None = None
    """Data tied to the conversation ID."""

    _intent_agent_only = False
    """If request should only be handled by agent, ignoring sentence triggers and local processing."""

    _streamed_response_text = False
    """If the conversation agent streamed response text to TTS result."""

    def __post_init__(self) -> None:
        """Set language for pipeline."""
        self.language = self.pipeline.language or self.hass.config.language

        # wake -> stt -> intent -> tts
        if PIPELINE_STAGE_ORDER.index(self.end_stage) < PIPELINE_STAGE_ORDER.index(
            self.start_stage
        ):
            raise InvalidPipelineStagesError(self.start_stage, self.end_stage)

        pipeline_data = self.hass.data[KEY_ASSIST_PIPELINE]
        if self.pipeline.id not in pipeline_data.pipeline_debug:
            pipeline_data.pipeline_debug[self.pipeline.id] = LimitedSizeDict(
                size_limit=STORED_PIPELINE_RUNS
            )
        pipeline_data.pipeline_debug[self.pipeline.id][self.id] = PipelineRunDebug()
        pipeline_data.pipeline_runs.add_run(self)

        # Initialize with audio settings
        if self.audio_settings.needs_processor and (self.audio_enhancer is None):
            # Default audio enhancer
            self.audio_enhancer = MicroVadSpeexEnhancer(
                self.audio_settings.auto_gain_dbfs,
                self.audio_settings.noise_suppression_level,
                self.audio_settings.is_vad_enabled,
            )

    def __eq__(self, other: object) -> bool:
        """Compare pipeline runs by id."""
        if isinstance(other, PipelineRun):
            return self.id == other.id

        return False

    @callback
    def process_event(self, event: PipelineEvent) -> None:
        """Log an event and call listener."""
        self.event_callback(event)
        pipeline_data = self.hass.data[KEY_ASSIST_PIPELINE]
        if self.id not in pipeline_data.pipeline_debug[self.pipeline.id]:
            # This run has been evicted from the logged pipeline runs already
            return
        pipeline_data.pipeline_debug[self.pipeline.id][self.id].events.append(event)

    def start(
        self, conversation_id: str, device_id: str | None, satellite_id: str | None
    ) -> None:
        """Emit run start event."""
        self._device_id = device_id
        self._satellite_id = satellite_id
        self._start_debug_recording_thread()

        data: dict[str, Any] = {
            "pipeline": self.pipeline.id,
            "language": self.language,
            "conversation_id": conversation_id,
        }
        if satellite_id is not None:
            data["satellite_id"] = satellite_id
        if self.runner_data is not None:
            data["runner_data"] = self.runner_data
        if self.tts_stream:
            data["tts_output"] = {
                "token": self.tts_stream.token,
                "url": self.tts_stream.url,
                "mime_type": self.tts_stream.content_type,
                "stream_response": (
                    self.tts_stream.supports_streaming_input
                    and self.intent_agent
                    and self.intent_agent.supports_streaming
                ),
            }

        self.process_event(PipelineEvent(PipelineEventType.RUN_START, data))

    async def end(self) -> None:
        """Emit run end event."""
        # Signal end of stream to listeners
        self._capture_chunk(None)

        # Stop the recording thread before emitting run-end.
        # This ensures that files are properly closed if the event handler reads them.
        await self._stop_debug_recording_thread()

        self.process_event(
            PipelineEvent(
                PipelineEventType.RUN_END,
            )
        )

        pipeline_data = self.hass.data[KEY_ASSIST_PIPELINE]
        pipeline_data.pipeline_runs.remove_run(self)

    async def prepare_wake_word_detection(self) -> None:
        """Prepare wake-word-detection."""
        entity_id = self.pipeline.wake_word_entity or wake_word.async_default_entity(
            self.hass
        )
        if entity_id is None:
            raise WakeWordDetectionError(
                code="wake-engine-missing", message="No wake word engine"
            )

        # Likely also @callback; call synchronously
        wake_word_entity = wake_word.async_get_wake_word_detection_entity(
            self.hass, entity_id
        )
        if wake_word_entity is None:
            raise WakeWordDetectionError(
                code="wake-provider-missing",
                message=f"No wake-word-detection provider for: {entity_id}",
            )

        self.wake_word_entity_id = entity_id
        self.wake_word_entity = wake_word_entity

        await asyncio.sleep(0)

    async def wake_word_detection(
        self,
        stream: AsyncIterable[EnhancedAudioChunk],
        audio_chunks_for_stt: list[EnhancedAudioChunk],
    ) -> wake_word.DetectionResult | None:
        """Run wake-word-detection portion of pipeline. Returns detection result."""
        metadata_dict = _wake_word_metadata_dict()
        settings = self.wake_word_settings or WakeWordSettings()

        self._emit_wake_word_start(metadata_dict, settings)
        self._debug_mark_wake_start()

        wake_word_vad = _maybe_create_wake_word_vad(settings)
        stt_audio_buffer = _maybe_create_stt_buffer(settings)

        try:
            # Detect wake word(s)
            result = await self.wake_word_entity.async_process_audio_stream(
                self._wake_word_audio_stream(
                    audio_stream=stream,
                    stt_audio_buffer=stt_audio_buffer,
                    wake_word_vad=wake_word_vad,
                ),
                self.pipeline.wake_word_id,
            )
        except WakeWordDetectionAborted:
            raise
        except WakeWordTimeoutError:
            _LOGGER.debug("Timeout during wake word detection")
            raise
        except Exception as src_error:
            _LOGGER.exception("Unexpected error during wake-word-detection")
            raise WakeWordDetectionError(
                code="wake-stream-failed",
                message="Unexpected error during wake-word-detection",
            ) from src_error

        # Forward buffered audio captured just before detection
        if stt_audio_buffer is not None:
            audio_chunks_for_stt.extend(stt_audio_buffer)

        _LOGGER.debug("wake-word-detection result %s", result)

        wake_word_output = self._build_wake_word_output(result, audio_chunks_for_stt)

        self.process_event(
            PipelineEvent(
                PipelineEventType.WAKE_WORD_END,
                {"wake_word_output": wake_word_output},
            )
        )

        return result

    def _emit_wake_word_start(
        self, metadata: dict[str, Any], settings: WakeWordSettings
    ) -> None:
        self.process_event(
            PipelineEvent(
                PipelineEventType.WAKE_WORD_START,
                {
                    "entity_id": self.wake_word_entity_id,
                    "metadata": metadata,
                    "timeout": settings.timeout or 0,
                },
            )
        )

    def _debug_mark_wake_start(self) -> None:
        if self.debug_recording_queue is not None:
            self.debug_recording_queue.put_nowait(f"00_wake-{self.wake_word_entity_id}")

    def _build_wake_word_output(
        self,
        result: wake_word.DetectionResult | None,
        audio_chunks_for_stt: list[EnhancedAudioChunk],
    ) -> dict[str, Any]:
        """Post-process detection result: cooldown check, queued audio, JSONable dict."""
        if result is None:
            return {}

        # Cooldown to avoid duplicate detections
        last_wake_up = self.hass.data[DATA_LAST_WAKE_UP].get(result.wake_word_phrase)
        if last_wake_up is not None:
            sec_since_last = time.monotonic() - last_wake_up
            if sec_since_last < WAKE_WORD_COOLDOWN:
                _LOGGER.debug(
                    "Duplicate wake word detection occurred for %s",
                    result.wake_word_phrase,
                )
                raise DuplicateWakeUpDetectedError(result.wake_word_phrase)

        # Record last wake up time
        self.hass.data[DATA_LAST_WAKE_UP][result.wake_word_phrase] = time.monotonic()

        # Forward any queued audio captured by the detector
        if result.queued_audio:
            audio_chunks_for_stt.extend(
                EnhancedAudioChunk(
                    audio=chunk_ts[0], timestamp_ms=chunk_ts[1], speech_probability=None
                )
                for chunk_ts in result.queued_audio
            )

        out = asdict(result)
        out.pop("queued_audio", None)  # remove non-JSON fields
        return out

    async def _wake_word_audio_stream(
        self,
        audio_stream: AsyncIterable[EnhancedAudioChunk],
        stt_audio_buffer: deque[EnhancedAudioChunk] | None,
        wake_word_vad: VoiceActivityTimeout | None,
        sample_rate: int = SAMPLE_RATE,
        sample_width: int = SAMPLE_WIDTH,
    ) -> AsyncIterable[tuple[bytes, int]]:
        """Yield audio chunks with timestamps (milliseconds since start of stream).

        Adds audio to a ring buffer that will be forwarded to speech-to-text after
        detection. Times out if VAD detects enough silence.
        """
        async for chunk in audio_stream:
            if self.abort_wake_word_detection:
                raise WakeWordDetectionAborted

            self._capture_chunk(chunk.audio)
            yield chunk.audio, chunk.timestamp_ms

            # Wake-word-detection occurs *after* the wake word was actually
            # spoken. Keeping audio right before detection allows the voice
            # command to be spoken immediately after the wake word.
            if stt_audio_buffer is not None:
                stt_audio_buffer.append(chunk)

            if wake_word_vad is not None:
                chunk_seconds = (len(chunk.audio) // sample_width) / sample_rate
                if not wake_word_vad.process(chunk_seconds, chunk.speech_probability):
                    raise WakeWordTimeoutError(
                        code="wake-word-timeout", message="Wake word was not detected"
                    )

    async def prepare_speech_to_text(self, metadata: stt.SpeechMetadata) -> None:
        """Prepare speech-to-text."""
        # pipeline.stt_engine can't be None or this function is not called
        stt_provider = stt.async_get_speech_to_text_engine(
            self.hass,
            self.pipeline.stt_engine,  # type: ignore[arg-type]
        )

        if stt_provider is None:
            engine = self.pipeline.stt_engine
            raise SpeechToTextError(
                code="stt-provider-missing",
                message=f"No speech-to-text provider for: {engine}",
            )

        metadata.language = self.pipeline.stt_language or self.language

        # Offload potentially-blocking sync check to the executor
        is_supported = await self.hass.async_add_executor_job(
            stt_provider.check_metadata, metadata
        )
        if not is_supported:
            raise SpeechToTextError(
                code="stt-provider-unsupported-metadata",
                message=(
                    f"Provider {stt_provider.name} does not support input speech "
                    f"to text metadata {metadata}"
                ),
            )

        self.stt_provider = stt_provider

    async def speech_to_text(
        self,
        metadata: stt.SpeechMetadata,
        stream: AsyncIterable[EnhancedAudioChunk],
    ) -> str:
        """Run speech-to-text portion of pipeline. Returns the spoken text."""
        # Create a background task to prepare the conversation agent
        if self.end_stage >= PipelineStage.INTENT and self.intent_agent:
            self.hass.async_create_background_task(
                conversation.async_prepare_agent(
                    self.hass, self.intent_agent.id, self.language
                ),
                f"prepare conversation agent {self.intent_agent.id}",
            )

        if isinstance(self.stt_provider, stt.Provider):
            engine = self.stt_provider.name
        else:
            engine = self.stt_provider.entity_id

        self.process_event(
            PipelineEvent(
                PipelineEventType.STT_START,
                {
                    "engine": engine,
                    "metadata": asdict(metadata),
                },
            )
        )

        if self.debug_recording_queue is not None:
            # New recording
            self.debug_recording_queue.put_nowait(f"01_stt-{engine}")

        try:
            # Transcribe audio stream
            stt_vad: VoiceCommandSegmenter | None = None
            if self.audio_settings.is_vad_enabled:
                stt_vad = VoiceCommandSegmenter(
                    silence_seconds=self.audio_settings.silence_seconds
                )

            result = await self.stt_provider.async_process_audio_stream(
                metadata,
                self._speech_to_text_stream(audio_stream=stream, stt_vad=stt_vad),
            )
        except (asyncio.CancelledError, TimeoutError):
            raise  # expected
        except hass_nabucasa.auth.Unauthenticated as src_error:
            raise SpeechToTextError(
                code="cloud-auth-failed",
                message="Home Assistant Cloud authentication failed",
            ) from src_error
        except Exception as src_error:
            _LOGGER.exception("Unexpected error during speech-to-text")
            raise SpeechToTextError(
                code="stt-stream-failed",
                message="Unexpected error during speech-to-text",
            ) from src_error

        _LOGGER.debug("speech-to-text result %s", result)

        if result.result != stt.SpeechResultState.SUCCESS:
            raise SpeechToTextError(
                code="stt-stream-failed",
                message="speech-to-text failed",
            )

        if not result.text:
            raise SpeechToTextError(
                code="stt-no-text-recognized", message="No text recognized"
            )

        self.process_event(
            PipelineEvent(
                PipelineEventType.STT_END,
                {
                    "stt_output": {
                        "text": result.text,
                    }
                },
            )
        )

        return result.text

    async def _speech_to_text_stream(
        self,
        audio_stream: AsyncIterable[EnhancedAudioChunk],
        stt_vad: VoiceCommandSegmenter | None,
        sample_rate: int = SAMPLE_RATE,
        sample_width: int = SAMPLE_WIDTH,
    ) -> AsyncGenerator[bytes]:
        """Yield audio chunks until VAD detects silence or speech-to-text completes."""
        sent_vad_start = False
        async for chunk in audio_stream:
            self._capture_chunk(chunk.audio)

            if stt_vad is not None:
                chunk_seconds = (len(chunk.audio) // sample_width) / sample_rate
                if not stt_vad.process(chunk_seconds, chunk.speech_probability):
                    # Silence detected at the end of voice command
                    self.process_event(
                        PipelineEvent(
                            PipelineEventType.STT_VAD_END,
                            {"timestamp": chunk.timestamp_ms},
                        )
                    )
                    break

                if stt_vad.in_command and (not sent_vad_start):
                    # Speech detected at start of voice command
                    self.process_event(
                        PipelineEvent(
                            PipelineEventType.STT_VAD_START,
                            {"timestamp": chunk.timestamp_ms},
                        )
                    )
                    sent_vad_start = True

            yield chunk.audio

    async def prepare_recognize_intent(self, session: chat_session.ChatSession) -> None:
        """Prepare recognizing an intent."""
        self._conversation_data = async_get_pipeline_conversation_data(
            self.hass, session
        )

        if self._conversation_data.continue_conversation_agent is not None:
            agent_info = conversation.async_get_agent_info(
                self.hass, self._conversation_data.continue_conversation_agent
            )
            self._conversation_data.continue_conversation_agent = None
            if agent_info is None:
                raise IntentRecognitionError(
                    code="intent-agent-not-found",
                    message=(
                        f"Intent recognition engine "
                        f"{self._conversation_data.continue_conversation_agent} asked for "
                        f"follow-up but is no longer found"
                    ),
                )
            self._intent_agent_only = True

        else:
            agent_info = conversation.async_get_agent_info(
                self.hass,
                self.pipeline.conversation_engine or conversation.HOME_ASSISTANT_AGENT,
            )
            if agent_info is None:
                engine = self.pipeline.conversation_engine or "default"
                raise IntentRecognitionError(
                    code="intent-not-supported",
                    message=f"Intent recognition engine {engine} is not found",
                )

        self.intent_agent = agent_info

        # Use an async feature so the coroutine is legitimate and non-blocking
        await asyncio.sleep(0)

    async def recognize_intent(
        self,
        intent_input: str,
        conversation_id: str,
        conversation_extra_system_prompt: str | None,
    ) -> tuple[str, bool]:
        """Run intent recognition portion of pipeline.

        Returns (speech, all_targets_in_satellite_area).
        """
        if self.intent_agent is None or self._conversation_data is None:
            raise RuntimeError("Recognize intent was not prepared")

        input_language = self._determine_input_language()

        self._emit_intent_start_event(
            engine=self.intent_agent.id,
            language=input_language,
            intent_input=intent_input,
            conversation_id=conversation_id,
        )

        try:
            user_input = self._build_user_input(
                intent_input=intent_input,
                conversation_id=conversation_id,
                input_language=input_language,
                conversation_extra_system_prompt=conversation_extra_system_prompt,
            )

            agent_id = self.intent_agent.id
            processed_locally = agent_id == conversation.HOME_ASSISTANT_AGENT
            all_targets_in_satellite_area = False
            intent_response: intent.IntentResponse | None = None

            (
                agent_id,
                processed_locally,
                intent_response,
            ) = await self._maybe_handle_triggers_and_local_intents(
                user_input=user_input,
                agent_id=agent_id,
                processed_locally=processed_locally,
            )

            tts_input_stream = self._create_tts_queue_if_supported()
            chat_log_delta_listener, stop_streaming_cb = (
                self._make_chat_log_delta_listener(tts_input_stream)
            )

            with (
                chat_session.async_get_chat_session(
                    self.hass, user_input.conversation_id
                ) as session,
                conversation.async_get_chat_log(
                    self.hass,
                    session,
                    user_input,
                    chat_log_delta_listener=chat_log_delta_listener,
                ) as chat_log,
            ):
                if intent_response is not None:
                    speech, conversation_result = self._handle_pre_resolved_intent(
                        intent_response=intent_response,
                        agent_id=agent_id,
                        session=session,
                        chat_log=chat_log,
                    )
                else:
                    conversation_result, speech = await self._fall_back_to_converse(
                        user_input=user_input,
                        stop_streaming_cb=stop_streaming_cb,
                    )

                if agent_id == conversation.HOME_ASSISTANT_AGENT:
                    all_targets_in_satellite_area = (
                        self._get_all_targets_in_satellite_area(
                            conversation_result.response,
                            self._satellite_id,
                            self._device_id,
                        )
                    )

        except Exception as src_error:
            _LOGGER.exception("Unexpected error during intent recognition")
            raise IntentRecognitionError(
                code="intent-failed",
                message="Unexpected error during intent recognition",
            ) from src_error

        _LOGGER.debug("conversation result %s", conversation_result)

        self.process_event(
            PipelineEvent(
                PipelineEventType.INTENT_END,
                {
                    "processed_locally": processed_locally,
                    "intent_output": conversation_result.as_dict(),
                },
            )
        )

        if conversation_result.continue_conversation:
            self._conversation_data.continue_conversation_agent = agent_id

        return (speech, all_targets_in_satellite_area)

    def _determine_input_language(self) -> str:
        if self.pipeline.conversation_language == MATCH_ALL:
            # Prefer more specific STT/TTS languages where possible
            return (
                self.pipeline.stt_language
                or self.pipeline.tts_language
                or self.pipeline.language
            )
        return self.pipeline.conversation_language

    def _emit_intent_start_event(
        self,
        *,
        engine: str,
        language: str,
        intent_input: str,
        conversation_id: str,
    ) -> None:
        self.process_event(
            PipelineEvent(
                PipelineEventType.INTENT_START,
                {
                    "engine": engine,
                    "language": language,
                    "intent_input": intent_input,
                    "conversation_id": conversation_id,
                    "device_id": self._device_id,
                    "satellite_id": self._satellite_id,
                    "prefer_local_intents": self.pipeline.prefer_local_intents,
                },
            )
        )

    def _build_user_input(
        self,
        *,
        intent_input: str,
        conversation_id: str,
        input_language: str,
        conversation_extra_system_prompt: str | None,
    ) -> conversation.ConversationInput:
        return conversation.ConversationInput(
            text=intent_input,
            context=self.context,
            conversation_id=conversation_id,
            device_id=self._device_id,
            satellite_id=self._satellite_id,
            language=input_language,
            agent_id=self.intent_agent.id,
            extra_system_prompt=conversation_extra_system_prompt,
        )

    async def _maybe_handle_triggers_and_local_intents(
        self,
        *,
        user_input: conversation.ConversationInput,
        agent_id: str,
        processed_locally: bool,
    ) -> tuple[str, bool, intent.IntentResponse | None]:
        """Apply sentence triggers and (optionally) local intents before LLM."""
        intent_response: intent.IntentResponse | None = None

        if processed_locally or self._intent_agent_only:
            return agent_id, processed_locally, intent_response

        # Sentence triggers override conversation agent
        trigger_response_text = await conversation.async_handle_sentence_triggers(
            self.hass, user_input
        )
        if trigger_response_text is not None:
            agent_id = "sentence_trigger"
            processed_locally = True
            intent_response = intent.IntentResponse(self.pipeline.conversation_language)
            intent_response.async_set_speech(trigger_response_text)
            return agent_id, processed_locally, intent_response

        # If the LLM has CONTROL feature, apply local fallback filter
        intent_filter: Callable[[RecognizeResult], bool] | None = None
        intent_agent_state = self.hass.states.get(self.intent_agent.id)
        if (
            intent_agent_state
            and intent_agent_state.attributes.get(ATTR_SUPPORTED_FEATURES, 0)
            & conversation.ConversationEntityFeature.CONTROL
        ):
            intent_filter = _async_local_fallback_intent_filter

        if self.pipeline.prefer_local_intents:
            intent_response = await conversation.async_handle_intents(
                self.hass, user_input, intent_filter=intent_filter
            )
            if intent_response:
                agent_id = conversation.HOME_ASSISTANT_AGENT
                processed_locally = True

        return agent_id, processed_locally, intent_response

    def _create_tts_queue_if_supported(self) -> asyncio.Queue[str | None] | None:
        if self.tts_stream and self.tts_stream.supports_streaming_input:
            return asyncio.Queue()
        return None

    def _make_chat_log_delta_listener(
        self, tts_input_stream: asyncio.Queue[str | None] | None
    ) -> tuple[Callable[[conversation.ChatLog, dict], None], Callable[[], None]]:
        """Return (listener, stop_streaming_cb)."""
        chat_log_role: str | None = None
        delta_character_count = 0

        @callback
        def chat_log_delta_listener(
            chat_log: conversation.ChatLog, delta: dict
        ) -> None:
            nonlocal chat_log_role, delta_character_count

            # 1) Always emit progress
            self._emit_chat_progress(delta)

            # 2) If we don't stream TTS, we're done
            if not self._should_handle_tts(tts_input_stream):
                return

            # 3) Track role and ignore non-assistant deltas
            chat_log_role = self._updated_role(chat_log_role, delta)
            if not self._is_assistant(chat_log_role):
                return

            # 4) Push any content into the queue
            content = delta.get("content")
            self._enqueue_content_if_any(tts_input_stream, content)

            # 5) If already started streaming, nothing else to do here
            if self._streamed_response_text:
                return

            # 6) Decide whether to start streaming now
            start_streaming, delta_character_count = self._should_start_streaming(
                delta, content, delta_character_count
            )
            if not start_streaming:
                return

            # 7) Kick off streaming and connect generator to TTS
            self._begin_tts_streaming(tts_input_stream)

        def stop_streaming_cb() -> None:
            if tts_input_stream and self._streamed_response_text:
                tts_input_stream.put_nowait(None)

        return chat_log_delta_listener, stop_streaming_cb

    def _emit_chat_progress(self, delta: dict) -> None:
        self.process_event(
            PipelineEvent(
                PipelineEventType.INTENT_PROGRESS,
                {"chat_log_delta": delta},
            )
        )

    @staticmethod
    def _should_handle_tts(tts_input_stream: asyncio.Queue[str | None] | None) -> bool:
        return tts_input_stream is not None

    @staticmethod
    def _updated_role(current: str | None, delta: dict) -> str | None:
        return delta.get("role") or current

    @staticmethod
    def _is_assistant(role: str | None) -> bool:
        return role == "assistant"

    @staticmethod
    def _enqueue_content_if_any(
        tts_input_stream: asyncio.Queue[str | None], content: str | None
    ) -> None:
        if content:
            tts_input_stream.put_nowait(content)

    def _should_start_streaming(
        self, delta: dict, content: str | None, delta_character_count: int
    ) -> tuple[bool, int]:
        """Return (start_streaming, new_delta_character_count)."""
        # Start on tool call after some text
        if delta_character_count > 0 and delta.get("tool_calls"):
            return True, delta_character_count

        # Or when we crossed the character threshold
        if content:
            new_count = delta_character_count + len(content)
            return new_count > STREAM_RESPONSE_CHARS, new_count

        return False, delta_character_count

    def _begin_tts_streaming(self, tts_input_stream: asyncio.Queue[str | None]) -> None:
        self._streamed_response_text = True

        self.process_event(
            PipelineEvent(
                PipelineEventType.INTENT_PROGRESS,
                {"tts_start_streaming": True},
            )
        )

        async def tts_input_stream_generator() -> AsyncGenerator[str]:
            while (tts_input := await tts_input_stream.get()) is not None:
                yield tts_input

        # Concatenate existing queued parts at the moment streaming begins
        parts: list[str | None] = []
        while not tts_input_stream.empty():
            parts.append(tts_input_stream.get_nowait())

        # Filter out None and join
        joined = "".join(cast(list[str], [p for p in parts if p]))
        if joined:
            tts_input_stream.put_nowait(joined)

        assert self.tts_stream is not None
        self.tts_stream.async_set_message_stream(tts_input_stream_generator())

    def _handle_pre_resolved_intent(
        self,
        *,
        intent_response: intent.IntentResponse,
        agent_id: str,
        session: chat_session.ChatSession,
        chat_log: conversation.ChatLog,
    ) -> tuple[str, conversation.ConversationResult]:
        speech: str = intent_response.speech.get("plain", {}).get("speech", "")
        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(
                agent_id=agent_id,
                content=speech,
            )
        )
        conversation_result = conversation.ConversationResult(
            response=intent_response,
            conversation_id=session.conversation_id,
        )
        return speech, conversation_result

    async def _fall_back_to_converse(
        self,
        *,
        user_input: conversation.ConversationInput,
        stop_streaming_cb: Callable[[], None],
    ) -> tuple[conversation.ConversationResult, str]:
        conversation_result = await conversation.async_converse(
            hass=self.hass,
            text=user_input.text,
            conversation_id=user_input.conversation_id,
            device_id=user_input.device_id,
            satellite_id=user_input.satellite_id,
            context=user_input.context,
            language=user_input.language,
            agent_id=user_input.agent_id,
            extra_system_prompt=user_input.extra_system_prompt,
        )
        speech = conversation_result.response.speech.get("plain", {}).get("speech", "")
        # If we were streaming, terminate the queue cleanly
        stop_streaming_cb()
        return conversation_result, speech

    def _get_all_targets_in_satellite_area(
        self,
        intent_response: intent.IntentResponse,
        satellite_id: str | None,
        device_id: str | None,
    ) -> bool:
        """Return true if all targeted entities were in the same area as the device."""
        if (
            intent_response.response_type != intent.IntentResponseType.ACTION_DONE
            or not intent_response.matched_states
        ):
            return False

        entity_registry = er.async_get(self.hass)
        device_registry = dr.async_get(self.hass)

        # Resolve the reference "source area" once (satellite or device)
        source_area_id = self._resolve_source_area_id(
            entity_registry, device_registry, satellite_id, device_id
        )
        if source_area_id is None:
            return False

        # Every matched entity must resolve to the same area
        for state in intent_response.matched_states:
            target_area_id = self._resolve_entity_area_id(
                entity_registry, device_registry, state.entity_id
            )
            if target_area_id != source_area_id:
                return False

        return True

    def _resolve_source_area_id(
        self,
        entity_registry: er.EntityRegistry,
        device_registry: dr.DeviceRegistry,
        satellite_id: str | None,
        device_id: str | None,
    ) -> str | None:
        """Resolve area id from satellite entity first, then its/device's area; None if unknown."""
        # Prefer satellite entity when provided
        if satellite_id:
            target_entity_entry = entity_registry.async_get(satellite_id)
            if target_entity_entry:
                # If the satellite entity has a direct area, use it
                if target_entity_entry.area_id:
                    return target_entity_entry.area_id
                # Otherwise, fall back to the satellite's device
                device_id = target_entity_entry.device_id

        # Fall back to the (possibly updated) device
        if not device_id:
            return None

        device_entry = device_registry.async_get(device_id)
        if not device_entry or not device_entry.area_id:
            return None

        return device_entry.area_id

    def _resolve_entity_area_id(
        self,
        entity_registry: er.EntityRegistry,
        device_registry: dr.DeviceRegistry,
        entity_id: str,
    ) -> str | None:
        """Resolve area id for a target entity via its own area or its device's area."""
        target_entity_entry = entity_registry.async_get(entity_id)
        if not target_entity_entry:
            return None

        if target_entity_entry.area_id:
            return target_entity_entry.area_id

        # No entity area -> try its device
        if not target_entity_entry.device_id:
            return None

        target_device_entry = device_registry.async_get(target_entity_entry.device_id)
        return target_device_entry.area_id if target_device_entry else None

    async def prepare_text_to_speech(self) -> None:
        """Prepare text-to-speech."""
        engine = cast(str, self.pipeline.tts_engine)

        tts_options: dict[str, Any] = {}
        if self.pipeline.tts_voice is not None:
            tts_options[tts.ATTR_VOICE] = self.pipeline.tts_voice

        if isinstance(self.tts_audio_output, dict):
            tts_options.update(self.tts_audio_output)
        elif isinstance(self.tts_audio_output, str):
            tts_options[tts.ATTR_PREFERRED_FORMAT] = self.tts_audio_output
            if self.tts_audio_output == "wav":
                # 16 Khz, 16-bit mono
                tts_options[tts.ATTR_PREFERRED_SAMPLE_RATE] = SAMPLE_RATE
                tts_options[tts.ATTR_PREFERRED_SAMPLE_CHANNELS] = SAMPLE_CHANNELS
                tts_options[tts.ATTR_PREFERRED_SAMPLE_BYTES] = SAMPLE_WIDTH

        try:
            # Offload potentially blocking creation to executor and await it
            create_stream_partial = functools.partial(
                tts.async_create_stream,
                hass=self.hass,
                engine=engine,
                language=self.pipeline.tts_language,
                options=tts_options,
            )
            self.tts_stream = await self.hass.async_add_executor_job(
                create_stream_partial
            )
        except HomeAssistantError as err:
            raise TextToSpeechError(
                code="tts-not-supported",
                message=(
                    f"Text-to-speech engine {engine} "
                    f"does not support language {self.pipeline.tts_language} or options {tts_options}: {err}"
                ),
            ) from err

    async def text_to_speech(
        self, tts_input: str, override_media_path: Path | None = None
    ) -> None:
        """Run text-to-speech portion of the pipeline."""
        if override_media_path:
            self.tts_stream.async_override_result(override_media_path)
        elif not self._streamed_response_text:
            self.tts_stream.async_set_message(tts_input)

        # satisfy S7503 and yield the loop
        await asyncio.sleep(0)

        tts_output = {
            "media_id": self.tts_stream.media_source_id,
            "token": self.tts_stream.token,
            "url": self.tts_stream.url,
            "mime_type": self.tts_stream.content_type,
        }

        self.process_event(
            PipelineEvent(PipelineEventType.TTS_END, {"tts_output": tts_output})
        )

    def _capture_chunk(self, audio_bytes: bytes | None) -> None:
        """Forward audio chunk to various capturing mechanisms."""
        if self.debug_recording_queue is not None:
            # Forward to debug WAV file recording
            self.debug_recording_queue.put_nowait(audio_bytes)

        if self._device_id is None:
            return

        # Forward to device audio capture
        pipeline_data = self.hass.data[KEY_ASSIST_PIPELINE]
        audio_queue = pipeline_data.device_audio_queues.get(self._device_id)
        if audio_queue is None:
            return

        try:
            audio_queue.queue.put_nowait(audio_bytes)
        except asyncio.QueueFull:
            audio_queue.overflow = True
            _LOGGER.warning("Audio queue full for device %s", self._device_id)

    def _start_debug_recording_thread(self) -> None:
        """Start thread to record wake/stt audio if debug_recording_dir is set."""
        if self.debug_recording_thread is not None:
            # Already started
            return

        # Directory to save audio for each pipeline run.
        # Configured in YAML for assist_pipeline.
        if debug_recording_dir := self.hass.data[DATA_CONFIG].get(
            CONF_DEBUG_RECORDING_DIR
        ):
            if self._device_id is None:
                # <debug_recording_dir>/<pipeline.name>/<run.id>
                run_recording_dir = (
                    Path(debug_recording_dir)
                    / self.pipeline.name
                    / str(time.monotonic_ns())
                )
            else:
                # <debug_recording_dir>/<device_id>/<pipeline.name>/<run.id>
                run_recording_dir = (
                    Path(debug_recording_dir)
                    / self._device_id
                    / self.pipeline.name
                    / str(time.monotonic_ns())
                )

            self.debug_recording_queue = Queue()
            self.debug_recording_thread = Thread(
                target=_pipeline_debug_recording_thread_proc,
                args=(run_recording_dir, self.debug_recording_queue),
                daemon=True,
            )
            self.debug_recording_thread.start()

    async def _stop_debug_recording_thread(self) -> None:
        """Stop recording thread."""
        if (self.debug_recording_thread is None) or (
            self.debug_recording_queue is None
        ):
            # Not running
            return

        # NOTE: Expecting a None to have been put in self.debug_recording_queue
        # in self.end() to signal the thread to stop.

        # Wait until the thread has finished to ensure that files are fully written
        await self.hass.async_add_executor_job(self.debug_recording_thread.join)

        self.debug_recording_queue = None
        self.debug_recording_thread = None

    async def process_volume_only(
        self, audio_stream: AsyncIterable[bytes]
    ) -> AsyncGenerator[EnhancedAudioChunk]:
        """Apply volume transformation only (no VAD/audio enhancements) with optional chunking."""
        timestamp_ms = 0
        async for chunk in audio_stream:
            if not math.isclose(self.audio_settings.volume_multiplier, 1.0):
                chunk = _multiply_volume(chunk, self.audio_settings.volume_multiplier)

            for sub_chunk in chunk_samples(
                chunk, BYTES_PER_CHUNK, self.audio_chunking_buffer
            ):
                yield EnhancedAudioChunk(
                    audio=sub_chunk,
                    timestamp_ms=timestamp_ms,
                    speech_probability=None,  # no VAD
                )
                timestamp_ms += MS_PER_CHUNK

    async def process_enhance_audio(
        self, audio_stream: AsyncIterable[bytes]
    ) -> AsyncGenerator[EnhancedAudioChunk]:
        """Split audio into chunks and apply VAD/noise suppression/auto gain/volume transformation."""
        assert self.audio_enhancer is not None

        timestamp_ms = 0
        async for dirty_samples in audio_stream:
            if not math.isclose(self.audio_settings.volume_multiplier, 1.0):
                # Static gain
                dirty_samples = _multiply_volume(
                    dirty_samples, self.audio_settings.volume_multiplier
                )

            # Split into chunks for audio enhancements/VAD
            for dirty_chunk in chunk_samples(
                dirty_samples, BYTES_PER_CHUNK, self.audio_chunking_buffer
            ):
                yield self.audio_enhancer.enhance_chunk(dirty_chunk, timestamp_ms)
                timestamp_ms += MS_PER_CHUNK


def _multiply_volume(chunk: bytes, volume_multiplier: float) -> bytes:
    """Multiplies 16-bit PCM samples by a constant."""

    def _clamp(val: float) -> float:
        """Clamp to signed 16-bit."""
        return max(-32768, min(32767, val))

    return array.array(
        "h",
        (int(_clamp(value * volume_multiplier)) for value in array.array("h", chunk)),
    ).tobytes()


def _pipeline_debug_recording_thread_proc(
    run_recording_dir: Path,
    queue: Queue[str | bytes | None],
    message_timeout: float = 5,
) -> None:
    wav_writer: wave.Wave_write | None = None

    try:
        _LOGGER.debug("Saving wake/stt audio to %s", run_recording_dir)
        run_recording_dir.mkdir(parents=True, exist_ok=True)

        while True:
            message = queue.get(timeout=message_timeout)
            if message is None:
                # Stop signal
                break

            if isinstance(message, str):
                # New WAV file name
                if wav_writer is not None:
                    wav_writer.close()

                wav_path = run_recording_dir / f"{message}.wav"
                wav_writer = wave.open(str(wav_path), "wb")
                wav_writer.setframerate(SAMPLE_RATE)
                wav_writer.setsampwidth(SAMPLE_WIDTH)
                wav_writer.setnchannels(SAMPLE_CHANNELS)
            elif isinstance(message, bytes):
                # Chunk of 16-bit mono audio at 16Khz
                if wav_writer is not None:
                    wav_writer.writeframes(message)
    except Empty:
        pass  # occurs when pipeline has unexpected error
    except Exception:
        _LOGGER.exception("Unexpected error in debug recording thread")
    finally:
        if wav_writer is not None:
            wav_writer.close()


@dataclass(kw_only=True)
class PipelineInput:
    """Input to a pipeline run."""

    run: PipelineRun

    session: chat_session.ChatSession
    """Session for the conversation."""

    stt_metadata: stt.SpeechMetadata | None = None
    """Metadata of stt input audio. Required when start_stage = stt."""

    stt_stream: AsyncIterable[bytes] | None = None
    """Input audio for stt. Required when start_stage = stt."""

    wake_word_phrase: str | None = None
    """Optional key used to de-duplicate wake-ups for local wake word detection."""

    intent_input: str | None = None
    """Input for conversation agent. Required when start_stage = intent."""

    tts_input: str | None = None
    """Input for text-to-speech. Required when start_stage = tts."""

    conversation_extra_system_prompt: str | None = None
    """Extra prompt information for the conversation agent."""

    device_id: str | None = None
    """Identifier of the device that is processing the input/output of the pipeline."""

    satellite_id: str | None = None
    """Identifier of the satellite that is processing the input/output of the pipeline."""

    async def execute(self) -> None:
        """Run pipeline."""
        self.run.start(
            conversation_id=self.session.conversation_id,
            device_id=self.device_id,
            satellite_id=self.satellite_id,
        )
        current_stage: PipelineStage | None = self.run.start_stage
        stt_audio_buffer: list[EnhancedAudioChunk] = []
        stt_processed_stream: AsyncIterable[EnhancedAudioChunk] | None = (
            self._prepare_stt_processed_stream()
        )

        try:
            if current_stage == PipelineStage.WAKE_WORD:
                current_stage = await self._maybe_run_wake_word(
                    stt_processed_stream, stt_audio_buffer
                )
                if current_stage is None:
                    # No wake word. Abort the rest of the pipeline.
                    return

            # STT
            intent_input = self.intent_input
            if current_stage == PipelineStage.STT:
                intent_input = await self._run_stt(
                    stt_processed_stream, stt_audio_buffer
                )
                current_stage = PipelineStage.INTENT

            # INTENT + TTS (unless caller wants to stop at STT)
            if self.run.end_stage == PipelineStage.STT:
                return

            tts_input = self.tts_input
            all_targets_in_satellite_area = False

            if current_stage == PipelineStage.INTENT:
                (
                    tts_input,
                    all_targets_in_satellite_area,
                ) = await self.run.recognize_intent(
                    intent_input,  # type: ignore[arg-type]
                    self.session.conversation_id,
                    self.conversation_extra_system_prompt,
                )
                current_stage = (
                    PipelineStage.TTS
                    if (
                        all_targets_in_satellite_area
                        or (tts_input and tts_input.strip())
                    )
                    else PipelineStage.END
                )

            if self.run.end_stage == PipelineStage.INTENT:
                return

            # TTS
            if current_stage == PipelineStage.TTS:
                await self._run_tts(tts_input, all_targets_in_satellite_area)

        except PipelineError as err:
            self.run.process_event(
                PipelineEvent(
                    PipelineEventType.ERROR,
                    {"code": err.code, "message": err.message},
                )
            )
        finally:
            # Always end the run since it needs to shut down the debug recording
            # thread, etc.
            await self.run.end()

    def _prepare_stt_processed_stream(self) -> AsyncIterable[EnhancedAudioChunk] | None:
        """Build the STT processed stream if any (enhance or volume-only)."""
        if self.stt_stream is None:
            return None
        if self.run.audio_settings.needs_processor:
            # VAD/noise suppression/auto gain/volume
            return self.run.process_enhance_audio(self.stt_stream)
        # Volume multiplier only
        return self.run.process_volume_only(self.stt_stream)

    async def _maybe_run_wake_word(
        self,
        stt_processed_stream: AsyncIterable[EnhancedAudioChunk] | None,
        stt_audio_buffer: list[EnhancedAudioChunk],
    ) -> PipelineStage | None:
        """Run wake-word detection if needed. Return next stage or None to abort."""
        # wake-word-detection
        assert stt_processed_stream is not None
        detect_result = await self.run.wake_word_detection(
            stt_processed_stream, stt_audio_buffer
        )
        if detect_result is None:
            return None
        return PipelineStage.STT

    async def _run_stt(
        self,
        stt_processed_stream: AsyncIterable[EnhancedAudioChunk] | None,
        stt_audio_buffer: list[EnhancedAudioChunk],
    ) -> str:
        """Run speech-to-text and return intent_input."""
        assert self.stt_metadata is not None
        assert stt_processed_stream is not None

        # Avoid duplicate wake-ups by checking cooldown
        if self.wake_word_phrase is not None:
            last_wake_up = self.run.hass.data[DATA_LAST_WAKE_UP].get(
                self.wake_word_phrase
            )
            if last_wake_up is not None:
                sec_since_last_wake_up = time.monotonic() - last_wake_up
                if sec_since_last_wake_up < WAKE_WORD_COOLDOWN:
                    _LOGGER.debug(
                        "Speech-to-text cancelled to avoid duplicate wake-up for %s",
                        self.wake_word_phrase,
                    )
                    raise DuplicateWakeUpDetectedError(self.wake_word_phrase)

            # Record last wake up time to block duplicate detections
            self.run.hass.data[DATA_LAST_WAKE_UP][self.wake_word_phrase] = (
                time.monotonic()
            )

        stt_input_stream: AsyncIterable[EnhancedAudioChunk] = (
            self._buffer_then_audio_stream(stt_audio_buffer, stt_processed_stream)
            if stt_audio_buffer
            else stt_processed_stream
        )

        return await self.run.speech_to_text(
            self.stt_metadata,
            stt_input_stream,
        )

    def _buffer_then_audio_stream(
        self,
        stt_audio_buffer: list[EnhancedAudioChunk],
        stt_processed_stream: AsyncIterable[EnhancedAudioChunk],
    ) -> AsyncGenerator[EnhancedAudioChunk]:
        """Async generator that yields buffered audio first, then live stream."""

        async def gen() -> AsyncGenerator[EnhancedAudioChunk]:
            for chunk in stt_audio_buffer:
                yield chunk
            async for chunk in stt_processed_stream:
                yield chunk

        return gen()

    async def _run_tts(
        self, tts_input: str | None, all_targets_in_satellite_area: bool
    ) -> None:
        """Run text-to-speech, with acknowledge beep path preserved."""
        if all_targets_in_satellite_area:
            await self.run.text_to_speech(
                tts_input or "", override_media_path=ACKNOWLEDGE_PATH
            )
            return
        assert tts_input is not None
        await self.run.text_to_speech(tts_input)

    async def validate(self) -> None:
        """Validate pipeline input against start stage."""
        self._validate_start_stage_requirements()
        self._validate_end_stage_requirements()

        start_stage_index = PIPELINE_STAGE_ORDER.index(self.run.start_stage)
        end_stage_index = PIPELINE_STAGE_ORDER.index(self.run.end_stage)

        def _in_range(stage: PipelineStage) -> bool:
            idx = PIPELINE_STAGE_ORDER.index(stage)
            return start_stage_index <= idx <= end_stage_index

        prepare_tasks = []

        if _in_range(PipelineStage.WAKE_WORD):
            prepare_tasks.append(self.run.prepare_wake_word_detection())

        if _in_range(PipelineStage.STT):
            # self.stt_metadata can't be None or we'd raise above
            prepare_tasks.append(self.run.prepare_speech_to_text(self.stt_metadata))  # type: ignore[arg-type]

        if _in_range(PipelineStage.INTENT):
            prepare_tasks.append(self.run.prepare_recognize_intent(self.session))

        if _in_range(PipelineStage.TTS):
            prepare_tasks.append(self.run.prepare_text_to_speech())

        if prepare_tasks:
            await asyncio.gather(*prepare_tasks)

    def _validate_start_stage_requirements(self) -> None:
        stage = self.run.start_stage

        if stage in (PipelineStage.WAKE_WORD, PipelineStage.STT):
            if self.run.pipeline.stt_engine is None:
                raise PipelineRunValidationError(
                    "the pipeline does not support speech-to-text"
                )
            if self.stt_metadata is None:
                raise PipelineRunValidationError(
                    "stt_metadata is required for speech-to-text"
                )
            if self.stt_stream is None:
                raise PipelineRunValidationError(
                    "stt_stream is required for speech-to-text"
                )
            return

        if stage == PipelineStage.INTENT:
            if self.intent_input is None:
                raise PipelineRunValidationError(
                    "intent_input is required for intent recognition"
                )
            return

        if stage == PipelineStage.TTS and self.tts_input is None:
            raise PipelineRunValidationError("tts_input is required for text-to-speech")

    def _validate_end_stage_requirements(self) -> None:
        if self.run.end_stage == PipelineStage.TTS:
            if self.run.pipeline.tts_engine is None:
                raise PipelineRunValidationError(
                    "the pipeline does not support text-to-speech"
                )


class PipelinePreferred(CollectionError):
    """Raised when attempting to delete the preferred pipelen."""

    def __init__(self, item_id: str) -> None:
        """Initialize pipeline preferred error."""
        super().__init__(f"Item {item_id} preferred.")
        self.item_id = item_id


class SerializedPipelineStorageCollection(SerializedStorageCollection):
    """Serialized pipeline storage collection."""

    preferred_item: str


class PipelineStorageCollection(
    StorageCollection[Pipeline, SerializedPipelineStorageCollection]
):
    """Pipeline storage collection."""

    _preferred_item: str

    async def _async_load_data(self) -> SerializedPipelineStorageCollection | None:
        """Load the data."""
        data = await super()._async_load_data()
        if not data:
            pipeline = await _async_create_default_pipeline(self.hass, self)
            self._preferred_item = pipeline.id
            return None  # explicitly return None when no data was loaded

        self._preferred_item = data["preferred_item"]

        return data

    async def _process_create_data(self, data: dict) -> dict:
        """Validate the config is valid."""
        validated_data: dict = validate_language(data)
        return validated_data

    @callback
    def _get_suggested_id(self, info: dict) -> str:
        """Suggest an ID based on the config."""
        return ulid_util.ulid_now()

    async def _update_data(self, item: Pipeline, update_data: dict) -> Pipeline:
        """Return a new updated item."""
        update_data = validate_language(update_data)
        return Pipeline(id=item.id, **update_data)

    def _create_item(self, item_id: str, data: dict) -> Pipeline:
        """Create an item from validated config."""
        return Pipeline(id=item_id, **data)

    def _deserialize_item(self, data: dict) -> Pipeline:
        """Create an item from its serialized representation."""
        return Pipeline.from_json(data)

    def _serialize_item(self, item_id: str, item: Pipeline) -> dict:
        """Return the serialized representation of an item for storing."""
        return item.to_json()

    async def async_delete_item(self, item_id: str) -> None:
        """Delete item."""
        if self._preferred_item == item_id:
            raise PipelinePreferred(item_id)
        await super().async_delete_item(item_id)

    @callback
    def async_get_preferred_item(self) -> str:
        """Get the id of the preferred item."""
        return self._preferred_item

    @callback
    def async_set_preferred_item(self, item_id: str) -> None:
        """Set the preferred pipeline."""
        if item_id not in self.data:
            raise ItemNotFound(item_id)
        self._preferred_item = item_id
        self._async_schedule_save()

    @callback
    def _data_to_save(self) -> SerializedPipelineStorageCollection:
        """Return JSON-compatible date for storing to file."""
        base_data = super()._base_data_to_save()
        return {
            "items": base_data["items"],
            "preferred_item": self._preferred_item,
        }


class PipelineStorageCollectionWebsocket(
    StorageCollectionWebsocket[PipelineStorageCollection]
):
    """Class to expose storage collection management over websocket."""

    @callback
    def async_setup(self, hass: HomeAssistant) -> None:
        """Set up the websocket commands."""
        super().async_setup(hass)

        websocket_api.async_register_command(
            hass,
            f"{self.api_prefix}/get",
            self.ws_get_item,
            websocket_api.BASE_COMMAND_MESSAGE_SCHEMA.extend(
                {
                    vol.Required("type"): f"{self.api_prefix}/get",
                    vol.Optional(self.item_id_key): str,
                }
            ),
        )

        websocket_api.async_register_command(
            hass,
            f"{self.api_prefix}/set_preferred",
            websocket_api.require_admin(
                websocket_api.async_response(self.ws_set_preferred_item)
            ),
            websocket_api.BASE_COMMAND_MESSAGE_SCHEMA.extend(
                {
                    vol.Required("type"): f"{self.api_prefix}/set_preferred",
                    vol.Required(self.item_id_key): str,
                }
            ),
        )

    async def ws_delete_item(
        self, hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
    ) -> None:
        """Delete an item."""
        try:
            await super().ws_delete_item(hass, connection, msg)
        except PipelinePreferred as exc:
            connection.send_error(msg["id"], websocket_api.ERR_NOT_ALLOWED, str(exc))

    @callback
    def ws_get_item(
        self, hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
    ) -> None:
        """Get an item."""
        item_id = msg.get(self.item_id_key)
        if item_id is None:
            item_id = self.storage_collection.async_get_preferred_item()

        if item_id.startswith("conversation.") and hass.states.get(item_id):
            connection.send_result(
                msg["id"], _async_get_pipeline_from_conversation_entity(hass, item_id)
            )
            return

        if item_id not in self.storage_collection.data:
            connection.send_error(
                msg["id"],
                websocket_api.ERR_NOT_FOUND,
                f"Unable to find {self.item_id_key} {item_id}",
            )
            return

        connection.send_result(msg["id"], self.storage_collection.data[item_id])

    @callback
    def ws_list_item(
        self, hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
    ) -> None:
        """List items."""
        connection.send_result(
            msg["id"],
            {
                "pipelines": async_get_pipelines(hass),
                "preferred_pipeline": self.storage_collection.async_get_preferred_item(),
            },
        )

    async def ws_set_preferred_item(
        self,
        hass: HomeAssistant,
        connection: websocket_api.ActiveConnection,
        msg: dict[str, Any],
    ) -> None:
        """Set the preferred item."""
        try:
            self.storage_collection.async_set_preferred_item(msg[self.item_id_key])
        except ItemNotFound:
            connection.send_error(
                msg["id"], websocket_api.ERR_NOT_FOUND, "unknown item"
            )
            return
        connection.send_result(msg["id"])


class PipelineRuns:
    """Class managing pipelineruns."""

    def __init__(self, pipeline_store: PipelineStorageCollection) -> None:
        """Initialize."""
        self._pipeline_runs: dict[str, dict[str, PipelineRun]] = defaultdict(dict)
        self._pipeline_store = pipeline_store
        pipeline_store.async_add_listener(self._change_listener)

    def add_run(self, pipeline_run: PipelineRun) -> None:
        """Add pipeline run."""
        pipeline_id = pipeline_run.pipeline.id
        self._pipeline_runs[pipeline_id][pipeline_run.id] = pipeline_run

    def remove_run(self, pipeline_run: PipelineRun) -> None:
        """Remove pipeline run."""
        pipeline_id = pipeline_run.pipeline.id
        self._pipeline_runs[pipeline_id].pop(pipeline_run.id)

    async def _change_listener(
        self, change_type: str, item_id: str, change: dict
    ) -> None:
        """Handle pipeline store changes."""
        if change_type != CHANGE_UPDATED:
            return

        if pipeline_runs := self._pipeline_runs.get(item_id):
            for pipeline_run in tuple(pipeline_runs.values()):
                pipeline_run.abort_wake_word_detection = True

        # legit async feature to satisfy the rule
        await asyncio.sleep(0)


@dataclass(slots=True)
class DeviceAudioQueue:
    """Audio capture queue for a satellite device."""

    queue: asyncio.Queue[bytes | None]
    """Queue of audio chunks (None = stop signal)"""

    id: str = field(default_factory=ulid_util.ulid_now)
    """Unique id to ensure the correct audio queue is cleaned up in websocket API."""

    overflow: bool = False
    """Flag to be set if audio samples were dropped because the queue was full."""


@dataclass(slots=True)
class AssistDevice:
    """Assist device."""

    domain: str
    unique_id_prefix: str


class PipelineData:
    """Store and debug data stored in hass.data."""

    def __init__(self, pipeline_store: PipelineStorageCollection) -> None:
        """Initialize."""
        self.pipeline_store = pipeline_store
        self.pipeline_debug: dict[str, LimitedSizeDict[str, PipelineRunDebug]] = {}
        self.pipeline_devices: dict[str, AssistDevice] = {}
        self.pipeline_runs = PipelineRuns(pipeline_store)
        self.device_audio_queues: dict[str, DeviceAudioQueue] = {}


@dataclass(slots=True)
class PipelineRunDebug:
    """Debug data for a pipelinerun."""

    events: list[PipelineEvent] = field(default_factory=list, init=False)
    timestamp: str = field(
        default_factory=lambda: dt_util.utcnow().isoformat(),
        init=False,
    )


class PipelineStore(Store[SerializedPipelineStorageCollection]):
    """Store pipeline data."""

    async def _async_migrate_func(
        self,
        old_major_version: int,
        old_minor_version: int,
        old_data: SerializedPipelineStorageCollection,
    ) -> SerializedPipelineStorageCollection:
        """Migrate to the new version."""
        if old_major_version == 1 and old_minor_version < 2:
            # Version 1.2 adds wake word configuration
            for pipeline in old_data["items"]:
                # Populate keys which were introduced before version 1.2
                pipeline.setdefault("wake_word_entity", None)
                pipeline.setdefault("wake_word_id", None)

        if old_major_version > 1:
            raise NotImplementedError
        return old_data


@singleton(KEY_ASSIST_PIPELINE, async_=True)
async def async_setup_pipeline_store(hass: HomeAssistant) -> PipelineData:
    """Set up the pipeline storage collection."""
    pipeline_store = PipelineStorageCollection(
        PipelineStore(
            hass, STORAGE_VERSION, STORAGE_KEY, minor_version=STORAGE_VERSION_MINOR
        )
    )
    await pipeline_store.async_load()
    PipelineStorageCollectionWebsocket(
        pipeline_store,
        f"{DOMAIN}/pipeline",
        "pipeline",
        PIPELINE_FIELDS,
        PIPELINE_FIELDS,
    ).async_setup(hass)
    return PipelineData(pipeline_store)


@dataclass
class PipelineConversationData:
    """Hold data for the duration of a conversation."""

    continue_conversation_agent: str | None = None
    """The agent that requested the conversation to be continued."""


@callback
def async_get_pipeline_conversation_data(
    hass: HomeAssistant, session: chat_session.ChatSession
) -> PipelineConversationData:
    """Get the pipeline data for a specific conversation."""
    all_conversation_data = hass.data.get(KEY_PIPELINE_CONVERSATION_DATA)
    if all_conversation_data is None:
        all_conversation_data = {}
        hass.data[KEY_PIPELINE_CONVERSATION_DATA] = all_conversation_data

    data = all_conversation_data.get(session.conversation_id)

    if data is not None:
        return data

    @callback
    def do_cleanup() -> None:
        """Handle cleanup."""
        all_conversation_data.pop(session.conversation_id)

    session.async_on_cleanup(do_cleanup)

    data = all_conversation_data[session.conversation_id] = PipelineConversationData()
    return data
