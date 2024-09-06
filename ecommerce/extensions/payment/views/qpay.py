

import logging

from django.core.exceptions import MultipleObjectsReturned, ObjectDoesNotExist
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import redirect
from oscar.apps.partner import strategy
from oscar.core.loading import get_class, get_model
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from ecommerce.extensions.basket.constants import PAYMENT_INTENT_ID_ATTRIBUTE
from ecommerce.extensions.basket.utils import basket_add_organization_attribute, basket_add_payment_intent_id_attribute, track_segment_event
from ecommerce.extensions.checkout.mixins import EdxOrderPlacementMixin
from ecommerce.extensions.checkout.utils import get_receipt_page_url
from ecommerce.extensions.payment.core.sdn import checkSDN
from ecommerce.extensions.payment.forms import QpaySubmitForm
from ecommerce.extensions.payment.processors.qpay import Qpay
from ecommerce.extensions.payment.views import BasePaymentSubmitView

logger = logging.getLogger(__name__)

Applicator = get_class('offer.applicator', 'Applicator')
BasketAttribute = get_model('basket', 'BasketAttribute')
BasketAttributeType = get_model('basket', 'BasketAttributeType')
BillingAddress = get_model('order', 'BillingAddress')
Country = get_model('address', 'Country')
NoShippingRequired = get_class('shipping.methods', 'NoShippingRequired')
OrderTotalCalculator = get_class('checkout.calculators', 'OrderTotalCalculator')
PaymentProcessorResponse = get_model('payment', 'PaymentProcessorResponse')


class QpayCheckView(EdxOrderPlacementMixin, BasePaymentSubmitView):
    payment_processor = "qpay"
    """ QPay payment handler.

    The payment form should POST here. This view will handle creating the charge at QPay, creating an order,
    and redirecting the user to the receipt page.
    """
    form_class = QpaySubmitForm

    def handle_payment(self, payment_intent_id, basket):  # pylint: disable=arguments-differ
        """
        Handle any payment processing and record payment sources and events.

        This method is responsible for handling payment and recording the
        payment sources (using the add_payment_source method) and payment
        events (using add_payment_event) so they can be
        linked to the order when it is saved later on.
        """
        properties = {
            'basket_id': basket.id,
            'processor_name': self.payment_processor.NAME,
            'stripe_enabled': False,
        }
        # If payment didn't go through, the handle_processor_response function will raise an error. We want to
        # send the event regardless of if the payment didn't go through.
        try:
            handled_processor_response = self.payment_processor.handle_processor_response(payment_intent_id, basket=basket)
        except Exception as ex:
            properties.update({'success': False, 'payment_error': type(ex).__name__, })
            raise
        else:
            # We only record successful payments in the database.
            self.record_payment(basket, handled_processor_response)
            properties.update({'total': handled_processor_response.total, 'success': True, })
        finally:
            track_segment_event(basket.site, basket.owner, 'Payment Processor Response', properties)

    def form_valid(self, form):
        form_data = form.cleaned_data
        basket = form_data['basket']
        payment_intent_id = form_data['payment_intent_id']
        order_number = basket.order_number

        basket_add_organization_attribute(basket, self.request.POST)
        basket_add_payment_intent_id_attribute(basket, self.request.POST)

        try:
            self.handle_payment(payment_intent_id, basket)
        except Exception:  # pylint: disable=broad-except
            logger.exception('An error occurred while processing the QPay payment for basket [%d].', basket.id)
            return JsonResponse({'error': "Төлбөр төлөгдөөгүй байна"}, status=400)

        try:
            self.create_order(self.request, basket)
        except Exception:  # pylint: disable=broad-except
            logger.exception('An error occurred while processing the QPay payment for basket [%d].', basket.id)
            return JsonResponse({}, status=400)

        receipt_url = get_receipt_page_url(
            self.request,
            site_configuration=self.request.site.siteconfiguration,
            order_number=order_number,
            disable_back_button=True
        )
        return JsonResponse({'url': receipt_url}, status=201)


class QpayAPICreateView(EdxOrderPlacementMixin, APIView):
    http_method_names = ['post']

    permission_classes = [IsAuthenticated]

    @property
    def payment_processor(self):
        return Qpay(self.request.site)

    def post(self, request):
        """
        CREATE PAYMENT QPAY
        """
        qpay_response = self.payment_processor.get_capture_context(request)

        return self.checkout_page_response(qpay_response)

    def checkout_page_response(self, qpay_response):
        """Tell the frontend to redirect to the receipt page."""
        return JsonResponse(qpay_response, status=201)


