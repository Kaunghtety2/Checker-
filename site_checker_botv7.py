"""
╔══════════════════════════════════════════════════════════╗
║   SITE CHECKER BOT v9 — FIXED EDITION                    ║
║   All functions fully working — no placeholders           ║
╠══════════════════════════════════════════════════════════╣
║  Bugs fixed vs previous:                                  ║
║  • Browser pool semaphore released before task done       ║
║  • httpx proxies kwarg wrong format                       ║
║  • DNS ThreadPoolExecutor timeout wrong usage             ║
║  • Wayback not parallelized properly                      ║
║  • Redirects never populated                              ║
║  • Webhook false positives (body not verified)            ║
╠══════════════════════════════════════════════════════════╣
║  SETUP:                                                   ║
║  pip install python-telegram-bot httpx aiosqlite          ║
║  pip install playwright beautifulsoup4 lxml dnspython     ║
║  pip install playwright-stealth curl-cffi                 ║
║  playwright install chromium                              ║
║                                                           ║
║  Set BOT_TOKEN, ADMIN_IDS below → run                    ║
╚══════════════════════════════════════════════════════════╝
"""

import asyncio
import csv
import io
import json
import logging
import re
import socket
import ssl
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import urljoin, urlparse
import random

import aiosqlite
import httpx
import requests
from bs4 import BeautifulSoup
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat,
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters,
)
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    from playwright.async_api import async_playwright, Browser
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

try:
    from playwright_stealth import stealth_async
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False

try:
    from curl_cffi import requests as cffi_req
    HAS_CURL_CFFI = True
    CFFI_PROFILES = ["chrome110","chrome116","chrome120","chrome124",
                     "firefox110","firefox117","safari17_0"]
except ImportError:
    HAS_CURL_CFFI = False
    CFFI_PROFILES = []

try:
    import dns.resolver
    HAS_DNS = True
except ImportError:
    HAS_DNS = False

# ════════════════════════════════════════════════════════
#  CONFIG
# ════════════════════════════════════════════════════════
BOT_TOKEN        = "8705263598:AAEHCIzhDjFSOUmrYkDho4N2QQFyN95eMqM"
BOT_AUTHOR       = "SiteChkBot"
ADMIN_IDS        = [1964475260]
DB_PATH          = "sitechecker.db"
CACHE_TTL        = 6 * 3600
BROWSER_POOL_SZ  = 3
CRAWLER_DEPTH    = 2
CRAWLER_MAX      = 8
MAX_URLS_MSG     = 20
MAX_URLS_FILE    = 100
RATE_WIN         = 60
RATE_MAX_DEFAULT = 8
REQ_TIMEOUT      = 12
JS_TIMEOUT       = 6
PW_TIMEOUT       = 20_000
WB_TIMEOUT       = 10
GW_THRESHOLD     = 4
PLT_MIN          = 3
MAX_JS           = 40
PROXY_LIST: list[str] = []

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

_cancel_flags: dict[int, bool] = {}
_active_scans: dict[int, bool] = {}
_user_rate:    dict[int, list] = defaultdict(list)
_monitors:     dict[str, dict] = {}
_last_results: dict[int, list] = defaultdict(list)

def is_scanning(uid):      return _active_scans.get(uid, False)
def set_scanning(uid, v):
    _active_scans[uid] = v
    if not v: _cancel_flags[uid] = False
def request_cancel(uid):   _cancel_flags[uid] = True
def is_cancelled(uid):     return _cancel_flags.get(uid, False)

UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
]

FRAMES = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]

# ════════════════════════════════════════════════════════
#  BROWSER POOL  (Fixed: semaphore held during full scan)
# ════════════════════════════════════════════════════════

class BrowserPool:
    def __init__(self, size: int = BROWSER_POOL_SZ):
        self._size      = size
        self._browsers: list[Browser] = []
        self._semaphore: asyncio.Semaphore | None = None   # created in start()
        self._pw        = None

    async def start(self):
        if not HAS_PLAYWRIGHT: return
        # Semaphore must be created inside event loop
        self._semaphore = asyncio.Semaphore(self._size)
        try:
            self._pw = await async_playwright().__aenter__()
            for _ in range(self._size):
                b = await self._pw.chromium.launch(
    headless=True,
    executable_path="/usr/bin/chromium",
    args=["--no-sandbox","--disable-setuid-sandbox",
                          "--disable-blink-features=AutomationControlled",
                          "--disable-dev-shm-usage","--disable-web-security"],
                )
                self._browsers.append(b)
            log.info("Browser pool started: %d instances", self._size)
        except Exception as e:
            log.warning("Browser pool failed: %s", e)

    async def stop(self):
        for b in self._browsers:
            try: await b.close()
            except Exception: pass
        if self._pw:
            try: await self._pw.__aexit__(None, None, None)
            except Exception: pass

    def available(self) -> bool:
        return bool(self._browsers and self._semaphore)

    @asynccontextmanager
    async def acquire(self):
        """
        Hold semaphore for the FULL duration of the context block.
        Caller does all work inside `async with pool.acquire() as browser`.
        """
        if not self._semaphore or not self._browsers:
            yield None
            return
        async with self._semaphore:
            yield random.choice(self._browsers)

_pool: BrowserPool | None = None

# ════════════════════════════════════════════════════════
#  GATEWAY SIGNATURES
# ════════════════════════════════════════════════════════

GW: dict[str, list[tuple[str, int]]] = {
    "Stripe": [
        ("js.stripe.com",8),("stripe.com/v3",8),
        ("Stripe(\"pk_live_",10),("Stripe('pk_live_",10),
        ("Stripe(\"pk_test_",8),("Stripe('pk_test_",8),
        ("pk_live_",6),("pk_test_",4),
        ("stripe.confirmCardPayment",6),("stripe.createPaymentMethod",6),
        ("stripe.confirmPayment",6),("stripe.handleCardAction",6),
        ("stripe.redirectToCheckout",6),("stripe.createToken",6),
        ("stripe.elements(",6),("stripeToken",4),
        ("stripe_publishable_key",6),("STRIPE_PUBLISHABLE_KEY",6),
        ("stripe.com/checkout",6),("stripe.js",4),("data-stripe",4),
    ],
    "PayPal": [
        ("paypal.com/sdk/js",8),("paypal.Buttons(",8),
        ("paypal.com/v2/checkout",8),("paypalobjects.com",6),
        ("PayPalScriptProvider",6),("PAYPAL_CLIENT_ID",6),
        ("paypal_client_id",6),("paypal_express",4),("paypalCheckout",4),
    ],
    "Braintree": [
        ("braintreepayments.com",8),("braintreegateway.com",8),
        ("braintree-web",6),("braintree.client.create",8),
        ("hostedFields.create",6),("braintree.dropin.create",8),
        ("braintree.js",6),("braintree_token",6),("clientToken",4),
    ],
    "Authorize.Net": [
        ("authorize.net",6),("Accept.js",8),("AcceptUI",8),
        ("AuthorizeNetPopup",8),("anet_params",6),
    ],
    "Adyen": [
        ("checkoutshopper-live.adyen.com",8),("checkoutshopper-test.adyen.com",6),
        ("AdyenCheckout(",8),("adyen.encrypt",6),("adyenConfiguration",6),
    ],
    "Square": [
        ("payments.squareup.com",8),("squareup.com/v2/square.js",8),
        ("Square.payments(",8),("sq-card-number",6),
        ("sq-payment-form",6),("square_application_id",6),
    ],
    "Checkout.com": [
        ("cdn.checkout.com",8),("Frames.init(",8),
        ("cko-card-number",6),("cko_public_key",6),
    ],
    "Worldpay":      [("cdn.worldpay.com",8),("Worldpay(",8),("worldpay.js",6)],
    "Cybersource":   [("cybersource.com",8),("flex-microform",8),("microform.createField",8)],
    "Mollie":        [("app.mollie.com",8),("mollie.createToken",8),("mollieCheckout",6)],
    "Paddle":        [("cdn.paddle.com",8),("Paddle.Setup(",8),("Paddle.Checkout",6)],
    "2Checkout":     [("2checkout.com",8),("TCO.loadCart",8)],
    "BlueSnap":      [("bluesnap.com",8),("hostedPaymentFieldsCreate",8)],
    "NMI":           [("secure.networkmerchants.com",8),("CollectJS",8)],
    "Recurly":       [("js.recurly.com",8),("recurly.configure(",8),("recurly.token",6)],
    "Chargebee":     [("js.chargebee.com",8),("Chargebee.init(",8),("cbInstance",6)],
    "Zuora":         [("static.zuora.com",8),("Z.renderWithErrorHandler",8)],
    "Paysafe":       [("hosted.paysafe.com",8),("paysafe.fields(",8)],
    "Opayo":         [("pi-live.sagepay.com",8),("sagepay.js",6)],
    "Nuvei":         [("nuvei.com",8),("nuvei.js",6)],
    "Heartland":     [("heartlandpaymentsystems.com",8),("Heartland.SecureSubmit",8)],
    "Elavon":        [("elavon.com",8),("convergepay.com",8)],
    "First Data":    [("payeezy.com",8),("fiserv.com",6)],
    "WePay":         [("wepay.com",8),("WePay",6)],
    "Skrill":        [("pay.skrill.com",8),("Skrill",6)],
    "Neteller":      [("neteller.com",8),("NETELLER",6)],
    "Klarna":    [("klarna.com/eu/payments",8),("KlarnaCheckout(",8),("Klarna.start(",8),("x.klarnacdn.net",8)],
    "Afterpay":  [("js.afterpay.com",8),("AfterPay.initialize",8),("afterpay.com",4)],
    "Affirm":    [("cdn1.affirm.com",8),("affirm.ui.ready",8),("_affirm_config",8)],
    "Sezzle":    [("widget.sezzle.com",8),("sezzleWidget",8)],
    "Zip":       [("quadpay.com",8),("zip.co/v2",8),("Zip.initialize",8)],
    "Splitit":   [("splitit.com",8),("Splitit.ui",8)],
    "Laybuy":    [("laybuy.com",8),("laybuy.checkout",8)],
    "Scalapay":  [("scalapay.com",8),("ScalapayWidget",8)],
    "Google Pay":  [("pay.google.com/gp/p/js",8),("google-pay-button",6),("GooglePay(",6)],
    "Apple Pay":   [("ApplePaySession",8),("apple-pay-button",6),("apple_pay_merchant",6)],
    "Shopify Pay": [("shop.app/pay",8),("ShopPay",8),("shopify_payments",6)],
    "Amazon Pay":  [("payments.amazon.com",8),("AmazonPay",8),("amazon-pay",6)],
    "WeChat Pay":  [("pay.weixin.qq.com",8),("wechatpay",6),("wxpay",4)],
    "Alipay":      [("alipay.com",8),("alipay.trade",8),("alipaySdk",6)],
    "Samsung Pay": [("samsungpay.com",8),("SamsungPay",8)],
    "Razorpay":  [("checkout.razorpay.com",8),("Razorpay(",8),("rzp_live_",8)],
    "Paytm":     [("securegw.paytm.in",8),("PaytmChecksum",8),("paytmCheckout",6)],
    "Cashfree":  [("sdk.cashfree.com",8),("CashFreeCheckout",8)],
    "Instamojo": [("instamojo.com",8),("Insta.pay",8)],
    "PayU":      [("checkout.payumoney.com",8),("PayU.getEasyPay",8),("payu.in",6)],
    "CCAvenue":  [("ccavenue.com",8),("ccavReqHandler",8)],
    "Juspay":    [("juspay.in",8),("Juspay",6)],
    "Xendit":    [("xendit.co",8),("Xendit.card",8),("xendit_public_key",6)],
    "GoPay":     [("gopay.com",8),("GoPay",6)],
    "GrabPay":   [("grab.com/sg/pay",8),("GrabPay",8)],
    "Omise":     [("cdn.omise.co",8),("Omise.createToken",8)],
    "2C2P":      [("2c2p.com",8),("2C2P",6)],
    "Mercado Pago": [("sdk.mercadopago.com",8),("MercadoPago(",8),("mp_public_key",6)],
    "PagSeguro":    [("pagseguro.com.br",8),("PagSeguroDirectPayment",8)],
    "dLocal":       [("dlocalgo.com",8),("dLocal(",8)],
    "OpenPay":      [("openpay.mx",8),("OpenPay.setId",8)],
    "Kushki":       [("kushki.com",8),("Kushki(",8)],
    "Tap Payments": [("tap.company",8),("Tap(",8),("goSell",6)],
    "HyperPay":     [("hyperpay.com",8),("wpwlOptions",8)],
    "Moyasar":      [("moyasar.com",8),("Moyasar.init",8)],
    "Telr":         [("secure.telr.com",8),("telr",4)],
    "PayTabs":      [("paytabs.com",8),("PayTabs",6)],
    "Geidea":       [("geidea.net",8),("Geidea",6)],
    "iDEAL":        [("ideal.nl",8),("iDEAL",6)],
    "Bancontact":   [("bancontact.com",8),("bancontact",4)],
    "Sofort":       [("sofort.com",8),("sofortbanking",6)],
    "Giropay":      [("giropay.de",8),("giropay",6)],
    "Iyzico":       [("iyzipay.com",8),("Iyzipay",8)],
    "PayTR":        [("paytr.com",8),("PayTR",6)],
}

