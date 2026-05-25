"""Endpoints for QuotePhoto and QuotePhotoCategory."""

from django.http import JsonResponse

from api._serializers import serialize_photo_category, serialize_quote_photo
from api._utils import CompanyAPIView
from sales.models import Quote, QuotePhoto, QuotePhotoCategory


class QuotePhotoListCreateView(CompanyAPIView):
    def get(self, request, quote_id):
        try:
            quote = Quote.objects.get(id=quote_id, company=self.company)
        except Quote.DoesNotExist:
            return JsonResponse({"error": "Not found"}, status=404)

        photos = (
            quote.photos
            .select_related("category")
            .order_by("category__order", "uploaded_at")
        )
        return JsonResponse({
            "results": [serialize_quote_photo(p, request) for p in photos]
        })

    def post(self, request, quote_id):
        try:
            quote = Quote.objects.get(id=quote_id, company=self.company)
        except Quote.DoesNotExist:
            return JsonResponse({"error": "Not found"}, status=404)

        image = request.FILES.get("image")
        if not image:
            return JsonResponse({"error": "No image provided"}, status=400)

        category_id = request.POST.get("category_id")
        caption = request.POST.get("caption", "")

        category = None
        if category_id:
            try:
                category = QuotePhotoCategory.objects.get(
                    id=int(category_id), company=self.company
                )
            except (QuotePhotoCategory.DoesNotExist, ValueError):
                return JsonResponse({"error": "Category not found"}, status=404)

        photo = QuotePhoto.objects.create(
            quote=quote,
            category=category,
            image=image,
            caption=caption,
        )
        return JsonResponse(serialize_quote_photo(photo, request), status=201)


class QuotePhotoDeleteView(CompanyAPIView):
    def delete(self, request, quote_id, photo_id):
        try:
            photo = QuotePhoto.objects.select_related("quote").get(
                id=photo_id, quote_id=quote_id, quote__company=self.company
            )
        except QuotePhoto.DoesNotExist:
            return JsonResponse({"error": "Not found"}, status=404)

        photo.image.delete(save=False)
        photo.delete()
        return JsonResponse({"success": True})


class PhotoCategoryListCreateView(CompanyAPIView):
    def get(self, request):
        cats = QuotePhotoCategory.objects.filter(company=self.company).order_by("order", "id")
        return JsonResponse({"results": [serialize_photo_category(c) for c in cats]})

    def post(self, request):
        import json
        body = json.loads(request.body)
        name = body.get("name", "").strip()
        if not name:
            return JsonResponse({"error": "name is required"}, status=400)
        order = body.get("order", 0)
        cat = QuotePhotoCategory.objects.create(
            company=self.company, name=name, order=order
        )
        return JsonResponse(serialize_photo_category(cat), status=201)


class PhotoCategoryDetailView(CompanyAPIView):
    def patch(self, request, category_id):
        import json
        try:
            cat = QuotePhotoCategory.objects.get(id=category_id, company=self.company)
        except QuotePhotoCategory.DoesNotExist:
            return JsonResponse({"error": "Not found"}, status=404)

        body = json.loads(request.body)
        update_fields = []
        if "name" in body:
            cat.name = body["name"].strip()
            update_fields.append("name")
        if "order" in body:
            cat.order = body["order"]
            update_fields.append("order")
        if "is_active" in body:
            cat.is_active = body["is_active"]
            update_fields.append("is_active")
        if update_fields:
            cat.save(update_fields=update_fields)
        return JsonResponse(serialize_photo_category(cat))

    def delete(self, request, category_id):
        try:
            cat = QuotePhotoCategory.objects.get(id=category_id, company=self.company)
        except QuotePhotoCategory.DoesNotExist:
            return JsonResponse({"error": "Not found"}, status=404)
        cat.delete()
        return JsonResponse({"success": True})