class QpayAPICheckView(EdxOrderPlacementMixin, APIView):
    http_method_names = ['get']

    # DRF APIView wrapper which allows clients to use JWT authentication when
    # making QPAY checkout submit requests.
    permission_classes = [IsAuthenticated]

    @property
    def payment_processor(self):
        return Qpay(self.request.site)

    def _get_basket(self, payment_intent_id):
        """
        Retrieve a basket using a payment intent ID.

        Arguments:
            payment_intent_id: payment_intent_id received from Stripe.

        Returns:
            It will return related basket or log exception and return None if
            duplicate payment_intent_id* received or any other exception occurred.
        """
        try:
            payment_intent_id_attribute, __ = BasketAttributeType.objects.get_or_create(
                name=PAYMENT_INTENT_ID_ATTRIBUTE
            )
            basket_attribute = BasketAttribute.objects.get(
                attribute_type=payment_intent_id_attribute,
                value_text=payment_intent_id,
            )
            basket = basket_attribute.basket
            basket.strategy = strategy.Default()

            Applicator().apply(basket, basket.owner, self.request)
            logger.info(
                'Applicator applied, basket id: [%s]. Processed by [%s].',
                basket.id, self.payment_processor.NAME)

            basket_add_organization_attribute(basket, self.request.GET)
        except MultipleObjectsReturned:
            logger.warning(u"Duplicate payment_intent_id [%s] received from QPAY.", payment_intent_id)
            return None
        except ObjectDoesNotExist:
            logger.warning(u"Could not find payment_intent_id [%s] among baskets.", payment_intent_id)
            return None
        except Exception:  # pylint: disable=broad-except
            logger.exception(u"Unexpected error during basket retrieval while executing QPAY payment.")
            return None
        return basket

    def get(self, request):
        """
        Handle an incoming payment submission from the payment MFE after capture-context.
        SDN Check and confirmation by QPAY on the payment intent is performed.
        """
        payment_intent_id = self.request.query_params.get('qpay_payment_id', None)

        basket = self._get_basket(payment_intent_id)

        if not basket:
            logger.info(
                'Received QPAY payment notification for non-existent basket with payment intent id [%s].',
                payment_intent_id,
            )
            return redirect(self.payment_processor.error_url)

        logger.info(
            '%s called for QPAY payment intent id [%s], basket [%d] with status [%s], and order number [%s].',
            self.__class__.__name__,
            payment_intent_id,
            basket.id,
            basket.status,
            basket.order_number,
        )

        try:
            with transaction.atomic():
                try:
                    self.handle_payment(payment_intent_id, basket)
                except:
                    return self.qpay_error_response()
        except:  # pylint: disable=bare-except
            logger.exception('Attempts to handle payment for basket [%d] failed.', basket.id)
            return self.error_page_response()

        billing_address = None

        try:
            order = self.create_order(request, basket, billing_address)
            self.handle_post_order(order)
        except Exception:  # pylint: disable=broad-except
            logger.exception(
                'Error processing order for transaction [%s], with order [%s] and basket [%d]. Processed by [%s].',
                payment_intent_id,
                basket.order_number,
                basket.id,
                self.payment_processor.NAME,
            )
            return self.error_page_response()

        return self.receipt_page_response(basket)

    def error_page_response(self):
        """Tell the frontend to redirect to a generic error page."""
        return JsonResponse({}, status=400)

    def sku_mismatch_error_response(self):
        """Tell the frontend the SKU in the request does not match the basket."""
        return JsonResponse({
            'sku_error': True,
        }, status=400)

    def sdn_error_page_response(self, hit_count):
        """Tell the frontend to redirect to the SDN error page."""
        return JsonResponse({
            'sdn_check_failure': {'hit_count': hit_count}
        }, status=400)

    def receipt_page_response(self, basket):
        """Tell the frontend to redirect to the receipt page."""
        receipt_page_url = get_receipt_page_url(
            self.request,
            order_number=basket.order_number,
            site_configuration=basket.site.siteconfiguration,
            disable_back_button=True
        )
        return JsonResponse({
            'receipt_page_url': receipt_page_url,
        }, status=201)

    def qpay_error_response(self):
        """Tell the frontend that a QPAY error has occurred."""
        return JsonResponse({
            'error_code': 400,
            'user_message': "Төлбөр төлөгдөөгүй байна",
        }, status=400)
