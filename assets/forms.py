from django import forms
from .models import AssetIdentifier, Asset

class AssetIdentifierForm(forms.ModelForm):
    class Meta:
        model = AssetIdentifier
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        asset = None
        if self.instance and hasattr(self.instance, 'asset'):
            asset = self.instance.asset
        elif self.initial.get('asset'):
            asset = Asset.objects.filter(pk=self.initial.get('asset')).first()
        
        if asset and asset.company and asset.company.asset_identifier_labels:
            labels = asset.company.asset_identifier_labels
            self.fields['label'] = forms.ChoiceField(
                choices=[('', '---------')] + [(l, l) for l in labels],
                required=True
            )
