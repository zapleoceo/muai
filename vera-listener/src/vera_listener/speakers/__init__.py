"""Опознание говорящих: кто именно сказал реплику.

Дорожка `system` смешивает всех удалённых участников в один поток — в
групповом созвоне пятеро людей неразличимы. Здесь они разделяются по голосу
и, где возможно, получают настоящие имена.

Публичный вход — `SpeakerSession`; остальное вспомогательное и разделено по
ответственностям: признаки, модель, кластеризация, хранилище отпечатков.
"""
from vera_listener.speakers.embedder import (
    EMBEDDING_DIM,
    OpenVinoSpeakerEmbedder,
    SpeakerEmbedder,
    similarity,
)
from vera_listener.speakers.registry import VoiceprintRegistry
from vera_listener.speakers.session import SpeakerSession

__all__ = [
    "EMBEDDING_DIM",
    "OpenVinoSpeakerEmbedder",
    "SpeakerEmbedder",
    "SpeakerSession",
    "VoiceprintRegistry",
    "similarity",
]