CSP_GW_DOMAINS: dict[str, str] = {
    "js.stripe.com":"Stripe","stripe.com":"Stripe",
    "paypal.com":"PayPal","paypalobjects.com":"PayPal",
    "braintreepayments.com":"Braintree","braintreegateway.com":"Braintree",
    "authorize.net":"Authorize.Net","checkoutshopper-live.adyen.com":"Adyen",
    "klarna.com":"Klarna","x.klarnacdn.net":"Klarna",
    "js.afterpay.com":"Afterpay","checkout.razorpay.com":"Razorpay",
    "cdn.worldpay.com":"Worldpay","cdn.checkout.com":"Checkout.com",
    "app.mollie.com":"Mollie","cdn.paddle.com":"Paddle",
    "payments.squareup.com":"Square","pay.google.com":"Google Pay",
    "payments.amazon.com":"Amazon Pay","widget.sezzle.com":"Sezzle",
    "js.recurly.com":"Recurly","js.chargebee.com":"Chargebee",
    "js.paystack.co":"Paystack","checkout.flutterwave.com":"Flutterwave",
    "securegw.paytm.in":"Paytm","sdk.cashfree.com":"Cashfree",
    "bluesnap.com":"BlueSnap","secure.networkmerchants.com":"NMI",
    "shop.app":"Shopify Pay","alipay.com":"Alipay",
    "cdn1.affirm.com":"Affirm","xendit.co":"Xendit",
    "sdk.mercadopago.com":"Mercado Pago","iyzipay.com":"Iyzico",
    "dlocalgo.com":"dLocal","hyperpay.com":"HyperPay",
    "moyasar.com":"Moyasar","tap.company":"Tap Payments",
    "instamojo.com":"Instamojo","cdn.omise.co":"Omise",
    "grab.com":"GrabPay",
}

API_KEY_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\bpk_live_[A-Za-z0-9]{20,}\b'),                    "Stripe"),
    (re.compile(r'\bpk_test_[A-Za-z0-9]{20,}\b'),                    "Stripe"),
    (re.compile(r'paypal[_\-]?client[_\-]?id["\s:=\']+[A-Za-z0-9_\-]{10,}'), "PayPal"),
    (re.compile(r'\bsq0[a-z]{3}-[A-Za-z0-9_\-]{20,}\b'),            "Square"),
    (re.compile(r'\brzp_(live|test)_[A-Za-z0-9]{14,}\b'),            "Razorpay"),
    (re.compile(r'recurly[_\-]?public[_\-]?key["\s:=\']+[A-Za-z0-9_\-]{8,}'), "Recurly"),
    (re.compile(r'\bpk_(test|live)_[A-Za-z0-9]{30,}\b'),             "Paystack"),
    (re.compile(r'braintree[_\-]?token["\s:=\']+[A-Za-z0-9_\-]{10,}'), "Braintree"),
    (re.compile(r'_affirm_config\s*='),                                "Affirm"),
    (re.compile(r'mp_public_key["\s:=\']+[A-Za-z0-9_\-]{10,}'),      "Mercado Pago"),
    (re.compile(r'xendit[_\-]?public[_\-]?key["\s:=\']+[A-Za-z0-9_\-]{8,}'), "Xendit"),
    (re.compile(r'tap[_\-]?public[_\-]?key["\s:=\']+[A-Za-z0-9_\-]{8,}'), "Tap Payments"),
]

PLT: dict[str, list[tuple[str, int]]] = {
    "Shopify":     [("cdn.shopify.com",5),("myshopify.com",5),("window.Shopify",5),("Shopify.theme",4)],
    "WooCommerce": [("woocommerce/assets",5),("wc_add_to_cart_params",5),("wc_cart_hash_key",4),("wc-ajax",4)],
    "WordPress":   [("wp-emoji-release.min.js",5),('content="WordPress',5),("/wp-includes/js/",4),("/wp-content/themes/",4)],
    "BigCommerce": [("cdn11.bigcommerce.com",6),("BCData",5),("stencil-utils",5)],
    "Magento":     [("MAGE_URLS",6),("Magento_Ui/js",5),("Mage.Cookies",4)],
    "PrestaShop":  [("prestashop.com",5),("PrestaShop",4)],
    "OpenCart":    [("route=product/product",5),("catalog/view/javascript/jquery",5)],
    "Squarespace": [("Y.Squarespace",6),("squarespace.com",5)],
    "Wix":         [("wixstatic.com",6),("wixCode",5),("wix-warmup-data",4)],
    "Webflow":     [("js.webflow.com",6),("w-webflow-badge",5)],
    "Joomla":      [('content="Joomla',6),("/components/com_content",6)],
    "Drupal":      [("drupalSettings",6),("Drupal.settings",6)],
    "Next.js":     [("__NEXT_DATA__",6),("_next/static/chunks",5),("next-head-count",4)],
    "Nuxt.js":     [("__NUXT__",6),("/_nuxt/",5)],
    "Laravel":     [("laravel_session",5),("XSRF-TOKEN",4),("/vendor/laravel",5)],
}

PLT_GROUPS = [
    ["Shopify","WooCommerce","WordPress","BigCommerce","Magento",
     "PrestaShop","OpenCart","Squarespace","Wix","Webflow","Joomla","Drupal","Laravel"],
    ["Next.js","Nuxt.js"],
]

TECH_STACK: dict[str, dict[str, list[str]]] = {
    "CDN": {
        "Cloudflare CDN": ["cdn-cgi/","__cfduid","cloudflare"],
        "Fastly":         ["x-fastly","fastly.net"],
        "AWS CloudFront": ["cloudfront.net","x-amz-cf"],
        "Bunny CDN":      ["b-cdn.net","bunnycdn"],
        "Akamai":         ["akamaized.net","akamaihd.net"],
        "Vercel":         ["vercel.app","x-vercel"],
    },
    "Analytics": {
        "Google Analytics 4":  ["gtag/js?id=G-","googletagmanager.com/gtag"],
        "Google Analytics UA": ["ga('send'","analytics.js","UA-"],
        "Hotjar":   ["hotjar.com","hjSiteSettings"],
        "Mixpanel": ["mixpanel.com","mixpanel.track"],
        "Segment":  ["segment.com/analytics","analytics.load"],
        "Amplitude":["amplitude.com","amplitude.getInstance"],
        "Heap":     ["heap.io","heap.track"],
        "FullStory":["fullstory.com","FS.identify"],
        "Clarity":  ["clarity.ms","microsoft.clarity"],
        "Plausible":["plausible.io"],
    },
    "A/B Testing": {
        "Optimizely":      ["optimizely.com","window.optimizely"],
        "VWO":             ["vwo.com","_vwo_code"],
        "Google Optimize": ["google-optimize","optimize.google"],
        "AB Tasty":        ["abtasty.com"],
    },
    "Chat / Support": {
        "Intercom":     ["intercom.io","window.Intercom"],
        "Zendesk":      ["zendesk.com","zd-messenger"],
        "Drift":        ["drift.com","window.drift"],
        "Crisp":        ["crisp.chat","window.$crisp"],
        "Freshchat":    ["freshchat.com","fcWidget"],
        "Tawk.to":      ["tawk.to","window.Tawk_API"],
        "LiveChat":     ["livechatinc.com","window.LiveChatWidget"],
        "Tidio":        ["tidio.co","tidioChatApi"],
    },
    "Email / Marketing": {
        "Mailchimp":      ["mailchimp.com","chimpstatic"],
        "Klaviyo":        ["klaviyo.com","_learnq"],
        "Hubspot":        ["hubspot.com","hs-analytics"],
        "Omnisend":       ["omnisend.com"],
        "ActiveCampaign": ["activehosted.com","activecampaign"],
        "Brevo":          ["sendinblue.com","sibConversations"],
    },
    "Fraud / Security": {
        "Sift":     ["siftscience.com","window._sift"],
        "Kount":    ["kount.net","ka.js"],
        "Signifyd": ["signifyd.com"],
        "Riskified":["riskified.com","beacon.js"],
    },
}

CAPTCHA = [
    "g-recaptcha","grecaptcha","recaptcha/api.js","hcaptcha.com/1/api.js",
    "challenges.cloudflare.com/turnstile","data-sitekey","arkoselabs","funcaptcha",
]

WAF: dict[str, list[str]] = {
    "Cloudflare": ["cf-ray","__cf_bm","cdn-cgi/","server: cloudflare"],
    "Akamai":     ["akamaized.net","ak_bmsc","akamaihd.net"],
    "Imperva":    ["incapsula","visid_incap","incap_ses"],
    "Sucuri":     ["sucuri.net","sucuri_cloudproxy"],
    "AWS WAF":    ["x-amzn-requestid","x-amz-cf-id"],
    "Fastly":     ["x-fastly-request-id","fastly"],
    "Vercel":     ["x-vercel-id","vercel.app"],
    "F5 BIG-IP":  ["TS0","BigIP"],
}

SEC_HEADERS = [
    "strict-transport-security","content-security-policy","x-frame-options",
    "x-content-type-options","x-xss-protection","permissions-policy","referrer-policy",
]

# Webhook body hints per gateway (prevents false positives)
WEBHOOK_BODY_HINTS: dict[str, list[str]] = {
    "Stripe":    ["stripe","webhook","signature","stripe-signature","payment_intent"],
    "PayPal":    ["paypal","payer_id","ipn_track_id","txn_type"],
    "Braintree": ["braintree","transaction","merchant_account"],
    "Adyen":     ["adyen","hmacSignature","eventCode"],
    "Klarna":    ["klarna","order_id","event_type"],
    "Square":    ["square","payment","webhook"],
    "Mollie":    ["mollie","payment","webhook"],
    "Razorpay":  ["razorpay","order_id","payment_id"],
    "Paddle":    ["paddle","p_vendor","alert_name"],
    "Checkout.com": ["cko","webhook","event_type"],
}

# 3D Secure signatures
THREEDS_CONFIRMED: list[str] = [
    "songbird.cardinalcommerce.com","Cardinal.setup(","cardinalcommerce",
    "stripe.confirmCardPayment","stripe.handleCardAction",
    "payment_intent","requires_action","use_stripe_sdk",
    "threeDS2","threeds2","adyen.threeDS","threeDS2Challenge",
    "braintree.threeDSecure","threeDSecureParameters","liabilityShifted",
    "3dsecure","3d_secure","3ds_method","ThreeDSMethodURL","threeDSMethodURL",
    "pa_req","PaReq","pareq","acs_url","ACSUrl","acsUrl",
    "authentication_url","authenticationUrl","term_url","TermUrl",
    "enrolled=Y","verifyEnrollment","payer_auth","payerauth",
    "liability_shift","liabilityShift",
    "verifiedbyvisa","mastercard securecode","securecode",
    "3ds2","EMV3DS","emv3ds","deviceChannel","browserAcceptHeader",
    "transStatus","authenticationType",
    "cko-3ds","risk.js","dfReferenceId","ThreeDSecure",
]

