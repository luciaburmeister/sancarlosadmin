from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.views import TokenObtainPairView


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["username"] = user.username
        token["full_name"] = f"{user.first_name} {user.last_name}".strip() or user.username
        return token

    def validate(self, attrs):
        data = super().validate(attrs)
        user = self.user
        data["user_id"] = user.id
        data["username"] = user.username
        data["full_name"] = f"{user.first_name} {user.last_name}".strip() or user.username
        return data


class LoginView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer
