"""Conservative Clover order-total calculation from an expanded, paged snapshot."""

from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP


class UnsupportedOrderCalculationError(ValueError):
    """The snapshot does not prove a safe total to write to Clover."""


def _cents(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int):
        raise UnsupportedOrderCalculationError("Missing or invalid monetary component")
    return Decimal(value)


def _elements(record: dict, field: str) -> list[dict]:
    value = record.get(field)
    if value is None:
        return []
    if not isinstance(value, dict) or not isinstance(value.get("elements"), list):
        raise UnsupportedOrderCalculationError("Incomplete order component")
    elements = value["elements"]
    if any(not isinstance(element, dict) for element in elements):
        raise UnsupportedOrderCalculationError("Invalid order component")
    return elements


def _rounded(value: Decimal) -> Decimal:
    return value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def _discounted(amount: Decimal, discounts: list[dict]) -> Decimal:
    percentages: list[Decimal] = []
    fixed: list[Decimal] = []
    for discount in discounts:
        if discount.get("percentage") is not None:
            percentage = _cents(discount["percentage"])
            if not 0 <= percentage <= 100:
                raise UnsupportedOrderCalculationError("Unsupported discount")
            percentages.append(percentage)
        elif discount.get("amount") is not None:
            value = _cents(discount["amount"])
            if value > 0:
                raise UnsupportedOrderCalculationError("Unsupported discount")
            fixed.append(-value)
        else:
            raise UnsupportedOrderCalculationError("Unsupported discount")
    for percentage in percentages:
        amount -= _rounded(amount * percentage / Decimal(100))
    amount -= sum(fixed, Decimal(0))
    if amount < 0:
        raise UnsupportedOrderCalculationError("Discount exceeds price")
    return amount


def calculate_order_total(order: dict, lines: list[dict]) -> int:
    """Return integer cents, refusing unmodeled or incomplete price components."""
    if order.get("currency") != "USD" or not lines:
        raise UnsupportedOrderCalculationError("Unsupported currency or empty order")
    if order.get("additionalCharges") or order.get("taxAmount") is not None:
        raise UnsupportedOrderCalculationError("Unsupported order charge")
    if any(line.get("isOrderFee") or line.get("priceType") not in (None, "FIXED")
           for line in lines):
        raise UnsupportedOrderCalculationError("Unsupported line type")
    bases: list[Decimal] = []
    tax_groups: dict[tuple[str, int], Decimal] = defaultdict(Decimal)
    vat = order.get("isVat", False)
    if not isinstance(vat, bool):
        raise UnsupportedOrderCalculationError("Invalid VAT flag")
    if order.get("taxRemoved", False) not in (True, False):
        raise UnsupportedOrderCalculationError("Invalid tax flag")
    for line in lines:
        qty = line.get("unitQty")
        if isinstance(qty, bool) or qty not in (None, 1):
            raise UnsupportedOrderCalculationError("Unsupported quantity representation")
        amount = _cents(line.get("price"))
        if amount < 0:
            raise UnsupportedOrderCalculationError("Negative line price")
        for modification in _elements(line, "modifications"):
            amount += _cents(modification.get("amount"))
        if amount < 0:
            raise UnsupportedOrderCalculationError("Invalid modifier total")
        amount = _discounted(amount, _elements(line, "discounts"))
        bases.append(amount)
        for tax in _elements(line, "taxRates"):
            rate = _cents(tax.get("rate"))
            tax_id = tax.get("id")
            if rate < 0 or not isinstance(tax_id, str) or not tax_id:
                raise UnsupportedOrderCalculationError("Unsupported tax rate")
            tax_groups[(tax_id, int(rate))] += amount
    order_discounts = _elements(order, "discounts")
    if not tax_groups and order.get("totalTaxAmount") not in (None, 0):
        raise UnsupportedOrderCalculationError("Missing tax-rate details")
    if order_discounts:
        if len(bases) > 1 and any(
            discount.get("amount") is not None for discount in order_discounts
        ):
            raise UnsupportedOrderCalculationError("Fixed order discount allocation")
        bases = [_discounted(base, order_discounts) for base in bases]
        if tax_groups:
            tax_groups.clear()
            for line, base in zip(lines, bases):
                for tax in _elements(line, "taxRates"):
                    tax_groups[(tax["id"], tax["rate"])] += base
    subtotal = sum(bases, Decimal(0))
    if order.get("taxRemoved") is True or vat:
        tax_total = Decimal(0)
    else:
        tax_total = sum(
            (_rounded(base * Decimal(rate) / Decimal(10_000_000))
             for (_, rate), base in tax_groups.items()), Decimal(0)
        )
    service = order.get("serviceCharge")
    charge = Decimal(0)
    if service is not None:
        if not isinstance(service, dict):
            raise UnsupportedOrderCalculationError("Invalid service charge")
        if service.get("percentageDecimal") is not None:
            percent = _cents(service["percentageDecimal"])
            if not 0 <= percent <= 1_000_000:
                raise UnsupportedOrderCalculationError("Unsupported service charge")
            charge = _rounded(subtotal * percent / Decimal(1_000_000))
        elif service.get("amount") is not None:
            charge = _cents(service["amount"])
            if charge < 0:
                raise UnsupportedOrderCalculationError("Unsupported service charge")
        else:
            raise UnsupportedOrderCalculationError("Incomplete service charge")
    return int(subtotal + tax_total + charge)
