from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView
from . import views, stripe_views

urlpatterns = [
    path("health/", views.health_check, name="health-check"),
    path("auth/passkey/register/begin/", views.passkey_register_begin, name="passkey-register-begin"),
    path("auth/passkey/register/complete/", views.passkey_register_complete, name="passkey-register-complete"),
    path("auth/passkey/authenticate/begin/", views.passkey_authenticate_begin, name="passkey-authenticate-begin"),
    path("auth/passkey/authenticate/complete/", views.passkey_authenticate_complete, name="passkey-authenticate-complete"),
    path("auth/token/refresh/", TokenRefreshView.as_view(), name="token-refresh"),
    path("auth/logout/", views.logout, name="logout"),
    path("me/", views.me, name="me"),
    path("me/images/", views.gallery_add, name="gallery-add"),
    path("me/images/<int:pk>/", views.gallery_delete, name="gallery-delete"),
    path("nearby/", views.nearby, name="nearby"),
    path("users/", views.users_list, name="users-list"),
    path("users/<int:pk>/", views.user_detail, name="user-detail"),
    path("image-grants/", views.image_grants, name="image-grants"),
    path("image-grants/<int:pk>/", views.image_grant_delete, name="image-grant-delete"),
    path("location-grants/", views.location_grants, name="location-grants"),
    path("location-grants/<int:pk>/", views.location_grant_delete, name="location-grant-delete"),
    path("blocks/", views.blocks, name="blocks"),
    path("blocks/<int:pk>/", views.block_delete, name="block-delete"),
    path("reports/", views.reports, name="reports"),
    path("verification/", views.verification, name="verification"),
    path("subscriptions/checkout/", stripe_views.create_checkout_session, name="subscription-checkout"),
    path("subscriptions/portal/", stripe_views.create_portal_session, name="subscription-portal"),
    path("webhooks/stripe/", stripe_views.stripe_webhook, name="stripe-webhook"),
    path("conversations/", views.conversations, name="conversations"),
    path("conversations/<int:pk>/messages/", views.conversation_messages, name="conversation-messages"),
    path("kinks/", views.kinks_list, name="kinks-list"),
    path("interests/", views.interests_list, name="interests-list"),
]
