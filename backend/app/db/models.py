from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from geoalchemy2 import Geometry
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Category(Base):
    __tablename__ = "categories"
    code: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    geometry_type: Mapped[str] = mapped_column(Text)


class DataSource(Base):
    __tablename__ = "data_sources"
    code: Mapped[str] = mapped_column(Text, primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    usage_note: Mapped[str] = mapped_column(Text)
    snapshot_date: Mapped[date] = mapped_column(Date)


class GisObject(Base):
    __tablename__ = "gis_objects"
    __table_args__ = (
        CheckConstraint("status IN ('active','closed','unknown')", name="object_status"),
        CheckConstraint("rating IS NULL OR rating BETWEEN 1 AND 5", name="object_rating"),
        CheckConstraint("capacity IS NULL OR capacity >= 0", name="object_capacity"),
        CheckConstraint(
            "ST_IsValid(geom) AND NOT ST_IsEmpty(geom) AND ST_NDims(geom)=2",
            name="object_geometry_valid",
        ),
        CheckConstraint(
            "(category_code IN ('school','hospital','pharmacy') AND "
            "GeometryType(geom)='POINT') OR (category_code='park' AND "
            "GeometryType(geom)='POLYGON') OR (category_code='improvement_zone' "
            "AND GeometryType(geom)='LINESTRING')",
            name="object_geometry_type",
        ),
        CheckConstraint(
            "(category_code='park' AND area_m2 IS NOT NULL AND area_m2 >= 0 AND "
            "area_m2 < 'Infinity'::float8) OR (category_code<>'park' AND "
            "area_m2 IS NULL)",
            name="object_area",
        ),
        CheckConstraint(
            "(category_code='improvement_zone' AND length_m IS NOT NULL AND length_m >= 0 AND "
            "length_m < 'Infinity'::float8) OR "
            "(category_code<>'improvement_zone' AND length_m IS NULL)",
            name="object_length",
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    category_code: Mapped[str] = mapped_column(ForeignKey("categories.code"), index=True)
    geom: Mapped[object] = mapped_column(Geometry("GEOMETRY", srid=4326, dimension=2))
    status: Mapped[str] = mapped_column(Text)
    rating: Mapped[Decimal | None] = mapped_column(Numeric)
    capacity: Mapped[int | None] = mapped_column(Integer)
    area_m2: Mapped[float | None] = mapped_column(Float)
    length_m: Mapped[float | None] = mapped_column(Float)
    opening_date: Mapped[date | None] = mapped_column(Date)
    is_accessible: Mapped[bool | None] = mapped_column(Boolean)
    source: Mapped[str] = mapped_column(ForeignKey("data_sources.code"))
    source_id: Mapped[str | None] = mapped_column(Text)
    synthetic_fields: Mapped[list[str]] = mapped_column(ARRAY(Text))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Region(Base):
    __tablename__ = "regions"
    __table_args__ = (
        UniqueConstraint("name"),
        CheckConstraint(
            "GeometryType(geom) IN ('POLYGON','MULTIPOLYGON') AND "
            "ST_IsValid(geom) AND NOT ST_IsEmpty(geom) AND ST_NDims(geom)=2",
            name="region_geometry",
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    geom: Mapped[object] = mapped_column(Geometry("GEOMETRY", srid=4326, dimension=2))
    source: Mapped[str] = mapped_column(ForeignKey("data_sources.code"))
    source_id: Mapped[str | None] = mapped_column(Text)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role IN ('user','admin')", name="user_role"),)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    login: Mapped[str] = mapped_column(Text, unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    csrf_token: Mapped[str] = mapped_column(Text)


class DatasetState(Base):
    __tablename__ = "dataset_state"
    __table_args__ = (CheckConstraint("id=1 AND version>=1", name="dataset_singleton"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(BigInteger)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LayerState(Base):
    __tablename__ = "layer_state"
    layer_code: Mapped[str] = mapped_column(Text, primary_key=True)
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(BigInteger)
