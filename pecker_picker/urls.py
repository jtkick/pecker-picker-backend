import json
from django.contrib import admin
from django.http import JsonResponse
from django.urls import path, include
from django.conf import settings


def apple_app_site_association(request):
    data = {
        "webcredentials": {
            "apps": [f"YGAVSW556H.com.jaredkick.peckerPicker"],
        }
    }
    return JsonResponse(data, content_type="application/json")


def asset_links(request):
    data = [
        {
            "relation": ["delegate_permission/common.get_login_creds"],
            "target": {
                "namespace": "android_app",
                "package_name": "com.jaredkick.peckerPicker",
                # Replace with your release signing cert SHA-256 fingerprint:
                # keytool -list -v -keystore your-release.jks
                "sha256_cert_fingerprints": [
                    settings.ANDROID_SHA256_CERT_FINGERPRINT if hasattr(settings, 'ANDROID_SHA256_CERT_FINGERPRINT') else "PLACEHOLDER"
                ],
            },
        }
    ]
    return JsonResponse(data, safe=False, content_type="application/json")


urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("api.urls")),
    path(".well-known/apple-app-site-association", apple_app_site_association),
    path(".well-known/assetlinks.json", asset_links),
]
