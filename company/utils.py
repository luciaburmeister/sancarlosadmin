from company.models import Employee

def get_current_employee(user):
    if not user or not user.is_authenticated:
        return None
    return Employee.objects.filter(user=user).first()