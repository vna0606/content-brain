"""
models.py — dataclass для идей, генерируемых анализатором.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional


@dataclass
class IdeaDraft:
    """
    Черновик идеи для поста.

    Создаётся анализатором (02-analyzer), читается ботом (03-bot).
    Хранится в таблице cb_ideas.
    """

    title: str
    """Короткий заголовок идеи (до 80 символов)."""

    thesis: str
    """Развёрнутый тезис: суть идеи, о чём пост, почему актуально."""

    relevant_history: List[str] = field(default_factory=list)
    """Список source_id из cb_diary_vectors — похожие старые записи дневника."""

    relevant_social: List[str] = field(default_factory=list)
    """Список source_id из cb_social_vectors — релевантные посты/видео."""

    source_entries: List[str] = field(default_factory=list)
    """Список дат записей дневника (вида '2025-05-30'), породивших идею."""

    status: str = "new"
    """Статус идеи: 'new' | 'shown' | 'used' | 'dismissed'."""

    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    """Время создания идеи, ISO 8601."""

    # Поле id заполняется после сохранения в БД
    id: Optional[int] = None

    def to_db_row(self) -> dict:
        """Конвертировать в словарь для INSERT в cb_ideas."""
        import json
        return {
            "title": self.title,
            "thesis": self.thesis,
            "relevant_history": json.dumps(self.relevant_history, ensure_ascii=False),
            "relevant_social": json.dumps(self.relevant_social, ensure_ascii=False),
            "source_entries": json.dumps(self.source_entries, ensure_ascii=False),
            "status": self.status,
            "created_at": self.created_at,
        }

    @classmethod
    def from_db_row(cls, row) -> "IdeaDraft":
        """Создать IdeaDraft из строки БД cb_ideas."""
        import json
        return cls(
            id=row[0],
            title=row[1],
            thesis=row[2],
            relevant_history=json.loads(row[3] or "[]"),
            relevant_social=json.loads(row[4] or "[]"),
            source_entries=json.loads(row[5] or "[]"),
            status=row[6] or "new",
            created_at=row[7],
        )
