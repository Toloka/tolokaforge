from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from database import get_db
from models import ReportSummary

router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.get("")
def get_reports(client_id: int = None, db: Session = Depends(get_db)):
    query = db.query(ReportSummary)
    if client_id:
        query = query.filter(ReportSummary.client_id == client_id)
    summaries = query.order_by(ReportSummary.period_date).all()
    return [
        {
            "id": s.id,
            "client_id": s.client_id,
            "period_date": s.period_date,
            "total_held": float(s.total_held),
            "total_committed": float(s.total_committed),
            "total_cancelled": float(s.total_cancelled),
            "total_fees": float(s.total_fees),
            "transaction_count": s.transaction_count,
        }
        for s in summaries
    ]
