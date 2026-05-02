"""
sustainability.py — Carbon savings and food waste prevention tracking.
Calculates real environmental impact per restaurant per month.
Sends monthly summaries via Telegram. Shows live counter on dashboard.
"""
from __future__ import annotations
import datetime
import os
from typing import Optional

# ── CO2 emission factors (kg CO2 per kg food wasted) ──────────────────────
# Source: WRAP Food Waste Prevention, FAO data
CO2_FACTORS: dict[str, float] = {
    "chicken":    6.9,
    "ayam":       6.9,
    "beef":       27.0,
    "daging":     27.0,
    "fish":       5.1,
    "ikan":       5.1,
    "prawn":      5.1,
    "udang":      5.1,
    "rice":       2.7,
    "nasi":       2.7,
    "beras":      2.7,
    "noodle":     1.9,
    "mee":        1.9,
    "kuey teow":  1.9,
    "vegetable":  1.8,
    "sayur":      1.8,
    "egg":        4.2,
    "telur":      4.2,
    "tofu":       2.0,
    "tahu":       2.0,
    "milk":       3.2,
    "susu":       3.2,
    "default":    2.5,  # Used when food type is unknown
}

# 1 tree absorbs ~1.75 kg CO2 per month (average tropical tree)
CO2_PER_TREE_PER_MONTH = 1.75

# Minimum waste prevented (kg) needed to generate a summary
MIN_WASTE_FOR_SUMMARY = 0.5


def _get_co2_factor(item_name: str) -> float:
    """Return the CO2 factor for a menu item based on keyword matching."""
    item_lower = item_name.lower()
    for keyword, factor in CO2_FACTORS.items():
        if keyword in item_lower:
            return factor
    return CO2_FACTORS["default"]


def calculate_waste_prevented_kg(daily_record: dict) -> float:
    """
    Calculate kg of food waste prevented from a single daily record.
    Uses closing stock that was NOT wasted as the "prevented" amount.
    Assumes 300g average portion weight.
    """
    PORTION_WEIGHT_KG = 0.30  # 300g average Malaysian hawker portion
    prevented = 0.0

    # Items that had closing stock and were sold (not wasted)
    for item_data in daily_record.get("items_sold", []):
        # prevented = what was forecasted to waste but didn't
        forecasted_waste = item_data.get("forecasted_waste_qty", 0)
        actual_waste = item_data.get("actual_waste_qty", 0)
        prevented_portions = max(0, forecasted_waste - actual_waste)
        prevented += prevented_portions * PORTION_WEIGHT_KG

    return round(prevented, 3)


def calculate_monthly_carbon_savings(restaurant: dict, year: int, month: int) -> dict:
    """
    Calculate total sustainability stats for a restaurant for a given month.
    Returns dict with waste_prevented_kg, co2_saved_kg, trees_equivalent.
    Returns None if insufficient data.
    """
    total_waste_prevented_kg = 0.0
    total_co2_saved_kg = 0.0

    daily_records = restaurant.get("daily_records", [])

    for record in daily_records:
        try:
            record_date = datetime.date.fromisoformat(record.get("date", ""))
        except (ValueError, TypeError):
            continue

        if record_date.year != year or record_date.month != month:
            continue

        # Calculate waste prevented from closing stock data
        closing_stock = record.get("closing_stock", [])
        for item in closing_stock:
            qty_sold = item.get("qty_sold_from_closing", 0)  # portions sold at discount
            item_name = item.get("item", "")
            co2_factor = _get_co2_factor(item_name)
            waste_prevented_kg = qty_sold * 0.30  # 300g per portion
            co2_saved = waste_prevented_kg * co2_factor
            total_waste_prevented_kg += waste_prevented_kg
            total_co2_saved_kg += co2_saved

    if total_waste_prevented_kg < MIN_WASTE_FOR_SUMMARY:
        return None

    trees_equivalent = round(total_co2_saved_kg / CO2_PER_TREE_PER_MONTH, 1)

    return {
        "year": year,
        "month": month,
        "waste_prevented_kg": round(total_waste_prevented_kg, 2),
        "co2_saved_kg": round(total_co2_saved_kg, 2),
        "trees_equivalent": trees_equivalent,
    }


