import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from fee_utils import calculate_fee
from models import Client, Hold, PlatformSetting, Transaction

router = APIRouter(prefix="/api/holds", tags=["holds"])


class HoldCreate(BaseModel):
    client_id: int
    amount: float


def _get_fee_rate_for_client(client: Client, db: Session) -> float:
    """Return the fee_rate value to store in the hold, depending on client's fee_type."""
    if client.fee_type == "tiered":
        # Tiered clients don't use fee_rate (tiers are hardcoded in fee_utils.py)
        return 0.0
    elif client.fee_type == "flat_rate":
        setting = db.query(PlatformSetting).filter(PlatformSetting.key == "flat_rate_fee").first()
        return float(setting.value) if setting else 25.0
    else:
        # percentage (default)
        setting = db.query(PlatformSetting).filter(PlatformSetting.key == "fee_rate").first()
        return float(setting.value) if setting else 0.05


@router.post("")
def create_hold(hold_in: HoldCreate, db: Session = Depends(get_db)):
    client = db.query(Client).filter(Client.id == hold_in.client_id).first()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    fee_rate = _get_fee_rate_for_client(client, db)

    hold = Hold(
        client_id=hold_in.client_id,
        amount=hold_in.amount,
        fee_rate=fee_rate,
        status="pending",
        created_at=datetime.datetime.utcnow(),
    )
    db.add(hold)
    db.commit()
    db.refresh(hold)

    tx = Transaction(
        hold_id=hold.id,
        client_id=hold_in.client_id,
        transaction_type="hold",
        amount=hold_in.amount,
        created_at=datetime.datetime.utcnow(),
    )
    db.add(tx)
    db.commit()

    return {"id": hold.id, "status": hold.status, "amount": float(hold.amount)}


@router.post("/{hold_id}/commit")
def commit_hold(hold_id: int, db: Session = Depends(get_db)):
    hold = db.query(Hold).filter(Hold.id == hold_id).first()
    if not hold:
        raise HTTPException(status_code=404, detail="Hold not found")
    if hold.status != "pending":
        raise HTTPException(status_code=400, detail=f"Hold is already {hold.status}")

    client = db.query(Client).filter(Client.id == hold.client_id).first()
    fee_type = client.fee_type if client else "percentage"

    amount = float(hold.amount)
    fee_rate = float(hold.fee_rate)

    # Calculate fee and net committed amount
    fee_charged = calculate_fee(amount, fee_type, fee_rate)
    committed_amount = amount - fee_charged

    hold.fee_charged = fee_charged
    hold.committed_amount = committed_amount
    hold.status = "committed"
    hold.committed_at = datetime.datetime.utcnow()
    db.commit()

    tx = Transaction(
        hold_id=hold.id,
        client_id=hold.client_id,
        transaction_type="commit",
        amount=committed_amount,
        created_at=datetime.datetime.utcnow(),
    )
    db.add(tx)
    db.commit()

    return {
        "id": hold.id,
        "status": hold.status,
        "amount": amount,
        "fee_charged": fee_charged,
        "committed_amount": committed_amount,
    }


@router.post("/{hold_id}/cancel")
def cancel_hold(hold_id: int, db: Session = Depends(get_db)):
    hold = db.query(Hold).filter(Hold.id == hold_id).first()
    if not hold:
        raise HTTPException(status_code=404, detail="Hold not found")
    if hold.status != "pending":
        raise HTTPException(status_code=400, detail=f"Hold is already {hold.status}")

    hold.status = "cancelled"
    hold.cancelled_at = datetime.datetime.utcnow()
    db.commit()

    tx = Transaction(
        hold_id=hold.id,
        client_id=hold.client_id,
        transaction_type="cancel",
        amount=float(hold.amount),
        created_at=datetime.datetime.utcnow(),
    )
    db.add(tx)
    db.commit()

    return {"id": hold.id, "status": hold.status}


@router.get("/{hold_id}")
def get_hold(hold_id: int, db: Session = Depends(get_db)):
    hold = db.query(Hold).filter(Hold.id == hold_id).first()
    if not hold:
        raise HTTPException(status_code=404, detail="Hold not found")
    return {
        "id": hold.id,
        "client_id": hold.client_id,
        "amount": float(hold.amount),
        "fee_rate": float(hold.fee_rate),
        "fee_charged": float(hold.fee_charged) if hold.fee_charged is not None else None,
        "committed_amount": float(hold.committed_amount) if hold.committed_amount is not None else None,
        "status": hold.status,
    }