TWOD_INDICATORS: list[str] = [
    "no3ds","skip_3ds","bypass_3ds","3ds=false","threeds=false",
    "disable_3ds","non_3ds","direct_charge","auth_type=0",
]

JS_HOOK_SCRIPT = """
(function() {
    window.__PAYMENT_HOOKS = {};
    const NAMES = ['Stripe','PayPal','braintree','Square','AdyenCheckout',
        'Klarna','Razorpay','Paddle','Mollie','Afterpay','affirm',
        'FlutterwaveCheckout','PaystackPop','MercadoPago'];
    NAMES.forEach(name => {
        const orig = window[name];
        try {
            Object.defineProperty(window, name, {
                set: function(v) {
                    if (v) window.__PAYMENT_HOOKS[name] = true;
                    window['_orig_'+name] = v;
                },
                get: function() { return window['_orig_'+name] || orig; },
                configurable: true
            });
        } catch(e) {}
    });
})();
"""

# ════════════════════════════════════════════════════════
#  DATABASE
# ════════════════════════════════════════════════════════

async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                uid INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
                joined_at TEXT DEFAULT (datetime('now')), scan_count INTEGER DEFAULT 0,
                rate_limit INTEGER DEFAULT 8, is_banned INTEGER DEFAULT 0, is_vip INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS cache (
                domain TEXT PRIMARY KEY, result_json TEXT, cached_at REAL
            );
            CREATE TABLE IF NOT EXISTS scan_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, domain TEXT,
                gateways TEXT, platform TEXT, scanned_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS monitors (
                id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, domain TEXT,
                interval_h INTEGER DEFAULT 6, last_gateways TEXT, last_check TEXT,
                active INTEGER DEFAULT 1
            );
        """)
        await db.commit()

async def db_upsert_user(uid, username, first_name):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (uid,username,first_name) VALUES(?,?,?)
            ON CONFLICT(uid) DO UPDATE SET username=excluded.username,first_name=excluded.first_name
        """, (uid, username or "", first_name or ""))
        await db.commit()

async def db_get_user(uid) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT * FROM users WHERE uid=?", (uid,)) as cur:
            row = await cur.fetchone()
            if not row: return None
            return dict(zip([d[0] for d in cur.description], row))

async def db_inc_scan(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET scan_count=scan_count+1 WHERE uid=?", (uid,))
        await db.commit()

async def db_log_scan(uid, domain, gateways, platform):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO scan_log (uid,domain,gateways,platform) VALUES(?,?,?,?)",
            (uid, domain, ",".join(gateways), ",".join(platform))
        )
        await db.commit()

async def db_get_stats() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        tu = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
        ts = (await (await db.execute("SELECT SUM(scan_count) FROM users")).fetchone())[0] or 0
        bn = (await (await db.execute("SELECT COUNT(*) FROM users WHERE is_banned=1")).fetchone())[0]
        vp = (await (await db.execute("SELECT COUNT(*) FROM users WHERE is_vip=1")).fetchone())[0]
        cs = (await (await db.execute("SELECT COUNT(*) FROM cache")).fetchone())[0]
        mn = (await (await db.execute("SELECT COUNT(*) FROM monitors WHERE active=1")).fetchone())[0]
        return dict(total_users=tu,total_scans=ts,banned=bn,vip=vp,cache_size=cs,monitors=mn)

async def db_get_all_uids() -> list[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT uid FROM users WHERE is_banned=0") as cur:
            return [row[0] for row in await cur.fetchall()]

async def db_set_ban(uid, val):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_banned=? WHERE uid=?", (val,uid)); await db.commit()

async def db_set_vip(uid, val):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_vip=? WHERE uid=?", (val,uid)); await db.commit()

async def db_set_rate(uid, rate):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET rate_limit=? WHERE uid=?", (rate,uid)); await db.commit()

async def db_get_cache(domain) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT result_json,cached_at FROM cache WHERE domain=?", (domain,)) as cur:
            row = await cur.fetchone()
            if row and time.time()-row[1] < CACHE_TTL:
                return json.loads(row[0])
    return None

async def db_set_cache(domain, result):
    safe = {k:v for k,v in result.items() if k not in ("from_cache","scanned_by")}
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO cache (domain,result_json,cached_at) VALUES(?,?,?)",
            (domain, json.dumps(safe, default=str), time.time())
        )
        await db.commit()

async def db_clear_cache():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM cache"); await db.commit()

async def db_delete_cache(domain):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM cache WHERE domain=?", (domain,)); await db.commit()

async def db_add_monitor(uid, domain, interval_h):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO monitors (uid,domain,interval_h,active) VALUES(?,?,?,1)",
            (uid,domain,interval_h)
        )
        await db.commit()

async def db_remove_monitor(uid, domain):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE monitors SET active=0 WHERE uid=? AND domain=?", (uid,domain))
        await db.commit()

async def db_get_monitors() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT * FROM monitors WHERE active=1") as cur:
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols,r)) for r in rows]

async def db_update_monitor_check(domain, gateways_json):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE monitors SET last_gateways=?,last_check=datetime('now') WHERE domain=?",
            (gateways_json,domain)
        )
        await db.commit()

# ════════════════════════════════════════════════════════
#  RATE LIMITING
# ════════════════════════════════════════════════════════

async def rate_ok(uid: int) -> tuple[bool, int]:
    user  = await db_get_user(uid)
    limit = user["rate_limit"] if user else RATE_MAX_DEFAULT
    if user and user.get("is_vip"): return True, 0
    now = time.time()
    ts  = [t for t in _user_rate[uid] if t > now-RATE_WIN]
    _user_rate[uid] = ts
    if len(ts) >= limit:
        return False, int(RATE_WIN-(now-ts[0]))+1
    _user_rate[uid].append(now)
    return True, 0

# ════════════════════════════════════════════════════════
#  HTTP LAYER  (Fixed: httpx proxy format, redirect tracking)
# ════════════════════════════════════════════════════════

def norm(u: str) -> str:
    u = u.strip()
    return u if u.startswith(("http://","https://")) else "https://"+u

def hdrs(ua: str = "") -> dict:
    return {
        "User-Agent": ua or random.choice(UAS),
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "DNT": "1",
    }

def get_proxy() -> str | None:
    return random.choice(PROXY_LIST) if PROXY_LIST else None

async def async_get(url: str, ua: str = "") -> tuple[int, str, dict, list[str]]:
    """
    Returns (status, text, headers, redirect_chain).
    Fixed: httpx uses `proxy=` not `proxies=`
    """
    proxy = get_proxy()
    tries = [url, url.replace("https://","http://",1)]

    for attempt_url in tries:
        # 1. httpx async (fastest, tracks redirects)
        try:
            async with httpx.AsyncClient(
                verify=False, follow_redirects=True,
                timeout=REQ_TIMEOUT,
                headers=hdrs(ua or random.choice(UAS)),
                proxy=proxy,          # ← Fixed: httpx uses `proxy` not `proxies`
            ) as c:
                r = await c.get(attempt_url)
                if r.status_code < 500:
                    redirects = [str(h.url) for h in r.history]
                    return r.status_code, r.text, dict(r.headers), redirects
        except Exception: pass

        # 2. curl_cffi — Cloudflare JA3 bypass
        if HAS_CURL_CFFI:
            try:
                profile = random.choice(CFFI_PROFILES)
                r = cffi_req.get(
                    attempt_url,
                    headers=hdrs(random.choice(UAS)),
                    timeout=REQ_TIMEOUT,
                    impersonate=profile,
                    verify=False,
                    proxies={"https": proxy, "http": proxy} if proxy else None,
                )
                if r.status_code < 500:
                    return r.status_code, r.text, dict(r.headers), []
            except Exception: pass

        # 3. requests fallback
        try:
            r = requests.get(
                attempt_url,
                headers=hdrs(random.choice(UAS)),
                timeout=REQ_TIMEOUT, verify=False, allow_redirects=True,
                proxies={"https": proxy, "http": proxy} if proxy else None,
            )
            if r.status_code < 500:
                redirects = [rr.url for rr in r.history]
                return r.status_code, r.text, dict(r.headers), redirects
        except Exception: pass

    return 0, "", {}, []

async def async_fetch_js(js_url: str, referer: str) -> str:
    proxy = get_proxy()
    try:
        async with httpx.AsyncClient(
            verify=False, follow_redirects=True,
            timeout=JS_TIMEOUT, proxy=proxy,
        ) as c:
            r = await c.get(js_url, headers={**hdrs(), "Referer": referer, "Accept": "*/*"})
            if r.status_code == 200:
                ct = r.headers.get("content-type","").lower()
                if not any(x in ct for x in ("image/","font/","audio/","video/")):
                    return r.text[:500_000] if len(r.text) > 10 else ""
    except Exception: pass
    return ""

def get_ip(domain: str) -> str:
    try: return socket.gethostbyname(domain)
    except Exception: return "N/A"

def get_ssl_info(domain: str) -> dict:
    info = {"valid":False,"expiry":"N/A","issuer":"N/A","days_left":None}
    try:
        ctx  = ssl.create_default_context()
        conn = ctx.wrap_socket(socket.create_connection((domain,443),timeout=6), server_hostname=domain)
        cert = conn.getpeercert(); conn.close()
        exp_str = cert.get("notAfter","")
        if exp_str:
            exp_dt = datetime.strptime(exp_str, "%b %d %H:%M:%S %Y %Z")
            days   = (exp_dt - datetime.utcnow()).days
            info.update(expiry=exp_dt.strftime("%Y-%m-%d"), days_left=days, valid=days>0)
        info["issuer"] = dict(x[0] for x in cert.get("issuer",[])).get("organizationName","Unknown")
    except Exception: pass
    return info

# ════════════════════════════════════════════════════════
#  WAYBACK  (Fixed: parallel start, proper async)
# ════════════════════════════════════════════════════════

async def fetch_wayback(domain: str) -> str:
    """
    Tries checkout/cart/payment paths first, then root.
    Uses a single persistent httpx client for all requests.
    """
    proxy = get_proxy()
    async with httpx.AsyncClient(
        verify=False, timeout=WB_TIMEOUT,
        proxy=proxy, follow_redirects=True,
    ) as c:
        for path in ["/checkout", "/cart", "/payment", ""]:
            try:
                cdx = await c.get(
                    "http://web.archive.org/cdx/search/cdx",
                    params={
                        "url":    domain + path,
                        "output": "json",
                        "limit":  "1",
                        "fl":     "timestamp,original",
                        "filter": "statuscode:200",
                        "from":   "20230101",
                    },
                )
                cdx.raise_for_status()
                rows = cdx.json()
                if len(rows) < 2:
                    continue
                ts, orig = rows[1]
                snap = await c.get(
                    f"http://web.archive.org/web/{ts}id_/{orig}",
                    headers=hdrs(random.choice(UAS)),
                )
                if snap.status_code == 200 and len(snap.text) > 300:
                    log.info("Wayback ✓ %s%s (ts=%s)", domain, path, ts)
                    return snap.text
            except Exception as e:
                log.debug("wayback %s%s: %s", domain, path, e)
    return ""

# ════════════════════════════════════════════════════════
#  DNS PROBE  (Fixed: per-task timeout with futures)
# ════════════════════════════════════════════════════════