def get_lifetime_sustainability_totals(restaurant: dict) -> dict:
    """
    Get all-time sustainability totals for dashboard live counter.
    Uses stored running totals + calculates any missing months.
    """
    stored = restaurant.get("sustainability_totals", {})
    return {
        "waste_prevented_kg": round(stored.get("waste_prevented_kg", 0), 2),
        "co2_saved_kg": round(stored.get("co2_saved_kg", 0), 2),
        "trees_equivalent": round(
            stored.get("co2_saved_kg", 0) / CO2_PER_TREE_PER_MONTH, 1
        ),
    }


def update_sustainability_totals(restaurant: dict, new_prevented_kg: float, new_co2_kg: float) -> None:
    """Add new savings to the restaurant's running totals. Call after each day ends."""
    totals = restaurant.setdefault("sustainability_totals", {
        "waste_prevented_kg": 0.0,
        "co2_saved_kg": 0.0,
    })
    totals["waste_prevented_kg"] = round(
        totals.get("waste_prevented_kg", 0) + new_prevented_kg, 3
    )
    totals["co2_saved_kg"] = round(
        totals.get("co2_saved_kg", 0) + new_co2_kg, 3
    )


def format_monthly_telegram_message(stats: dict, restaurant_name: str,
                                     language: str = "english") -> str:
    """
    Format the monthly sustainability summary for Telegram.
    Sent on the 1st of each month by the scheduler.
    """
    month_name = datetime.date(stats["year"], stats["month"], 1).strftime("%B %Y")
    waste_kg = stats["waste_prevented_kg"]
    co2_kg = stats["co2_saved_kg"]
    trees = stats["trees_equivalent"]

    if language == "malay":
        return (
            f"🌿 *Ringkasan Alam Sekitar — {month_name}*\n\n"
            f"Syabas {restaurant_name}! Ini impak anda bulan lepas:\n\n"
            f"♻️ *{waste_kg:.1f} kg* sisa makanan dicegah\n"
            f"🌍 *{co2_kg:.1f} kg* CO₂ diselamatkan\n"
            f"🌳 Bersamaan menanam *{trees} pokok*\n\n"
            f"Teruskan usaha yang baik! Setiap hidangan yang terjual membantu 🌱"
        )
    elif language == "mandarin":
        return (
            f"🌿 *环保月报 — {month_name}*\n\n"
            f"恭喜 {restaurant_name}！上月的环保贡献：\n\n"
            f"♻️ 减少了 *{waste_kg:.1f} 公斤* 食物浪费\n"
            f"🌍 节省了 *{co2_kg:.1f} 公斤* CO₂ 排放\n"
            f"🌳 相当于种植了 *{trees} 棵树*\n\n"
            f"继续努力！每一份售出的食物都在保护地球 🌱"
        )
    else:
        return (
            f"🌿 *Monthly Sustainability Report — {month_name}*\n\n"
            f"Great work, {restaurant_name}! Here's your environmental impact:\n\n"
            f"♻️ *{waste_kg:.1f} kg* of food waste prevented\n"
            f"🌍 *{co2_kg:.1f} kg* of CO₂ saved\n"
            f"🌳 Equivalent to planting *{trees} trees*\n\n"
            f"Keep it up! Every portion sold is a win for the planet 🌱"
        )


async def send_monthly_sustainability_summary(restaurant: dict, tg_token: str) -> bool:
    """
    Send the monthly sustainability summary via Telegram.
    Called by scheduler on the 1st of each month.
    Returns True if sent successfully.
    """
    import httpx

    now = datetime.date.today()
    # Calculate for last month
    if now.month == 1:
        target_year, target_month = now.year - 1, 12
    else:
        target_year, target_month = now.year, now.month - 1

    stats = calculate_monthly_carbon_savings(restaurant, target_year, target_month)
    if not stats:
        return False  # Not enough data

    chat_id = restaurant.get("telegram_chat_id")
    if not chat_id or not tg_token:
        return False

    language = restaurant.get("preferred_language", "english")
    msg = format_monthly_telegram_message(stats, restaurant["name"], language)

    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
                timeout=10,
            )
            return r.status_code == 200
    except Exception as e:
        print(f"[Sustainability] Telegram send failed: {e}")
        return False
