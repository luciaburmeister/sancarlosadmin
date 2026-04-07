from typing import Any, Dict

def get_company_context(company) -> Dict[str, Any]:
    """
    Returns a common context dictionary for company information,
    including basic data and default bank account.
    """
    if not company:
        return {
            "company": None,
            "company_data": {},
        }

    company_data = {
        "name": company.name,
        "legal_name": company.legal_name,
        "address": company.address,
        "email": company.email,
        "phone": company.phone,
        "tax_identifier": company.tax_identifier,
        "logo": company.logo.url if company.logo and hasattr(company.logo, 'url') else None,
    }

    result = {
        "company": company,
        "company_data": company_data,
    }

    if hasattr(company, "bankaccounts"):
        default_bank = company.bankaccounts.filter(is_default=True).first()
        if default_bank:
            result["bank_account"] = {
                "bank_name": default_bank.bank_name,
                "holder_name": default_bank.holder_name,
                "iban": default_bank.iban,
                "swift_bic": default_bank.swift_bic,
                "account_number": default_bank.account_number,
                "routing_number": default_bank.routing_number,
                "bank_address": default_bank.bank_address,
                "country": str(default_bank.country) if default_bank.country else None,
            }

    return result