def probe_dns(domain: str) -> list[str]:
    """
    Fixed: use submit + wait(timeout) instead of map(timeout)
    to properly enforce per-call timeouts.
    """
    DNS_SUBS = [
        "pay","checkout","payment","payments","billing",
        "stripe","paypal","shop","secure","order","cart","merchant",
    ]
    CNAME_MAP = {
        "stripe.com":"Stripe","paypal.com":"PayPal",
        "braintreepayments.com":"Braintree","authorize.net":"Authorize.Net",
        "squareup.com":"Square","adyen.com":"Adyen",
        "checkout.com":"Checkout.com","klarna.com":"Klarna",
        "afterpay.com":"Afterpay","razorpay.com":"Razorpay",
        "worldpay.com":"Worldpay","mollie.com":"Mollie",
        "paddle.com":"Paddle","2checkout.com":"2Checkout",
        "xendit.co":"Xendit","mercadopago.com":"Mercado Pago",
        "alipay.com":"Alipay",
    }
    found: list[str] = []
    parts = domain.split(".")
    root  = ".".join(parts[-2:]) if len(parts) >= 2 else domain

    def probe_one(sub: str) -> list[str]:
        hits = []
        if not HAS_DNS:
            return hits
        fqdn = f"{sub}.{root}"
        try:
            resolver = dns.resolver.Resolver()
            resolver.lifetime = 3.0     # per-query timeout
            resolver.timeout  = 3.0
            ans = resolver.resolve(fqdn, "CNAME")
            for rd in ans:
                cname = str(rd.target).rstrip(".")
                for key, gw in CNAME_MAP.items():
                    if key in cname and gw not in hits:
                        hits.append(gw)
        except Exception:
            pass
        return hits

    # Use futures with wall-clock timeout
    with ThreadPoolExecutor(max_workers=8) as ex:
        future_map = {ex.submit(probe_one, sub): sub for sub in DNS_SUBS}
        done, _ = wait(list(future_map.keys()), timeout=8)
        for fut in done:
            try:
                for gw in fut.result():
                    if gw not in found:
                        found.append(gw)
            except Exception:
                pass
    return found

# ════════════════════════════════════════════════════════
#  WELL-KNOWN
# ════════════════════════════════════════════════════════

async def check_well_known(base: str) -> list[str]:
    WK = {
        "/.well-known/apple-developer-merchantid-domain-association": "Apple Pay",
        "/.well-known/pay-web":          "Google Pay",
        "/apple-pay-merchant-validation":"Apple Pay",
    }
    found: list[str] = []
    proxy = get_proxy()
    async with httpx.AsyncClient(verify=False, timeout=5, proxy=proxy) as c:
        tasks = []
        for path, gw in WK.items():
            tasks.append((path, gw, c.get(base+path, headers=hdrs(), follow_redirects=False)))
        for path, gw, coro in tasks:
            try:
                r = await coro
                if r.status_code in (200,301,302) and gw not in found:
                    found.append(gw)
            except Exception:
                pass
    return found

# ════════════════════════════════════════════════════════
#  WEBHOOK PROBE  (Fixed: body verification to reduce FP)
# ════════════════════════════════════════════════════════

WEBHOOK_PATHS: dict[str, str] = {
    "/webhook/stripe":"Stripe", "/stripe/webhook":"Stripe",
    "/wc-api/wc_stripe":"Stripe",
    "/webhook/paypal":"PayPal", "/paypal/webhook":"PayPal", "/ipn.php":"PayPal",
    "/braintree/webhook":"Braintree",
    "/adyen/webhook":"Adyen",
    "/klarna/webhook":"Klarna",
    "/square/webhook":"Square",  "/wc-api/wc_square":"Square",
    "/mollie/webhook":"Mollie",
    "/razorpay/webhook":"Razorpay",
    "/paddle/webhook":"Paddle",
    "/checkout/webhook":"Checkout.com",
}

async def probe_webhooks(base: str) -> list[str]:
    """
    Fixed: verify response body contains gateway-specific keywords
    to avoid false positives from generic 200/400 responses.
    """
    found: list[str] = []
    proxy = get_proxy()

    async def probe_one(path: str, gw: str) -> str | None:
        url = base + path
        hints = WEBHOOK_BODY_HINTS.get(gw, [gw.lower()])
        try:
            async with httpx.AsyncClient(
                verify=False, timeout=5, proxy=proxy,
                follow_redirects=False,
            ) as c:
                # HEAD first — fast existence check
                r_head = await c.head(url, headers=hdrs())
                if r_head.status_code == 405:
                    # 405 = endpoint exists, only POST allowed
                    return gw
                if r_head.status_code == 200:
                    return gw

                # POST with empty body
                r_post = await c.post(
                    url,
                    headers={**hdrs(), "Content-Type": "application/json"},
                    content=b"{}",
                )
                if r_post.status_code in (400, 422):
                    # Verify body mentions the gateway (not generic 400)
                    body_lo = r_post.text.lower()
                    if any(h in body_lo for h in hints):
                        return gw
                elif r_post.status_code == 200:
                    body_lo = r_post.text.lower()
                    if any(h in body_lo for h in hints):
                        return gw
        except Exception:
            pass
        return None

    results = await asyncio.gather(
        *[probe_one(path, gw) for path, gw in WEBHOOK_PATHS.items()],
        return_exceptions=True,
    )
    for r in results:
        if r and isinstance(r, str) and r not in found:
            found.append(r)
    return found

# ════════════════════════════════════════════════════════
#  SOURCE MAPS
# ════════════════════════════════════════════════════════

async def fetch_source_maps(js_urls: list[str], referer: str) -> str:
    corpus = ""
    proxy  = get_proxy()
    async with httpx.AsyncClient(verify=False, timeout=5, proxy=proxy) as c:
        for murl in [u+".map" for u in js_urls[:8]]:
            try:
                r = await c.get(murl, headers={**hdrs(), "Referer": referer})
                if r.status_code == 200 and len(r.text) > 100:
                    try:
                        sm = json.loads(r.text)
                        corpus += " ".join(str(s) for s in sm.get("sources",[])) + " "
                        corpus += " ".join(str(c2) for c2 in sm.get("sourcesContent",[])
                                           if isinstance(c2, str))[:60_000]
                    except Exception:
                        corpus += r.text[:30_000]
            except Exception:
                pass
    return corpus

# ════════════════════════════════════════════════════════
#  PAYMENT PAGE CRAWLER
# ════════════════════════════════════════════════════════

PAYMENT_KW = (
    "checkout","cart","payment","pay","order","billing","stripe","paypal",
    "purchase","buy","shop","proceed","basket","invoice","transaction",
)

