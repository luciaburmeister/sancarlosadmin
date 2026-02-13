from abc import ABC, abstractmethod
from typing import Tuple, Optional
from accounting.models import CompanyAccount, AccountType, BalanceSide, CompanyChartOfAccounts, ChartOfAccountsTemplate, TemplateAccount
from django.db import transaction

class ChartTypeInferenceStrategy(ABC):
    """
    Strategy interface for inferring account attributes from a code.
    """
    @abstractmethod
    def infer(self, code: str) -> Tuple[AccountType, BalanceSide]:
        pass

class PGCInferenceStrategy(ChartTypeInferenceStrategy):
    """
    Inference strategy based on Spanish PGC standard.
    """
    def infer(self, code: str) -> Tuple[AccountType, BalanceSide]:
        code = str(code)
        first_digit = code[0]
        two_digits = code[:2]
        three_digits = code[:3] if len(code) >= 3 else ""

        # Defaults
        account_type = AccountType.ASSET
        normal_balance = BalanceSide.DEBIT

        if first_digit == '1':
            # Financiación Básica -> Patrimonio/Pasivo -> Haber
            account_type = AccountType.LIABILITY 
            if two_digits in ['10', '11', '12', '13', '14']: # 13 Subvenciones, 14 Provisiones (Pasivo)
                 if two_digits in ['10', '11', '12', '13']: # 13 is Net Equity in PGC 2007
                     account_type = AccountType.EQUITY
            normal_balance = BalanceSide.CREDIT

        elif first_digit == '2':
            # Activo No Corriente -> Activo -> Debe
            account_type = AccountType.ASSET
            normal_balance = BalanceSide.DEBIT

        elif first_digit == '3':
            # Existencias -> Activo -> Debe
            account_type = AccountType.ASSET
            normal_balance = BalanceSide.DEBIT

        elif first_digit == '4':
            # Acreedores y Deudores
            if two_digits in ['40', '41']:
                account_type = AccountType.LIABILITY
                normal_balance = BalanceSide.CREDIT
            elif two_digits in ['43', '44']:
                account_type = AccountType.ASSET
                normal_balance = BalanceSide.DEBIT
            elif two_digits == '47':
                if three_digits in ['470', '472']:
                    account_type = AccountType.ASSET
                    normal_balance = BalanceSide.DEBIT
                elif three_digits in ['475', '476', '477']:
                    account_type = AccountType.LIABILITY
                    normal_balance = BalanceSide.CREDIT
        
        elif first_digit == '5':
            # Cuentas Financieras
            if two_digits == '57':
                account_type = AccountType.ASSET
                normal_balance = BalanceSide.DEBIT
            elif two_digits == '52':
                account_type = AccountType.LIABILITY
                normal_balance = BalanceSide.CREDIT
            elif two_digits == '55':
                 # 550 Titulares acciones -> Activo
                 # 551 c/c socios -> Pasivo/Activo (Usually Liability if credit balance)
                 pass

        elif first_digit == '6':
            account_type = AccountType.EXPENSE
            normal_balance = BalanceSide.DEBIT

        elif first_digit == '7':
            account_type = AccountType.INCOME
            normal_balance = BalanceSide.CREDIT
        
        elif first_digit == '8':
            account_type = AccountType.EXPENSE
            normal_balance = BalanceSide.DEBIT

        elif first_digit == '9':
            account_type = AccountType.INCOME
            normal_balance = BalanceSide.CREDIT

        return account_type, normal_balance


class ChartInstallerService:
    """
    Service to install a Chart of Accounts Template into a Company.
    """
    def __init__(self, inference_strategy: ChartTypeInferenceStrategy):
        self.strategy = inference_strategy

    @transaction.atomic
    def install(self, company, template: ChartOfAccountsTemplate) -> int:
        """
        Clones accounts from Template to Company using the configured strategy.
        """
        installed_count = 0
        
        # 1. Fetch template accounts ordered by logic (Code length usually implies hierarchy in PGC)
        # Ordering by 'code' works for '1', '10', '100'.
        template_accounts = template.accounts.all().order_by('code')
        
        # 2. Pre-load existing company accounts to resolve parents and avoid duplicates
        existing_map = {acc.code: acc for acc in CompanyAccount.objects.filter(company=company)}
        
        for t_acc in template_accounts:
            if t_acc.code in existing_map:
                continue
                
            account_type, normal_balance = self.strategy.infer(t_acc.code)
            
            # Resolve parent
            parent_account = None
            if t_acc.parent:
                parent_account = existing_map.get(t_acc.parent.code)
            
            # Create
            c_acc = CompanyAccount.objects.create(
                company=company,
                code=t_acc.code,
                name=t_acc.name,
                template_account=t_acc,
                is_postable=t_acc.is_postable,
                account_type=account_type,
                normal_balance=normal_balance,
                parent=parent_account
            )
            
            existing_map[c_acc.code] = c_acc
            installed_count += 1
            
        return installed_count

def install_chart_standard(company, template) -> int:
    """
    Helper function using standard PGC strategy.
    """
    strategy = PGCInferenceStrategy()
    service = ChartInstallerService(strategy)
    return service.install(company, template)
