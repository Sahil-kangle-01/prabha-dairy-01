"""
api/routes/print.py

Requirement #9: Print engine routes.

Serves PDF versions of vouchers and reports. All routes return a PDF file
response with appropriate Content-Disposition header (inline for browser
preview, attachment for download).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from api.deps import get_db
from database.models import PurchaseMilk
from print_service import render_purchase_milk_voucher, render_purchase_milk_vouchers_batch

router = APIRouter(prefix="/print", tags=["print"])


@router.get("/purchase-milk/{voucher_id}")
def print_purchase_milk_voucher(
    voucher_id: int,
    download: bool = Query(False, description="True = download, False = preview in browser"),
    db: Session = Depends(get_db),
):
    """
    Generate PDF for one Purchase Milk voucher.

    Args:
        voucher_id: Database ID of the voucher
        download: If True, browser downloads the file; if False, displays inline
    """
    voucher = db.query(PurchaseMilk).filter(PurchaseMilk.id == voucher_id).first()

    if not voucher:
        raise HTTPException(status_code=404, detail=f"Purchase Milk voucher {voucher_id} not found")

    pdf_bytes = render_purchase_milk_voucher(voucher)

    # Filename for download
    filename = f"PurchaseMilk_{voucher.voucher_number or voucher_id}.pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'{"attachment" if download else "inline"}; filename="{filename}"'
        },
    )


@router.get("/purchase-milk/batch")
def print_purchase_milk_batch(
    ids: str = Query(..., description="Comma-separated voucher IDs"),
    download: bool = Query(False),
    db: Session = Depends(get_db),
):
    """
    Generate a multi-page PDF for multiple Purchase Milk vouchers.

    Args:
        ids: Comma-separated list of voucher IDs (e.g., "1,2,3,10,15")
        download: If True, browser downloads; if False, displays inline
    """
    try:
        voucher_ids = [int(x.strip()) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid voucher IDs format")

    if not voucher_ids:
        raise HTTPException(status_code=400, detail="No voucher IDs provided")

    if len(voucher_ids) > 100:
        raise HTTPException(status_code=400, detail="Maximum 100 vouchers per batch")

    vouchers = db.query(PurchaseMilk).filter(PurchaseMilk.id.in_(voucher_ids)).all()

    if not vouchers:
        raise HTTPException(status_code=404, detail="No vouchers found for the given IDs")

    # Sort by date, then voucher number for consistent ordering
    vouchers.sort(key=lambda v: (v.date or "", v.voucher_number or ""))

    pdf_bytes = render_purchase_milk_vouchers_batch(vouchers)

    filename = f"PurchaseMilk_Batch_{len(vouchers)}_vouchers.pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'{"attachment" if download else "inline"}; filename="{filename}"'
        },
    )


@router.get("/purchase-milk/date-range")
def print_purchase_milk_date_range(
    from_date: str = Query(..., description="YYYYMMDD format"),
    to_date: str = Query(..., description="YYYYMMDD format"),
    download: bool = Query(False),
    db: Session = Depends(get_db),
):
    """
    Generate PDF for all Purchase Milk vouchers in a date range.

    Args:
        from_date: Start date in YYYYMMDD format
        to_date: End date in YYYYMMDD format
        download: If True, browser downloads; if False, displays inline
    """
    from datetime import datetime

    try:
        start = datetime.strptime(from_date, "%Y%m%d").date()
        end = datetime.strptime(to_date, "%Y%m%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYYMMDD.")

    vouchers = (
        db.query(PurchaseMilk)
        .filter(PurchaseMilk.date >= start, PurchaseMilk.date <= end)
        .order_by(PurchaseMilk.date, PurchaseMilk.voucher_number)
        .all()
    )

    if not vouchers:
        raise HTTPException(
            status_code=404,
            detail=f"No Purchase Milk vouchers found between {from_date} and {to_date}",
        )

    if len(vouchers) > 500:
        raise HTTPException(
            status_code=400,
            detail=f"Date range contains {len(vouchers)} vouchers. "
            "Maximum 500 per PDF. Please narrow the date range.",
        )

    pdf_bytes = render_purchase_milk_vouchers_batch(vouchers)

    filename = f"PurchaseMilk_{from_date}_to_{to_date}.pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'{"attachment" if download else "inline"}; filename="{filename}"'
        },
    )
