import os
import sys
import django

# Setup Django
sys.path.append('/Users/joangaes/Documents/Scalewave/fistex/alumniniooerp')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'alumniniooerp.settings')
django.setup()

from configurator.models import ConfigurationExportTemplate
from configurator.services.export.helpers import strip_wrappers, strip_css_wrappers

for t in ConfigurationExportTemplate.objects.all():
    try:
        old_html = t.html
        old_css = t.css
        
        t.html = strip_wrappers(t.html)
        t.css = strip_css_wrappers(t.css)
        t.save()
        
        if old_html != t.html or old_css != t.css:
            print(f"Cleaned up template {t.id}: {t.name}")
        else:
            pass # No changes needed
    except Exception as e:
        print(f"Error cleaning {t.id}: {e}")