async def discover_payment_pages(base: str, parsed, html: str) -> list[str]:
    found: list[str]   = []
    visited: set[str]  = {base}
    proxy = get_proxy()

    def extract_links(page_html: str, page_url: str) -> list[str]:
        soup  = BeautifulSoup(page_html, "html.parser")
        links = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            text = a.get_text().strip().lower()
            if not href or href.startswith(("mailto:","tel:","javascript:","#")): continue
            if href.startswith("//"): href = "https:"+href
            elif href.startswith("/"): href = f"{parsed.scheme}://{parsed.netloc}{href}"
            elif not href.startswith("http"): href = urljoin(page_url, href)
            if parsed.netloc not in href: continue
            if any(k in href.lower() or k in text for k in PAYMENT_KW):
                links.append(href)
        return links

    level1 = extract_links(html, base)
    found.extend(level1)

    if CRAWLER_DEPTH >= 2:
        async with httpx.AsyncClient(
            verify=False, follow_redirects=True,
            timeout=8, proxy=proxy,
        ) as c:
            for url in level1[:CRAWLER_MAX//2]:
                if url in visited: continue
                visited.add(url)
                try:
                    r = await c.get(url, headers=hdrs(random.choice(UAS)))
                    if r.status_code == 200 and len(r.text) > 200:
                        deeper = extract_links(r.text, url)
                        for link in deeper:
                            if link not in found:
                                found.append(link)
                except Exception:
                    pass

    return list(dict.fromkeys(found))[:CRAWLER_MAX]

# ════════════════════════════════════════════════════════
#  HTML EXTRACTION
# ════════════════════════════════════════════════════════

def extract_all(html: str, base_url: str, parsed) -> tuple[str, list[str]]:
    soup   = BeautifulSoup(html, "html.parser")
    inline = " ".join(t.get_text() for t in soup.find_all("script") if t.get_text())
    data   = " ".join(f"{k}={v}" for t in soup.find_all(True)
                      for k, v in t.attrs.items() if isinstance(v, str))
    forms  = []
    for form in soup.find_all("form"):
        action = form.get("action","").strip()
        if action:
            if action.startswith("//"): action = "https:"+action
            elif action.startswith("/"): action = f"{parsed.scheme}://{parsed.netloc}{action}"
            forms.append(f"form_action={action}")
        for inp in form.find_all("input"):
            n = inp.get("name","")
            if n: forms.append(f"{n}={inp.get('value','')}")
            for a, v in inp.attrs.items():
                if a.startswith("data-"): forms.append(f"{a}={v}")
    iframes  = [f"iframe={t.get('src','')}" for t in soup.find_all(["iframe","embed"]) if t.get("src")]
    preloads = [lk.get("href","") for lk in soup.find_all("link")
                if any(r in " ".join(lk.get("rel",[])).lower()
                       for r in ("preload","prefetch","dns-prefetch","preconnect"))
                and lk.get("href")]
    extra    = " ".join(forms+iframes+preloads)
    js_urls: list[str] = []
    for tag in soup.find_all("script", src=True):
        src = tag.get("src","").strip()
        if not src or src.startswith("data:"): continue
        if src.startswith("//"): src = "https:"+src
        elif src.startswith("/"): src = f"{parsed.scheme}://{parsed.netloc}{src}"
        elif not src.startswith("http"): src = urljoin(base_url, src)
        if src not in js_urls: js_urls.append(src)
    return f"{html}\n{inline}\n{data}\n{extra}", js_urls

# ════════════════════════════════════════════════════════
#  DETECTION HELPERS
# ════════════════════════════════════════════════════════

def score_corpus(text: str, sigs: dict) -> dict[str, int]:
    lo = text.lower()
    return {n: sum(w for s,w in entries if s.lower() in lo) for n,entries in sigs.items()}

def match_any(text: str, sigs: dict) -> list[str]:
    lo = text.lower()
    return [n for n,pats in sigs.items() if any(p.lower() in lo for p in pats)]

def best_platform(scores: dict[str, int]) -> list[str]:
    out = []
    for group in PLT_GROUPS:
        gs = {p: scores.get(p,0) for p in group}
        best, bsc = max(gs.items(), key=lambda x: x[1])
        if bsc < PLT_MIN: continue
        out.append(best)
        for n, sc in gs.items():
            if n != best and sc >= PLT_MIN and bsc-sc <= 1: out.append(n)
    done = {p for g in PLT_GROUPS for p in g}
    for n, sc in scores.items():
        if n not in done and sc >= PLT_MIN: out.append(n)
    return list(dict.fromkeys(out))

def scan_api_keys(corpus: str) -> list[str]:
    return list({gw for pat,gw in API_KEY_PATTERNS if pat.search(corpus)})

def parse_csp(rh: dict) -> list[str]:
    csp = rh.get("Content-Security-Policy") or rh.get("content-security-policy","")
    if not csp: return []
    found: list[str] = []
    for token in re.split(r'[\s;]+', csp.lower()):
        token = re.sub(r'^(https?://|\*\.)', '', token.strip())
        for domain, gw in CSP_GW_DOMAINS.items():
            if domain.lower() in token and gw not in found:
                found.append(gw)
    return found

def detect_tech_stack(corpus: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    lo = corpus.lower()
    for category, services in TECH_STACK.items():
        detected = [name for name,patterns in services.items()
                    if any(p.lower() in lo for p in patterns)]
        if detected: result[category] = detected
    return result

def detect_checkout_security(corpus: str) -> dict:
    lo      = corpus.lower()
    matched = []
    score   = 0
    for sig in THREEDS_CONFIRMED:
        if sig.lower() in lo:
            matched.append(sig)
            score += 3 if any(k in sig.lower() for k in (
                "cardinal","songbird","3ds2","emv3ds",
                "pa_req","acs_url","liabilityshift","payment_intent",
            )) else 1
    twod_hits = [s for s in TWOD_INDICATORS if s.lower() in lo]
    if score >= 2:   mode = "3D"
    elif score == 1 and not twod_hits: mode = "3D"
    elif twod_hits:  mode = "2D"
    else:            mode = "Unknown"
    return {"mode": mode, "evidence": matched[:5], "score": score}

# ════════════════════════════════════════════════════════
#  BROWSER SCAN  (Fixed: called inside pool.acquire() context)
# ════════════════════════════════════════════════════════

async def browser_scan(base: str, browser: Browser) -> dict:
    """
    Called with `browser` already acquired from pool.
    The caller holds the semaphore for the full duration.
    """
    corpus_parts: list[str] = []
    intercepted_gw: list[str] = []
    PAY_DOMAINS = tuple(CSP_GW_DOMAINS.keys())

    try:
        ctx = await browser.new_context(
            user_agent=UAS[0],
            viewport={"width":1280,"height":800},
            java_script_enabled=True,
            ignore_https_errors=True,
        )
        page = await ctx.new_page()
        if HAS_STEALTH:
            await stealth_async(page)
        await page.add_init_script(JS_HOOK_SCRIPT)

        async def on_request(req):
            url = req.url
            for domain, gw in CSP_GW_DOMAINS.items():
                if domain in url and gw not in intercepted_gw:
                    intercepted_gw.append(gw)

        page.on("request", on_request)

        for path in ["", "/checkout", "/cart", "/payment"]:
            try:
                await page.goto(base+path, wait_until="networkidle", timeout=PW_TIMEOUT)
                await asyncio.sleep(1.5)
                await page.evaluate("window.scrollTo(0,document.body.scrollHeight)")
                await asyncio.sleep(0.8)

                # Try clicking checkout-related buttons
                for sel in ["button[class*='checkout']","a[href*='/checkout']",
                             "button[class*='pay']","[data-testid*='checkout']"]:
                    try:
                        el = page.locator(sel).first
                        if await el.count() > 0:
                            await el.click(timeout=2000)
                            await asyncio.sleep(1.5)
                            break
                    except Exception: pass

                html = await page.content()
                corpus_parts.append(html)

                # JS hooks result
                hooks = await page.evaluate("() => window.__PAYMENT_HOOKS || {}")
                if hooks:
                    corpus_parts.append(json.dumps(hooks))
                    hook_map = {
                        "Stripe":"Stripe","PayPal":"PayPal","braintree":"Braintree",
                        "Square":"Square","AdyenCheckout":"Adyen","Klarna":"Klarna",
                        "Razorpay":"Razorpay","Paddle":"Paddle","affirm":"Affirm",
                        "FlutterwaveCheckout":"Flutterwave","PaystackPop":"Paystack",
                        "MercadoPago":"Mercado Pago",
                    }
                    for k in hooks:
                        for kw, gw in hook_map.items():
                            if kw.lower() in k.lower() and gw not in intercepted_gw:
                                intercepted_gw.append(gw)

                # Window globals
                win_obj = await page.evaluate("""() => {
                    const keys = ['Stripe','PayPal','braintree','Square','AdyenCheckout',
                        'Klarna','Razorpay','Paddle','Mollie','Afterpay','affirm',
                        'FlutterwaveCheckout','PaystackPop','MercadoPago',
                        'STRIPE_KEY','PAYPAL_CLIENT_ID'];
                    const out = {};
                    keys.forEach(k => {
                        if (window[k] !== undefined)
                            try { out[k] = String(window[k]).substring(0,400); } catch(e) {}
                    });
                    return out;
                }""")
                if win_obj:
                    corpus_parts.append(json.dumps(win_obj))

                # localStorage
                storage = await page.evaluate("""() => {
                    try {
                        return Object.entries(localStorage)
                            .filter(([k]) => ['stripe','paypal','payment',
                                'braintree','square','adyen'].some(p => k.toLowerCase().includes(p)))
                            .map(([k,v]) => k+'='+v).join(' ');
                    } catch(e) { return ''; }
                }""")
                if storage: corpus_parts.append(storage)

            except Exception as e:
                log.debug("PW path %s%s: %s", base, path, e)

        await ctx.close()

    except Exception as e:
        log.debug("browser_scan err %s: %s", base, e)

    corpus = "\n".join(corpus_parts)
    gws    = list(dict.fromkeys(intercepted_gw + scan_api_keys(corpus)))
    log.info("Browser ✓ %s — GW:%s", base, gws)
    return {
        "corpus":   corpus,
        "gateways": gws,
        "platform": best_platform(score_corpus(corpus, PLT)),
        "tech":     detect_tech_stack(corpus),
    }

# ════════════════════════════════════════════════════════
#  CORE SCANNER  (Fixed: browser pool usage, all parallel)
# ════════════════════════════════════════════════════════

async def scan(raw: str, uid: int, force_fresh: bool = False, scanned_by: str = "") -> dict:
    url    = norm(raw)
    parsed = urlparse(url)
    domain = parsed.netloc or url
    base   = f"{parsed.scheme}://{parsed.netloc}"

    # Cache
    if not force_fresh:
        cached = await db_get_cache(domain)
        if cached:
            cached["from_cache"] = True
            cached["scanned_by"] = scanned_by
            log.info("Cache hit: %s", domain)
            return cached

    res = dict(
        url=url, domain=domain, status="N/A", ip="N/A",
        waf=[], captcha=False, gateways=[], platform=[],
        tech_stack={}, wb=False, error=None, response_ms=None,
        ssl={"valid":False,"expiry":"N/A","issuer":"N/A","days_left":None},
        server_tech="Unknown", sec_headers={}, redirects=[],
        gw_sources={}, used_playwright=False, from_cache=False,
        scanned_by=scanned_by,
        checkout_security={"mode":"Unknown","evidence":[],"score":0},
    )

    c = lambda: is_cancelled(uid)
    loop = asyncio.get_event_loop()

    # Start background tasks immediately (parallel from t=0)
    ip_fut       = loop.run_in_executor(None, get_ip, domain)
    ssl_fut      = loop.run_in_executor(None, get_ssl_info, domain)
    dns_fut      = loop.run_in_executor(None, probe_dns, domain)
    wb_task      = asyncio.create_task(fetch_wayback(domain))
    wk_task      = asyncio.create_task(check_well_known(base))
    wh_task      = asyncio.create_task(probe_webhooks(base))

    t0 = time.time()
    status, html_text, resp_headers, redirects = await async_get(url, UAS[0])
    res["response_ms"] = int((time.time()-t0)*1000)
    res["redirects"]   = redirects

    # Use wayback if blocked
    wb_html = ""
    if status in (403, 429, 503, 0):
        log.info("Site blocked (%s), waiting for wayback...", status)
        wb_html = await wb_task
        if wb_html: res["wb"] = True
        if status == 0:
            res["error"] = "Unreachable" + (" — Wayback ✓" if wb_html else "")
            if not wb_html:
                res["ip"]  = await ip_fut
                res["ssl"] = await ssl_fut
                for t in [wk_task, wh_task]: t.cancel()
                return res
    else:
        wb_task.cancel()

    res["status"]      = status
    csp_gateways       = parse_csp(resp_headers)
    hdr_blob           = " ".join(f"{k.lower()}:{v.lower()}" for k,v in resp_headers.items())
    res["server_tech"] = " ".join(v for k in ("server","x-powered-by")
                                   for v in [resp_headers.get(k,resp_headers.get(k.title(),""))] if v) or "Unknown"
    res["sec_headers"] = {h: h in {k.lower() for k in resp_headers} for h in SEC_HEADERS}

    if c():
        res["error"] = "Cancelled"
        for t in [wk_task, wh_task]: t.cancel()
        return res

    # Build corpus
    all_corpus = ""
    js_urls: list[str] = []

    def absorb(ht: str, pu: str):
        nonlocal all_corpus, js_urls
        text, new_js = extract_all(ht, pu, parsed)
        all_corpus += text+"\n"
        for u2 in new_js:
            if u2 not in js_urls: js_urls.append(u2)

    if html_text: absorb(html_text, url)
    if wb_html:   absorb(wb_html, url)

    # Discover payment pages + fixed extra pages
    discovered = await discover_payment_pages(base, parsed, html_text or "")
    FIXED_EXTRA = [
        "/checkout","/cart","/shop","/payment","/pay","/order","/billing",
        "/wp-login.php","/?wc-ajax=get_refreshed_fragments",
        "/index.php?route=checkout/cart","/cart.js",
    ]
    all_extra = list(dict.fromkeys(discovered + [base+p for p in FIXED_EXTRA]))

    async def fetch_extra(eu: str) -> str:
        if c(): return ""
        s, t, _, _ = await async_get(eu, random.choice(UAS))
        return t if s == 200 and len(t) > 200 else ""

    extra_results = await asyncio.gather(
        *[fetch_extra(eu) for eu in all_extra],
        return_exceptions=True,
    )
    for txt in extra_results:
        if isinstance(txt, str) and txt:
            absorb(txt, url)

    if c():
        res["error"] = "Cancelled"
        for t in [wk_task, wh_task]: t.cancel()
        return res

    # JS fetch — payment-related URLs first
    PAY_KW = ("stripe","paypal","braintree","adyen","klarna","checkout","square","authorize")
    priority_js = [u for u in js_urls if any(k in u.lower() for k in PAY_KW)]
    other_js    = [u for u in js_urls if u not in priority_js]
    batch       = (priority_js + other_js)[:MAX_JS]

    js_results = await asyncio.gather(
        *[async_fetch_js(u, url) for u in batch],
        return_exceptions=True,
    )
    ext_js = "\n".join(r for r in js_results if isinstance(r, str) and r)

    source_maps = await fetch_source_maps(batch, url)

    # Browser scan — Fixed: run INSIDE acquire() context so semaphore is held
    pw_result = {"corpus":"","gateways":[],"platform":[],"tech":{}}
    if _pool and _pool.available():
        try:
            async with _pool.acquire() as browser:
                if browser:
                    pw_result = await browser_scan(base, browser)
                    res["used_playwright"] = bool(pw_result["corpus"])
        except Exception as e:
            log.debug("browser pool err: %s", e)

    # Gather all parallel results
    wk_gateways      = await wk_task
    webhook_gateways = await wh_task
    dns_gateways     = await dns_fut
    res["ip"]        = await ip_fut
    res["ssl"]       = await ssl_fut

    # Final corpus
    corpus = "\n".join([
        all_corpus, ext_js, source_maps,
        hdr_blob, pw_result.get("corpus",""),
    ])

    api_key_gateways = scan_api_keys(corpus)

    res["waf"] = list(dict.fromkeys(
        match_any(hdr_blob, WAF) + match_any(corpus, WAF)
    ))
    res["captcha"] = any(p in corpus.lower() for p in CAPTCHA)

    # Tech stack
    res["tech_stack"] = detect_tech_stack(corpus)
    for cat, items in pw_result.get("tech",{}).items():
        existing = res["tech_stack"].setdefault(cat, [])
        for item in items:
            if item not in existing: existing.append(item)

    # Bayesian gateway scoring
    gw_sc = score_corpus(corpus, GW)
    all_confirmed = set(
        csp_gateways + api_key_gateways +
        webhook_gateways + wk_gateways + dns_gateways +
        pw_result.get("gateways",[])
    )
    for gw_name in all_confirmed:
        gw_sc[gw_name] = max(gw_sc.get(gw_name,0), GW_THRESHOLD*3)

    res["gateways"] = [n for n,sc in gw_sc.items() if sc >= GW_THRESHOLD]

    plt_scores  = score_corpus(corpus, PLT)
    res["platform"] = best_platform(plt_scores)
    for p in pw_result.get("platform",[]):
        if p not in res["platform"]: res["platform"].append(p)

    res["checkout_security"] = detect_checkout_security(corpus)

    res["gw_sources"] = {
        "csp":       csp_gateways,
        "api_key":   api_key_gateways,
        "webhook":   webhook_gateways,
        "wellknown": wk_gateways,
        "dns":       dns_gateways,
        "browser":   pw_result.get("gateways",[]),
    }

    log.info("✓ %s GW:%s PLT:%s WAF:%s Checkout:%s PW:%s",
             domain, res["gateways"], res["platform"],
             res["waf"], res["checkout_security"]["mode"], res["used_playwright"])

    await db_set_cache(domain, res)
    return res

# ════════════════════════════════════════════════════════
#  EXPORT
# ════════════════════════════════════════════════════════

def export_txt(results: list[dict]) -> str:
    lines = [f"Site Checker Export — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"]
    for r in results:
        cs = r.get("checkout_security",{})
        lines += [
            "="*50,
            f"Site    : {r['domain']}",
            f"IP      : {r.get('ip','N/A')}",
            f"Status  : {r.get('status','N/A')}",
            f"Gateways: {', '.join(r['gateways']) if r['gateways'] else 'None'}",
            f"Platform: {', '.join(r['platform']) if r['platform'] else 'Unknown'}",
            f"Checkout: {cs.get('mode','Unknown')} (score:{cs.get('score',0)})",
            f"WAF/CDN : {', '.join(r.get('waf',[])) or 'None'}",
            f"Captcha : {'Yes' if r.get('captcha') else 'No'}",
        ]
        ssl_info = r.get("ssl",{})
        if ssl_info.get("expiry","N/A") != "N/A":
            lines.append(f"SSL     : {ssl_info['expiry']} ({ssl_info['days_left']}d) — {ssl_info['issuer']}")
        for cat, items in r.get("tech_stack",{}).items():
            lines.append(f"{cat[:8]:8}: {', '.join(items)}")
        lines.append("")
    return "\n".join(lines)

def export_csv(results: list[dict]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=[
        "domain","ip","status","gateways","platform","checkout_security",
        "waf","captcha","ssl_expiry","ssl_issuer","server",
        "analytics","chat","email","response_ms",
    ])
    writer.writeheader()
    for r in results:
        ssl_ = r.get("ssl",{})
        ts   = r.get("tech_stack",{})
        cs   = r.get("checkout_security",{})
        writer.writerow({
            "domain":            r["domain"],
            "ip":                r.get("ip","N/A"),
            "status":            r.get("status","N/A"),
            "gateways":          "|".join(r.get("gateways",[])),
            "platform":          "|".join(r.get("platform",[])),
            "checkout_security": cs.get("mode","Unknown"),
            "waf":               "|".join(r.get("waf",[])),
            "captcha":           "Yes" if r.get("captcha") else "No",
            "ssl_expiry":        ssl_.get("expiry","N/A"),
            "ssl_issuer":        ssl_.get("issuer","N/A"),
            "server":            r.get("server_tech","Unknown"),
            "analytics":         "|".join(ts.get("Analytics",[])),
            "chat":              "|".join(ts.get("Chat / Support",[])),
            "email":             "|".join(ts.get("Email / Marketing",[])),
            "response_ms":       r.get("response_ms",""),
        })
    return out.getvalue()

def export_json(results: list[dict]) -> str:
    return json.dumps(results, indent=2, default=str)

# ════════════════════════════════════════════════════════
#  MONITORING
# ════════════════════════════════════════════════════════

async def monitor_loop(app, uid: int, domain: str, interval_h: int):
    log.info("Monitor started: %s every %dh for uid=%d", domain, interval_h, uid)
    last_gateways: set[str] = set()

    while True:
        await asyncio.sleep(interval_h * 3600)

        monitors = await db_get_monitors()
        if not any(m["domain"]==domain and m["uid"]==uid for m in monitors):
            log.info("Monitor stopped: %s", domain)
            break

        try:
            result = await scan(f"https://{domain}", uid=0, force_fresh=True)
            current_gw = set(result.get("gateways",[]))
            added   = current_gw - last_gateways
            removed = last_gateways - current_gw

            if last_gateways and (added or removed):
                parts = [f"🔔 *Monitor Alert — {domain}*\n"]
                if added:   parts.append(f"✅ New: {', '.join(added)}")
                if removed: parts.append(f"❌ Removed: {', '.join(removed)}")
                parts.append(f"Status: {result.get('status')}")
                try:
                    await app.bot.send_message(uid, "\n".join(parts), parse_mode="Markdown")
                except Exception: pass
            elif not last_gateways:
                gw_str = ", ".join(current_gw) if current_gw else "None"
                try:
                    await app.bot.send_message(
                        uid,
                        f"✅ *Monitor active — {domain}*\nGateways: `{gw_str}`",
                        parse_mode="Markdown",
                    )
                except Exception: pass

            last_gateways = current_gw
            await db_update_monitor_check(domain, json.dumps(list(current_gw)))

        except Exception as e:
            log.debug("Monitor scan err %s: %s", domain, e)

# ════════════════════════════════════════════════════════
#  FORMATTER
# ════════════════════════════════════════════════════════

SE = {200:"🟢",201:"🟢",301:"🔀",302:"🔀",400:"🟡",403:"🔴",404:"🟡",429:"🟠",500:"⛔",503:"⛔"}

def ssl_line(s: dict) -> str:
    if not s or s["expiry"]=="N/A": return "N/A"
    d = s["days_left"]
    icon = "✅" if d and d>30 else ("⚠️" if d and d>0 else "❌")
    return f"{icon} {s['expiry']} ({d}d) — {s['issuer']}"

def sec_line(sh: dict) -> str:
    if not sh: return "N/A"
    short = {"strict-transport-security":"HSTS","content-security-policy":"CSP",
             "x-frame-options":"XFO","x-content-type-options":"XCTO",
             "x-xss-protection":"XSS","permissions-policy":"PP","referrer-policy":"RP"}
    have    = [short[h] for h,ok in sh.items() if ok]
    missing = [short[h] for h,ok in sh.items() if not ok]
    line = ""
    if have:    line += "✅ "+" ".join(have)
    if missing: line += ("  " if have else "")+"❌ "+" ".join(missing)
    return line or "None"

def fmt(r: dict) -> str:
    if r.get("error") == "Cancelled":
        return f"❌ Scan cancelled — {r['domain']}"

    if r["error"] and not r["wb"] and not r["gateways"] and not r["platform"]:
        return (
            "◇  ° • ¡SITE CHECKER! • °  ◇\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"▒➳ Site   : {r['domain']}\n"
            f"▒➳ IP     : {r['ip']}\n"
            f"▒➳ Error  : {r['error']}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Bot By: {r.get('scanned_by') or BOT_AUTHOR}"
        )

    gw  = " | ".join(f"🔥{g}" for g in r["gateways"]) if r["gateways"] else "❌ None"
    plt = ", ".join(r["platform"]) if r["platform"] else "Unknown"
    waf = ", ".join(r["waf"])      if r["waf"]      else "None"
    cap = "✅ Yes" if r["captcha"] else "❌ No"
    st  = r["status"]
    ms  = f" ({r['response_ms']}ms)" if r["response_ms"] else ""
    srv = r.get("server_tech","Unknown")
    rdr = f"{len(r.get('redirects',[]))} hop(s)" if r.get("redirects") else "None"

    cs    = r.get("checkout_security",{})
    mode  = cs.get("mode","Unknown")
    score = cs.get("score",0)
    if mode == "3D":
        cs_str = f"🔒 3D Secure (score:{score})"
        evids  = cs.get("evidence",[])
        if evids: cs_str += f" [{', '.join(evids[:2])}]"
    elif mode == "2D":
        cs_str = "⚠️ 2D — No 3DS detected"
    else:
        cs_str = "❓ Unknown"

    badges = []
    if r["wb"]:                  badges.append("WB✓")
    if r.get("used_playwright"): badges.append("🌐Browser")
    if r.get("from_cache"):      badges.append("Cache✓")
    badge_str = " ["+", ".join(badges)+"]" if badges else ""

    src   = r.get("gw_sources",{})
    evids = []
    for key, label in [("csp","CSP"),("api_key","Key"),("browser","Browser"),
                        ("webhook","Hook"),("wellknown","WK"),("dns","DNS")]:
        vals = src.get(key,[])
        if vals: evids.append(f"{label}:{','.join(vals)}")
    evid_line = ("\n▒➳ Evidence : "+" | ".join(evids)) if evids else ""

    ts = r.get("tech_stack",{})
    tech_lines = []
    for cat, items in ts.items():
        if items: tech_lines.append(f"▒➳ {cat[:8]:8}: {', '.join(items[:3])}")
    tech_block = ("\n"+"\n".join(tech_lines)) if tech_lines else ""

    tags = [f"#{g.replace(' ','').replace('/','').replace('.','')}" for g in r["gateways"]]
    if not r["gateways"]: tags.append("#NoGateway")
    tags += [f"#{p.replace(' ','').replace('.','')}" for p in r["platform"]]
    tags += [f"#{''.join(w.split()[0] for w in r['waf'])}"] if r["waf"] else ["#NoCF"]
    tags.append("#Captcha" if r["captcha"] else "#NoCaptcha")
    tags.append(f"#{mode}" if mode in ("2D","3D") else "#UnknownCheckout")

    return (
        "◇  ° • ¡SITE CHECKER! • °  ◇\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"▒➳ Site     : {r['domain']}\n"
        f"▒➳ IP       : {r['ip']}\n"
        f"▒➳ Status   : {SE.get(st,'⚪')} {st}{ms}{badge_str}\n"
        f"▒➳ Redirect : {rdr}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"▒➳ Gateways : {gw}\n"
        f"▒➳ Platform : {plt}\n"
        f"▒➳ Checkout : {cs_str}\n"
        f"▒➳ Server   : {srv}\n"
        f"▒➳ WAF/CDN  : {waf}\n"
        f"▒➳ Captcha  : {cap}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"▒➳ SSL      : {ssl_line(r.get('ssl',{}))}\n"
        f"▒➳ SecHdrs  : {sec_line(r.get('sec_headers',{}))}"
        f"{evid_line}"
        f"{tech_block}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{' '.join(tags)}\n"
        f"Bot By: {r.get('scanned_by') or BOT_AUTHOR}"
    )

# ════════════════════════════════════════════════════════
#  LOADER
# ════════════════════════════════════════════════════════

async def animated_loader(msg, uid, domain, total, idx, stop_evt, t0, est=70):
    fi, bar_len = 0, 14
    multi = f" [{idx}/{total}]" if total>1 else ""
    while not stop_evt.is_set():
        if is_cancelled(uid):
            try: await msg.edit_text(f"❌ *Cancelled* — `{domain}`", parse_mode="Markdown")
            except Exception: pass
            return
        elapsed = time.time()-t0
        pct     = min(int(elapsed/est*100), 95)
        filled  = int(bar_len*pct/100)
        bar     = "█"*filled+"░"*(bar_len-filled)
        try:
            await msg.edit_text(
                f"{FRAMES[fi%len(FRAMES)]} *Scanning{multi}* `{domain}`\n\n"
                f"`[{bar}]` {pct}%\n\n_/cancel to stop_",
                parse_mode="Markdown",
            )
        except Exception: pass
        fi += 1
        await asyncio.sleep(1.5)

async def do_scan(uid, raw_url, reply_fn, idx=1, total=1, force_fresh=False, scanned_by="") -> dict | None:
    domain = urlparse(norm(raw_url)).netloc or raw_url
    t0     = time.time()
    est    = 80 if HAS_PLAYWRIGHT else 50

    loader_msg = await reply_fn(
        f"⠋ *Scanning [{idx}/{total}]* `{domain}`\n\n"
        "`[░░░░░░░░░░░░░░]` 0%\n\n_/cancel to stop_",
        parse_mode="Markdown",
    )
    stop_evt  = asyncio.Event()
    anim_task = asyncio.create_task(animated_loader(loader_msg, uid, domain, total, idx, stop_evt, t0, est))
    result    = await scan(raw_url, uid, force_fresh, scanned_by=scanned_by)
    stop_evt.set()
    await anim_task
    try: await loader_msg.delete()
    except Exception: pass
    return result

def save_result(uid, result):
    _last_results[uid].insert(0, result)
    _last_results[uid] = _last_results[uid][:20]

# ════════════════════════════════════════════════════════
#  SCAN FLOW
# ════════════════════════════════════════════════════════

async def run_scans(uid, urls, reply_fn, force_fresh=False, scanned_by=""):
    set_scanning(uid, True)
    try:
        for idx, raw_url in enumerate(urls, 1):
            if is_cancelled(uid):
                await reply_fn("❌ Scan cancelled."); break
            result = await do_scan(uid, raw_url, reply_fn, idx, len(urls), force_fresh, scanned_by)
            if not result or result.get("error") == "Cancelled":
                await reply_fn("❌ Scan cancelled."); break
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Rescan",  callback_data=f"rescan:{raw_url}"),
                InlineKeyboardButton("📊 Export",  callback_data=f"export1:{raw_url}"),
            ]])
            await reply_fn(f"```\n{fmt(result)}\n```", parse_mode="Markdown", reply_markup=kb)
            save_result(uid, result)
            await db_inc_scan(uid)
            await db_log_scan(uid, result["domain"], result["gateways"], result["platform"])
    finally:
        set_scanning(uid, False)

