"""
Helper fucntions for OCR invoice processing
"""


from typing import Tuple, Optional
from difflib import SequenceMatcher

def calculate_name_similarity(name1: str, name2: str) -> int:
    """
    - Calculate similarity between two company names (0-100%).
    - name1:OCR & name2:Database
    - Uses fuzzy matching with preprocessing to ignore common variations.
    - Returns: Similarity percentage (0-100)
    
    Example:
        >>> calculate_name_similarity("ACME Corp SA", "ACME Corporation S.A.")
        85
    """
    if not name1 or not name2:
        return 0
    
    # Normalize for comparison
    def normalize(text):
        text = text.lower()
        # Remove common company suffixes
        for suffix in [' s.a.', ' sa', ' s.l.', ' sl', ' s.a', ' s.l', ' ltd', ' inc', ' corp']:
            text = text.replace(suffix, '')
        # Remove punctuation and extra spaces
        text = ''.join(c for c in text if c.isalnum() or c.isspace())
        return ' '.join(text.split())
    
    norm1 = normalize(name1)
    norm2 = normalize(name2)
    
    # Calculate similarity ratio
    similarity = SequenceMatcher(None, norm1, norm2).ratio() # finds longest matching sequences and calculates a ratio
    
    return int(similarity * 100)


def match_provider(vendor_name: str, vendor_tax_id: str) -> Tuple[Optional[object], int, str]:
    """
    Find matching provider in database using tax ID and name  

    Matching strategy:
    1. Try exact match by tax_identifier (highest confidence)
    2. If found by tax_id, verify name similarity
    3. If not found by tax_id, try fuzzy name matching
    4. Return provider, confidence level, and message
    
    Args:
        vendor_name: Company name from OCR
        vendor_tax_id: Tax ID/NIF/CIF from OCR
    
    Returns:
        Tuple of (provider_object, confidence_percent, message):
        - (Provider, 100, "Matched by tax ID") → Auto-assign
        - (Provider, 85, "Matched by name") → Auto-assign
        - (None, 65, "Low confidence") → Manual review
        - (None, 0, "No match") → Manual selection
    
    Example:
        >>> provider, confidence, msg = match_provider("ACME Corp", "A12345678")
        >>> if confidence >= 80:
        >>>     invoice.provider = provider  # Auto-assign
    """
    from materials.models import Provider
    
    # Step 1: Try exact tax_id match
    if vendor_tax_id and vendor_tax_id.strip(): #if not empty
        try: # find provider with exact matching tax ID
            provider = Provider.objects.get(tax_identifier__iexact=vendor_tax_id.strip()) #__iexact is case-insensitive 
            
            # Verify name similarity
            name_similarity = calculate_name_similarity(vendor_name, provider.name)
            
            if name_similarity >= 60:
                return (
                    provider,
                    name_similarity,
                    f"Matched by tax ID (name: {name_similarity}%)"
                )
            else:
                return (
                    None,
                    name_similarity,
                    f"Tax ID found but name mismatch: {provider.name}"
                )
        
        except Provider.DoesNotExist: # no provider found with that tax ID
            pass
        
        except Provider.MultipleObjectsReturned:
            return (None, 0, "Multiple providers with same tax ID")
    

    # Step 2: Fuzzy name matching (if no tax_id match)
    if not vendor_name or not vendor_name.strip():
        return (None, 0, "No vendor name provided")
    
    all_providers = Provider.objects.all()
    best_match = None
    best_score = 0
    
    for provider in all_providers:
        score = calculate_name_similarity(vendor_name, provider.name)
        if score > best_score:
            best_score = score
            best_match = provider
    
    # Return based on confidence threshold
    if best_score >= 80:
        return (best_match, best_score, f"Matched by name ({best_score}%)")
    elif best_score >= 60:
        return (None, best_score, f"Low confidence: {best_match.name} ({best_score}%)")
    else:
        return (None, best_score, "No match found")

