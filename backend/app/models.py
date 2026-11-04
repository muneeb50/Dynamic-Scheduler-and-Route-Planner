"""Database tables.

Real-world reference data (hospitals, mapped intersections) is stored separately
from simulated state (ambulance positions, signal events, traffic snapshots).
Every simulated row carries the simulation run id and the simulated time; wall
clock timestamps are stored in UTC.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SimulationRun(Base):
    __tablename__ = "simulation_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    network_name: Mapped[str] = mapped_column(String(200))
    network_source: Mapped[str] = mapped_column(String(20))
    engine: Mapped[str] = mapped_column(String(20))
    demand: Mapped[str] = mapped_column(String(20))
    seed: Mapped[int] = mapped_column(Integer)
    priority_enabled: Mapped[bool] = mapped_column(Boolean)
    recovery_enabled: Mapped[bool] = mapped_column(Boolean)
    queue_source: Mapped[str] = mapped_column(String(10), default="sim")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Hospital(Base):
    __tablename__ = "hospitals"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    node: Mapped[str] = mapped_column(String(60))


class Ambulance(Base):
    __tablename__ = "ambulances"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(40), default="AVAILABLE")
    base_node: Mapped[str] = mapped_column(String(60))
    current_lat: Mapped[float] = mapped_column(Float)
    current_lon: Mapped[float] = mapped_column(Float)
    position_source: Mapped[str] = mapped_column(String(20), default="simulated")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Booking(Base):
    __tablename__ = "bookings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    run_id: Mapped[int] = mapped_column(ForeignKey("simulation_runs.id"), index=True)
    ambulance_id: Mapped[Optional[str]] = mapped_column(ForeignKey("ambulances.id"), nullable=True)
    hospital_id: Mapped[str] = mapped_column(ForeignKey("hospitals.id"))
    pickup_lat: Mapped[float] = mapped_column(Float)
    pickup_lon: Mapped[float] = mapped_column(Float)
    pickup_node: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="REQUESTED", index=True)
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    eta_s: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    assigned_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    @property
    def journey_id(self) -> str:
        return f"B{self.id}"


class Route(Base):
    __tablename__ = "routes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"), index=True)
    leg: Mapped[str] = mapped_column(String(20))  # to_pickup | to_hospital
    edges: Mapped[list] = mapped_column(JSON)
    geometry_geojson: Mapped[str] = mapped_column(Text)
    length_m: Mapped[float] = mapped_column(Float)
    estimated_time_s: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Intersection(Base):
    __tablename__ = "intersections"
    id: Mapped[str] = mapped_column(String(60), primary_key=True)  # network node id
    name: Mapped[str] = mapped_column(String(200))
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    approaches: Mapped[list] = mapped_column(JSON)
    signal_config: Mapped[dict] = mapped_column(JSON)


class SignalEvent(Base):
    __tablename__ = "signal_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("simulation_runs.id"), index=True)
    intersection_id: Mapped[str] = mapped_column(String(60), index=True)
    booking_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(40))
    sim_time: Mapped[float] = mapped_column(Float)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TrafficSnapshot(Base):
    __tablename__ = "traffic_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("simulation_runs.id"), index=True)
    intersection_id: Mapped[str] = mapped_column(String(60), index=True)
    sim_time: Mapped[float] = mapped_column(Float)
    queue_lengths: Mapped[dict] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(20))  # simulated | cv_estimated
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RecoveryEventRow(Base):
    __tablename__ = "recovery_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("simulation_runs.id"), index=True)
    recovery_key: Mapped[str] = mapped_column(String(40), index=True)
    intersection_id: Mapped[str] = mapped_column(String(60), index=True)
    booking_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    priority_session: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20))
    started_sim: Mapped[float] = mapped_column(Float)
    completed_sim: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    duration_s: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    details: Mapped[dict] = mapped_column(JSON)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class JourneyMetrics(Base):
    __tablename__ = "journey_metrics"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"), unique=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("simulation_runs.id"), index=True)
    status: Mapped[str] = mapped_column(String(40))
    travel_time_s: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stops: Mapped[int] = mapped_column(Integer, default=0)
    signal_delay_s: Mapped[float] = mapped_column(Float, default=0.0)
    priority_enabled: Mapped[bool] = mapped_column(Boolean)
    metrics: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SystemEvent(Base):
    """Journey / dispatch event history (booking created, assigned, arrived, failed …)."""

    __tablename__ = "system_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("simulation_runs.id"), index=True)
    booking_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(40))
    sim_time: Mapped[float] = mapped_column(Float)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Experiment(Base):
    __tablename__ = "experiments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="QUEUED")
    params: Mapped[dict] = mapped_column(JSON)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    results: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
