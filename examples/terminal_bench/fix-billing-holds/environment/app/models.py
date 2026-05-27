import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, Numeric, String, Text

from database import Base


class Client(Base):
    __tablename__ = "clients"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    email = Column(String(255), nullable=False)
    balance = Column(Numeric(15, 2), default=0)
    fee_type = Column(String(20), nullable=False, default="percentage")


class Hold(Base):
    __tablename__ = "holds"

    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)
    amount = Column(Numeric(15, 2), nullable=False)
    fee_rate = Column(Numeric(8, 4), nullable=False)
    fee_charged = Column(Numeric(15, 2))
    committed_amount = Column(Numeric(15, 2))
    status = Column(String(20), default="pending")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    committed_at = Column(DateTime)
    cancelled_at = Column(DateTime)


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True)
    hold_id = Column(Integer, ForeignKey("holds.id"), nullable=False)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)
    transaction_type = Column(String(20), nullable=False)
    amount = Column(Numeric(15, 2), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class PlatformSetting(Base):
    __tablename__ = "platform_settings"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False)


class ReportSummary(Base):
    __tablename__ = "report_summaries"

    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)
    period_date = Column(String(10), nullable=False)  # YYYY-MM-DD
    total_held = Column(Numeric(15, 2), default=0)
    total_committed = Column(Numeric(15, 2), default=0)
    total_cancelled = Column(Numeric(15, 2), default=0)
    total_fees = Column(Numeric(15, 2), default=0)
    transaction_count = Column(Integer, default=0)


class ClientFeeTotal(Base):
    __tablename__ = "client_fee_totals"

    client_id = Column(Integer, ForeignKey("clients.id"), primary_key=True)
    lifetime_fees = Column(Numeric(15, 2), default=0)
    ytd_fees = Column(Numeric(15, 2), default=0)
    last_updated = Column(DateTime)
