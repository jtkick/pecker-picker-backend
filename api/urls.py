from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView
from . import views

urlpatterns = [
    path("health/", views.health_check, name="health-check"),
    path("auth/request-otp/", views.request_otp, name="request-otp"),
    path("auth/verify-otp/", views.verify_otp, name="verify-otp"),
    path("auth/token/refresh/", TokenRefreshView.as_view(), name="token-refresh"),
    path("me/", views.me, name="me"),
    path("me/avatar/", views.avatar, name="avatar"),
    path("me/images/", views.gallery_add, name="gallery-add"),
    path("me/images/<int:pk>/", views.gallery_delete, name="gallery-delete"),
]
