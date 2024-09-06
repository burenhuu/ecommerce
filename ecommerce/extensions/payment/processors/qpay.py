""" QPAY payment processing. """

import json
import logging
import requests
from oscar.apps.payment.exceptions import GatewayError
from oscar.core.loading import get_model

from ecommerce.core.url_utils import get_ecommerce_url
from ecommerce.extensions.basket.constants import PAYMENT_INTENT_ID_ATTRIBUTE
from ecommerce.extensions.basket.models import Basket
from ecommerce.extensions.basket.utils import (
    basket_add_payment_intent_id_attribute,
    get_billing_address_from_payment_intent_data
)
from ecommerce.extensions.payment.processors import (
    ApplePayMixin,
    BaseClientSidePaymentProcessor,
    HandledProcessorResponse
)
from rest_framework.exceptions import APIException


class OrderNotPaidException(APIException):
    status_code = 400
    default_detail = "NOT PAID"
    default_code = 'order_not_paid'


logger = logging.getLogger(__name__)

BasketAttribute = get_model('basket', 'BasketAttribute')
BasketAttributeType = get_model('basket', 'BasketAttributeType')
BillingAddress = get_model('order', 'BillingAddress')
Country = get_model('address', 'Country')
PaymentEvent = get_model('order', 'PaymentEvent')
PaymentEventType = get_model('order', 'PaymentEventType')
PaymentProcessorResponse = get_model('payment', 'PaymentProcessorResponse')
Source = get_model('payment', 'Source')
SourceType = get_model('payment', 'SourceType')


class Qpay(BaseClientSidePaymentProcessor):
    NAME = 'qpay'
    TITLE = 'Q-Pay'
    DEFAULT_PROFILE_NAME = 'default'
    base_url = ''
    invoice_code = ""
    client_id = ""
    client_secret = ""

    def __init__(self, site):
        self.base_url = get_ecommerce_url('')
        super(Qpay, self).__init__(site)

    def create(self, basket):
        token = self.token()
        url = self.base_url + "invoice"
        order_id = self._get_order_number(basket)
        payload = json.dumps({
            "invoice_code": self.invoice_code,
            "sender_invoice_no": order_id,
            "invoice_receiver_code": "terminal",
            "invoice_description": "Course",
            "amount": self._get_basket_amount(basket),
            "callback_url": self.base_url + f'payment/qpay/check/{order_id}/'
        })
        headers = {
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + token,
        }

        response = requests.request("POST", url, headers=headers, data=payload)
        return response.json()

    def token(self):
        url = self.base_url + "auth/token"

        payload = ""
        auth = (self.client_id, self.client_secret)

        response = requests.request("POST", url, auth=auth, data=payload)

        res = response.json()
        return res['access_token']

    def refund(self, order_id):
        token = self.token()
        url = self.base_url + "payment/refund/" + str(order_id)

        headers = {
            'Authorization': 'Bearer ' + token}
        response = requests.request("DELETE", url, headers=headers)

        res = response.json()
        return res

    def check(self, order_id):
        token = self.token()
        url = self.base_url + "payment/check"

        payload = {
            "object_type": "INVOICE",
            "object_id": order_id,
            "offset": json.dumps({
                "page_number": 1,
                "page_limit": 100
            })
        }
        headers = {
            'Authorization': 'Bearer ' + token}
        response = requests.request("POST", url, headers=headers, data=payload)
        res = response.json()
        if res.get('error') != "PAYMENT_NOTFOUND":
            for i in res['rows']:
                if i['payment_status'] == "PAID":
                    return i
        return {'payment_status': "NOT-PAID"}

    @property
    def cancel_url(self):
        return get_ecommerce_url(self.configuration['cancel_checkout_path'])

    @property
    def error_url(self):
        return get_ecommerce_url(self.configuration['error_path'])

    def _get_basket_amount(self, basket):
        """Convert to qpay amount, which is in cents."""
        return str((basket.total_incl_tax * 100).to_integral_value())

    def _get_order_number(self, basket):
        return str(basket.order_number)

    def _build_payment_intent_parameters(self, basket):
        order_number = basket.order_number
        amount = self._get_basket_amount(basket)
        currency = basket.currency
        return {
            'amount': amount,
            'currency': currency,
            'description': order_number,
            'metadata': {'order_number': order_number},
        }

    def generate_basket_pi_idempotency_key(self, basket):
        """
        Generate an idempotency key for creating a PaymentIntent for a Basket.
        Using a version number in they key to aid in future development.
        """
        return f'basket_pi_create_v1_{basket.order_number}'

    def get_capture_context(self, request):
        # TODO: consider whether the basket should be passed in from MFE, not retrieved from Oscar
        basket = Basket.get_basket(request.user, request.site)
        if not basket.lines.exists():
            logger.info(
                'QPay capture-context called with empty basket [%d] and order number [%s].',
                basket.id,
                basket.order_number,
            )
            # Create a default qpay_response object with the necessary fields to combat 400 errors
            qpay_response = {
                'invoice_id': '',
                'qr_code': '',
                'qr_link': ''
            }
        else:
            logger.info("*** GETTING QPAY RESPONSE ***")
            qpay_response = self.create(basket)
            logger.info("*** QPAY RESPONSE %s ***", qpay_response)
            transaction_id = qpay_response['invoice_id']
            basket_add_payment_intent_id_attribute(basket, transaction_id)

        new_capture_context = {
            'invoice_id': qpay_response['invoice_id'],
            'qpay_link': qpay_response['qPay_shortUrl'],
            'qpay_qr': qpay_response['qr_image'],
            'order_id': basket.order_number,
        }
        return new_capture_context

    def get_transaction_parameters(self, basket, request=None, use_client_side_checkout=True, **kwargs):
        return {'payment_page_url': self.client_side_payment_url}

    def handle_processor_response(self, payment_intent_id, basket=None):
        # pretty sure we should simply return/error if basket is None, as not
        # sure what it would mean if there
        # NOTE: In the future we may want to get/create a Customer. See https://stripe.com/docs/api#customers.
        confirm_api_response = self.check(payment_intent_id)
        if confirm_api_response['payment_status'] == "NOT-PAID":
            self.record_processor_response(confirm_api_response, transaction_id=payment_intent_id, basket=basket)
            logger.exception('QPAY NOT PAID for basket [%d]: %s}', basket.id, payment_intent_id)
            raise OrderNotPaidException()

        # proceed only if payment went through
        # pylint: disable=E1136
        assert confirm_api_response['status'] == "succeeded"
        self.record_processor_response(confirm_api_response, transaction_id=payment_intent_id, basket=basket)

        logger.info(
            'Successfully confirmed QPay payment intent [%s] for basket [%d] and order number [%s].',
            payment_intent_id,
            basket.id,
            basket.order_number,
        )

        total = basket.total_incl_tax
        currency = basket.currency

        return HandledProcessorResponse(
            transaction_id=payment_intent_id,
            total=total,
            currency=currency,
            card_number="Qpay",
            card_type=payment_intent_id
        )

    def issue_credit(self, order_number, basket, reference_number, amount, currency):
        try:
            # QPAY Refund
            refund = self.refund(reference_number)
        except:
            self.record_processor_response({}, transaction_id=reference_number, basket=basket)
            msg = 'An error occurred while attempting to Refund (via QPAY) for order [{}].'.format(
                order_number)
            logger.exception(msg)
            raise

        transaction_id = reference_number
        self.record_processor_response(refund, transaction_id=transaction_id, basket=basket)

        return transaction_id