# ════════════════════════════════════════════════════════
#  TELEGRAM HANDLERS
# ════════════════════════════════════════════════════════

async def reg(update: Update):
    u = update.effective_user
    await db_upsert_user(u.id, u.username, u.first_name)

def display_name(user) -> str:
    return f"@{user.username}" if user.username else (user.first_name or "Unknown")

async def banned(uid) -> bool:
    u = await db_get_user(uid)
    return bool(u and u.get("is_banned"))

def is_admin(uid): return uid in ADMIN_IDS

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await reg(update)
    uid = update.effective_user.id
    if await banned(uid): await update.message.reply_text("🚫 You are banned."); return
    pw = "✅ PW+Stealth" if (HAS_PLAYWRIGHT and HAS_STEALTH) else ("✅ Playwright" if HAS_PLAYWRIGHT else "⚠️ No PW")
    cf = "✅ curl_cffi" if HAS_CURL_CFFI else "⚠️ No cffi"
    await update.message.reply_text(
        f"👾 *Site Checker Bot v9*  —  _by {BOT_AUTHOR}_\n\n"
        f"{pw} | {cf} | {'✅ DNS' if HAS_DNS else '⚠️ No DNS'}\n"
        f"💳 *{len(GW)}+ gateways* | 🏗 *{len(PLT)} platforms*\n\n"
        "📌 *Commands:*\n"
        "`/check <url>` — Scan\n"
        "`/fresh <url>` — Force rescan\n"
        "`/bulk` — Upload .txt file\n"
        "`/last` — Last results\n"
        "`/export [txt|csv|json]` — Export\n"
        "`/monitor <url> [hours]` — Auto monitor\n"
        "`/unmonitor <url>` — Stop monitor\n"
        "`/cancel` — Stop scan\n"
        "`/help` — Help\n\n"
        "💡 URL တိုက်ရိုက်ပို့လည်း ရတယ်!",
        parse_mode="Markdown",
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Help — v9*\n\n"
        "*Detection layers:*\n"
        "① Browser pool + stealth + JS hooking\n"
        "② Network request interception\n"
        "③ Payment page crawler (depth 2)\n"
        "④ CSP header analysis\n"
        "⑤ API key regex (pk_live_ etc)\n"
        "⑥ Webhook probing (body-verified)\n"
        "⑦ DNS CNAME resolution\n"
        "⑧ .well-known files\n"
        "⑨ Source map analysis\n"
        "⑩ HTTP/2 fingerprint rotation\n"
        "⑪ Wayback Machine fallback\n"
        "⑫ 2D/3D Secure detection\n\n"
        "*Tech detected:* CDN • Analytics • A/B\n"
        "Chat/Support • Email • Fraud tools\n\n"
        "*Bulk:* Send .txt file (1 URL per line)\n"
        "*Monitor:* `/monitor stripe.com 6`\n"
        f"Rate: {RATE_MAX_DEFAULT}/{RATE_WIN}s | Max: {MAX_URLS_MSG} URLs",
        parse_mode="Markdown",
    )

