import stripe
from django.conf import settings
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.http import HttpResponse
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from .models import User

stripe.api_key = settings.STRIPE_SECRET_KEY


def _get_or_create_customer(user: User) -> str:
    """Return the Stripe customer ID for this user, creating one if needed."""
    if user.stripe_customer_id:
        return user.stripe_customer_id
    customer = stripe.Customer.create(
        metadata={'user_id': user.pk},
        phone=user.phone_number,
    )
    User.objects.filter(pk=user.pk).update(stripe_customer_id=customer.id)
    user.stripe_customer_id = customer.id
    return customer.id


@api_view(['POST'])
def create_checkout_session(request):
    """
    Create a Stripe Checkout session for the given tier and return the URL.
    Body: { "tier": 1 }
    """
    tier = request.data.get('tier', 1)
    price_id = settings.STRIPE_TIER_PRICES.get(int(tier))
    if not price_id:
        return Response({'error': f'No price configured for tier {tier}.'},
                        status=status.HTTP_400_BAD_REQUEST)
    if not settings.STRIPE_SECRET_KEY:
        return Response({'error': 'Payments not configured.'},
                        status=status.HTTP_503_SERVICE_UNAVAILABLE)

    customer_id = _get_or_create_customer(request.user)
    session = stripe.checkout.Session.create(
        customer=customer_id,
        mode='subscription',
        line_items=[{'price': price_id, 'quantity': 1}],
        success_url=settings.STRIPE_SUCCESS_URL,
        cancel_url=settings.STRIPE_CANCEL_URL,
        metadata={'user_id': request.user.pk, 'tier': tier},
    )
    return Response({'url': session.url})


@api_view(['POST'])
def create_portal_session(request):
    """Open the Stripe billing portal so the user can manage or cancel."""
    if not request.user.stripe_customer_id:
        return Response({'error': 'No active subscription found.'},
                        status=status.HTTP_404_NOT_FOUND)
    if not settings.STRIPE_SECRET_KEY:
        return Response({'error': 'Payments not configured.'},
                        status=status.HTTP_503_SERVICE_UNAVAILABLE)

    session = stripe.billing_portal.Session.create(
        customer=request.user.stripe_customer_id,
        return_url=settings.STRIPE_SUCCESS_URL,
    )
    return Response({'url': session.url})


@csrf_exempt
@require_POST
def stripe_webhook(request):
    """Receive and verify Stripe webhook events."""
    payload = request.body
    sig_header = request.META.get('HTTP_STRIPE_SIGNATURE', '')

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
        )
    except (ValueError, stripe.error.SignatureVerificationError):
        return HttpResponse(status=400)

    _handle_event(event)
    return HttpResponse(status=200)


def _handle_event(event):
    etype = event['type']

    if etype == 'checkout.session.completed':
        session = event['data']['object']
        if session.get('mode') != 'subscription':
            return
        user_id = session.get('metadata', {}).get('user_id')
        tier = int(session.get('metadata', {}).get('tier', 1))
        sub_id = session.get('subscription')
        customer_id = session.get('customer')
        if not user_id:
            return
        # Fetch subscription to get period end.
        sub = stripe.Subscription.retrieve(sub_id)
        expires_at = timezone.datetime.fromtimestamp(
            sub['current_period_end'], tz=timezone.utc
        )
        User.objects.filter(pk=user_id).update(
            subscription_tier=tier,
            subscription_expires_at=expires_at,
            stripe_customer_id=customer_id,
            stripe_subscription_id=sub_id,
        )

    elif etype == 'customer.subscription.updated':
        sub = event['data']['object']
        _sync_subscription(sub)

    elif etype == 'customer.subscription.deleted':
        sub = event['data']['object']
        User.objects.filter(stripe_subscription_id=sub['id']).update(
            subscription_tier=User.TIER_FREE,
            subscription_expires_at=None,
            stripe_subscription_id='',
        )

    elif etype == 'invoice.payment_failed':
        # Could send a notification here; for now just log.
        pass


def _sync_subscription(sub):
    """Update tier and expiry from a Stripe Subscription object."""
    sub_id = sub['id']
    active = sub['status'] in ('active', 'trialing')
    expires_at = timezone.datetime.fromtimestamp(
        sub['current_period_end'], tz=timezone.utc
    ) if active else None

    # Determine tier from the price metadata or default to 1.
    tier = User.TIER_FREE
    if active:
        price_id = sub['items']['data'][0]['price']['id']
        for t, pid in settings.STRIPE_TIER_PRICES.items():
            if pid == price_id:
                tier = t
                break
        else:
            tier = User.TIER_PLUS  # fallback

    User.objects.filter(stripe_subscription_id=sub_id).update(
        subscription_tier=tier,
        subscription_expires_at=expires_at,
    )
