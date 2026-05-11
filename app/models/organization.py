from sqlalchemy import Column, String, DateTime, Boolean
from sqlalchemy.dialects.postgresql import UUID
from app.db.session import Base
from sqlalchemy.sql import func
import uuid

class Organization(Base):
    __tablename__ = "organizations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    admin_email = Column(String, nullable=True)
    admin_name = Column(String, nullable=True)
    email_domain = Column(String, nullable=True)
    region = Column(String, nullable=False)
    environment = Column(String, nullable=False)
    call_type = Column(String, nullable=True)
    language = Column(String, nullable=True)
    plan_type = Column(String, nullable=True)
    product_tier = Column(String, nullable=False, server_default="nokvo_prime", default="nokvo_prime")
    status = Column(String, nullable=False, server_default="active", default="active")
    calling_enabled = Column(Boolean, nullable=False, server_default="false", default=False)
    stores_pii = Column(Boolean, nullable=False, default=True)
    record_calls = Column(Boolean, nullable=False, default=True)
    create_resource_group = Column(Boolean, nullable=False, default=True)
    twilio_auto_provision = Column(Boolean, nullable=False, default=False)
    industry = Column(String, nullable=True)
    country_code = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
