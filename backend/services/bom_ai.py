"""
When an owner doesn't know their ingredient ratios for a dish,
this asks the AI to generate a precise recipe based on the dish name
and the restaurant's region, so the shopping list stays accurate.
"""

import os, sys
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)


def generate_bom_for_item(item_name: str, region: str, restaurant_type: str) -> dict:
    """
    Ask the AI for a realistic ingredient BOM for this menu item
    based on how it's typically made in the given Malaysian region.
    Returns a dict like {rice_g: 200, coconut_milk_ml: 50, cost_rm: 1.20}
    """
    from services.ai_provider import call_ai_json

    prompt = (
        f"You are a Malaysian culinary expert who knows exact ingredient amounts used in food businesses.\n\n"
        f"Generate the ingredient bill-of-materials (BOM) for ONE serving of: '{item_name}'\n"
        f"Restaurant type: {restaurant_type}\n"
        f"Location: {region}, Malaysia\n\n"
        "Use amounts typical for a commercial Malaysian food stall — not home cooking.\n"
        "Return a JSON object with ingredient keys (snake_case, _g for grams, _ml for millilitres, "
        "no unit suffix for countable items like 'egg') and numeric values per serving.\n"
        "Also include 'cost_rm' (estimated raw material cost per serving in Malaysian Ringgit at 2026 prices).\n\n"
        "Example format: {\"rice_g\": 200, \"coconut_milk_ml\": 50, \"anchovy_g\": 20, \"cost_rm\": 1.20}\n\n"
        "Return ONLY the JSON object, nothing else."
    )

    result = call_ai_json(prompt)
    if isinstance(result, dict) and result:
        cleaned = {}
        for k, v in result.items():
            if isinstance(v, (int, float)) and v > 0:
                cleaned[k] = round(float(v), 2) if k == "cost_rm" else v
        return cleaned
    return {}


def ask_bom_conversational(item_name: str, region: str, restaurant_type: str,
                            owner_input: str) -> dict:
    """
    Owner said something about ingredients but it may be vague.
    Combine their input with AI knowledge to fill in gaps.
    If owner said 'don't know', fall back entirely to AI.
    """
    from services.ai_provider import call_ai_json

    dont_know = any(p in owner_input.lower() for p in [
        "don't know", "dont know", "not sure", "no idea", "skip", "idk", "tak tahu"
    ])

    if dont_know or not owner_input.strip():
        return generate_bom_for_item(item_name, region, restaurant_type)

    prompt = (
        f"A Malaysian restaurant owner describes the ingredients for '{item_name}' ({restaurant_type}, {region}):\n"
        f"\"{owner_input}\"\n\n"
        "Fill in any missing details based on how this dish is typically made commercially in Malaysia.\n"
        "Return a precise JSON BOM with units (_g, _ml) and 'cost_rm' (RM per serving, 2026 prices).\n"
        "Return ONLY the JSON object."
    )

    result = call_ai_json(prompt)
    if isinstance(result, dict) and result:
        cleaned = {k: v for k, v in result.items() if isinstance(v, (int, float)) and v > 0}
        return cleaned

    # If AI fails, generate from scratch
    return generate_bom_for_item(item_name, region, restaurant_type)
