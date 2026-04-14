from decimal import Decimal
from djmoney.money import Money

unit_price = Money("100.00", "EUR")
quantity = Decimal("1.000")
discount_percentage = Decimal("10.00")

discount_amount = (unit_price * quantity) * (discount_percentage / Decimal("100"))
print(f"Discount Amount: {discount_amount}")
print(f"Type: {type(discount_amount)}")

gross_total = unit_price * quantity
discount_amount_2 = gross_total * (discount_percentage / Decimal("100"))
line_total = gross_total - discount_amount_2
print(f"Line Total: {line_total}")
