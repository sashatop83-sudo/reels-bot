"""Telegram Payments + ЮKassa (provider token из BotFather)."""

import json


def yookassa_provider_data(price_rub: int, title: str, vat_code: int = 1) -> str:
    """Чек для 54-ФЗ — нужен ЮKassa в live-режиме."""
    amount = f"{price_rub:.2f}"
    payload = {
        "receipt": {
            "items": [
                {
                    "description": title[:128],
                    "quantity": "1.00",
                    "amount": {"value": amount, "currency": "RUB"},
                    "vat_code": vat_code,
                    "payment_mode": "full_payment",
                    "payment_subject": "service",
                }
            ]
        }
    }
    return json.dumps(payload, ensure_ascii=False)


def invoice_prices(price_rub: int, sub_days: int) -> list[dict]:
    return [{"label": f"PRO {sub_days} дн.", "amount": price_rub * 100}]