async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await reg(update)
    uid = update.effective_user.id
    if await banned(uid): await update.message.reply_text("🚫 Banned."); return
    if is_scanning(uid): await update.message.reply_text("⚠️ Scan run နေတယ်။ /cancel"); return
    if not ctx.args: await update.message.reply_text("Usage: `/check <url>`", parse_mode="Markdown"); return
    ok, wait = await rate_ok(uid)
    if not ok: await update.message.reply_text(f"⏳ {wait}s စောင့်ပါ။"); return
    await run_scans(uid, [" ".join(ctx.args)], update.message.reply_text,
                    scanned_by=display_name(update.effective_user))

async def cmd_fresh(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await reg(update)
    uid = update.effective_user.id
    if await banned(uid): await update.message.reply_text("🚫 Banned."); return
    if is_scanning(uid): await update.message.reply_text("⚠️ Scan run နေတယ်။"); return
    if not ctx.args: await update.message.reply_text("Usage: `/fresh <url>`", parse_mode="Markdown"); return
    ok, wait = await rate_ok(uid)
    if not ok: await update.message.reply_text(f"⏳ {wait}s"); return
    domain = urlparse(norm(" ".join(ctx.args))).netloc
    if domain: await db_delete_cache(domain)
    await run_scans(uid, [" ".join(ctx.args)], update.message.reply_text,
                    force_fresh=True, scanned_by=display_name(update.effective_user))

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_scanning(uid): request_cancel(uid); await update.message.reply_text("⏹ Cancelling...")
    else: await update.message.reply_text("ℹ️ Run နေတဲ့ scan မရှိဘူး။")

async def cmd_last(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    results = _last_results.get(uid,[])
    if not results: await update.message.reply_text("📭 မရှိသေးဘူး — scan တစ်ခုလုပ်ပါ။"); return
    lines = ["📋 *Last Scans:*\n"]
    for i, r in enumerate(results[:10], 1):
        gw  = ", ".join(r["gateways"]) if r["gateways"] else "None"
        plt = ", ".join(r["platform"]) if r["platform"] else "?"
        cs  = r.get("checkout_security",{}).get("mode","?")
        lines.append(f"`{i}.` `{r['domain']}`\n   GW:`{gw}` PLT:`{plt}` `{cs}` `{r['status']}`\n")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid     = update.effective_user.id
    results = _last_results.get(uid,[])
    if not results: await update.message.reply_text("📭 No results to export."); return
    fmt_arg = (ctx.args[0] if ctx.args else "txt").lower()
    if fmt_arg == "csv":
        content, filename = export_csv(results), f"scan_{int(time.time())}.csv"
    elif fmt_arg == "json":
        content, filename = export_json(results), f"scan_{int(time.time())}.json"
    else:
        content, filename = export_txt(results), f"scan_{int(time.time())}.txt"
    bio = io.BytesIO(content.encode()); bio.name = filename
    await update.message.reply_document(document=bio, filename=filename,
                                         caption=f"📊 {len(results)} results — {fmt_arg.upper()}")

async def cmd_bulk(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📎 *Bulk Scan*\n\n.txt file upload လုပ်ပါ\n"
        f"(တစ်ကြောင်းတစ်ခု URL — max {MAX_URLS_FILE})",
        parse_mode="Markdown",
    )

async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await reg(update)
    uid = update.effective_user.id
    if await banned(uid): return
    if is_scanning(uid): await update.message.reply_text("⚠️ Scan run နေတယ်။ /cancel"); return
    doc = update.message.document
    if not doc or not doc.file_name.endswith(".txt"):
        await update.message.reply_text("⚠️ .txt file ပဲ လက်ခံတယ်။"); return
    file = await ctx.bot.get_file(doc.file_id)
    bio  = io.BytesIO()
    await file.download_to_memory(bio)
    content = bio.getvalue().decode("utf-8", errors="ignore")
    urls = [line.strip() for line in content.splitlines()
            if line.strip() and "." in line.strip() and len(line.strip()) > 3]
    if not urls: await update.message.reply_text("⚠️ Valid URL မတွေ့ဘူး။"); return
    if len(urls) > MAX_URLS_FILE:
        await update.message.reply_text(f"⚠️ ပထမ {MAX_URLS_FILE} ခုပဲ scan မယ်။")
        urls = urls[:MAX_URLS_FILE]
    ok, wait = await rate_ok(uid)
    if not ok: await update.message.reply_text(f"⏳ {wait}s"); return
    await update.message.reply_text(f"📎 *Bulk scan* — {len(urls)} URLs", parse_mode="Markdown")
    await run_scans(uid, urls, update.message.reply_text,
                    scanned_by=display_name(update.effective_user))

async def cmd_monitor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if await banned(uid): return
    if len(ctx.args or []) < 1:
        await update.message.reply_text("Usage: `/monitor <domain> [hours]`", parse_mode="Markdown"); return
    domain     = ctx.args[0].strip().lstrip("https://").lstrip("http://").split("/")[0]
    interval_h = int(ctx.args[1]) if len(ctx.args)>1 and ctx.args[1].isdigit() else 6
    interval_h = max(1, min(interval_h, 24))
    await db_add_monitor(uid, domain, interval_h)
    key = f"{uid}:{domain}"
    if key not in _monitors:
        task = asyncio.create_task(monitor_loop(ctx.application, uid, domain, interval_h))
        _monitors[key] = {"task": task}
    await update.message.reply_text(
        f"🔔 *Monitor active*\n`{domain}` — every `{interval_h}h`\n"
        "Gateway ပြောင်းရင် alert ပေးမယ်", parse_mode="Markdown",
    )

async def cmd_unmonitor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not ctx.args: await update.message.reply_text("Usage: `/unmonitor <domain>`", parse_mode="Markdown"); return
    domain = ctx.args[0].strip().lstrip("https://").lstrip("http://").split("/")[0]
    await db_remove_monitor(uid, domain)
    key = f"{uid}:{domain}"
    if key in _monitors:
        _monitors[key]["task"].cancel()
        del _monitors[key]
    await update.message.reply_text(f"✅ Monitor stopped: `{domain}`", parse_mode="Markdown")

async def on_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await reg(update)
    uid  = update.effective_user.id
    if await banned(uid): return
    text = (update.message.text or "").strip()
    if not text: return
    if is_scanning(uid): await update.message.reply_text("⚠️ Scan run နေတယ်။ /cancel ဒါမှမဟုတ် စောင့်ပါ။"); return
    ok, wait = await rate_ok(uid)
    if not ok: await update.message.reply_text(f"⏳ {wait}s"); return
    urls = [u.strip() for u in re.split(r"[\n,]+", text)
            if u.strip() and "." in u and len(u.strip())>3]
    if not urls:
        parts = text.split()
        urls  = [p for p in parts if "." in p and len(p)>3]
    if not urls: await update.message.reply_text("⚠️ Valid URL မတွေ့ဘူး။"); return
    if len(urls) > MAX_URLS_MSG:
        await update.message.reply_text(f"⚠️ ပထမ {MAX_URLS_MSG} ခုပဲ scan မယ်။")
        urls = urls[:MAX_URLS_MSG]
    await run_scans(uid, urls, update.message.reply_text,
                    scanned_by=display_name(update.effective_user))

# ── Admin ──────────────────────────────────────────────

async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): await update.message.reply_text("⛔ Admin only."); return
    stats = await db_get_stats()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats",       callback_data="adm:stats"),
         InlineKeyboardButton("📢 Broadcast",   callback_data="adm:broadcast")],
        [InlineKeyboardButton("🗑 Clear Cache", callback_data="adm:clearcache"),
         InlineKeyboardButton("🔔 Monitors",    callback_data="adm:monitors")],
        [InlineKeyboardButton("⚙️ Help",        callback_data="adm:help")],
    ])
    await update.message.reply_text(
        f"🛡 *Admin Panel*\n\n"
        f"👥 Users: `{stats['total_users']}`\n"
        f"🔍 Scans: `{stats['total_scans']}`\n"
        f"💾 Cache: `{stats['cache_size']}`\n"
        f"🔔 Monitors: `{stats['monitors']}`\n"
        f"🚫 Banned: `{stats['banned']}`\n"
        f"⭐ VIP: `{stats['vip']}`",
        parse_mode="Markdown", reply_markup=kb,
    )

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    stats = await db_get_stats()
    await update.message.reply_text(
        f"📊 *Statistics*\n\n"
        f"👥 Users: `{stats['total_users']}`\n"
        f"🔍 Scans: `{stats['total_scans']}`\n"
        f"💾 Cache: `{stats['cache_size']}`\n"
        f"🔔 Monitors: `{stats['monitors']}`\n"
        f"Playwright:{' ✅' if HAS_PLAYWRIGHT else ' ❌'} "
        f"Stealth:{' ✅' if HAS_STEALTH else ' ❌'} "
        f"cffi:{' ✅' if HAS_CURL_CFFI else ' ❌'} "
        f"DNS:{' ✅' if HAS_DNS else ' ❌'}",
        parse_mode="Markdown",
    )

