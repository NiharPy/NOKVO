"""SuperAdmin to-do items.

Internal task list for the Nokvo team, managed from the SuperAdmin console. A
to-do can optionally be tagged to a piece of tenant feedback (e.g. "build the
feature this customer asked for"), linking the request to the work.
"""
from __future__ import annotations

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.db.session import Base


class SuperAdminTodo(Base):
    __tablename__ = "superadmin_todos"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String, nullable=False)
    notes = Column(Text, nullable=True)
    status = Column(String, nullable=False, server_default="open")  # 'open' | 'done'
    # Optional link to the tenant feedback that prompted this task. SET NULL so a
    # purged feedback row doesn't delete the to-do.
    feedback_id = Column(
        UUID(as_uuid=True),
        ForeignKey("tenant_feedback.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_by_superadmin_id = Column(
        UUID(as_uuid=True),
        ForeignKey("superadmin_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_superadmin_todos_status_created", "status", "created_at"),
    )