async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not ctx.args: await update.message.reply_text("Usage: /ban <uid>"); return
    try: await db_set_ban(int(ctx.args[0]),1); await update.message.reply_text(f"🚫 `{ctx.args[0]}` banned.", parse_mode="Markdown")
    except ValueError: await update.message.reply_text("❌ Invalid ID.")

async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not ctx.args: await update.message.reply_text("Usage: /unban <uid>"); return
    try: await db_set_ban(int(ctx.args[0]),0); await update.message.reply_text(f"✅ `{ctx.args[0]}` unbanned.", parse_mode="Markdown")
    except ValueError: await update.message.reply_text("❌ Invalid ID.")

async def cmd_vip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not ctx.args: await update.message.reply_text("Usage: /vip <uid>"); return
    try: await db_set_vip(int(ctx.args[0]),1); await update.message.reply_text(f"⭐ `{ctx.args[0]}` is VIP.", parse_mode="Markdown")
    except ValueError: await update.message.reply_text("❌ Invalid ID.")

async def cmd_setlimit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(ctx.args or [])<2: await update.message.reply_text("Usage: /setlimit <uid> <n>"); return
    try: await db_set_rate(int(ctx.args[0]),int(ctx.args[1])); await update.message.reply_text("⚙️ Rate limit updated.")
    except ValueError: await update.message.reply_text("❌ Invalid args.")

async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not ctx.args: await update.message.reply_text("Usage: /broadcast <msg>"); return
    msg  = " ".join(ctx.args)
    uids = await db_get_all_uids()
    sent = fail = 0
    ack  = await update.message.reply_text(f"📢 Sending to {len(uids)} users...")
    for uid in uids:
        try: await ctx.bot.send_message(uid, f"📢 *Broadcast:*\n\n{msg}", parse_mode="Markdown"); sent+=1
        except Exception: fail+=1
        await asyncio.sleep(0.05)
    try: await ack.edit_text(f"📢 Done! ✅{sent} ❌{fail}")
    except Exception: pass

async def cmd_clearcache(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    await db_clear_cache()
    await update.message.reply_text("🗑 Cache cleared!")

# ── Callbacks ──────────────────────────────────────────

async def on_admin_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    uid   = query.from_user.id
    if not is_admin(uid): return
    action = query.data.replace("adm:","")
    if action == "stats":
        await cmd_stats(update, ctx)
    elif action == "clearcache":
        await db_clear_cache(); await query.message.reply_text("🗑 Cache cleared!")
    elif action == "monitors":
        monitors = await db_get_monitors()
        lines = ["🔔 *Active Monitors:*\n"] if monitors else ["🔔 No active monitors."]
        for m in monitors:
            lines.append(f"• `{m['domain']}` uid:{m['uid']} every {m['interval_h']}h")
        await query.message.reply_text("\n".join(lines), parse_mode="Markdown")
    elif action == "broadcast":
        await query.message.reply_text("Usage: `/broadcast msg`", parse_mode="Markdown")
    elif action == "help":
        await query.message.reply_text(
            "🛡 *Admin Commands:*\n\n"
            "`/admin` `/stats` `/ban <uid>` `/unban <uid>`\n"
            "`/vip <uid>` `/setlimit <uid> <n>`\n"
            "`/broadcast <msg>` `/clearcache`",
            parse_mode="Markdown",
        )

async def on_rescan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    uid   = query.from_user.id
    raw_url = query.data.replace("rescan:","",1)
    if is_scanning(uid): await query.message.reply_text("⚠️ Scan run နေတယ်။"); return
    ok, wait = await rate_ok(uid)
    if not ok: await query.message.reply_text(f"⏳ {wait}s"); return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes", callback_data=f"do_rescan:{raw_url}"),
        InlineKeyboardButton("❌ No",  callback_data="no_rescan"),
    ]])
    domain = urlparse(norm(raw_url)).netloc
    await query.message.reply_text(f"🔄 Rescan `{domain}`?", parse_mode="Markdown", reply_markup=kb)

async def on_do_rescan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    uid   = query.from_user.id
    raw_url = query.data.replace("do_rescan:","",1)
    try: await query.message.delete()
    except Exception: pass
    if is_scanning(uid): return
    ok, wait = await rate_ok(uid)
    if not ok: await query.message.reply_text(f"⏳ {wait}s"); return
    await run_scans(uid, [raw_url], query.message.reply_text,
                    scanned_by=display_name(query.from_user))

async def on_no_rescan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer("Cancelled.")
    try: await query.message.delete()
    except Exception: pass

async def on_export1(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    uid   = query.from_user.id
    raw_url = query.data.replace("export1:","",1)
    domain  = urlparse(norm(raw_url)).netloc or raw_url
    results = [r for r in _last_results.get(uid,[]) if r["domain"]==domain]
    if not results: await query.message.reply_text("❌ No cached result."); return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📄 TXT",  callback_data=f"efmt:txt:{raw_url}"),
        InlineKeyboardButton("📊 CSV",  callback_data=f"efmt:csv:{raw_url}"),
        InlineKeyboardButton("🔧 JSON", callback_data=f"efmt:json:{raw_url}"),
    ]])
    await query.message.reply_text("📊 Format ရွေးပါ:", reply_markup=kb)

async def on_exp_fmt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    uid   = query.from_user.id
    parts = query.data.split(":", 2)
    fmt_  = parts[1] if len(parts)>1 else "txt"
    raw_url = parts[2] if len(parts)>2 else ""
    domain  = urlparse(norm(raw_url)).netloc or raw_url
    results = [r for r in _last_results.get(uid,[]) if r["domain"]==domain]
    if not results: await query.message.reply_text("❌ No result."); return
    if fmt_=="csv":  content,filename = export_csv(results),  f"{domain}.csv"
    elif fmt_=="json": content,filename = export_json(results), f"{domain}.json"
    else:            content,filename = export_txt(results),  f"{domain}.txt"
    bio = io.BytesIO(content.encode()); bio.name = filename
    await query.message.reply_document(document=bio, filename=filename,
                                        caption=f"📊 {domain} — {fmt_.upper()}")

async def on_err(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("PTB err: %s", ctx.error, exc_info=ctx.error)

# ════════════════════════════════════════════════════════
#  COMMAND MENU
# ════════════════════════════════════════════════════════

USER_CMDS = [
    BotCommand("start",     "🚀 Start bot"),
    BotCommand("check",     "🔍 Scan — /check stripe.com"),
    BotCommand("fresh",     "🔄 Force rescan (skip cache)"),
    BotCommand("bulk",      "📎 Upload .txt for bulk scan"),
    BotCommand("last",      "📋 Last scan results"),
    BotCommand("export",    "📊 Export (txt/csv/json)"),
    BotCommand("monitor",   "🔔 /monitor site.com 6"),
    BotCommand("unmonitor", "🔕 Stop monitoring"),
    BotCommand("cancel",    "⏹ Stop running scan"),
    BotCommand("help",      "📖 Help"),
]

ADMIN_CMDS = USER_CMDS + [
    BotCommand("admin",      "🛡 Admin panel"),
    BotCommand("stats",      "📊 Bot statistics"),
    BotCommand("ban",        "🚫 /ban <uid>"),
    BotCommand("unban",      "✅ /unban <uid>"),
    BotCommand("vip",        "⭐ /vip <uid>"),
    BotCommand("setlimit",   "⚙️ /setlimit <uid> <n>"),
    BotCommand("broadcast",  "📢 /broadcast <msg>"),
    BotCommand("clearcache", "🗑 Clear cache"),
]

# ════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════

def main():
    global _pool
    _pool = BrowserPool(BROWSER_POOL_SZ)

    async def post_init(app):
        await db_init()
        await _pool.start()
        await app.bot.set_my_commands(USER_CMDS, scope=BotCommandScopeAllPrivateChats())
        for aid in ADMIN_IDS:
            try:
                await app.bot.set_my_commands(ADMIN_CMDS, scope=BotCommandScopeChat(chat_id=aid))
            except Exception as e:
                log.warning("Admin cmd set failed %d: %s", aid, e)
        monitors = await db_get_monitors()
        for m in monitors:
            key = f"{m['uid']}:{m['domain']}"
            if key not in _monitors:
                task = asyncio.create_task(monitor_loop(app, m["uid"], m["domain"], m["interval_h"]))
                _monitors[key] = {"task": task}
        log.info("Bot ready ✓ | Monitors restored: %d", len(monitors))

    async def post_shutdown(app):
        await _pool.stop()

    log.info("="*55)
    log.info("Site Checker Bot v9 — %s", BOT_AUTHOR)
    log.info("Gateways:%d Platforms:%d TechCats:%d", len(GW), len(PLT), len(TECH_STACK))
    log.info("PW:%s Stealth:%s cffi:%s DNS:%s Proxies:%d",
             "✅" if HAS_PLAYWRIGHT else "❌",
             "✅" if HAS_STEALTH    else "❌",
             "✅" if HAS_CURL_CFFI  else "❌",
             "✅" if HAS_DNS        else "❌",
             len(PROXY_LIST))
    log.info("="*55)

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .concurrent_updates(True)
        .build()
    )

    for cmd, handler in [
        ("start",cmd_start),("help",cmd_help),("check",cmd_check),
        ("fresh",cmd_fresh),("cancel",cmd_cancel),("last",cmd_last),
        ("export",cmd_export),("bulk",cmd_bulk),
        ("monitor",cmd_monitor),("unmonitor",cmd_unmonitor),
        ("admin",cmd_admin),("stats",cmd_stats),
        ("ban",cmd_ban),("unban",cmd_unban),("vip",cmd_vip),
        ("setlimit",cmd_setlimit),("broadcast",cmd_broadcast),
        ("clearcache",cmd_clearcache),
    ]:
        app.add_handler(CommandHandler(cmd, handler))

    app.add_handler(CallbackQueryHandler(on_admin_cb,  pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(on_rescan,    pattern=r"^rescan:"))
    app.add_handler(CallbackQueryHandler(on_do_rescan, pattern=r"^do_rescan:"))
    app.add_handler(CallbackQueryHandler(on_no_rescan, pattern=r"^no_rescan$"))
    app.add_handler(CallbackQueryHandler(on_export1,   pattern=r"^export1:"))
    app.add_handler(CallbackQueryHandler(on_exp_fmt,   pattern=r"^efmt:"))
    app.add_handler(MessageHandler(filters.Document.FileExtension("txt"), on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_msg))
    app.add_error_handler(on_err)

    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
